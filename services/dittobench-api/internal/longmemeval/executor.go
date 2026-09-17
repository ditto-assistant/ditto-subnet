package longmemeval

import (
	"context"
	"errors"
	"fmt"
	"io"
	"sort"
	"time"
)

const (
	ReaderLane = "reader"
	JudgeLane  = "judge"
)

// JudgeInput is private trusted-evaluator input. The submitted Harness never
// receives it. Keeping the complete cleaned row lets a later transport adapter
// call the pinned official evaluator without reproducing or editing its prompt.
type JudgeInput struct {
	Reference  DatasetCase
	Hypothesis string
}

type Judge interface {
	Judge(context.Context, JudgeInput) (bool, error)
}

// ProviderMeter returns authoritative cumulative accounting for one dedicated
// confirmation session. Snapshots must include every profile lane, start at
// zero, never regress, and be backed by one receipt per request.
type ProviderMeter interface {
	Snapshot(context.Context) ([]ProviderEvidence, error)
}

// ExecutionLimits are host/runtime bounds. Provider request, token, and cost
// ceilings remain frozen in Profile; MaxElapsed is supplied from the remaining
// ticket TTL and must be positive.
type ExecutionLimits struct {
	MaxElapsed     time.Duration
	SeedBatchPairs int
}

type Executor struct {
	Harness Harness
	Judge   Judge
	Meter   ProviderMeter
	Limits  ExecutionLimits
	// OnCase is optional privacy-safe progress: completed cases out of the
	// selected total. It must not observe question IDs, prompts, or answers.
	OnCase func(completed, total int)
}

// ExecutionResult keeps the raw selected IDs private while giving trusted
// scheduler/signing code a validation-gated evidence digest. Only Evidence is
// suitable for report serialization.
type ExecutionResult struct {
	Evidence  Evidence `json:"evidence"`
	selection Selection
	// Diagnostics is scorer-owned context about received harness case
	// failures. It is not evidence: it never enters the signed root or the
	// native wire digest and it carries no bodies, identities, or exception
	// text. It exists so a completed official zero can still say which
	// boundary the submitted harness failed at instead of reading like an
	// execution outage. Excluded from the result's JSON so the marshaled
	// result stays evidence-only; the confirmation executor copies it onto its
	// own unsigned side channel.
	Diagnostics ExecutionDiagnostics `json:"-"`
}

// ExecutionDiagnostics reduces every received HarnessCaseFailure of one run to
// allowlisted, low-cardinality counters.
type ExecutionDiagnostics struct {
	// ReceivedFailures counts selected cases whose /run response was received
	// but unjudgeable (non-2xx, malformed JSON, or missing final_text).
	ReceivedFailures int `json:"received_failures"`
	// ReceivedFailureKinds histograms those failures by receivedFailureKindKey
	// (for example http_status_503 or missing_final_text).
	ReceivedFailureKinds map[string]int `json:"received_failure_kinds,omitempty"`
	// ReceivedFailureReaderAttempts sums the trusted broker's reader attempts
	// observed inside the failed cases: zero means the harness never tried the
	// frozen reader before answering.
	ReceivedFailureReaderAttempts uint64 `json:"received_failure_reader_attempts"`
	// ReceivedFailureReaderAgentRejections sums the reader attempts the
	// trusted reader refused before any provider reservation (its
	// pre-reservation 400/413 marker): attempts equal to rejections means the
	// harness was calling the reader and every call was refused as sent.
	ReceivedFailureReaderAgentRejections uint64 `json:"received_failure_reader_agent_rejections"`
	// ReceivedFailureEmbeddingDispatches sums the embedding dispatches the
	// broker admitted inside the failed cases.
	ReceivedFailureEmbeddingDispatches uint64 `json:"received_failure_embedding_dispatches"`
}

// receivedFailureStatusBuckets are the exact HTTP statuses a diagnostics key
// may name; anything else collapses into its class so the histogram stays
// bounded regardless of what the submitted harness returns.
var receivedFailureStatusBuckets = map[int]struct{}{
	400: {}, 401: {}, 403: {}, 404: {}, 405: {}, 408: {}, 409: {}, 413: {}, 415: {},
	422: {}, 429: {}, 500: {}, 501: {}, 502: {}, 503: {}, 504: {},
}

// receivedFailureKindKey maps one received HarnessCaseFailure to its allowlisted
// histogram key.
func receivedFailureKindKey(failure *HarnessCaseFailure) string {
	if failure == nil {
		return "other"
	}
	switch failure.Kind {
	case "malformed_json", "missing_final_text":
		return failure.Kind
	case "http_status":
		status := failure.StatusCode
		if _, exact := receivedFailureStatusBuckets[status]; exact {
			return fmt.Sprintf("http_status_%d", status)
		}
		switch {
		case status >= 400 && status < 500:
			return "http_status_4xx"
		case status >= 500 && status < 600:
			return "http_status_5xx"
		default:
			return "http_status_other"
		}
	default:
		return "other"
	}
}

func (d *ExecutionDiagnostics) recordReceivedFailure(failure *HarnessCaseFailure) {
	d.ReceivedFailures++
	if d.ReceivedFailureKinds == nil {
		d.ReceivedFailureKinds = make(map[string]int)
	}
	d.ReceivedFailureKinds[receivedFailureKindKey(failure)]++
	if failure != nil && failure.activity != nil {
		d.ReceivedFailureReaderAttempts += failure.activity.ReaderAttempts
		d.ReceivedFailureReaderAgentRejections += failure.activity.ReaderAgentRejections
		d.ReceivedFailureEmbeddingDispatches += failure.activity.EmbeddingDispatches
	}
}

func (r ExecutionResult) Validate(profile Profile) error {
	return r.Evidence.Validate(profile, r.selection)
}

func (r ExecutionResult) Digest(profile Profile) (string, error) {
	return r.Evidence.Digest(profile, r.selection)
}

// Execute performs the complete bounded confirmation dimension and returns a
// private validation handle plus public evidence. It emits no partial score: any dataset,
// harness, judge, accounting, fallback, or deadline failure aborts the bundle.
func (e Executor) Execute(
	parent context.Context,
	source io.ReadSeeker,
	profile Profile,
	artifactSHA256 string,
	projectionKey []byte,
) (ExecutionResult, error) {
	if e.Harness == nil || e.Judge == nil || e.Meter == nil {
		return ExecutionResult{}, errors.New("LongMemEval executor dependencies are incomplete")
	}
	if e.Limits.MaxElapsed <= 0 || e.Limits.SeedBatchPairs <= 0 {
		return ExecutionResult{}, errors.New("LongMemEval executor limits must be positive")
	}
	if len(projectionKey) < minimumProjectionKeyBytes {
		return ExecutionResult{}, fmt.Errorf("LongMemEval projection key must contain at least %d bytes", minimumProjectionKeyBytes)
	}
	started := time.Now()
	ctx, cancel := context.WithTimeout(parent, e.Limits.MaxElapsed)
	defer cancel()

	dataset, err := LoadSelectedDataset(ctx, source, profile)
	if err != nil {
		return ExecutionResult{}, err
	}
	projected, err := ProjectSelectedCases(dataset, projectionKey, e.Limits.SeedBatchPairs)
	if err != nil {
		return ExecutionResult{}, err
	}
	current, err := readBudgetSnapshot(ctx, profile, e.Meter, true)
	if err != nil {
		return ExecutionResult{}, fmt.Errorf("LongMemEval initial provider accounting: %w", err)
	}

	outcomes := make([]Outcome, 0, len(projected))
	receivedFailures := 0
	var diagnostics ExecutionDiagnostics
	if e.OnCase != nil {
		e.OnCase(0, len(projected))
	}
	for _, item := range projected {
		for _, seed := range item.SeedRequests {
			if err := requireLaneHeadroom(profile, current, ReaderLane); err != nil {
				return ExecutionResult{}, err
			}
			seedResponse, operationErr := e.Harness.Seed(ctx, seed)
			if operationErr == nil && (seedResponse.Pairs != len(seed.Pairs) ||
				seedResponse.Subjects != len(seed.Subjects) || seedResponse.Links != len(seed.Links)) {
				operationErr = errors.New("harness acknowledged incomplete seed payload")
			}
			next, accountingErr := readBudgetSnapshot(ctx, profile, e.Meter, false)
			if accountingErr == nil {
				accountingErr = validateMonotonicSnapshot(current, next)
			}
			if accountingErr != nil {
				return ExecutionResult{}, fmt.Errorf("LongMemEval provider accounting after seed: %w", accountingErr)
			}
			current = next
			if operationErr != nil {
				return ExecutionResult{}, fmt.Errorf("LongMemEval seed failed for opaque case: %w", operationErr)
			}
		}

		if err := requireLaneHeadroom(profile, current, ReaderLane); err != nil {
			return ExecutionResult{}, err
		}
		response, operationErr := e.Harness.Run(ctx, item.RunRequest)
		next, accountingErr := readBudgetSnapshot(ctx, profile, e.Meter, false)
		if accountingErr == nil {
			accountingErr = validateMonotonicSnapshot(current, next)
		}
		if accountingErr != nil {
			return ExecutionResult{}, fmt.Errorf("LongMemEval provider accounting after run: %w", accountingErr)
		}
		if operationErr != nil {
			if err := ctx.Err(); err != nil {
				return ExecutionResult{}, fmt.Errorf("LongMemEval execution exceeded its time budget: %w", err)
			}
			var caseFailure *HarnessCaseFailure
			if !errors.As(operationErr, &caseFailure) || !caseFailure.received {
				return ExecutionResult{}, fmt.Errorf("LongMemEval run failed for opaque case: %w", operationErr)
			}
			beforeReader := current[ReaderLane]
			afterReader := next[ReaderLane]
			beforeJudge := current[JudgeLane]
			afterJudge := next[JudgeLane]
			requestDelta := afterReader.Requests - beforeReader.Requests
			successDelta := afterReader.Successes - beforeReader.Successes
			receiptDelta := afterReader.ReceiptedRequests - beforeReader.ReceiptedRequests
			completeReaderActivity := beforeJudge == afterJudge && requestDelta > 0 &&
				requestDelta == successDelta && requestDelta == receiptDelta &&
				validReaderBackedCaseActivity(caseFailure.activity, requestDelta)
			completeEmbeddingOnlyActivity := beforeReader == afterReader && beforeJudge == afterJudge &&
				validEmbeddingOnlyCaseActivity(caseFailure.activity)
			if !completeReaderActivity && !completeEmbeddingOnlyActivity {
				return ExecutionResult{}, fmt.Errorf(
					"LongMemEval unjudgeable run lacks complete provider receipts: %w",
					operationErr,
				)
			}
			current = next
			outcomes = append(outcomes, Outcome{QuestionID: item.questionID, Correct: false})
			receivedFailures++
			diagnostics.recordReceivedFailure(caseFailure)
			if e.OnCase != nil {
				e.OnCase(len(outcomes), len(projected))
			}
			continue
		}
		current = next

		entry := dataset.selected[item.questionID]
		if err := requireLaneHeadroom(profile, current, JudgeLane); err != nil {
			return ExecutionResult{}, err
		}
		correct, operationErr := e.Judge.Judge(ctx, JudgeInput{
			Reference:  cloneDatasetCase(entry),
			Hypothesis: response.FinalText,
		})
		next, accountingErr = readBudgetSnapshot(ctx, profile, e.Meter, false)
		if accountingErr == nil {
			accountingErr = validateMonotonicSnapshot(current, next)
		}
		if accountingErr != nil {
			return ExecutionResult{}, fmt.Errorf("LongMemEval provider accounting after judge: %w", accountingErr)
		}
		current = next
		if operationErr != nil {
			return ExecutionResult{}, fmt.Errorf("LongMemEval official judge failed for opaque case: %w", operationErr)
		}
		outcomes = append(outcomes, Outcome{QuestionID: item.questionID, Correct: correct})
		if e.OnCase != nil {
			e.OnCase(len(outcomes), len(projected))
		}
	}

	if err := ctx.Err(); err != nil {
		return ExecutionResult{}, fmt.Errorf("LongMemEval execution exceeded its time budget: %w", err)
	}
	score, err := Aggregate(dataset.Selection, outcomes)
	if err != nil {
		return ExecutionResult{}, err
	}
	final := snapshotSlice(current)
	zeroProviderCount := 0
	var zeroProviderLane string
	for _, policy := range profile.Providers {
		observed := current[policy.Lane]
		if zeroProviderCounters(observed) && observed.ReceiptSetSHA256 == "" {
			zeroProviderCount++
			zeroProviderLane = policy.Lane
		}
		if err := validateBudgetSnapshot(policy, observed); err != nil {
			return ExecutionResult{}, fmt.Errorf("LongMemEval final provider evidence: %w", err)
		}
	}
	unusedReader := zeroProviderCount == 1 && zeroProviderLane == ReaderLane
	if zeroProviderCount != 0 && zeroProviderCount != len(profile.Providers) && !unusedReader {
		return ExecutionResult{}, fmt.Errorf(
			"LongMemEval final provider evidence: lane %q is zero while another frozen lane is positive",
			zeroProviderLane,
		)
	}
	zeroProviders := zeroProviderCount == len(profile.Providers)
	elapsedMS := time.Since(started).Milliseconds()
	if elapsedMS < 1 {
		elapsedMS = 1
	}
	var evidence Evidence
	if zeroProviders {
		evidence, err = newAllReceivedFailuresEvidence(
			profile, dataset.Selection, artifactSHA256, uint64(elapsedMS), score, final, receivedFailures,
		)
	} else if unusedReader {
		if receivedFailures != 0 {
			return ExecutionResult{}, errors.New("LongMemEval unused-reader evidence cannot include received harness failures")
		}
		for index := range outcomes {
			outcomes[index].Correct = false
		}
		score, err = Aggregate(dataset.Selection, outcomes)
		if err == nil {
			evidence, err = newUnusedReaderJudgedZeroEvidence(
				profile, dataset.Selection, artifactSHA256, uint64(elapsedMS), score, final, receivedFailures,
			)
		}
	} else {
		evidence, err = NewEvidence(profile, dataset.Selection, artifactSHA256, uint64(elapsedMS), score, final)
	}
	if err != nil {
		return ExecutionResult{}, err
	}
	return ExecutionResult{Evidence: evidence, selection: dataset.Selection, Diagnostics: diagnostics}, nil
}

// validEmbeddingOnlyCaseActivity is the narrow Rev14 attribution boundary.
// It accepts an unjudgeable submitted /run response only when the source-bound
// broker generation proves that this exact case completed at least one query
// embedding, every admitted/signed embedding dispatch returned a validated
// Platform 200, no reader request ran, and no request remained in flight or
// observed cancellation. The 200 is corroboration, not canonical receipt
// evidence; the executor separately requires exact-zero reader and judge meter
// deltas before this path is eligible.
// Reader-backed failures continue through the canonical ProviderMeter path.
func validEmbeddingOnlyCaseActivity(activity *TrustedCaseInferenceActivity) bool {
	return activity != nil &&
		validReaderCaseActivity(activity, 0) &&
		validEmbeddingActivity(activity, true)
}

func validReaderBackedCaseActivity(activity *TrustedCaseInferenceActivity, readerRequests uint64) bool {
	return activity != nil && readerRequests > 0 &&
		validReaderCaseActivity(activity, readerRequests) && validEmbeddingActivity(activity, false)
}

// validReaderCaseActivity proves that every reader attempt in the isolated
// case reached one terminal, agent-attributable outcome. Fully receipted
// provider work remains canonical evidence. A request rejected before any
// provider reservation is also terminal agent work, but contributes no
// provider receipt: it may veto credit, never create credit or a retry.
func validReaderCaseActivity(activity *TrustedCaseInferenceActivity, receipted uint64) bool {
	if activity == nil || activity.ReaderReceipted != receipted ||
		activity.ReaderInFlight != 0 || activity.ReaderCancellations != 0 {
		return false
	}
	if activity.ReaderAgentRejections > activity.ReaderAttempts ||
		activity.ReaderReceipted > activity.ReaderAttempts ||
		activity.ReaderAgentRejections+activity.ReaderReceipted != activity.ReaderAttempts {
		return false
	}
	// A local schema rejection occurs before the trusted reader handler, while
	// a Platform schema rejection occurs after it. Dispatches therefore range
	// from the receipted count through all admitted attempts; anything outside
	// that interval is unattributed retry or accounting drift.
	return activity.ReaderDispatches >= activity.ReaderReceipted &&
		activity.ReaderDispatches <= activity.ReaderAttempts
}

func validEmbeddingActivity(activity *TrustedCaseInferenceActivity, required bool) bool {
	if activity.EmbeddingAttempts == 0 && activity.EmbeddingDispatches == 0 &&
		activity.EmbeddingDelivered == 0 && activity.EmbeddingInFlight == 0 &&
		activity.EmbeddingCancellations == 0 {
		return !required
	}
	return activity.EmbeddingAttempts > 0 &&
		activity.EmbeddingAttempts == activity.EmbeddingDispatches &&
		activity.EmbeddingAttempts == activity.EmbeddingDelivered &&
		activity.EmbeddingInFlight == 0 && activity.EmbeddingCancellations == 0
}

func readBudgetSnapshot(
	ctx context.Context,
	profile Profile,
	meter ProviderMeter,
	requireZero bool,
) (map[string]ProviderEvidence, error) {
	values, err := meter.Snapshot(ctx)
	if err != nil {
		return nil, err
	}
	policies := make(map[string]ProviderPolicy, len(profile.Providers))
	for _, policy := range profile.Providers {
		policies[policy.Lane] = policy
	}
	result := make(map[string]ProviderEvidence, len(values))
	for _, value := range values {
		policy, ok := policies[value.Lane]
		if !ok {
			return nil, fmt.Errorf("unexpected provider lane %q", value.Lane)
		}
		if _, duplicate := result[value.Lane]; duplicate {
			return nil, fmt.Errorf("duplicate provider lane %q", value.Lane)
		}
		if err := validateBudgetSnapshot(policy, value); err != nil {
			return nil, err
		}
		if requireZero && !zeroProviderCounters(value) {
			return nil, fmt.Errorf("provider lane %q did not start at zero", value.Lane)
		}
		result[value.Lane] = value
	}
	if len(result) != len(policies) {
		return nil, errors.New("provider meter does not cover every frozen lane")
	}
	return result, nil
}

func validateBudgetSnapshot(policy ProviderPolicy, value ProviderEvidence) error {
	if value.Lane != policy.Lane || value.Provider != policy.Provider ||
		value.ProfileRevision != policy.ProfileRevision || value.Model != policy.Model {
		return fmt.Errorf("provider identity drift on lane %q", policy.Lane)
	}
	if value.FallbackUsed {
		return fmt.Errorf("provider fallback is forbidden on lane %q", policy.Lane)
	}
	if value.CostSource != AuthoritativeCostV1 || value.Currency != "USD" {
		return fmt.Errorf("lane %q lacks authoritative USD provider receipt cost", policy.Lane)
	}
	if value.Requests == 0 {
		if !zeroProviderCounters(value) || value.ReceiptSetSHA256 != "" {
			return fmt.Errorf("lane %q has accounting without a provider request", policy.Lane)
		}
		return nil
	}
	return ValidateProviderEvidence(policy, value)
}

func zeroProviderCounters(value ProviderEvidence) bool {
	return value.Requests == 0 && value.Successes == 0 && value.ReceiptedRequests == 0 &&
		value.PromptTokens == 0 && value.CompletionTokens == 0 && value.TotalTokens == 0 &&
		value.CostUSDmicros == 0
}

func validateMonotonicSnapshot(previous, next map[string]ProviderEvidence) error {
	if len(previous) != len(next) {
		return errors.New("provider lane coverage changed during execution")
	}
	for lane, before := range previous {
		after, ok := next[lane]
		if !ok {
			return fmt.Errorf("provider lane %q disappeared during execution", lane)
		}
		if after.Requests < before.Requests || after.Successes < before.Successes ||
			after.ReceiptedRequests < before.ReceiptedRequests || after.PromptTokens < before.PromptTokens ||
			after.CompletionTokens < before.CompletionTokens || after.TotalTokens < before.TotalTokens ||
			after.CostUSDmicros < before.CostUSDmicros {
			return fmt.Errorf("provider lane %q accounting regressed", lane)
		}
		if after.Requests == before.Requests && after.ReceiptSetSHA256 != before.ReceiptSetSHA256 {
			return fmt.Errorf("provider lane %q receipt set changed without a request", lane)
		}
		if after.Requests > before.Requests && after.ReceiptSetSHA256 == before.ReceiptSetSHA256 {
			return fmt.Errorf("provider lane %q receipt set did not commit new requests", lane)
		}
	}
	return nil
}

func requireLaneHeadroom(profile Profile, current map[string]ProviderEvidence, lane string) error {
	for _, policy := range profile.Providers {
		if policy.Lane == lane {
			if current[lane].Requests >= policy.MaxRequests {
				return fmt.Errorf("LongMemEval provider lane %q has no request budget remaining", lane)
			}
			return nil
		}
	}
	return fmt.Errorf("LongMemEval profile lacks required provider lane %q", lane)
}

func snapshotSlice(snapshot map[string]ProviderEvidence) []ProviderEvidence {
	result := make([]ProviderEvidence, 0, len(snapshot))
	for _, value := range snapshot {
		result = append(result, value)
	}
	sort.Slice(result, func(i, j int) bool { return result[i].Lane < result[j].Lane })
	return result
}

func cloneDatasetCase(value DatasetCase) DatasetCase {
	value.HaystackSessionIDs = append([]string(nil), value.HaystackSessionIDs...)
	value.HaystackDates = append([]string(nil), value.HaystackDates...)
	value.AnswerSessionIDs = append([]string(nil), value.AnswerSessionIDs...)
	value.HaystackSessions = append([][]DatasetTurn(nil), value.HaystackSessions...)
	for index := range value.HaystackSessions {
		value.HaystackSessions[index] = append([]DatasetTurn(nil), value.HaystackSessions[index]...)
	}
	return value
}
