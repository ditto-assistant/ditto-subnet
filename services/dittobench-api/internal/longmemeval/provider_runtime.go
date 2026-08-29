package longmemeval

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/big"
	"net"
	"net/http"
	"net/url"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	maxProviderRequestBytes         = 4 << 20
	maxProviderResponseBytes        = 16 << 20
	readerRejectionProvenanceHeader = "X-Ditto-Reader-Rejection-Provenance"
	readerRejectionPreReservation   = "pre-reservation"
	officialJudgeMaxTokens          = 10
	officialJudgeModel              = "openai/gpt-4o-2024-08-06"
	officialJudgeRevision           = "longmemeval-official-gpt4o-azure-zdr-v2"
	officialJudgeRouteProvider      = "azure"
	officialJudgeReceiptProvider    = "Azure"
	platformConfirmationChatPath    = "/api/v1/inference/confirmation/chat/completions"
)

type readerHandler struct {
	session *ProviderSession
}

func (h *readerHandler) ServeHTTP(writer http.ResponseWriter, request *http.Request) {
	h.session.handleReader(writer, request)
}

// RequestAuthorizer applies trusted provider authorization to one outbound
// request. The lane is a public profile identity. The authorizer exchanges the
// validator-signed, ticket-scoped Platform capability held by the broker; no
// provider credential or cloud secret reference enters the scorer runtime.
type RequestAuthorizer interface {
	Authorize(context.Context, string, *http.Request) error
}

// ProviderLaneRuntimeConfig binds one frozen profile lane to its route and
// receipt identities. The official judge still pins OpenRouter only/order to
// RouteProvider and requires ReceiptProvider equality. The reader uses the
// scoring-lane throughput aggregate: RouteProvider is that routing identity,
// and receipts accept any non-empty OpenRouter provider rather than pinning one.
type ProviderLaneRuntimeConfig struct {
	Lane            string
	UpstreamURL     string
	RouteProvider   string
	ReceiptProvider string
	RequestTimeout  time.Duration
}

// ProviderRuntimeConfig contains no credential values. A caller normally
// builds one from a server-owned, checksum-verified execution profile.
type ProviderRuntimeConfig struct {
	Lanes      []ProviderLaneRuntimeConfig
	HTTPClient *http.Client
	Authorizer RequestAuthorizer
}

// ProviderSession is a dedicated, zero-start accounting and transport session
// for one v9 confirmation bundle. It is safe for concurrent reader requests.
// The session is both the authoritative ProviderMeter and the owner of the
// official Judge and reader relay.
type ProviderSession struct {
	ctx        context.Context
	cancel     context.CancelFunc
	client     *http.Client
	authorizer RequestAuthorizer

	mu         sync.Mutex
	closed     bool
	lanes      map[string]*providerLaneRuntime
	receiptIDs map[string]string
}

type providerLaneRuntime struct {
	policy ProviderPolicy
	config ProviderLaneRuntimeConfig

	requests           uint64
	successes          uint64
	receiptedRequests  uint64
	promptTokens       uint64
	completionTokens   uint64
	totalTokens        uint64
	costUSD            *big.Rat
	receipts           map[string]providerReceipt
	poisoned           error
	pendingRequests    uint64
	reservedPrompt     uint64
	reservedCompletion uint64
}

type providerReservation struct {
	PromptTokens     uint64
	CompletionTokens uint64
}

type providerReceipt struct {
	ID               string `json:"id"`
	Model            string `json:"model"`
	ReceiptProvider  string `json:"receipt_provider"`
	PromptTokens     uint64 `json:"prompt_tokens"`
	CompletionTokens uint64 `json:"completion_tokens"`
	TotalTokens      uint64 `json:"total_tokens"`
	CostUSD          string `json:"cost_usd"`
	HTTPStatus       int    `json:"http_status"`
}

// NewProviderSession constructs a fail-closed OpenRouter transport. It accepts
// loopback HTTP URLs for trusted sidecars and tests; every other upstream must
// use HTTPS. Redirect following is disabled so authorization cannot cross an
// unreviewed origin.
func NewProviderSession(parent context.Context, profile Profile, config ProviderRuntimeConfig) (*ProviderSession, error) {
	if parent == nil {
		return nil, errors.New("provider session context is nil")
	}
	if err := ValidateProviderRuntimeConfig(profile, config); err != nil {
		return nil, err
	}

	policies := make(map[string]ProviderPolicy, len(profile.Providers))
	for _, policy := range profile.Providers {
		policies[policy.Lane] = policy
	}
	client := &http.Client{}
	if config.HTTPClient != nil {
		*client = *config.HTTPClient
	}
	client.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	ctx, cancel := context.WithCancel(parent)
	session := &ProviderSession{
		ctx: ctx, cancel: cancel, client: client, authorizer: config.Authorizer,
		lanes: make(map[string]*providerLaneRuntime, 2), receiptIDs: make(map[string]string),
	}
	for _, laneConfig := range config.Lanes {
		policy := policies[laneConfig.Lane]
		session.lanes[laneConfig.Lane] = &providerLaneRuntime{
			policy: policy, config: laneConfig, costUSD: new(big.Rat),
			receipts: make(map[string]providerReceipt),
		}
	}
	return session, nil
}

// ValidateProviderRuntimeConfig is a side-effect-free installation check for
// the aggregate confirmation runtime factory. It performs no metadata, Secret
// Manager, provider, dataset, harness, or image I/O.
func ValidateProviderRuntimeConfig(profile Profile, config ProviderRuntimeConfig) error {
	if err := profile.Validate(); err != nil {
		return err
	}
	if isNilInterface(config.Authorizer) {
		return errors.New("provider session authorizer is nil")
	}
	if len(profile.Providers) != 2 || len(config.Lanes) != 2 {
		return errors.New("provider session requires exactly reader and judge lanes")
	}

	policies := make(map[string]ProviderPolicy, len(profile.Providers))
	for _, policy := range profile.Providers {
		policies[policy.Lane] = policy
	}
	if _, ok := policies[ReaderLane]; !ok {
		return errors.New("provider session profile lacks reader lane")
	}
	if _, ok := policies[JudgeLane]; !ok {
		return errors.New("provider session profile lacks judge lane")
	}
	seen := make(map[string]struct{}, 2)
	for _, laneConfig := range config.Lanes {
		policy, ok := policies[laneConfig.Lane]
		if !ok {
			return fmt.Errorf("provider runtime has unexpected lane %q", laneConfig.Lane)
		}
		if _, duplicate := seen[laneConfig.Lane]; duplicate {
			return fmt.Errorf("provider runtime duplicates lane %q", laneConfig.Lane)
		}
		if err := validateProviderLaneRuntime(policy, laneConfig); err != nil {
			return err
		}
		seen[laneConfig.Lane] = struct{}{}
	}
	if len(seen) != 2 {
		return errors.New("provider runtime does not cover both required lanes")
	}
	return nil
}

func isNilInterface(value any) bool {
	if value == nil {
		return true
	}
	reflected := reflect.ValueOf(value)
	switch reflected.Kind() {
	case reflect.Chan, reflect.Func, reflect.Interface, reflect.Map, reflect.Pointer, reflect.Slice:
		return reflected.IsNil()
	default:
		return false
	}
}

func validateProviderLaneRuntime(policy ProviderPolicy, config ProviderLaneRuntimeConfig) error {
	if config.Lane != policy.Lane || strings.TrimSpace(config.RouteProvider) == "" ||
		strings.TrimSpace(config.ReceiptProvider) == "" {
		return fmt.Errorf("provider runtime identity is incomplete for lane %q", policy.Lane)
	}
	if strings.ContainsAny(config.RouteProvider+config.ReceiptProvider, "\r\n\x00") {
		return fmt.Errorf("provider runtime identity is malformed for lane %q", policy.Lane)
	}
	if config.RequestTimeout <= 0 {
		return fmt.Errorf("provider runtime timeout must be positive for lane %q", policy.Lane)
	}
	parsed, err := url.Parse(config.UpstreamURL)
	if err != nil || parsed.Host == "" || parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" {
		return fmt.Errorf("provider upstream URL is invalid for lane %q", policy.Lane)
	}
	platformProxy := parsed.Path == platformConfirmationChatPath
	directProvider := parsed.Path == "/api/v1/chat/completions" || parsed.Path == "/v1/chat/completions" ||
		parsed.Path == "/chat/completions"
	if !platformProxy && !directProvider {
		return fmt.Errorf("provider upstream path is not a chat completion endpoint for lane %q", policy.Lane)
	}
	if parsed.Scheme != "https" && !(directProvider && parsed.Scheme == "http" && isLoopbackHost(parsed.Hostname())) {
		return fmt.Errorf("provider upstream transport is insecure for lane %q", policy.Lane)
	}
	// The ticket-scoped Platform route may live on a deployment-specific host.
	// Its RequestAuthorizer binds the exact URL from the signed grant before it
	// adds any bearer or request signature. Direct provider routes retain the
	// stricter OpenRouter-or-loopback origin allowlist below.
	if directProvider && !isLoopbackHost(parsed.Hostname()) && !strings.EqualFold(parsed.Hostname(), "openrouter.ai") {
		return fmt.Errorf("provider upstream host is not OpenRouter for lane %q", policy.Lane)
	}
	if policy.Provider != "openrouter" {
		return fmt.Errorf("provider transport must be OpenRouter for lane %q", policy.Lane)
	}
	if policy.Lane == ReaderLane {
		if _, err := frozenReaderCompletionBound(policy); err != nil {
			return err
		}
	}
	if policy.Lane == JudgeLane && (policy.Model != officialJudgeModel ||
		policy.ProfileRevision != officialJudgeRevision ||
		config.RouteProvider != officialJudgeRouteProvider ||
		config.ReceiptProvider != officialJudgeReceiptProvider) {
		return errors.New("judge lane does not match the pinned official OpenRouter profile")
	}
	return nil
}

func isLoopbackHost(host string) bool {
	if strings.EqualFold(host, "localhost") {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

// ReaderHandler returns the session-scoped relay installed as the submitted
// harness's trusted OpenAI-compatible reader endpoint. It exposes no health,
// stats-reset, credential, or arbitrary proxy route.
func (s *ProviderSession) ReaderHandler() http.Handler {
	return &readerHandler{session: s}
}

// IsPreReservationReaderRejection reports whether response was emitted by the
// trusted ProviderSession reader while validating the submitted request, before
// provider budget reservation or transport. The private response marker is not
// copied from upstream provider headers, and the concrete handler check prevents
// an arbitrary in-process HTTP handler from minting this provenance.
func IsPreReservationReaderRejection(handler http.Handler, response *http.Response) bool {
	if response == nil {
		return false
	}
	if _, ok := handler.(*readerHandler); !ok {
		return false
	}
	if response.StatusCode != http.StatusBadRequest && response.StatusCode != http.StatusRequestEntityTooLarge {
		return false
	}
	return response.Header.Get(readerRejectionProvenanceHeader) == readerRejectionPreReservation
}

func writePreReservationReaderRejection(writer http.ResponseWriter, status int, message string) {
	writer.Header().Set(readerRejectionProvenanceHeader, readerRejectionPreReservation)
	writeProviderError(writer, status, message)
}

func (s *ProviderSession) handleReader(writer http.ResponseWriter, request *http.Request) {
	writer.Header().Set("Cache-Control", "no-store")
	if request.Method != http.MethodPost ||
		(request.URL.Path != "/v1/chat/completions" && request.URL.Path != "/chat/completions") ||
		request.URL.RawQuery != "" {
		writeProviderError(writer, http.StatusNotFound, "not found")
		return
	}
	raw, err := io.ReadAll(io.LimitReader(request.Body, maxProviderRequestBytes+1))
	if err != nil || len(raw) == 0 || len(raw) > maxProviderRequestBytes {
		writePreReservationReaderRejection(writer, http.StatusRequestEntityTooLarge, "invalid request size")
		return
	}
	body, reservation, err := rewriteReaderRequest(raw, s.lanes[ReaderLane].policy, s.lanes[ReaderLane].config.RouteProvider)
	if err != nil {
		writePreReservationReaderRejection(writer, http.StatusBadRequest, "invalid reader request")
		return
	}
	status, response, err := s.execute(request.Context(), ReaderLane, body, reservation)
	if err != nil {
		writeProviderError(writer, http.StatusBadGateway, "reader provider unavailable")
		return
	}
	writer.Header().Set("Content-Type", "application/json")
	writer.WriteHeader(status)
	_, _ = writer.Write(response)
}

func rewriteReaderRequest(raw []byte, policy ProviderPolicy, route string) ([]byte, providerReservation, error) {
	if err := rejectDuplicateJSONFields(raw); err != nil {
		return nil, providerReservation{}, errors.New("reader request contains ambiguous JSON")
	}
	var body map[string]any
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if err := decoder.Decode(&body); err != nil || body == nil {
		return nil, providerReservation{}, errors.New("reader request must be a JSON object")
	}
	var trailing json.RawMessage
	if err := decoder.Decode(&trailing); err != io.EOF {
		return nil, providerReservation{}, errors.New("reader request contains trailing JSON")
	}
	model, ok := body["model"].(string)
	if !ok || model != policy.Model {
		return nil, providerReservation{}, errors.New("reader request model does not match the frozen profile")
	}
	if stream, exists := body["stream"]; exists {
		value, ok := stream.(bool)
		if !ok || value {
			return nil, providerReservation{}, errors.New("streaming is not supported")
		}
	}
	completionLimit, err := frozenReaderCompletionBound(policy)
	if err != nil {
		return nil, providerReservation{}, err
	}
	completion, explicit, err := requestedCompletionTokens(body)
	if err != nil {
		return nil, providerReservation{}, err
	}
	if !explicit {
		completion = completionLimit
		body["max_tokens"] = json.Number(strconv.FormatUint(completion, 10))
	}
	if completion > completionLimit {
		return nil, providerReservation{}, errors.New("reader request completion bound exceeds the frozen per-request limit")
	}
	if count, exists := body["n"]; exists {
		value, err := requestUint(count)
		if err != nil || value != 1 {
			return nil, providerReservation{}, errors.New("reader request must ask for exactly one completion")
		}
	}
	body["model"] = policy.Model
	body["stream"] = false
	delete(body, "models")
	_ = route
	// Match the scoring LLM relay: OpenRouter throughput aggregate, not a
	// single-vendor pin. The Platform confirmation proxy echoes this dict
	// and adds zdr=true. CoreWeave is ignored because its reviewed route is 4-bit.
	body["provider"] = map[string]any{
		"sort":            "throughput",
		"ignore":          []string{"coreweave"},
		"allow_fallbacks": false,
		"data_collection": "deny",
	}
	encoded, err := json.Marshal(body)
	if err != nil {
		return nil, providerReservation{}, err
	}
	return encoded, providerReservation{PromptTokens: uint64(len(encoded)), CompletionTokens: completion}, nil
}

func frozenReaderCompletionBound(policy ProviderPolicy) (uint64, error) {
	if policy.MaxRequests == 0 {
		return 0, errors.New("reader provider policy has no request budget")
	}
	bound := policy.MaxCompletionTokens / policy.MaxRequests
	if bound == 0 {
		return 0, errors.New("reader provider policy has no usable per-request completion budget")
	}
	return bound, nil
}

func requestedCompletionTokens(body map[string]any) (uint64, bool, error) {
	var value uint64
	found := false
	for _, field := range []string{"max_tokens", "max_completion_tokens"} {
		raw, exists := body[field]
		if !exists {
			continue
		}
		parsed, err := requestUint(raw)
		if err != nil || parsed == 0 {
			return 0, false, errors.New("reader request completion bound must be a positive integer")
		}
		if found && parsed != value {
			return 0, false, errors.New("reader request has contradictory completion bounds")
		}
		value = parsed
		found = true
	}
	return value, found, nil
}

func requestUint(value any) (uint64, error) {
	number, ok := value.(json.Number)
	if !ok {
		return 0, errors.New("request bound is not a JSON number")
	}
	return parseReceiptUint(json.RawMessage(number.String()))
}

// Judge returns the pinned, official LongMemEval evaluator behavior backed by
// this session's dedicated judge lane.
func (s *ProviderSession) Judge() Judge {
	return officialProviderJudge{session: s}
}

type officialProviderJudge struct {
	session *ProviderSession
}

func (j officialProviderJudge) Judge(ctx context.Context, input JudgeInput) (bool, error) {
	if j.session == nil {
		return false, errors.New("official LongMemEval judge is unconfigured")
	}
	prompt, err := officialJudgePrompt(input)
	if err != nil {
		return false, err
	}
	lane := j.session.lanes[JudgeLane]
	body, err := json.Marshal(map[string]any{
		"model":                 lane.policy.Model,
		"messages":              []map[string]string{{"role": "user", "content": prompt}},
		"n":                     1,
		"temperature":           0,
		"max_completion_tokens": officialJudgeMaxTokens,
		"stream":                false,
		"provider": map[string]any{
			"only":               []string{lane.config.RouteProvider},
			"order":              []string{lane.config.RouteProvider},
			"allow_fallbacks":    false,
			"require_parameters": true,
			"data_collection":    "deny",
		},
	})
	if err != nil {
		return false, errors.New("encode official LongMemEval judge request")
	}
	status, raw, err := j.session.execute(ctx, JudgeLane, body, providerReservation{
		PromptTokens: uint64(len(body)), CompletionTokens: officialJudgeMaxTokens,
	})
	if err != nil {
		return false, errors.New("official LongMemEval judge provider unavailable")
	}
	if status < 200 || status >= 300 {
		return false, fmt.Errorf("official LongMemEval judge returned HTTP %d", status)
	}
	var response struct {
		Choices []struct {
			Message struct {
				Content string `json:"content"`
			} `json:"message"`
		} `json:"choices"`
	}
	if err := json.Unmarshal(raw, &response); err != nil || len(response.Choices) != 1 ||
		strings.TrimSpace(response.Choices[0].Message.Content) == "" {
		return false, errors.New("official LongMemEval judge response is malformed")
	}
	// Match the pinned official evaluator at LongMemEval revision
	// 9e0b455f4ef0e2ab8f2e582289761153549043fc: its label is true when the
	// normalized completion contains "yes". This intentionally accepts the
	// punctuation emitted by the frozen Azure GPT-4o endpoint.
	return strings.Contains(
		strings.ToLower(strings.TrimSpace(response.Choices[0].Message.Content)),
		"yes",
	), nil
}

func officialJudgePrompt(input JudgeInput) (string, error) {
	reference := input.Reference
	if strings.TrimSpace(reference.Question) == "" || strings.TrimSpace(reference.QuestionType) == "" {
		return "", errors.New("official LongMemEval judge input is incomplete")
	}
	const ordinary = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no."
	var prefix, answerLabel, suffix string
	if strings.Contains(reference.QuestionID, "_abs") {
		prefix = "I will give you an unanswerable question, an explanation, and a response from a model. Please answer yes if the model correctly identifies the question as unanswerable. The model could say that the information is incomplete, or some other information is given but the asked information is not."
		answerLabel = "Explanation"
		suffix = "Does the model correctly identify the question as unanswerable? Answer yes or no only."
	} else {
		answerLabel = "Correct Answer"
		suffix = "Is the model response correct? Answer yes or no only."
		switch reference.QuestionType {
		case "single-session-user", "single-session-assistant", "multi-session":
			prefix = ordinary
		case "temporal-reasoning":
			prefix = ordinary + " In addition, do not penalize off-by-one errors for the number of days.\nIf the question asks for the number of days/weeks/months, etc., and the model makes off-by-one errors (e.g., predicting 19 days when the answer is 18), the model's response is still correct. "
		case "knowledge-update":
			prefix = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response contains some previous information along with an updated answer, the response should be considered as correct as long as the updated answer is the required answer."
		case "single-session-preference":
			prefix = "I will give you a question, a rubric for desired personalized response, and a response from a model. Please answer yes if the response satisfies the desired response. Otherwise, answer no. The model does not need to reflect all the points in the rubric. The response is correct as long as it recalls and utilizes the user's personal information correctly."
			answerLabel = "Rubric"
		default:
			return "", fmt.Errorf("official LongMemEval judge does not support question type %q", reference.QuestionType)
		}
	}
	return fmt.Sprintf("%s\n\nQuestion: %s\n\n%s: %s\n\nModel Response: %s\n\n%s",
		prefix, reference.Question, answerLabel, reference.Answer, input.Hypothesis, suffix), nil
}

func (s *ProviderSession) execute(
	caller context.Context,
	laneName string,
	body []byte,
	reservation providerReservation,
) (int, []byte, error) {
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return 0, nil, errors.New("provider session is closed")
	}
	if s.ctx.Err() != nil {
		s.mu.Unlock()
		return 0, nil, errors.New("provider session context is no longer live")
	}
	lane, ok := s.lanes[laneName]
	if !ok {
		s.mu.Unlock()
		return 0, nil, errors.New("provider lane is unavailable")
	}
	if lane.poisoned != nil {
		s.mu.Unlock()
		return 0, nil, errors.New("provider lane accounting is unavailable")
	}
	if err := reserveProviderBudget(lane, reservation); err != nil {
		s.mu.Unlock()
		return 0, nil, err
	}
	s.mu.Unlock()

	ctx, cancel := context.WithCancel(caller)
	stop := context.AfterFunc(s.ctx, cancel)
	defer func() {
		stop()
		cancel()
	}()
	ctx, timeoutCancel := context.WithTimeout(ctx, lane.config.RequestTimeout)
	defer timeoutCancel()
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, lane.config.UpstreamURL, bytes.NewReader(body))
	if err != nil {
		return 0, nil, errors.New("build provider request")
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Accept", "application/json")
	request.Header["User-Agent"] = []string{}
	if err := s.authorizer.Authorize(ctx, lane.config.Lane, request); err != nil {
		request.Header.Del("Authorization")
		s.releaseReservation(laneName, reservation, true)
		return 0, nil, errors.New("provider authorization unavailable")
	}
	defer request.Header.Del("Authorization")

	s.mu.Lock()
	if s.closed || lane.poisoned != nil || ctx.Err() != nil {
		releaseProviderReservation(lane, reservation, true)
		s.mu.Unlock()
		return 0, nil, errors.New("provider lane is unavailable")
	}
	lane.pendingRequests--
	lane.requests++
	s.mu.Unlock()

	response, err := s.client.Do(request)
	request.Header.Del("Authorization")
	if err != nil {
		s.poisonAndRelease(laneName, reservation, errors.New("provider request lacks an authoritative receipt"))
		return 0, nil, errors.New("provider request failed")
	}
	defer response.Body.Close()
	raw, readErr := io.ReadAll(io.LimitReader(response.Body, maxProviderResponseBytes+1))
	if readErr != nil || len(raw) == 0 || len(raw) > maxProviderResponseBytes {
		s.poisonAndRelease(laneName, reservation, errors.New("provider response lacks a readable authoritative receipt"))
		return 0, nil, errors.New("provider response is unreadable")
	}
	receipt, err := decodeProviderReceipt(raw, response.StatusCode, lane.policy, lane.config)
	if err != nil {
		s.poisonAndRelease(laneName, reservation, err)
		return 0, nil, errors.New("provider response lacks a valid authoritative receipt")
	}
	if err := s.recordReceipt(laneName, receipt, reservation, response.StatusCode >= 200 && response.StatusCode < 300); err != nil {
		return 0, nil, errors.New("provider receipt could not be recorded")
	}
	return response.StatusCode, raw, nil
}

func reserveProviderBudget(lane *providerLaneRuntime, reservation providerReservation) error {
	if reservation.PromptTokens == 0 || reservation.CompletionTokens == 0 {
		return errors.New("provider request reservation is incomplete")
	}
	requests, ok := addUint64(lane.requests, lane.pendingRequests)
	if !ok || requests >= lane.policy.MaxRequests {
		return errors.New("provider request cap is exhausted")
	}
	promptReserved, ok := addUint64(lane.reservedPrompt, reservation.PromptTokens)
	if !ok {
		return errors.New("provider prompt-token reservation overflow")
	}
	promptUpper, ok := addUint64(lane.promptTokens, promptReserved)
	if !ok || promptUpper > lane.policy.MaxPromptTokens {
		return errors.New("provider request exceeds remaining prompt-token budget")
	}
	completionReserved, ok := addUint64(lane.reservedCompletion, reservation.CompletionTokens)
	if !ok {
		return errors.New("provider completion-token reservation overflow")
	}
	completionUpper, ok := addUint64(lane.completionTokens, completionReserved)
	if !ok || completionUpper > lane.policy.MaxCompletionTokens {
		return errors.New("provider request exceeds remaining completion-token budget")
	}
	reservedTotal, ok := addUint64(promptReserved, completionReserved)
	if !ok {
		return errors.New("provider total-token reservation overflow")
	}
	totalUpper, ok := addUint64(lane.totalTokens, reservedTotal)
	if !ok || totalUpper > lane.policy.MaxTotalTokens {
		return errors.New("provider request exceeds remaining total-token budget")
	}
	lane.pendingRequests++
	lane.reservedPrompt = promptReserved
	lane.reservedCompletion = completionReserved
	return nil
}

func releaseProviderReservation(lane *providerLaneRuntime, reservation providerReservation, pending bool) {
	if pending && lane.pendingRequests > 0 {
		lane.pendingRequests--
	}
	if lane.reservedPrompt >= reservation.PromptTokens {
		lane.reservedPrompt -= reservation.PromptTokens
	}
	if lane.reservedCompletion >= reservation.CompletionTokens {
		lane.reservedCompletion -= reservation.CompletionTokens
	}
}

func (s *ProviderSession) releaseReservation(laneName string, reservation providerReservation, pending bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if lane := s.lanes[laneName]; lane != nil {
		releaseProviderReservation(lane, reservation, pending)
	}
}

func decodeProviderReceipt(raw []byte, status int, policy ProviderPolicy, config ProviderLaneRuntimeConfig) (providerReceipt, error) {
	if err := rejectDuplicateJSONFields(raw); err != nil {
		return providerReceipt{}, errors.New("provider response contains ambiguous JSON")
	}
	var wire struct {
		ID       string `json:"id"`
		Model    string `json:"model"`
		Provider string `json:"provider"`
		Usage    *struct {
			PromptTokens     json.RawMessage `json:"prompt_tokens"`
			CompletionTokens json.RawMessage `json:"completion_tokens"`
			TotalTokens      json.RawMessage `json:"total_tokens"`
			Cost             json.RawMessage `json:"cost"`
		} `json:"usage"`
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if err := decoder.Decode(&wire); err != nil || strings.TrimSpace(wire.ID) == "" || wire.Usage == nil {
		return providerReceipt{}, errors.New("provider receipt identity or usage is missing")
	}
	if wire.Model != policy.Model || strings.TrimSpace(wire.Provider) == "" {
		return providerReceipt{}, errors.New("provider receipt identity drift")
	}
	if policy.Lane == JudgeLane && wire.Provider != config.ReceiptProvider {
		return providerReceipt{}, errors.New("provider receipt identity drift")
	}
	prompt, err := parseReceiptUint(wire.Usage.PromptTokens)
	if err != nil {
		return providerReceipt{}, errors.New("provider prompt-token receipt is invalid")
	}
	completion, err := parseReceiptUint(wire.Usage.CompletionTokens)
	if err != nil {
		return providerReceipt{}, errors.New("provider completion-token receipt is invalid")
	}
	total, err := parseReceiptUint(wire.Usage.TotalTokens)
	expectedTotal, totalOK := addUint64(prompt, completion)
	if err != nil || !totalOK || total != expectedTotal {
		return providerReceipt{}, errors.New("provider total-token receipt is invalid")
	}
	costValue, err := parseReceiptNumber(wire.Usage.Cost)
	if err != nil {
		return providerReceipt{}, errors.New("provider cost receipt is invalid")
	}
	cost, ok := new(big.Rat).SetString(costValue)
	if !ok || cost.Sign() < 0 {
		return providerReceipt{}, errors.New("provider cost receipt is invalid")
	}
	return providerReceipt{
		ID: wire.ID, Model: wire.Model, ReceiptProvider: wire.Provider,
		PromptTokens: prompt, CompletionTokens: completion, TotalTokens: total,
		CostUSD: cost.RatString(), HTTPStatus: status,
	}, nil
}

func parseReceiptUint(raw json.RawMessage) (uint64, error) {
	value, err := parseReceiptNumber(raw)
	if err != nil || strings.ContainsAny(value, ".eE+-") {
		return 0, errors.New("receipt counter is not an unsigned integer")
	}
	var parsed uint64
	if _, err := fmt.Sscanf(value, "%d", &parsed); err != nil {
		return 0, err
	}
	return parsed, nil
}

func parseReceiptNumber(raw json.RawMessage) (string, error) {
	if len(raw) == 0 || raw[0] == '"' || bytes.Equal(raw, []byte("null")) {
		return "", errors.New("receipt value is not a JSON number")
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var value json.Number
	if err := decoder.Decode(&value); err != nil {
		return "", err
	}
	if err := decoder.Decode(&struct{}{}); err != io.EOF {
		return "", errors.New("receipt number contains trailing JSON")
	}
	return value.String(), nil
}

func (s *ProviderSession) poisonAndRelease(laneName string, reservation providerReservation, cause error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if lane := s.lanes[laneName]; lane != nil {
		releaseProviderReservation(lane, reservation, false)
		if lane.poisoned == nil {
			lane.poisoned = cause
		}
	}
}

func (s *ProviderSession) recordReceipt(
	laneName string,
	receipt providerReceipt,
	reservation providerReservation,
	success bool,
) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	lane := s.lanes[laneName]
	if lane == nil {
		return errors.New("provider lane is unavailable")
	}
	releaseProviderReservation(lane, reservation, false)
	if lane.poisoned != nil {
		return errors.New("provider lane is unavailable")
	}
	reservedTotal, reservedOK := addUint64(reservation.PromptTokens, reservation.CompletionTokens)
	if !reservedOK || receipt.PromptTokens > reservation.PromptTokens ||
		receipt.CompletionTokens > reservation.CompletionTokens || receipt.TotalTokens > reservedTotal {
		lane.poisoned = errors.New("provider receipt exceeds the request's reserved token bounds")
		return lane.poisoned
	}
	if _, duplicate := lane.receipts[receipt.ID]; duplicate {
		lane.poisoned = errors.New("provider returned a duplicate receipt identity")
		return lane.poisoned
	}
	if previousLane, duplicate := s.receiptIDs[receipt.ID]; duplicate {
		lane.poisoned = fmt.Errorf("provider receipt identity was already used by lane %q", previousLane)
		return lane.poisoned
	}
	prompt, ok := addUint64(lane.promptTokens, receipt.PromptTokens)
	if !ok {
		lane.poisoned = errors.New("provider prompt-token accounting overflow")
		return lane.poisoned
	}
	completion, ok := addUint64(lane.completionTokens, receipt.CompletionTokens)
	if !ok {
		lane.poisoned = errors.New("provider completion-token accounting overflow")
		return lane.poisoned
	}
	total, ok := addUint64(lane.totalTokens, receipt.TotalTokens)
	if !ok {
		lane.poisoned = errors.New("provider total-token accounting overflow")
		return lane.poisoned
	}
	cost, ok := new(big.Rat).SetString(receipt.CostUSD)
	if !ok {
		lane.poisoned = errors.New("provider cost accounting is invalid")
		return lane.poisoned
	}
	lane.receipts[receipt.ID] = receipt
	s.receiptIDs[receipt.ID] = laneName
	lane.receiptedRequests++
	if success {
		lane.successes++
	}
	lane.promptTokens = prompt
	lane.completionTokens = completion
	lane.totalTokens = total
	lane.costUSD.Add(lane.costUSD, cost)
	costMicros, ok := costMicrosCeil(lane.costUSD)
	if !ok || lane.requests > lane.policy.MaxRequests ||
		lane.promptTokens > lane.policy.MaxPromptTokens || lane.completionTokens > lane.policy.MaxCompletionTokens ||
		lane.totalTokens > lane.policy.MaxTotalTokens || costMicros > lane.policy.MaxCostUSDmicros {
		lane.poisoned = errors.New("provider lane exceeded its frozen accounting budget")
		return lane.poisoned
	}
	return nil
}

func costMicrosCeil(cost *big.Rat) (uint64, bool) {
	if cost == nil || cost.Sign() < 0 {
		return 0, false
	}
	scaled := new(big.Rat).Mul(cost, big.NewRat(1_000_000, 1))
	quotient := new(big.Int).Quo(scaled.Num(), scaled.Denom())
	if new(big.Int).Rem(scaled.Num(), scaled.Denom()).Sign() != 0 {
		quotient.Add(quotient, big.NewInt(1))
	}
	if !quotient.IsUint64() {
		return 0, false
	}
	return quotient.Uint64(), true
}

// Snapshot implements ProviderMeter. Any dispatched request without a complete
// authoritative receipt permanently poisons its lane and makes evidence
// construction impossible.
func (s *ProviderSession) Snapshot(ctx context.Context) ([]ProviderEvidence, error) {
	if ctx == nil {
		return nil, errors.New("provider meter context is nil")
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	lanes := []string{JudgeLane, ReaderLane}
	result := make([]ProviderEvidence, 0, len(lanes))
	for _, laneName := range lanes {
		lane := s.lanes[laneName]
		if lane == nil {
			return nil, errors.New("provider meter lane coverage is incomplete")
		}
		if lane.poisoned != nil || lane.receiptedRequests != lane.requests {
			return nil, fmt.Errorf("provider lane %q lacks complete authoritative receipts", laneName)
		}
		costMicros, ok := costMicrosCeil(lane.costUSD)
		if !ok {
			return nil, fmt.Errorf("provider lane %q cost cannot be represented", laneName)
		}
		evidence := ProviderEvidence{
			Lane: laneName, CostSource: AuthoritativeCostV1, Currency: "USD",
			Provider: lane.policy.Provider, ProfileRevision: lane.policy.ProfileRevision,
			Model: lane.policy.Model, FallbackUsed: false,
			Requests: lane.requests, Successes: lane.successes, ReceiptedRequests: lane.receiptedRequests,
			PromptTokens: lane.promptTokens, CompletionTokens: lane.completionTokens,
			TotalTokens: lane.totalTokens, CostUSDmicros: costMicros,
		}
		if lane.requests > 0 {
			evidence.ReceiptSetSHA256 = receiptSetDigest(laneName, lane.receipts)
		}
		result = append(result, evidence)
	}
	return result, nil
}

func receiptSetDigest(lane string, receipts map[string]providerReceipt) string {
	values := make([]providerReceipt, 0, len(receipts))
	for _, receipt := range receipts {
		values = append(values, receipt)
	}
	sort.Slice(values, func(i, j int) bool { return values[i].ID < values[j].ID })
	raw, _ := json.Marshal(struct {
		Domain   string            `json:"domain"`
		Lane     string            `json:"lane"`
		Receipts []providerReceipt `json:"receipts"`
	}{Domain: "ditto-v9-longmemeval-provider-receipts-v1", Lane: lane, Receipts: values})
	digest := sha256.Sum256(raw)
	return hex.EncodeToString(digest[:])
}

// Close cancels all in-flight calls and prevents later provider spend. It is
// idempotent and intentionally does not erase the final in-memory receipt set.
func (s *ProviderSession) Close() error {
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return nil
	}
	s.closed = true
	s.mu.Unlock()
	s.cancel()
	return nil
}

func writeProviderError(writer http.ResponseWriter, status int, message string) {
	writer.Header().Set("Content-Type", "application/json")
	writer.Header().Set("Cache-Control", "no-store")
	writer.WriteHeader(status)
	_ = json.NewEncoder(writer).Encode(map[string]string{"error": message})
}
