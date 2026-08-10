// Package ablation defines the trusted, provider-free primitives used by the
// DittoBench v9 inference and embedding sanity checks.
//
// Nothing in this package is wired into the scoring server. That is deliberate:
// callers must first establish a version-9 confirmation session at the trusted
// control plane, then explicitly construct a Responder for one intervention.
package ablation

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"sort"
	"strings"
	"sync"
	"time"
)

const (
	BenchVersionV9      = 9
	ContractVersion     = "dittobench-v9-ablation-v1"
	EmbeddingDimensions = 768

	maximumSyntheticRequests = uint64(4096)
	// A 768-dimensional float64 vector occupies 6 KiB. Limiting the entire
	// session to 4096 outputs bounds generated vector storage to 24 MiB.
	maximumSyntheticEmbeddingInputs = uint64(4096)
	maximumSyntheticInputBytes      = uint64(64 << 20)
)

var (
	ErrBudgetExhausted   = errors.New("synthetic ablation budget exhausted")
	ErrWrongIntervention = errors.New("synthetic operation does not match intervention")
)

// Intervention is the one provider lane replaced by trusted synthetic output.
type Intervention string

const (
	InterventionNone      Intervention = "none"
	InterventionInference Intervention = "inference"
	InterventionEmbedding Intervention = "embedding"
)

func ParseIntervention(raw string) (Intervention, error) {
	if raw == "" {
		return InterventionNone, nil
	}
	value := Intervention(raw)
	if !value.valid() {
		return "", fmt.Errorf("invalid ablation intervention %q", raw)
	}
	return value, nil
}

func (i Intervention) valid() bool {
	return i == InterventionNone || i == InterventionInference || i == InterventionEmbedding
}

// ValidateFor rejects every active intervention outside an explicit v9
// confirmation run. Omitted/none remains valid for every ordinary run.
func (i Intervention) ValidateFor(benchVersion int, confirmation bool) error {
	if !i.valid() {
		return fmt.Errorf("invalid ablation intervention %q", i)
	}
	if i == InterventionNone {
		return nil
	}
	if benchVersion != BenchVersionV9 || !confirmation {
		return fmt.Errorf("ablation intervention %q requires a v9 confirmation run", i)
	}
	return nil
}

// Mode is the audited operator posture for one ablation gate.
type Mode string

const (
	ModeOff     Mode = "off"
	ModeShadow  Mode = "shadow"
	ModeEnforce Mode = "enforce"
)

func ParseMode(raw string) (Mode, error) {
	value := Mode(raw)
	if !value.valid() {
		return "", fmt.Errorf("invalid ablation mode %q", raw)
	}
	return value, nil
}

func (m Mode) valid() bool {
	return m == ModeOff || m == ModeShadow || m == ModeEnforce
}

// Budget is a hard per-session ceiling. The integration may choose smaller
// frozen profile values, but cannot construct an unbounded ledger.
type Budget struct {
	MaxChatRequests        uint64 `json:"max_chat_requests,omitempty"`
	MaxChatInputBytes      uint64 `json:"max_chat_input_bytes,omitempty"`
	MaxEmbeddingRequests   uint64 `json:"max_embedding_requests,omitempty"`
	MaxEmbeddingInputs     uint64 `json:"max_embedding_inputs,omitempty"`
	MaxEmbeddingInputBytes uint64 `json:"max_embedding_input_bytes,omitempty"`
}

func (b Budget) validate(intervention Intervention) error {
	switch intervention {
	case InterventionInference:
		if b.MaxChatRequests == 0 || b.MaxChatRequests > maximumSyntheticRequests ||
			b.MaxChatInputBytes == 0 || b.MaxChatInputBytes > maximumSyntheticInputBytes {
			return fmt.Errorf("invalid inference ablation budget")
		}
		if b.MaxEmbeddingRequests != 0 || b.MaxEmbeddingInputs != 0 || b.MaxEmbeddingInputBytes != 0 {
			return fmt.Errorf("inference ablation budget contains embedding limits")
		}
	case InterventionEmbedding:
		if b.MaxEmbeddingRequests == 0 || b.MaxEmbeddingRequests > maximumSyntheticRequests ||
			b.MaxEmbeddingInputs == 0 || b.MaxEmbeddingInputs > maximumSyntheticEmbeddingInputs ||
			b.MaxEmbeddingInputBytes == 0 || b.MaxEmbeddingInputBytes > maximumSyntheticInputBytes {
			return fmt.Errorf("invalid embedding ablation budget")
		}
		if b.MaxChatRequests != 0 || b.MaxChatInputBytes != 0 {
			return fmt.Errorf("embedding ablation budget contains chat limits")
		}
	default:
		return fmt.Errorf("cannot create synthetic budget for intervention %q", intervention)
	}
	return nil
}

// Usage is trusted synthetic accounting. Upstream fields are always zero and
// are carried explicitly so synthetic traffic cannot be mistaken for provider
// billing or ordinary model-use evidence.
type Usage struct {
	Synthetic                    bool         `json:"synthetic"`
	TelemetryNamespace           string       `json:"telemetry_namespace"`
	Intervention                 Intervention `json:"intervention"`
	Budget                       Budget       `json:"budget"`
	ChatAttempts                 uint64       `json:"chat_attempts,omitempty"`
	ChatApplied                  uint64       `json:"chat_applied,omitempty"`
	ChatInputBytes               uint64       `json:"chat_input_bytes,omitempty"`
	EmbeddingAttempts            uint64       `json:"embedding_attempts,omitempty"`
	EmbeddingApplied             uint64       `json:"embedding_applied,omitempty"`
	EmbeddingInputs              uint64       `json:"embedding_inputs,omitempty"`
	EmbeddingInputBytes          uint64       `json:"embedding_input_bytes,omitempty"`
	RejectedRequests             uint64       `json:"rejected_requests,omitempty"`
	BudgetExhausted              bool         `json:"budget_exhausted,omitempty"`
	UpstreamRequests             uint64       `json:"upstream_requests"`
	UpstreamInputTokens          uint64       `json:"upstream_input_tokens"`
	UpstreamOutputTokens         uint64       `json:"upstream_output_tokens"`
	UpstreamProviderCostMicroUSD uint64       `json:"upstream_provider_cost_microusd"`
}

func telemetryNamespace(intervention Intervention) string {
	return "dittobench.v9.ablation." + string(intervention)
}

func (u Usage) affectedCalls() uint64 {
	if u.Intervention == InterventionInference {
		return u.ChatApplied
	}
	if u.Intervention == InterventionEmbedding {
		return u.EmbeddingApplied
	}
	return 0
}

func (u Usage) validate(intervention Intervention) error {
	if !u.Synthetic || u.Intervention != intervention || u.TelemetryNamespace != telemetryNamespace(intervention) {
		return fmt.Errorf("synthetic usage does not match intervention")
	}
	if err := u.Budget.validate(intervention); err != nil {
		return err
	}
	if u.UpstreamRequests != 0 || u.UpstreamInputTokens != 0 || u.UpstreamOutputTokens != 0 ||
		u.UpstreamProviderCostMicroUSD != 0 {
		return fmt.Errorf("synthetic usage contains upstream provider accounting")
	}
	if u.ChatApplied > u.ChatAttempts || u.EmbeddingApplied > u.EmbeddingAttempts {
		return fmt.Errorf("synthetic usage applied count exceeds attempts")
	}
	if (u.RejectedRequests > 0) != u.BudgetExhausted {
		return fmt.Errorf("synthetic usage exhaustion accounting is inconsistent")
	}
	switch intervention {
	case InterventionInference:
		if u.ChatApplied > u.Budget.MaxChatRequests || u.ChatInputBytes > u.Budget.MaxChatInputBytes ||
			u.EmbeddingAttempts != 0 || u.EmbeddingApplied != 0 || u.EmbeddingInputs != 0 || u.EmbeddingInputBytes != 0 {
			return fmt.Errorf("invalid inference synthetic usage")
		}
		if u.ChatAttempts != u.ChatApplied+u.RejectedRequests {
			return fmt.Errorf("invalid inference synthetic attempt accounting")
		}
	case InterventionEmbedding:
		if u.EmbeddingApplied > u.Budget.MaxEmbeddingRequests || u.EmbeddingInputs > u.Budget.MaxEmbeddingInputs ||
			u.EmbeddingInputBytes > u.Budget.MaxEmbeddingInputBytes || u.ChatAttempts != 0 ||
			u.ChatApplied != 0 || u.ChatInputBytes != 0 {
			return fmt.Errorf("invalid embedding synthetic usage")
		}
		if u.EmbeddingAttempts != u.EmbeddingApplied+u.RejectedRequests {
			return fmt.Errorf("invalid embedding synthetic attempt accounting")
		}
	}
	return nil
}

// Ledger admits synthetic calls atomically. It deliberately does not contain
// an HTTP client or upstream URL, so an admitted call cannot contact a provider.
type Ledger struct {
	mu    sync.Mutex
	usage Usage
}

func NewLedger(intervention Intervention, budget Budget) (*Ledger, error) {
	if intervention != InterventionInference && intervention != InterventionEmbedding {
		return nil, fmt.Errorf("active intervention is required")
	}
	if err := budget.validate(intervention); err != nil {
		return nil, err
	}
	return &Ledger{usage: Usage{
		Synthetic: true, TelemetryNamespace: telemetryNamespace(intervention),
		Intervention: intervention, Budget: budget,
	}}, nil
}

func exceeds(current, addition, maximum uint64) bool {
	return addition > maximum || current > maximum-addition
}

func (l *Ledger) AdmitChat(inputBytes uint64) error {
	if inputBytes == 0 {
		return fmt.Errorf("synthetic chat request is empty")
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.usage.Intervention != InterventionInference {
		return ErrWrongIntervention
	}
	l.usage.ChatAttempts++
	if exceeds(l.usage.ChatApplied, 1, l.usage.Budget.MaxChatRequests) ||
		exceeds(l.usage.ChatInputBytes, inputBytes, l.usage.Budget.MaxChatInputBytes) {
		l.usage.RejectedRequests++
		l.usage.BudgetExhausted = true
		return ErrBudgetExhausted
	}
	l.usage.ChatApplied++
	l.usage.ChatInputBytes += inputBytes
	return nil
}

func (l *Ledger) AdmitEmbedding(inputs, inputBytes uint64) error {
	if inputs == 0 || inputBytes == 0 {
		return fmt.Errorf("synthetic embedding request is empty")
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.usage.Intervention != InterventionEmbedding {
		return ErrWrongIntervention
	}
	l.usage.EmbeddingAttempts++
	if exceeds(l.usage.EmbeddingApplied, 1, l.usage.Budget.MaxEmbeddingRequests) ||
		exceeds(l.usage.EmbeddingInputs, inputs, l.usage.Budget.MaxEmbeddingInputs) ||
		exceeds(l.usage.EmbeddingInputBytes, inputBytes, l.usage.Budget.MaxEmbeddingInputBytes) {
		l.usage.RejectedRequests++
		l.usage.BudgetExhausted = true
		return ErrBudgetExhausted
	}
	l.usage.EmbeddingApplied++
	l.usage.EmbeddingInputs += inputs
	l.usage.EmbeddingInputBytes += inputBytes
	return nil
}

func (l *Ledger) Snapshot() Usage {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.usage
}

// ChatCompletion is an OpenAI-compatible response envelope. Synthetic
// accounting stays out of this harness-visible object; each response is
// independently projected so the envelope itself is not a fixed marker.
type ChatCompletion struct {
	ID      string       `json:"id"`
	Object  string       `json:"object"`
	Created int64        `json:"created"`
	Model   string       `json:"model"`
	Choices []ChatChoice `json:"choices"`
	Usage   ChatUsage    `json:"usage"`
}

type ChatChoice struct {
	Index        int         `json:"index"`
	Message      ChatMessage `json:"message"`
	FinishReason string      `json:"finish_reason"`
}

type ChatMessage struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

type ChatUsage struct {
	PromptTokens     int `json:"prompt_tokens"`
	CompletionTokens int `json:"completion_tokens"`
	TotalTokens      int `json:"total_tokens"`
}

func projectedDigest(key []byte, domain string, sequence uint64, payload []byte) []byte {
	mac := hmac.New(sha256.New, key)
	_, _ = mac.Write([]byte(domain))
	var encoded [8]byte
	binary.BigEndian.PutUint64(encoded[:], sequence)
	_, _ = mac.Write(encoded[:])
	_, _ = mac.Write(payload)
	return mac.Sum(nil)
}

// digestChoice consumes independent keyed bytes for one grammar decision. The
// response vocabulary is deliberately compositional: there is no published
// list of complete marker strings for a harness to compare byte-for-byte.
func digestChoice(seed []byte, decision uint64, choices []string) string {
	digest := projectedDigest(seed, "neutral-chat-grammar", decision, nil)
	return choices[int(binary.BigEndian.Uint16(digest[:2]))%len(choices)]
}

func keyedNeutralChatContent(seed []byte) string {
	sources := []string{
		"the available material", "the supplied context", "the information here",
		"what is provided", "the current record", "the details at hand",
		"the available evidence", "this context",
	}
	judgments := []string{
		"does not establish", "does not support", "is insufficient to determine",
		"does not provide", "is not enough to identify", "does not justify",
		"cannot reliably settle", "leaves unresolved",
	}
	conclusions := []string{
		"a reliable answer", "a specific conclusion", "a confident response",
		"a verifiable result", "a definite answer", "the requested conclusion",
		"a supported determination", "a dependable finding",
	}
	qualifiers := []string{
		"without additional context", "from these details alone", "with sufficient confidence",
		"on the present evidence", "without more information", "as stated",
		"from the current record", "reliably",
	}
	requests := []string{
		"More specific context", "Additional relevant information", "Further supporting detail",
		"A clearer source", "More complete evidence", "Additional verified context",
		"A more specific record", "Further pertinent material",
	}
	verbs := []string{
		"would be needed to answer accurately", "would be required for a reliable answer",
		"could support a more definite response", "would allow a better-supported conclusion",
		"would be necessary to resolve it", "could make a reliable determination possible",
		"would help establish the answer", "would be needed to verify a conclusion",
	}

	first := digestChoice(seed, 0, sources) + " " + digestChoice(seed, 1, judgments) + " " +
		digestChoice(seed, 2, conclusions) + " " + digestChoice(seed, 3, qualifiers) + "."
	if seed[0]&1 == 0 {
		return strings.ToUpper(first[:1]) + first[1:]
	}
	second := digestChoice(seed, 4, requests) + " " + digestChoice(seed, 5, verbs) + "."
	return strings.ToUpper(first[:1]) + first[1:] + " " + second
}

const projectedCreatedLookback = 90 * time.Second

// keyedCreated keeps the provider-shaped timestamp close to the response
// clock without exposing a fixed epoch or a future-dated projection. The
// keyed lag makes independent calls and keys occupy the ordinary recent-past
// window instead of sharing one synthetic marker. Passing the clock in keeps
// the projection deterministic and directly testable.
func keyedCreated(seed []byte, now time.Time) int64 {
	lookbackSeconds := uint64(projectedCreatedLookback / time.Second)
	lagSeconds := 1 + binary.BigEndian.Uint64(seed[16:24])%lookbackSeconds
	return now.UTC().Unix() - int64(lagSeconds)
}

func syntheticChatCompletion(model string, requestBytes uint64, seed []byte, now time.Time) (ChatCompletion, error) {
	if model == "" || strings.TrimSpace(model) != model {
		return ChatCompletion{}, fmt.Errorf("synthetic chat model is invalid")
	}
	if requestBytes == 0 || len(seed) < sha256.Size {
		return ChatCompletion{}, fmt.Errorf("synthetic chat projection is invalid")
	}
	content := keyedNeutralChatContent(seed)
	promptTokens := int((requestBytes + 3) / 4)
	completionTokens := len(strings.Fields(content))
	return ChatCompletion{
		ID: "chatcmpl-" + hex.EncodeToString(seed[1:13]), Object: "chat.completion",
		Created: keyedCreated(seed, now), Model: model,
		Choices: []ChatChoice{{
			Index: 0, Message: ChatMessage{Role: "assistant", Content: content},
			FinishReason: "stop",
		}},
		Usage: ChatUsage{PromptTokens: promptTokens, CompletionTokens: completionTokens, TotalTokens: promptTokens + completionTokens},
	}, nil
}

type EmbeddingResponse struct {
	Embeddings      [][]float64 `json:"embeddings"`
	PromptEvalCount int         `json:"prompt_eval_count"`
}

func projectedEmbedding(seed []byte) []float64 {
	vector := make([]float64, EmbeddingDimensions)
	var norm float64
	for blockIndex, index := 0, 0; index < EmbeddingDimensions; blockIndex++ {
		block := projectedDigest(seed, "embedding-block", uint64(blockIndex), nil)
		for offset := 0; offset+1 < len(block) && index < EmbeddingDimensions; offset, index = offset+2, index+1 {
			value := float64(int16(binary.BigEndian.Uint16(block[offset:offset+2]))) / 32768
			vector[index] = value
			norm += value * value
		}
	}
	norm = math.Sqrt(norm)
	for index := range vector {
		vector[index] /= norm
	}
	return vector
}

// syntheticEmbeddingResponse is private so the only externally callable path
// that allocates N vectors is Responder.Embeddings, after bounded admission.
func normalizeEmbeddingInput(input string) (string, error) {
	normalized := strings.Join(strings.Fields(input), " ")
	if normalized == "" {
		return "", fmt.Errorf("synthetic embedding input is empty")
	}
	return normalized, nil
}

func syntheticEmbeddingResponse(inputs []string, projectionKey []byte) (EmbeddingResponse, error) {
	if len(inputs) == 0 {
		return EmbeddingResponse{}, fmt.Errorf("synthetic embedding input count must be positive")
	}
	if len(projectionKey) < sha256.Size {
		return EmbeddingResponse{}, fmt.Errorf("synthetic embedding projection is invalid")
	}
	response := EmbeddingResponse{Embeddings: make([][]float64, len(inputs))}
	for index, input := range inputs {
		normalized, err := normalizeEmbeddingInput(input)
		if err != nil {
			return EmbeddingResponse{}, err
		}
		seed := projectedDigest(projectionKey, "embedding-input", 0, []byte(normalized))
		response.Embeddings[index] = projectedEmbedding(seed)
		response.PromptEvalCount += int((uint64(len(normalized)) + 3) / 4)
	}
	return response, nil
}

// Responder combines provider-free synthesis with the atomic ledger.
type Responder struct {
	ledger    *Ledger
	synthesis sync.Mutex
	key       []byte
	sequence  uint64
	now       func() time.Time
}

func NewResponder(intervention Intervention, budget Budget) (*Responder, error) {
	ledger, err := NewLedger(intervention, budget)
	if err != nil {
		return nil, err
	}
	key := make([]byte, sha256.Size)
	if _, err := rand.Read(key); err != nil {
		return nil, fmt.Errorf("create responder entropy: %w", err)
	}
	return &Responder{ledger: ledger, key: key, now: time.Now}, nil
}

func (r *Responder) bindProjection(projectionKey []byte, artifactSHA256 string, intervention Intervention) {
	r.synthesis.Lock()
	defer r.synthesis.Unlock()
	payload := []byte(artifactSHA256 + "\x00" + string(intervention))
	r.key = projectedDigest(projectionKey, "dittobench-v9-synthetic-response", 0, payload)
	r.sequence = 0
}

func (r *Responder) nextProjection(domain string, payload []byte) []byte {
	r.synthesis.Lock()
	defer r.synthesis.Unlock()
	r.sequence++
	return projectedDigest(r.key, domain, r.sequence, payload)
}

func (r *Responder) stableProjectionKey() []byte {
	r.synthesis.Lock()
	defer r.synthesis.Unlock()
	return append([]byte(nil), r.key...)
}

func (r *Responder) Chat(model string, requestBytes uint64) (ChatCompletion, error) {
	if model == "" || strings.TrimSpace(model) != model {
		return ChatCompletion{}, fmt.Errorf("synthetic chat model is invalid")
	}
	if err := r.ledger.AdmitChat(requestBytes); err != nil {
		return ChatCompletion{}, err
	}
	var encoded [8]byte
	binary.BigEndian.PutUint64(encoded[:], requestBytes)
	seed := r.nextProjection("chat\x00"+model, encoded[:])
	return syntheticChatCompletion(model, requestBytes, seed, r.now())
}

func (r *Responder) Embeddings(inputs []string) (EmbeddingResponse, error) {
	if len(inputs) == 0 {
		return EmbeddingResponse{}, fmt.Errorf("synthetic embedding inputs are empty")
	}
	var inputBytes uint64
	for _, input := range inputs {
		if _, err := normalizeEmbeddingInput(input); err != nil {
			return EmbeddingResponse{}, err
		}
		if exceeds(inputBytes, uint64(len(input)), ^uint64(0)) {
			return EmbeddingResponse{}, fmt.Errorf("synthetic embedding input bytes overflow")
		}
		inputBytes += uint64(len(input))
	}
	if err := r.ledger.AdmitEmbedding(uint64(len(inputs)), inputBytes); err != nil {
		return EmbeddingResponse{}, err
	}
	return syntheticEmbeddingResponse(inputs, r.stableProjectionKey())
}

func (r *Responder) Snapshot() Usage { return r.ledger.Snapshot() }

type Status string

const (
	StatusNotRun      Status = "not_run"
	StatusPassed      Status = "passed"
	StatusFailed      Status = "failed"
	StatusUnavailable Status = "unavailable"
)

type Reason string

const (
	ReasonDisabled                Reason = "disabled"
	ReasonThresholdMet            Reason = "threshold_met"
	ReasonDeltaBelowThreshold     Reason = "delta_below_threshold"
	ReasonNoRelevantCalls         Reason = "no_relevant_trusted_calls"
	ReasonInsufficientCalls       Reason = "insufficient_relevant_trusted_calls"
	ReasonBudgetExhausted         Reason = "synthetic_budget_exhausted"
	ReasonInterventionUnavailable Reason = "intervention_unavailable"
	ReasonEnforceProofUnavailable Reason = "counterfactual_proof_unavailable"
)

type CaseScore struct {
	CaseID string
	Score  float64
}

type EvaluateInput struct {
	BenchVersion            int
	ArtifactSHA256          string
	Intervention            Intervention
	Mode                    Mode
	ProfileRevision         string
	ThresholdManifestSHA256 string
	DatasetSHA256           string
	Threshold               float64
	Baseline                []CaseScore
	Ablated                 []CaseScore
	Usage                   Usage
	InterventionUnavailable bool
	FrozenProfile           FrozenProfile
	AblationProfileSHA256   string
	Coordinator             CoordinationReport
}

// Evidence is safe aggregate evidence. Raw case identities remain private and
// are represented only by content digests.
type Evidence struct {
	ContractVersion         string       `json:"contract_version"`
	BenchVersion            int          `json:"bench_version"`
	Intervention            Intervention `json:"intervention"`
	Mode                    Mode         `json:"mode"`
	Status                  Status       `json:"status"`
	Reason                  Reason       `json:"reason"`
	ProfileRevision         string       `json:"profile_revision,omitempty"`
	ArtifactSHA256          string       `json:"artifact_sha256,omitempty"`
	AblationProfileSHA256   string       `json:"ablation_profile_sha256,omitempty"`
	CoordinatorSHA256       string       `json:"coordinator_sha256,omitempty"`
	SelectedCasesSHA256     string       `json:"selected_cases_sha256,omitempty"`
	ThresholdManifestSHA256 string       `json:"threshold_manifest_sha256,omitempty"`
	DatasetSHA256           string       `json:"dataset_sha256,omitempty"`
	CaseSetSHA256           string       `json:"case_set_sha256,omitempty"`
	BaselineScoresSHA256    string       `json:"baseline_scores_sha256,omitempty"`
	AblatedScoresSHA256     string       `json:"ablated_scores_sha256,omitempty"`
	BaselineMean            float64      `json:"baseline_mean"`
	AblatedMean             float64      `json:"ablated_mean"`
	Delta                   float64      `json:"delta"`
	Threshold               float64      `json:"threshold"`
	SampleCount             int          `json:"sample_count"`
	AffectedCallCount       uint64       `json:"affected_call_count"`
	SemanticFactor          float64      `json:"semantic_factor"`
	AppliedFactor           float64      `json:"applied_factor"`
	SyntheticUsage          Usage        `json:"synthetic_usage"`
}

type Evaluation struct {
	Evidence      Evidence
	SHA256        string
	CanonicalJSON []byte
}

func canonicalSHA256(value string) bool {
	if len(value) != sha256.Size*2 || value != strings.ToLower(value) {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}

func validateRevision(value string) bool {
	return value != "" && len(value) <= 128 && strings.TrimSpace(value) == value
}

type canonicalCaseScore struct {
	CaseID string  `json:"case_id"`
	Score  float64 `json:"score"`
}

func canonicalizeScores(scores []CaseScore, requireNonEmpty bool) ([]canonicalCaseScore, error) {
	if requireNonEmpty && len(scores) == 0 {
		return nil, fmt.Errorf("paired ablation sample is empty")
	}
	ordered := make([]canonicalCaseScore, len(scores))
	seen := make(map[string]struct{}, len(scores))
	for index, score := range scores {
		if score.CaseID == "" || len(score.CaseID) > 256 || strings.TrimSpace(score.CaseID) != score.CaseID {
			return nil, fmt.Errorf("invalid ablation case id")
		}
		if _, duplicate := seen[score.CaseID]; duplicate {
			return nil, fmt.Errorf("duplicate ablation case id %q", score.CaseID)
		}
		seen[score.CaseID] = struct{}{}
		if math.IsNaN(score.Score) || math.IsInf(score.Score, 0) || score.Score < 0 || score.Score > 1 {
			return nil, fmt.Errorf("invalid score for ablation case %q", score.CaseID)
		}
		ordered[index] = canonicalCaseScore{CaseID: score.CaseID, Score: score.Score}
	}
	sort.Slice(ordered, func(left, right int) bool { return ordered[left].CaseID < ordered[right].CaseID })
	return ordered, nil
}

func validateAndSort(scores []CaseScore) ([]canonicalCaseScore, error) {
	return canonicalizeScores(scores, true)
}

// scoresSHA256 is the canonical score commitment carried by every lane. Raw
// case scores intentionally remain off the coordination-report wire, but this
// digest survives JSON persistence and binds later gate inputs to the values
// produced by the coordinator.
func scoresSHA256(scores []CaseScore) (string, error) {
	ordered, err := canonicalizeScores(scores, false)
	if err != nil {
		return "", err
	}
	if ordered == nil {
		ordered = []canonicalCaseScore{}
	}
	digest, _, err := digestJSON(ordered)
	return digest, err
}

func digestJSON(value any) (string, []byte, error) {
	body, err := json.Marshal(value)
	if err != nil {
		return "", nil, err
	}
	sum := sha256.Sum256(body)
	return hex.EncodeToString(sum[:]), body, nil
}

func round6(value float64) float64 { return math.Round(value*1e6) / 1e6 }

func (r CoordinationReport) lane(intervention Intervention) (LaneReport, error) {
	switch intervention {
	case InterventionInference:
		return r.InferenceIntervention, nil
	case InterventionEmbedding:
		return r.EmbeddingIntervention, nil
	default:
		return LaneReport{}, fmt.Errorf("active intervention is required")
	}
}

func validateStaticBinding(input EvaluateInput) error {
	profileSHA256, err := FrozenProfileSHA256(input.FrozenProfile)
	if err != nil {
		return err
	}
	if !canonicalSHA256(input.AblationProfileSHA256) || input.AblationProfileSHA256 != profileSHA256 {
		return fmt.Errorf("frozen ablation profile checksum mismatch")
	}
	profile := input.FrozenProfile
	if !canonicalSHA256(input.ArtifactSHA256) {
		return fmt.Errorf("invalid runtime artifact digest")
	}
	if input.BenchVersion != profile.BenchVersion || input.ProfileRevision != profile.Revision ||
		input.DatasetSHA256 != profile.DatasetSHA256 || input.ThresholdManifestSHA256 != profile.ThresholdManifestSHA256 {
		return fmt.Errorf("gate inputs do not match frozen ablation profile")
	}
	profileThreshold := profile.InferenceThreshold
	if input.Intervention == InterventionEmbedding {
		profileThreshold = profile.EmbeddingThreshold
	}
	if input.Threshold != profileThreshold {
		return fmt.Errorf("gate threshold does not match frozen ablation profile")
	}
	return nil
}

func validUnavailableReason(reason UnavailableReason) bool {
	switch reason {
	case UnavailableOrdinary, UnavailableCaseFailure, UnavailablePartialIntervention,
		UnavailableRetryExhausted, UnavailableRequestLimit, UnavailableCancelled,
		UnavailableDeadline, UnavailableZeroRelevantCalls, UnavailableInsufficientCalls,
		UnavailableSyntheticBudget:
		return true
	default:
		return false
	}
}

func validateLaneScoreCommitment(report LaneReport, selectedCount int) error {
	if !canonicalSHA256(report.ScoresSHA256) {
		return fmt.Errorf("%s lane score commitment is invalid", report.Lane)
	}
	if report.AttemptCount < 0 || report.CompletedCases < 0 || report.CompletedCases > selectedCount {
		return fmt.Errorf("%s lane counters are invalid", report.Lane)
	}
	if report.Complete {
		if report.CompletedCases != selectedCount || report.UnavailableReason != UnavailableNone ||
			report.Observation == ObservationUnavailable {
			return fmt.Errorf("%s completed lane shape is invalid", report.Lane)
		}
		if report.Lane == LaneOrdinary && report.Observation != ObservationAvailable {
			return fmt.Errorf("ordinary completed lane observation is invalid")
		}
		if report.Lane != LaneOrdinary && report.Observation != ObservationChanged && report.Observation != ObservationInvariant {
			return fmt.Errorf("%s completed lane observation is invalid", report.Lane)
		}
		if len(report.Scores) > 0 {
			if len(report.Scores) != selectedCount {
				return fmt.Errorf("%s lane score count is invalid", report.Lane)
			}
			digest, err := scoresSHA256(report.Scores)
			if err != nil || digest != report.ScoresSHA256 {
				return fmt.Errorf("%s lane scores do not match their commitment", report.Lane)
			}
		}
		return nil
	}
	if report.Observation != ObservationUnavailable || !validUnavailableReason(report.UnavailableReason) || len(report.Scores) != 0 {
		return fmt.Errorf("%s unavailable lane shape is invalid", report.Lane)
	}
	if report.Lane == LaneOrdinary && (report.UnavailableReason == UnavailableOrdinary ||
		report.UnavailableReason == UnavailableZeroRelevantCalls ||
		report.UnavailableReason == UnavailableInsufficientCalls ||
		report.UnavailableReason == UnavailableSyntheticBudget) {
		return fmt.Errorf("ordinary unavailable reason is invalid")
	}
	emptyDigest, err := scoresSHA256(nil)
	if err != nil || report.ScoresSHA256 != emptyDigest {
		return fmt.Errorf("%s unavailable lane has a non-empty score commitment", report.Lane)
	}
	return nil
}

func validateCoordinatorBinding(input EvaluateInput) (LaneReport, error) {
	if err := validateStaticBinding(input); err != nil {
		return LaneReport{}, err
	}
	profileSHA256 := input.AblationProfileSHA256
	profile := input.FrozenProfile
	report := input.Coordinator
	if report.ContractVersion != ContractVersion || report.BenchVersion != BenchVersionV9 ||
		report.ArtifactSHA256 != input.ArtifactSHA256 || report.DatasetSHA256 != profile.DatasetSHA256 ||
		report.ThresholdManifestSHA256 != profile.ThresholdManifestSHA256 ||
		report.AblationProfileSHA256 != profileSHA256 || report.CoordinatorPolicy != profile.CoordinatorPolicy ||
		report.SelectedCaseCount != profile.CoordinatorPolicy.SampleSize ||
		!canonicalSHA256(report.SelectedCasesSHA256) || !canonicalSHA256(report.SelectedCaseSetSHA256) ||
		!canonicalSHA256(report.CoordinatorSHA256) {
		return LaneReport{}, fmt.Errorf("coordinator report does not match frozen ablation profile")
	}
	coordinatorSHA256, err := coordinatorDigest(report)
	if err != nil {
		return LaneReport{}, err
	}
	if report.CoordinatorSHA256 != coordinatorSHA256 {
		return LaneReport{}, fmt.Errorf("coordinator report checksum mismatch")
	}
	if report.Ordinary.Lane != LaneOrdinary || report.Ordinary.SyntheticUsage != (Usage{}) {
		return LaneReport{}, fmt.Errorf("ordinary coordinator telemetry contains synthetic accounting")
	}
	if report.InferenceIntervention.Lane != LaneInference || report.EmbeddingIntervention.Lane != LaneEmbedding {
		return LaneReport{}, fmt.Errorf("coordinator intervention lanes are mismatched")
	}
	for _, lane := range []LaneReport{report.Ordinary, report.InferenceIntervention, report.EmbeddingIntervention} {
		if err := validateLaneScoreCommitment(lane, report.SelectedCaseCount); err != nil {
			return LaneReport{}, err
		}
	}
	if err := report.InferenceIntervention.SyntheticUsage.validate(InterventionInference); err != nil {
		return LaneReport{}, fmt.Errorf("invalid coordinator inference telemetry: %w", err)
	}
	if err := report.EmbeddingIntervention.SyntheticUsage.validate(InterventionEmbedding); err != nil {
		return LaneReport{}, fmt.Errorf("invalid coordinator embedding telemetry: %w", err)
	}
	if report.InferenceIntervention.SyntheticUsage.Budget != profile.InferenceBudget ||
		report.EmbeddingIntervention.SyntheticUsage.Budget != profile.EmbeddingBudget {
		return LaneReport{}, fmt.Errorf("coordinator synthetic budgets do not match frozen profile")
	}
	for _, interventionLane := range []LaneReport{report.InferenceIntervention, report.EmbeddingIntervention} {
		if interventionLane.Complete && interventionLane.SyntheticUsage.affectedCalls() <
			uint64(report.SelectedCaseCount)*minimumRelevantCallsPerCase {
			return LaneReport{}, fmt.Errorf("%s completed lane lacks per-case relevance coverage", interventionLane.Lane)
		}
		if interventionLane.SyntheticUsage.BudgetExhausted !=
			(interventionLane.UnavailableReason == UnavailableSyntheticBudget) {
			return LaneReport{}, fmt.Errorf("%s budget-exhaustion shape is invalid", interventionLane.Lane)
		}
	}
	if !report.Ordinary.Complete {
		for _, interventionLane := range []LaneReport{report.InferenceIntervention, report.EmbeddingIntervention} {
			if interventionLane.Complete || interventionLane.UnavailableReason != UnavailableOrdinary ||
				interventionLane.AttemptCount != 0 || interventionLane.CompletedCases != 0 ||
				interventionLane.SyntheticUsage.affectedCalls() != 0 {
				return LaneReport{}, fmt.Errorf("intervention lane ran after ordinary unavailability")
			}
		}
	} else if report.InferenceIntervention.UnavailableReason == UnavailableOrdinary ||
		report.EmbeddingIntervention.UnavailableReason == UnavailableOrdinary {
		return LaneReport{}, fmt.Errorf("ordinary-unavailable reason contradicts completed ordinary lane")
	}
	lane, err := report.lane(input.Intervention)
	if err != nil {
		return LaneReport{}, err
	}
	if input.Usage != (Usage{}) && lane.SyntheticUsage != input.Usage {
		return LaneReport{}, fmt.Errorf("gate usage does not match coordinator telemetry")
	}
	if input.InterventionUnavailable {
		return LaneReport{}, fmt.Errorf("caller-supplied intervention availability is not authoritative")
	}
	return lane, nil
}

func coordinatorReportPopulated(report CoordinationReport) bool {
	return report.ContractVersion != "" || report.BenchVersion != 0 || report.ArtifactSHA256 != "" ||
		report.DatasetSHA256 != "" || report.ThresholdManifestSHA256 != "" ||
		report.AblationProfileSHA256 != "" || report.SelectedCaseCount != 0 ||
		report.SelectedCasesSHA256 != "" || report.SelectedCaseSetSHA256 != "" ||
		report.CoordinatorSHA256 != "" || report.Ordinary.Lane != "" ||
		report.InferenceIntervention.Lane != "" || report.EmbeddingIntervention.Lane != ""
}

func pristineUsage(intervention Intervention, profile FrozenProfile) Usage {
	budget := profile.InferenceBudget
	if intervention == InterventionEmbedding {
		budget = profile.EmbeddingBudget
	}
	return Usage{
		Synthetic: true, TelemetryNamespace: telemetryNamespace(intervention),
		Intervention: intervention, Budget: budget,
	}
}

func unavailableReason(reason UnavailableReason) Reason {
	switch reason {
	case UnavailableSyntheticBudget:
		return ReasonBudgetExhausted
	case UnavailableZeroRelevantCalls:
		return ReasonNoRelevantCalls
	case UnavailableInsufficientCalls:
		return ReasonInsufficientCalls
	default:
		return ReasonInterventionUnavailable
	}
}

func Evaluate(input EvaluateInput) (Evaluation, error) {
	if input.BenchVersion != BenchVersionV9 {
		return Evaluation{}, fmt.Errorf("ablation gate requires bench_version 9")
	}
	if input.Intervention != InterventionInference && input.Intervention != InterventionEmbedding {
		return Evaluation{}, fmt.Errorf("active ablation intervention is required")
	}
	if !input.Mode.valid() {
		return Evaluation{}, fmt.Errorf("invalid ablation mode %q", input.Mode)
	}
	if input.Mode == ModeOff {
		if err := validateStaticBinding(input); err != nil {
			return Evaluation{}, err
		}
		if coordinatorReportPopulated(input.Coordinator) || input.Usage != (Usage{}) ||
			len(input.Baseline) != 0 || len(input.Ablated) != 0 || input.InterventionUnavailable {
			return Evaluation{}, fmt.Errorf("disabled ablation gate contains runtime activity")
		}
		usage := pristineUsage(input.Intervention, input.FrozenProfile)
		evidence := Evidence{
			ContractVersion: ContractVersion, BenchVersion: input.BenchVersion,
			Intervention: input.Intervention, Mode: input.Mode, Status: StatusNotRun,
			Reason: ReasonDisabled, ProfileRevision: input.ProfileRevision,
			ArtifactSHA256:          input.ArtifactSHA256,
			AblationProfileSHA256:   input.AblationProfileSHA256,
			ThresholdManifestSHA256: input.ThresholdManifestSHA256, DatasetSHA256: input.DatasetSHA256,
			SemanticFactor: 1, AppliedFactor: 1, SyntheticUsage: usage,
		}
		return digestEvidence(evidence)
	}
	lane, err := validateCoordinatorBinding(input)
	if err != nil {
		return Evaluation{}, err
	}
	usage := lane.SyntheticUsage
	if err := usage.validate(input.Intervention); err != nil {
		return Evaluation{}, err
	}
	if !validateRevision(input.ProfileRevision) {
		return Evaluation{}, fmt.Errorf("invalid ablation profile revision")
	}
	if !canonicalSHA256(input.ThresholdManifestSHA256) {
		return Evaluation{}, fmt.Errorf("invalid ablation threshold manifest digest")
	}
	if !canonicalSHA256(input.DatasetSHA256) {
		return Evaluation{}, fmt.Errorf("invalid ablation dataset digest")
	}
	if math.IsNaN(input.Threshold) || math.IsInf(input.Threshold, 0) || input.Threshold < 0 || input.Threshold > 1 {
		return Evaluation{}, fmt.Errorf("invalid ablation threshold")
	}
	if !input.Coordinator.Ordinary.Complete || !lane.Complete {
		if len(input.Baseline) != 0 || len(input.Ablated) != 0 {
			return Evaluation{}, fmt.Errorf("unavailable coordinator lane contains gate populations")
		}
		reason := unavailableReason(lane.UnavailableReason)
		if !input.Coordinator.Ordinary.Complete {
			reason = ReasonInterventionUnavailable
		}
		evidence := Evidence{
			ContractVersion: ContractVersion, BenchVersion: input.BenchVersion,
			Intervention: input.Intervention, Mode: input.Mode, Status: StatusUnavailable, Reason: reason,
			ProfileRevision: input.ProfileRevision, ArtifactSHA256: input.ArtifactSHA256,
			AblationProfileSHA256:   input.AblationProfileSHA256,
			CoordinatorSHA256:       input.Coordinator.CoordinatorSHA256,
			SelectedCasesSHA256:     input.Coordinator.SelectedCasesSHA256,
			ThresholdManifestSHA256: input.ThresholdManifestSHA256,
			DatasetSHA256:           input.DatasetSHA256, CaseSetSHA256: input.Coordinator.SelectedCaseSetSHA256,
			Threshold: round6(input.Threshold), AffectedCallCount: usage.affectedCalls(),
			SyntheticUsage: usage,
		}
		if input.Mode != ModeEnforce {
			evidence.AppliedFactor = 1
		}
		return digestEvidence(evidence)
	}
	baseline, err := validateAndSort(input.Baseline)
	if err != nil {
		return Evaluation{}, err
	}
	ablated, err := validateAndSort(input.Ablated)
	if err != nil {
		return Evaluation{}, err
	}
	if len(baseline) != len(ablated) {
		return Evaluation{}, fmt.Errorf("paired ablation sample size mismatch")
	}
	baselineCommitment, err := scoresSHA256(input.Baseline)
	if err != nil || baselineCommitment != input.Coordinator.Ordinary.ScoresSHA256 {
		return Evaluation{}, fmt.Errorf("baseline scores do not match coordinator commitment")
	}
	ablatedCommitment, err := scoresSHA256(input.Ablated)
	if err != nil || ablatedCommitment != lane.ScoresSHA256 {
		return Evaluation{}, fmt.Errorf("ablated scores do not match coordinator commitment")
	}
	orderedCaseIDs := make([]string, len(input.Baseline))
	for index := range input.Baseline {
		if input.Baseline[index].CaseID != input.Ablated[index].CaseID {
			return Evaluation{}, fmt.Errorf("paired ablation order does not match coordinator selection")
		}
		orderedCaseIDs[index] = input.Baseline[index].CaseID
	}
	selectionSHA256, err := selectedCasesSHA256(orderedCaseIDs)
	if err != nil {
		return Evaluation{}, err
	}
	if selectionSHA256 != input.Coordinator.SelectedCasesSHA256 {
		return Evaluation{}, fmt.Errorf("paired ablation order does not match coordinator selection digest")
	}
	caseIDs := make([]string, len(baseline))
	var baselineTotal, ablatedTotal float64
	for index := range baseline {
		if baseline[index].CaseID != ablated[index].CaseID {
			return Evaluation{}, fmt.Errorf("paired ablation case identities mismatch")
		}
		caseIDs[index] = baseline[index].CaseID
		baselineTotal += baseline[index].Score
		ablatedTotal += ablated[index].Score
	}
	caseSetSHA, err := selectedCaseSetSHA256(input.DatasetSHA256, caseIDs)
	if err != nil {
		return Evaluation{}, err
	}
	if len(caseIDs) != input.Coordinator.SelectedCaseCount || caseSetSHA != input.Coordinator.SelectedCaseSetSHA256 {
		return Evaluation{}, fmt.Errorf("paired gate population does not match coordinator selection")
	}
	baselineSHA, _, err := digestJSON(baseline)
	if err != nil {
		return Evaluation{}, err
	}
	ablatedSHA, _, err := digestJSON(ablated)
	if err != nil {
		return Evaluation{}, err
	}
	baselineMeanRaw := baselineTotal / float64(len(baseline))
	ablatedMeanRaw := ablatedTotal / float64(len(ablated))
	deltaRaw := baselineMeanRaw - ablatedMeanRaw
	baselineMean := round6(baselineMeanRaw)
	ablatedMean := round6(ablatedMeanRaw)
	delta := round6(deltaRaw)
	threshold := round6(input.Threshold)
	status, reason := StatusFailed, ReasonDeltaBelowThreshold
	affected := usage.affectedCalls()
	switch {
	case input.Mode == ModeEnforce:
		// Contract v1 does not hide the treatment from an adaptive miner. It
		// therefore cannot prove a positive counterfactual in enforce mode.
		status, reason = StatusUnavailable, ReasonEnforceProofUnavailable
	case deltaRaw >= input.Threshold:
		// A score drop is observationally ambiguous: it can mean genuine model
		// dependence, or it can be manufactured after recognizing the synthetic
		// lane. Never promote that ambiguity to a passing shadow result. The
		// committed coordinator report remains available for private research,
		// while signed evidence carries no numeric gate that could be mistaken
		// for causal proof.
		status, reason = StatusUnavailable, ReasonEnforceProofUnavailable
	}
	semanticFactor := 1.0
	if status != StatusPassed {
		semanticFactor = 0
	}
	appliedFactor := semanticFactor
	if input.Mode != ModeEnforce {
		appliedFactor = 1
	}
	evidence := Evidence{
		ContractVersion: ContractVersion, BenchVersion: input.BenchVersion,
		Intervention: input.Intervention, Mode: input.Mode, Status: status, Reason: reason,
		ProfileRevision: input.ProfileRevision, ThresholdManifestSHA256: input.ThresholdManifestSHA256,
		ArtifactSHA256:        input.ArtifactSHA256,
		AblationProfileSHA256: input.AblationProfileSHA256,
		CoordinatorSHA256:     input.Coordinator.CoordinatorSHA256,
		SelectedCasesSHA256:   input.Coordinator.SelectedCasesSHA256,
		DatasetSHA256:         input.DatasetSHA256, CaseSetSHA256: caseSetSHA,
		Threshold: threshold, SampleCount: len(baseline), AffectedCallCount: affected,
		SemanticFactor: semanticFactor, AppliedFactor: appliedFactor, SyntheticUsage: usage,
	}
	if status == StatusFailed {
		evidence.BaselineScoresSHA256 = baselineSHA
		evidence.AblatedScoresSHA256 = ablatedSHA
		evidence.BaselineMean = baselineMean
		evidence.AblatedMean = ablatedMean
		evidence.Delta = delta
	}
	return digestEvidence(evidence)
}

func digestEvidence(evidence Evidence) (Evaluation, error) {
	digest, body, err := digestJSON(evidence)
	if err != nil {
		return Evaluation{}, err
	}
	return Evaluation{Evidence: evidence, SHA256: digest, CanonicalJSON: body}, nil
}
