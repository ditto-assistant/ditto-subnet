package ablation

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"math"
	"sort"
	"strings"
	"sync"
	"time"
)

const (
	maximumEligibleCases   = 100_000
	maximumPairedCases     = 512
	maximumAttemptsPerCase = 5
	maximumCoordinatorRuns = 4096
	maximumRequestTimeout  = 5 * time.Minute
	maximumTotalTimeout    = 30 * time.Minute
	minimumProjectionBytes = 32
	maximumProjectionBytes = 1024
	// A single admitted request is only a probe: it is not enough evidence that
	// the selected case exercised the intervened provider path. Requiring two
	// independently synthesized responses removes the trivial one-probe oracle.
	minimumRelevantCallsPerCase = uint64(2)
)

var ErrUnavailablePopulation = errors.New("paired ablation population is unavailable")

// EligibleCase is an identity from the caller's already-filtered v9
// confirmation population. The runner resolves CaseID to trusted case data;
// raw user identity is used only to derive lane-isolated opaque namespaces.
type EligibleCase struct {
	CaseID string
	UserID string
}

type EligiblePopulation struct {
	BenchVersion int
	Confirmation bool
	Cases        []EligibleCase
}

// CoordinatorConfig is deliberately fully bounded. SelectionKey fixes the
// deterministic case order. ProjectionKey isolates case-local user namespaces
// without revealing or reusing the caller's raw user identity.
type CoordinatorConfig struct {
	SampleSize     int
	MaxAttempts    int
	MaxRequests    int
	RequestTimeout time.Duration
	TotalTimeout   time.Duration
	ArtifactSHA256 string
	SelectionKey   []byte
	ProjectionKey  []byte
	FrozenProfile  FrozenProfile
	ProfileSHA256  string
}

type Lane string

const (
	LaneOrdinary  Lane = "ordinary"
	LaneInference Lane = "inference"
	LaneEmbedding Lane = "embedding"
)

func (l Lane) intervention() Intervention {
	switch l {
	case LaneInference:
		return InterventionInference
	case LaneEmbedding:
		return InterventionEmbedding
	default:
		return InterventionNone
	}
}

// SyntheticResponder is the narrow, provider-free capability visible to an
// intervention attempt. The coordinator supplies a revocable wrapper around
// the caller's concrete *Responder; arbitrary implementations are not accepted.
type SyntheticResponder interface {
	Chat(model string, requestBytes uint64) (ChatCompletion, error)
	Embeddings(inputs []string) (EmbeddingResponse, error)
}

type RunRequest struct {
	Lane                Lane
	CaseID              string
	OpaqueUserNamespace string
	Responder           SyntheticResponder
}

type CaseRunResult struct {
	Score float64
}

// CaseRunner is the only integration boundary that may perform ordinary
// harness work. This package owns no upstream client, endpoint, or credential.
type CaseRunner interface {
	RunCase(context.Context, RunRequest) (CaseRunResult, error)
}

type retryableError struct{ error }

func (retryableError) Retryable() bool { return true }

// MarkRetryable explicitly opts an attempt failure into bounded retry.
func MarkRetryable(err error) error {
	if err == nil {
		return nil
	}
	return retryableError{error: err}
}

func canRetry(err error) bool {
	var retryable interface{ Retryable() bool }
	return errors.As(err, &retryable) && retryable.Retryable()
}

type Observation string

const (
	ObservationAvailable   Observation = "available"
	ObservationChanged     Observation = "changed"
	ObservationInvariant   Observation = "invariant"
	ObservationUnavailable Observation = "unavailable"
)

type UnavailableReason string

const (
	UnavailableNone                UnavailableReason = ""
	UnavailableOrdinary            UnavailableReason = "ordinary_unavailable"
	UnavailableCaseFailure         UnavailableReason = "case_failure"
	UnavailablePartialIntervention UnavailableReason = "partial_intervention_failure"
	UnavailableRetryExhausted      UnavailableReason = "retry_exhausted"
	UnavailableRequestLimit        UnavailableReason = "request_limit"
	UnavailableCancelled           UnavailableReason = "cancelled"
	UnavailableDeadline            UnavailableReason = "deadline_exceeded"
	UnavailableZeroRelevantCalls   UnavailableReason = "zero_relevant_calls"
	UnavailableInsufficientCalls   UnavailableReason = "insufficient_relevant_calls"
	UnavailableSyntheticBudget     UnavailableReason = "synthetic_budget_exhausted"
)

type LaneReport struct {
	Lane              Lane              `json:"lane"`
	Observation       Observation       `json:"observation"`
	UnavailableReason UnavailableReason `json:"unavailable_reason,omitempty"`
	Complete          bool              `json:"complete"`
	AttemptCount      int               `json:"attempt_count"`
	CompletedCases    int               `json:"completed_cases"`
	ScoresSHA256      string            `json:"scores_sha256"`
	Scores            []CaseScore       `json:"-"`
	SyntheticUsage    Usage             `json:"synthetic_usage"`
}

type CoordinationReport struct {
	ContractVersion         string            `json:"contract_version"`
	BenchVersion            int               `json:"bench_version"`
	ArtifactSHA256          string            `json:"artifact_sha256"`
	DatasetSHA256           string            `json:"dataset_sha256"`
	ThresholdManifestSHA256 string            `json:"threshold_manifest_sha256"`
	AblationProfileSHA256   string            `json:"ablation_profile_sha256"`
	CoordinatorPolicy       CoordinatorPolicy `json:"coordinator_policy"`
	SelectedCaseCount       int               `json:"selected_case_count"`
	SelectedCasesSHA256     string            `json:"selected_cases_sha256"`
	SelectedCaseSetSHA256   string            `json:"selected_case_set_sha256"`
	CoordinatorSHA256       string            `json:"coordinator_sha256"`
	Ordinary                LaneReport        `json:"ordinary"`
	InferenceIntervention   LaneReport        `json:"inference_intervention"`
	EmbeddingIntervention   LaneReport        `json:"embedding_intervention"`
}

type PairedPopulation struct {
	Intervention Intervention
	Baseline     []CaseScore
	Ablated      []CaseScore
	Usage        Usage
	Observation  Observation
}

// GatePopulation returns only a complete, same-case population. It cannot
// accidentally use one intervention as another's baseline or ablated lane.
func (r CoordinationReport) GatePopulation(intervention Intervention) (PairedPopulation, error) {
	var lane LaneReport
	switch intervention {
	case InterventionInference:
		lane = r.InferenceIntervention
	case InterventionEmbedding:
		lane = r.EmbeddingIntervention
	default:
		return PairedPopulation{}, fmt.Errorf("active intervention is required")
	}
	if !r.Ordinary.Complete || !lane.Complete || len(r.Ordinary.Scores) != r.SelectedCaseCount || len(lane.Scores) != r.SelectedCaseCount {
		return PairedPopulation{}, fmt.Errorf("%w: %s", ErrUnavailablePopulation, lane.UnavailableReason)
	}
	baseline := append([]CaseScore(nil), r.Ordinary.Scores...)
	ablated := append([]CaseScore(nil), lane.Scores...)
	for index := range baseline {
		if baseline[index].CaseID != ablated[index].CaseID {
			return PairedPopulation{}, fmt.Errorf("%w: case identity mismatch", ErrUnavailablePopulation)
		}
	}
	return PairedPopulation{
		Intervention: intervention, Baseline: baseline, Ablated: ablated,
		Usage: lane.SyntheticUsage, Observation: lane.Observation,
	}, nil
}

type Coordinator struct {
	config         CoordinatorConfig
	artifactSHA256 string
	selectionKey   []byte
	projectionKey  []byte
	profile        FrozenProfile
	profileSHA256  string
	policy         CoordinatorPolicy
}

func (c CoordinatorConfig) policy() (CoordinatorPolicy, error) {
	if c.RequestTimeout%time.Millisecond != 0 || c.TotalTimeout%time.Millisecond != 0 {
		return CoordinatorPolicy{}, fmt.Errorf("coordinator timeouts must use whole milliseconds")
	}
	return CoordinatorPolicy{
		SampleSize: c.SampleSize, MaxAttempts: c.MaxAttempts, MaxRequests: c.MaxRequests,
		RequestTimeoutMilliseconds: c.RequestTimeout.Milliseconds(),
		TotalTimeoutMilliseconds:   c.TotalTimeout.Milliseconds(),
	}, nil
}

func NewCoordinator(config CoordinatorConfig) (*Coordinator, error) {
	if config.SampleSize <= 0 || config.SampleSize > maximumPairedCases {
		return nil, fmt.Errorf("invalid paired sample size")
	}
	if config.MaxAttempts <= 0 || config.MaxAttempts > maximumAttemptsPerCase {
		return nil, fmt.Errorf("invalid maximum attempts")
	}
	minimumRequests := config.SampleSize * 3
	maximumUsefulRequests := minimumRequests * config.MaxAttempts
	if config.MaxRequests < minimumRequests || config.MaxRequests > maximumCoordinatorRuns || config.MaxRequests > maximumUsefulRequests {
		return nil, fmt.Errorf("invalid coordinator request cap")
	}
	if config.RequestTimeout <= 0 || config.RequestTimeout > maximumRequestTimeout {
		return nil, fmt.Errorf("invalid per-request timeout")
	}
	if config.TotalTimeout <= 0 || config.TotalTimeout > maximumTotalTimeout || config.TotalTimeout < config.RequestTimeout {
		return nil, fmt.Errorf("invalid total timeout")
	}
	if len(config.SelectionKey) < minimumProjectionBytes || len(config.SelectionKey) > maximumProjectionBytes ||
		len(config.ProjectionKey) < minimumProjectionBytes || len(config.ProjectionKey) > maximumProjectionBytes {
		return nil, fmt.Errorf("selection and projection keys must each contain between %d and %d bytes", minimumProjectionBytes, maximumProjectionBytes)
	}
	if !canonicalSHA256(config.ArtifactSHA256) {
		return nil, fmt.Errorf("invalid runtime artifact digest")
	}
	policy, err := config.policy()
	if err != nil {
		return nil, err
	}
	if err := policy.validate(); err != nil {
		return nil, err
	}
	profileSHA256, err := FrozenProfileSHA256(config.FrozenProfile)
	if err != nil {
		return nil, err
	}
	if !canonicalSHA256(config.ProfileSHA256) || config.ProfileSHA256 != profileSHA256 {
		return nil, fmt.Errorf("frozen ablation profile checksum mismatch")
	}
	if config.FrozenProfile.CoordinatorPolicy != policy {
		return nil, fmt.Errorf("coordinator config does not match frozen ablation profile")
	}
	if config.FrozenProfile.SelectionKeySHA256 != bytesSHA256(config.SelectionKey) ||
		config.FrozenProfile.ProjectionKeySHA256 != bytesSHA256(config.ProjectionKey) {
		return nil, fmt.Errorf("coordinator key config does not match frozen ablation profile")
	}
	selectionKey := append([]byte(nil), config.SelectionKey...)
	projectionKey := append([]byte(nil), config.ProjectionKey...)
	profile := config.FrozenProfile
	artifactSHA256 := config.ArtifactSHA256
	config.ArtifactSHA256 = ""
	config.SelectionKey = nil
	config.ProjectionKey = nil
	config.FrozenProfile = FrozenProfile{}
	config.ProfileSHA256 = ""
	return &Coordinator{
		config: config, artifactSHA256: artifactSHA256, selectionKey: selectionKey, projectionKey: projectionKey,
		profile: profile, profileSHA256: profileSHA256, policy: policy,
	}, nil
}

func bytesSHA256(value []byte) string {
	digest := sha256.Sum256(value)
	return hex.EncodeToString(digest[:])
}

type rankedCase struct {
	EligibleCase
	rank [sha256.Size]byte
}

func (c *Coordinator) selectCases(population EligiblePopulation) ([]EligibleCase, string, error) {
	if population.BenchVersion != BenchVersionV9 || !population.Confirmation {
		return nil, "", fmt.Errorf("paired ablation requires an eligible v9 confirmation population")
	}
	if len(population.Cases) < c.config.SampleSize || len(population.Cases) > maximumEligibleCases {
		return nil, "", fmt.Errorf("invalid eligible population size")
	}
	ranked := make([]rankedCase, len(population.Cases))
	seen := make(map[string]struct{}, len(population.Cases))
	for index, candidate := range population.Cases {
		if candidate.CaseID == "" || len(candidate.CaseID) > 256 || strings.TrimSpace(candidate.CaseID) != candidate.CaseID {
			return nil, "", fmt.Errorf("invalid eligible case id")
		}
		if candidate.UserID == "" || len(candidate.UserID) > 512 || strings.TrimSpace(candidate.UserID) != candidate.UserID {
			return nil, "", fmt.Errorf("invalid eligible user id")
		}
		if _, duplicate := seen[candidate.CaseID]; duplicate {
			return nil, "", fmt.Errorf("duplicate eligible case id %q", candidate.CaseID)
		}
		seen[candidate.CaseID] = struct{}{}
		ranked[index] = rankedCase{EligibleCase: candidate, rank: keyedDigest(c.selectionKey, "selection", candidate.CaseID)}
	}
	sort.Slice(ranked, func(left, right int) bool {
		comparison := bytes.Compare(ranked[left].rank[:], ranked[right].rank[:])
		if comparison == 0 {
			return ranked[left].CaseID < ranked[right].CaseID
		}
		return comparison < 0
	})
	selected := make([]EligibleCase, c.config.SampleSize)
	caseIDs := make([]string, c.config.SampleSize)
	for index := range selected {
		selected[index] = ranked[index].EligibleCase
		caseIDs[index] = selected[index].CaseID
	}
	digest, err := selectedCasesSHA256(caseIDs)
	if err != nil {
		return nil, "", err
	}
	return selected, digest, nil
}

func selectedCasesSHA256(caseIDs []string) (string, error) {
	digest, _, err := digestJSON(struct {
		ContractVersion string   `json:"contract_version"`
		CaseIDs         []string `json:"case_ids"`
	}{ContractVersion: ContractVersion, CaseIDs: caseIDs})
	return digest, err
}

func keyedDigest(key []byte, domain string, values ...string) [sha256.Size]byte {
	mac := hmac.New(sha256.New, key)
	_, _ = mac.Write([]byte(ContractVersion))
	_, _ = mac.Write([]byte{0})
	_, _ = mac.Write([]byte(domain))
	for _, value := range values {
		_, _ = mac.Write([]byte{0})
		_, _ = mac.Write([]byte(value))
	}
	var digest [sha256.Size]byte
	copy(digest[:], mac.Sum(nil))
	return digest
}

func selectedCaseSetSHA256(datasetSHA256 string, caseIDs []string) (string, error) {
	ordered := append([]string(nil), caseIDs...)
	sort.Strings(ordered)
	digest, _, err := digestJSON(struct {
		ContractVersion string   `json:"contract_version"`
		DatasetSHA256   string   `json:"dataset_sha256"`
		CaseIDs         []string `json:"case_ids"`
	}{ContractVersion: ContractVersion, DatasetSHA256: datasetSHA256, CaseIDs: ordered})
	return digest, err
}

type coordinatorDigestPayload struct {
	ContractVersion         string            `json:"contract_version"`
	BenchVersion            int               `json:"bench_version"`
	ArtifactSHA256          string            `json:"artifact_sha256"`
	DatasetSHA256           string            `json:"dataset_sha256"`
	ThresholdManifestSHA256 string            `json:"threshold_manifest_sha256"`
	AblationProfileSHA256   string            `json:"ablation_profile_sha256"`
	CoordinatorPolicy       CoordinatorPolicy `json:"coordinator_policy"`
	SelectedCaseCount       int               `json:"selected_case_count"`
	SelectedCasesSHA256     string            `json:"selected_cases_sha256"`
	SelectedCaseSetSHA256   string            `json:"selected_case_set_sha256"`
	Ordinary                LaneReport        `json:"ordinary"`
	InferenceIntervention   LaneReport        `json:"inference_intervention"`
	EmbeddingIntervention   LaneReport        `json:"embedding_intervention"`
}

func coordinatorDigest(report CoordinationReport) (string, error) {
	payload := coordinatorDigestPayload{
		ContractVersion: report.ContractVersion, BenchVersion: report.BenchVersion,
		ArtifactSHA256: report.ArtifactSHA256, DatasetSHA256: report.DatasetSHA256,
		ThresholdManifestSHA256: report.ThresholdManifestSHA256,
		AblationProfileSHA256:   report.AblationProfileSHA256,
		CoordinatorPolicy:       report.CoordinatorPolicy, SelectedCaseCount: report.SelectedCaseCount,
		SelectedCasesSHA256: report.SelectedCasesSHA256, SelectedCaseSetSHA256: report.SelectedCaseSetSHA256,
		Ordinary: report.Ordinary, InferenceIntervention: report.InferenceIntervention,
		EmbeddingIntervention: report.EmbeddingIntervention,
	}
	digest, _, err := digestJSON(payload)
	return digest, err
}

func (c *Coordinator) userNamespace(lane Lane, candidate EligibleCase) string {
	digest := keyedDigest(c.projectionKey, "user-namespace", string(lane), candidate.CaseID, candidate.UserID)
	return "usr_" + hex.EncodeToString(digest[:])
}

type scopedResponder struct {
	mu        sync.RWMutex
	active    bool
	responder *Responder
}

func newScopedResponder(responder *Responder) *scopedResponder {
	if responder == nil {
		return nil
	}
	return &scopedResponder{active: true, responder: responder}
}

func (r *scopedResponder) Chat(model string, requestBytes uint64) (ChatCompletion, error) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	if !r.active {
		return ChatCompletion{}, context.Canceled
	}
	return r.responder.Chat(model, requestBytes)
}

func (r *scopedResponder) Embeddings(inputs []string) (EmbeddingResponse, error) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	if !r.active {
		return EmbeddingResponse{}, context.Canceled
	}
	return r.responder.Embeddings(inputs)
}

func (r *scopedResponder) deactivate() {
	if r == nil {
		return
	}
	r.mu.Lock()
	r.active = false
	r.mu.Unlock()
}

type attemptResult struct {
	result CaseRunResult
	err    error
}

type requestCounter struct {
	used int
	max  int
}

func (b *requestCounter) reserve() bool {
	if b.used >= b.max {
		return false
	}
	b.used++
	return true
}

func reasonForContext(err error) UnavailableReason {
	if errors.Is(err, context.DeadlineExceeded) {
		return UnavailableDeadline
	}
	return UnavailableCancelled
}

func runAttempt(ctx context.Context, timeout time.Duration, runner CaseRunner, request RunRequest) (CaseRunResult, error) {
	attemptCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	scope, _ := request.Responder.(*scopedResponder)
	result := make(chan attemptResult, 1)
	go func() {
		completed := attemptResult{}
		defer func() {
			if recovered := recover(); recovered != nil {
				completed.err = fmt.Errorf("case runner panic")
			}
			result <- completed
		}()
		completed.result, completed.err = runner.RunCase(attemptCtx, request)
	}()
	select {
	case completed := <-result:
		scope.deactivate()
		return completed.result, completed.err
	case <-attemptCtx.Done():
		scope.deactivate()
		return CaseRunResult{}, attemptCtx.Err()
	}
}

func (c *Coordinator) runOne(
	ctx context.Context,
	runner CaseRunner,
	request RunRequest,
	requests *requestCounter,
) (CaseRunResult, int, UnavailableReason) {
	for attempt := 1; attempt <= c.config.MaxAttempts; attempt++ {
		if err := ctx.Err(); err != nil {
			return CaseRunResult{}, attempt - 1, reasonForContext(err)
		}
		if !requests.reserve() {
			return CaseRunResult{}, attempt - 1, UnavailableRequestLimit
		}
		request.Responder = newScopedResponderFromCapability(request.Responder)
		result, err := runAttempt(ctx, c.config.RequestTimeout, runner, request)
		if err == nil {
			if math.IsNaN(result.Score) || math.IsInf(result.Score, 0) || result.Score < 0 || result.Score > 1 {
				return CaseRunResult{}, attempt, UnavailableCaseFailure
			}
			return result, attempt, UnavailableNone
		}
		if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
			return CaseRunResult{}, attempt, reasonForContext(err)
		}
		if !canRetry(err) {
			return CaseRunResult{}, attempt, UnavailableCaseFailure
		}
		if attempt == c.config.MaxAttempts {
			return CaseRunResult{}, attempt, UnavailableRetryExhausted
		}
	}
	panic("bounded retry loop terminated unexpectedly")
}

func newScopedResponderFromCapability(capability SyntheticResponder) SyntheticResponder {
	if capability == nil {
		return nil
	}
	switch value := capability.(type) {
	case *Responder:
		if value == nil {
			return nil
		}
		return newScopedResponder(value)
	case *scopedResponder:
		if value == nil {
			return nil
		}
		return newScopedResponder(value.responder)
	default:
		panic("coordinator received a non-concrete synthetic responder")
	}
}

func cleanResponder(responder *Responder, intervention Intervention, expectedBudget Budget) error {
	if responder == nil {
		return fmt.Errorf("missing %s responder", intervention)
	}
	usage := responder.Snapshot()
	if usage.Intervention != intervention || !usage.Synthetic {
		return fmt.Errorf("responder intervention mismatch")
	}
	if usage.Budget != expectedBudget {
		return fmt.Errorf("responder budget does not match frozen ablation profile")
	}
	if usage.ChatAttempts != 0 || usage.ChatApplied != 0 || usage.ChatInputBytes != 0 ||
		usage.EmbeddingAttempts != 0 || usage.EmbeddingApplied != 0 || usage.EmbeddingInputs != 0 ||
		usage.EmbeddingInputBytes != 0 || usage.RejectedRequests != 0 || usage.BudgetExhausted ||
		usage.UpstreamRequests != 0 || usage.UpstreamInputTokens != 0 || usage.UpstreamOutputTokens != 0 ||
		usage.UpstreamProviderCostMicroUSD != 0 {
		return fmt.Errorf("responder contains pre-existing activity")
	}
	return nil
}

func unavailableLane(lane Lane, reason UnavailableReason, usage Usage) LaneReport {
	digest, err := scoresSHA256(nil)
	if err != nil {
		panic("empty score commitment is not serializable")
	}
	return LaneReport{
		Lane: lane, Observation: ObservationUnavailable, UnavailableReason: reason,
		ScoresSHA256: digest, SyntheticUsage: usage,
	}
}

func (c *Coordinator) runLane(
	ctx context.Context,
	lane Lane,
	selected []EligibleCase,
	runner CaseRunner,
	responder *Responder,
	requests *requestCounter,
) (LaneReport, error) {
	report := LaneReport{Lane: lane, Observation: ObservationAvailable, Scores: make([]CaseScore, 0, len(selected))}
	for _, candidate := range selected {
		beforeCalls := uint64(0)
		if responder != nil {
			beforeCalls = responder.Snapshot().affectedCalls()
		}
		var capability SyntheticResponder
		if responder != nil {
			capability = responder
		}
		request := RunRequest{
			Lane: lane, CaseID: candidate.CaseID,
			OpaqueUserNamespace: c.userNamespace(lane, candidate), Responder: capability,
		}
		result, attempts, reason := c.runOne(ctx, runner, request, requests)
		report.AttemptCount += attempts
		if reason != UnavailableNone {
			report.Observation = ObservationUnavailable
			report.UnavailableReason = reason
			if responder != nil && report.CompletedCases > 0 && reason == UnavailableCaseFailure {
				report.UnavailableReason = UnavailablePartialIntervention
			}
			break
		}
		if responder != nil && responder.Snapshot().affectedCalls() == beforeCalls {
			report.Observation = ObservationUnavailable
			report.UnavailableReason = UnavailableZeroRelevantCalls
			break
		}
		if responder != nil && responder.Snapshot().affectedCalls()-beforeCalls < minimumRelevantCallsPerCase {
			report.Observation = ObservationUnavailable
			report.UnavailableReason = UnavailableInsufficientCalls
			break
		}
		report.Scores = append(report.Scores, CaseScore{CaseID: candidate.CaseID, Score: result.Score})
		report.CompletedCases++
	}
	if responder != nil {
		report.SyntheticUsage = responder.Snapshot()
		if report.SyntheticUsage.BudgetExhausted {
			report.Observation = ObservationUnavailable
			report.UnavailableReason = UnavailableSyntheticBudget
		}
	}
	report.Complete = report.CompletedCases == len(selected) && report.Observation != ObservationUnavailable
	if !report.Complete {
		report.Scores = nil
	}
	digest, err := scoresSHA256(report.Scores)
	if err != nil {
		return LaneReport{}, fmt.Errorf("commit %s lane scores: %w", lane, err)
	}
	report.ScoresSHA256 = digest
	return report, nil
}

func classifyIntervention(ordinary LaneReport, intervention *LaneReport) {
	if !ordinary.Complete || !intervention.Complete {
		return
	}
	intervention.Observation = ObservationInvariant
	for index := range ordinary.Scores {
		if ordinary.Scores[index].Score != intervention.Scores[index].Score {
			intervention.Observation = ObservationChanged
			return
		}
	}
}

// Coordinate fixes one deterministic sample and runs exactly the same case
// identities through isolated ordinary, inference-ablation, and
// embedding-ablation lanes. Runtime failures are evidence outcomes, not input
// errors; invalid contracts and contaminated responders are returned as errors.
func (c *Coordinator) Coordinate(
	ctx context.Context,
	population EligiblePopulation,
	runner CaseRunner,
	inferenceResponder *Responder,
	embeddingResponder *Responder,
) (CoordinationReport, error) {
	if ctx == nil || runner == nil {
		return CoordinationReport{}, fmt.Errorf("context and case runner are required")
	}
	selected, selectionDigest, err := c.selectCases(population)
	if err != nil {
		return CoordinationReport{}, err
	}
	if err := cleanResponder(inferenceResponder, InterventionInference, c.profile.InferenceBudget); err != nil {
		return CoordinationReport{}, err
	}
	if err := cleanResponder(embeddingResponder, InterventionEmbedding, c.profile.EmbeddingBudget); err != nil {
		return CoordinationReport{}, err
	}
	// Bind harness-visible synthetic envelopes to the frozen projection key and
	// artifact. The key never enters the response or evidence; it only prevents
	// a reusable fixed marker across cases and confirmation runs.
	inferenceResponder.bindProjection(c.projectionKey, c.artifactSHA256, InterventionInference)
	embeddingResponder.bindProjection(c.projectionKey, c.artifactSHA256, InterventionEmbedding)
	totalCtx, cancel := context.WithTimeout(ctx, c.config.TotalTimeout)
	defer cancel()
	requests := &requestCounter{max: c.config.MaxRequests}
	caseIDs := make([]string, len(selected))
	for index := range selected {
		caseIDs[index] = selected[index].CaseID
	}
	caseSetDigest, err := selectedCaseSetSHA256(c.profile.DatasetSHA256, caseIDs)
	if err != nil {
		return CoordinationReport{}, err
	}
	report := CoordinationReport{
		ContractVersion: ContractVersion, BenchVersion: BenchVersionV9,
		ArtifactSHA256: c.artifactSHA256, DatasetSHA256: c.profile.DatasetSHA256,
		ThresholdManifestSHA256: c.profile.ThresholdManifestSHA256,
		AblationProfileSHA256:   c.profileSHA256, CoordinatorPolicy: c.policy,
		SelectedCaseCount: len(selected), SelectedCasesSHA256: selectionDigest,
		SelectedCaseSetSHA256: caseSetDigest,
	}
	report.Ordinary, err = c.runLane(totalCtx, LaneOrdinary, selected, runner, nil, requests)
	if err != nil {
		return CoordinationReport{}, err
	}
	if !report.Ordinary.Complete {
		report.InferenceIntervention = unavailableLane(LaneInference, UnavailableOrdinary, inferenceResponder.Snapshot())
		report.EmbeddingIntervention = unavailableLane(LaneEmbedding, UnavailableOrdinary, embeddingResponder.Snapshot())
		report.CoordinatorSHA256, err = coordinatorDigest(report)
		if err != nil {
			return CoordinationReport{}, err
		}
		return report, nil
	}
	report.InferenceIntervention, err = c.runLane(totalCtx, LaneInference, selected, runner, inferenceResponder, requests)
	if err != nil {
		return CoordinationReport{}, err
	}
	report.EmbeddingIntervention, err = c.runLane(totalCtx, LaneEmbedding, selected, runner, embeddingResponder, requests)
	if err != nil {
		return CoordinationReport{}, err
	}
	classifyIntervention(report.Ordinary, &report.InferenceIntervention)
	classifyIntervention(report.Ordinary, &report.EmbeddingIntervention)
	report.CoordinatorSHA256, err = coordinatorDigest(report)
	if err != nil {
		return CoordinationReport{}, err
	}
	return report, nil
}
