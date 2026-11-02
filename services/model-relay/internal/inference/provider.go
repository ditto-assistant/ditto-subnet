package inference

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"math"
	"math/rand/v2"
	"net"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/ditto-assistant/model-relay/internal/config"
)

const (
	providerMaxAttempts          = 1
	providerSafeRetryMaxAttempts = 2
	providerGatewayRetryDelay    = 100 * time.Millisecond
	providerRetryAfterMaxSeconds = 5
	// Every confirmation lane gets one re-dispatch of a provably receipt-free
	// provider rejection. A bundle makes ~144 serial provider calls with
	// allow_fallbacks false, so a single uncovered 429 ends a 48-case run.
	confirmationBackpressureMaxAttempts  = 2
	confirmationBackpressureMaxElapsed   = 80 * time.Second
	confirmationRetryAfterMaxSeconds     = 60
	confirmationRetryAfterDefaultSeconds = 10

	// Transient-failure retry for confirmation: a provably receipt-free 5xx,
	// or a connection that never opened. Measured 2026-10-29: about 1 reader
	// call in 120 failed before OpenRouter selected a provider, and with no
	// retry one failure ends a ~281-call bundle (~10% of bundles complete).
	// Four attempts take that to ~99.9999% if failures are independent. The
	// backoff is exponential with full jitter -- a uniform wait in
	// [0, base*2^(n-1)] -- so concurrent bundles spread out instead of
	// re-dispatching in step, and the cumulative ceiling (2+4+8 = 14s) rides
	// over a short outage without holding a slot past the elapsed cap.
	// Whether 14s is long enough is an empirical question: it is the share of
	// failures that outlast the window, which confirmation_inference_requests
	// now records. Timeouts are deliberately NOT retried: a request that timed
	// out may still complete and be billed upstream.
	confirmationTransientMaxAttempts = 4
	confirmationTransientBaseDelay   = 2 * time.Second
	confirmationTransientMaxDelay    = 16 * time.Second
	// confirmationGrantRetryBudget bounds the retries one grant (one lane of one
	// bundle attempt) may spend across all its calls. A bundle averages 2.3
	// failing reader calls, so on independent blips the budget is reached less
	// than once in a million bundles; it exists for the degraded upstream that
	// fails often but not always, where per-call caps alone would let a single
	// bundle multiply its load several-fold for its whole run.
	confirmationGrantRetryBudget = 12

	pplxEmbedContractModel = "perplexity/pplx-embed-v1-0.6b"
	pplxEmbedResponseModel = "pplx-embed-v1-0.6b"

	providerGenerationInvalidCode = "provider_generation_invalid"
	minerRecoverableFailureHeader = "X-Ditto-Inference-Failure-Class"
	minerRecoverableGeneration    = "miner_recoverable_generation"
)

func isProviderRetryStatus(status int) bool {
	switch status {
	case 408, 429, 500, 502, 503, 504:
		return true
	}
	return false
}

// sleepFunc is injectable for tests; the default respects context
// cancellation.
type sleepFunc func(ctx context.Context, d time.Duration)

func defaultSleep(ctx context.Context, d time.Duration) {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
	case <-timer.C:
	}
}

// providerHTTPResult is the buffered outcome of one logical provider request
// (the httpx.Response analog: the entire body is read before returning).
type providerHTTPResult struct {
	status        int
	header        http.Header
	body          []byte
	bodyOverLimit bool
	attempts      int
}

// providerCallError mirrors _ProviderCallError.
type providerCallError struct {
	attempts int
	timedOut bool
	// neverSent is true only when the final failure was a dial error: the
	// connection never opened, so the request provably never reached the
	// gateway. Every other transport failure may have been received and acted
	// on, including a timeout, so it is not evidence that nothing was billed.
	neverSent bool
}

func (e *providerCallError) Error() string { return "provider request failed" }

type providerRetryPolicy struct {
	retryBackpressure             bool
	retryPreProviderNotFoundModel string
	backpressureMaxAttempts       int
	receiptFreeExpectedModel      string
	maxElapsed                    time.Duration
	// retryTransient re-dispatches a receipt-free 5xx and a dial failure, up to
	// transientMaxAttempts, with jittered exponential backoff. It never relaxes
	// the receipt-free proof, and never retries a timeout or a reset after the
	// request was written, where the provider may already be doing work.
	retryTransient       bool
	transientMaxAttempts int
	// jitter returns a duration in [0, ceiling]. Nil means uniform full jitter;
	// tests inject a deterministic one.
	jitter func(ceiling time.Duration) time.Duration
}

// providerIsTransientServerError reports a gateway or upstream server failure
// that is worth re-dispatching once proven receipt-free. A 503 that carries
// Retry-After is backpressure and is handled by that path instead; a 501 or
// 505 is a permanent protocol fault, not a transient one.
func providerIsTransientServerError(status int, header http.Header) bool {
	switch status {
	case 500, 502, 504:
		return true
	case 503:
		return !providerIsBackpressure(status, header)
	}
	return false
}

// confirmationTransientErrorEnvelope reports whether a 2xx response is
// OpenRouter's documented in-body failure -- an error object with no choices,
// usage or provider -- carrying a transient error code, and returns that code.
//
// OpenRouter sends 200 as soon as a provider accepts a request, before any
// token is produced, and a status is final once sent; a failure after that
// point (including every provider failing over) arrives as a 200 whose JSON
// body holds only {"id","error":{"code","message","metadata"}}. Measured
// 2026-10-30 on bundle 7fa29ad3: the confirmation path, which had no such
// check, recorded this as identity_mismatch (no model field) and ended a
// bundle 28 of 48 cases in. The ordinary scoring lane already classifies the
// same envelope as provider_unavailable and moves on to its next attempt.
//
// This is NOT a receipt-free proof. OpenRouter documents that the upstream may
// still charge for prompt processing even when no content is generated, and the
// envelope reports no usage to record. Re-dispatching it trades that bounded,
// unrecorded prompt cost for completing the bundle -- the same trade the
// ordinary lane already makes -- and is capped by the per-call attempt limit
// and the per-grant retry budget like every other confirmation retry. A
// permanent code (a 4xx other than 408/429) is never retried.
func confirmationTransientErrorEnvelope(result *providerHTTPResult) (int64, bool) {
	if result == nil || result.status < 200 || result.status > 299 || result.bodyOverLimit {
		return 0, false
	}
	decoded, ok := decodeJSONNumbersRejectDuplicateKeys(result.body)
	if !ok {
		return 0, false
	}
	payload, ok := decoded.(map[string]any)
	if !ok {
		return 0, false
	}
	code, _, ok := providerErrorEnvelope(payload)
	if !ok {
		return 0, false
	}
	switch code {
	case 408, 429, 500, 502, 503, 504:
		return code, true
	}
	return code, false
}

// confirmationTransientDelay is the full-jitter backoff before attempt n+1,
// given n attempts so far: uniform in [0, min(max, base*2^(n-1))].
func confirmationTransientDelay(policy providerRetryPolicy, attempts int) time.Duration {
	ceiling := confirmationTransientBaseDelay
	for i := 1; i < attempts && ceiling < confirmationTransientMaxDelay; i++ {
		ceiling *= 2
	}
	if ceiling > confirmationTransientMaxDelay {
		ceiling = confirmationTransientMaxDelay
	}
	if policy.jitter != nil {
		delay := policy.jitter(ceiling)
		if delay < 0 {
			return 0
		}
		if delay > ceiling {
			return ceiling
		}
		return delay
	}
	return time.Duration(rand.Int64N(int64(ceiling) + 1))
}

// providerRetryAfterSeconds mirrors _provider_retry_after_seconds: a bounded
// backoff hint, clamp(int(Retry-After), 1, 5), non-integer -> 1.
func providerRetryAfterSeconds(header http.Header) int {
	raw := ""
	if header != nil {
		raw = header.Get("Retry-After")
	}
	seconds, err := strconv.Atoi(trimSpace(raw))
	if err != nil {
		return 1
	}
	if seconds < 1 {
		return 1
	}
	if seconds > providerRetryAfterMaxSeconds {
		return providerRetryAfterMaxSeconds
	}
	return seconds
}

func trimSpace(s string) string {
	start, end := 0, len(s)
	for start < end && (s[start] == ' ' || s[start] == '\t') {
		start++
	}
	for end > start && (s[end-1] == ' ' || s[end-1] == '\t') {
		end--
	}
	return s[start:end]
}

// providerIsBackpressure mirrors _provider_is_backpressure: 429, or 503 with
// a non-empty Retry-After.
func providerIsBackpressure(status int, header http.Header) bool {
	if status == 429 {
		return true
	}
	if status != 503 || header == nil {
		return false
	}
	return trimSpace(header.Get("Retry-After")) != ""
}

// classifyTransportError buckets a Do/read error into the httpx taxonomy:
// dial (ConnectError/ConnectTimeout — retryable) vs non-dial timeout
// (fail immediately, timed_out) vs other transport failure.
func classifyTransportError(err error) (dial bool, timeout bool) {
	var opErr *net.OpError
	if errors.As(err, &opErr) && opErr.Op == "dial" {
		return true, opErr.Timeout()
	}
	if errors.Is(err, context.DeadlineExceeded) {
		return false, true
	}
	var netErr net.Error
	if errors.As(err, &netErr) && netErr.Timeout() {
		return false, true
	}
	return false, false
}

// postProviderWithRetry retains the rolling wire name but performs exactly one
// provider request. A failed scored request is parked for an operator retry.
func postProviderWithRetry(ctx context.Context, client *http.Client, url string, payload any,
	headers map[string]string, maxBody int64, timeoutSeconds int, retryBackpressure bool,
	retryPreProviderNotFoundModel string, sleep sleepFunc) (*providerHTTPResult, *providerCallError) {
	return postProviderWithRetryPolicy(ctx, client, url, payload, headers, maxBody, timeoutSeconds,
		providerRetryPolicy{
			retryBackpressure:             retryBackpressure,
			retryPreProviderNotFoundModel: retryPreProviderNotFoundModel,
			backpressureMaxAttempts:       providerMaxAttempts,
		}, sleep)
}

// postProviderWithRetryPolicy performs one provider request and, when the
// policy allows it, re-dispatches the identical frozen request for a provider
// rejection that is provably receipt-free.
//
// A retry here is an accounting boundary, not a convenience. The request may
// only be sent again when the response proves that no provider endpoint was
// selected and no receipt was issued, so the retry cannot duplicate provider
// work or billing. Transport failures and timeouts are never retried: an
// ambiguous outcome may already have been charged. A caller that leaves
// backpressureMaxAttempts at providerMaxAttempts stays single-shot.
func postProviderWithRetryPolicy(ctx context.Context, client *http.Client, url string, payload any,
	headers map[string]string, maxBody int64, timeoutSeconds int, policy providerRetryPolicy,
	sleep sleepFunc) (*providerHTTPResult, *providerCallError) {

	encoded, err := json.Marshal(payload)
	if err != nil {
		return nil, &providerCallError{attempts: 1, timedOut: false}
	}
	maxAttempts := policy.backpressureMaxAttempts
	if maxAttempts < 1 {
		maxAttempts = 1
	}
	started := time.Now()
	attempts := 0
	var last *providerHTTPResult
	for {
		if ctx.Err() != nil {
			// A rejection already in hand is the truthful outcome. Discarding it
			// for a transport error would convert a clean provider 429 into an
			// internal server error and lose the settlement's real cause.
			if last != nil {
				return last, nil
			}
			failed := attempts
			if failed < 1 {
				failed = 1
			}
			return nil, &providerCallError{attempts: failed, timedOut: true}
		}
		result, requestErr := postOnce(ctx, client, url, encoded, headers, maxBody, timeoutSeconds)
		attempts++
		if requestErr != nil {
			dial, timedOut := classifyTransportError(requestErr)
			// A dial failure means the connection never opened, so no request
			// reached the gateway and nothing can have been billed. Any other
			// transport failure -- a timeout, a reset after the body was written
			// -- may have been received and acted on, so it is final.
			if dial && policy.retryTransient && attempts < policy.transientMaxAttempts {
				delay := confirmationTransientDelay(policy, attempts)
				if policy.maxElapsed <= 0 || time.Since(started)+delay <= policy.maxElapsed {
					sleep(ctx, delay)
					continue
				}
			}
			return nil, &providerCallError{attempts: attempts, timedOut: timedOut, neverSent: dial}
		}
		result.attempts = attempts
		last = result
		delay, retry := providerRetryDelay(policy, result, attempts, maxAttempts)
		if !retry {
			return result, nil
		}
		if policy.maxElapsed > 0 && time.Since(started)+delay > policy.maxElapsed {
			return result, nil
		}
		sleep(ctx, delay)
	}
}

// providerRetryDelay decides whether a received response may be re-dispatched
// and how long to wait first. Both paths require the unconditional receipt-free
// proof -- positive evidence that no provider endpoint was selected -- because
// the cost of being wrong is a duplicated provider charge against a frozen grant.
func providerRetryDelay(policy providerRetryPolicy, result *providerHTTPResult, attempts, backpressureMax int) (time.Duration, bool) {
	backpressure := policy.retryBackpressure && attempts < backpressureMax &&
		providerIsBackpressure(result.status, result.header)
	transient := policy.retryTransient && attempts < policy.transientMaxAttempts &&
		providerIsTransientServerError(result.status, result.header)
	if policy.retryTransient && attempts < policy.transientMaxAttempts {
		// The documented 200-with-error-body failure. Deliberately outside the
		// receipt-free proof below: see confirmationTransientErrorEnvelope.
		if _, envelope := confirmationTransientErrorEnvelope(result); envelope {
			return confirmationTransientDelay(policy, attempts), true
		}
	}
	if !backpressure && !transient {
		return 0, false
	}
	if !providerFailureIsReceiptFree(result, policy.receiptFreeExpectedModel) {
		return 0, false
	}
	if backpressure {
		return confirmationBackpressureDelay(result.header), true
	}
	return confirmationTransientDelay(policy, attempts), true
}

func confirmationBackpressureDelay(header http.Header) time.Duration {
	values := header.Values("Retry-After")
	raw := ""
	if len(values) == 1 {
		raw = values[0]
	}
	trimmed := strings.TrimSpace(raw)
	asciiDigits := trimmed != ""
	for i := 0; i < len(trimmed); i++ {
		if trimmed[i] < '0' || trimmed[i] > '9' {
			asciiDigits = false
			break
		}
	}
	seconds, err := strconv.ParseUint(trimmed, 10, 64)
	if !asciiDigits {
		err = strconv.ErrSyntax
	}
	if errors.Is(err, strconv.ErrRange) {
		seconds = confirmationRetryAfterMaxSeconds
	} else if err != nil || seconds == 0 {
		seconds = confirmationRetryAfterDefaultSeconds
	}
	if seconds > confirmationRetryAfterMaxSeconds {
		seconds = confirmationRetryAfterMaxSeconds
	}
	return time.Duration(seconds) * time.Second
}

// providerFailureIsReceiptFree fails closed unless the response proves that no
// provider endpoint was selected and no completion, usage, cost, provider
// identity or receipt field was returned. Duplicate keys are rejected so
// contradictory metadata cannot authorize another billed attempt.
//
// The gateway adds informational fields to its error envelope over time
// (user_id, error.metadata, region, summary, generation_time, is_byok have all
// appeared). Those carry no receipt, so an exact-key allowlist over the whole
// envelope only guarantees that the classifier stops recognizing real
// rejections; the receipt walk below is the durable guard.
func providerFailureIsReceiptFree(result *providerHTTPResult, expectedModel string) bool {
	if result == nil {
		return false
	}
	decoded, ok := decodeJSONNumbersRejectDuplicateKeys(result.body)
	if !ok {
		return false
	}
	payload, ok := decoded.(map[string]any)
	if !ok {
		return false
	}
	// Bounded allowlists stay: an unrecognized sibling of "error" may carry
	// billing, so it must fail closed. They are extended to the fields the
	// gateway actually returns, and provider_envelope_test.go pins that exact
	// envelope so the next drift fails a test instead of silently disabling
	// every receipt-free retry.
	if len(payload) < 1 || !onlyJSONKeys(payload, "error", "openrouter_metadata", "user_id") {
		return false
	}
	errorPayload, ok := payload["error"].(map[string]any)
	if !ok || !onlyJSONKeys(errorPayload, "code", "message", "metadata") {
		return false
	}
	// error.metadata is diagnostic, but an unrecognized member of it could
	// describe provider work, so its keys are bounded to the observed set
	// rather than left to the receipt walk alone.
	if rawErrMeta, present := errorPayload["metadata"]; present {
		errMeta, isMap := rawErrMeta.(map[string]any)
		if !isMap || !onlyJSONKeys(errMeta,
			"headers", "is_byok", "limit_source", "provider_error_code",
			"provider_name", "raw", "remedy_hint",
			"retry_after_seconds", "retry_after_seconds_raw") {
			return false
		}
	}
	errorCode, ok := errorPayload["code"].(json.Number)
	if !ok {
		return false
	}
	errorCodeInt, err := errorCode.Int64()
	if err != nil || errorCodeInt != int64(result.status) {
		return false
	}
	message, ok := errorPayload["message"].(string)
	if !ok || strings.TrimSpace(message) == "" {
		return false
	}
	if !providerBackpressureMetadataIsPreProvider(payload, expectedModel) {
		return false
	}
	withoutMetadata := make(map[string]any, len(payload))
	for key, value := range payload {
		if key != "openrouter_metadata" {
			withoutMetadata[key] = value
		}
	}
	return !providerReceiptFieldPresent(withoutMetadata)
}

func providerBackpressureIsReceiptFree(result *providerHTTPResult, expectedModel string) bool {
	return result != nil &&
		(result.status == http.StatusTooManyRequests || result.status == http.StatusServiceUnavailable) &&
		providerFailureIsReceiptFree(result, expectedModel)
}

func providerGatewayIsReceiptFree(result *providerHTTPResult, expectedModel string) bool {
	return result != nil && result.status == http.StatusBadGateway &&
		providerFailureIsReceiptFree(result, expectedModel)
}

// providerBackpressureMetadataIsPreProvider proves from the routing metadata
// that no provider endpoint was selected for this request.
//
// `selected` is the proof, not `attempt`. The gateway reports attempt 1 for a
// first dispatch, so a billed success and an unbilled pre-provider rejection
// carry the same attempt value and attempt alone cannot separate them; only a
// value above 1 shows that an endpoint was already tried.
//
// `endpoints.total` counts the model's whole endpoint catalogue while
// `available` counts the endpoints this request's provider constraints permit.
// They are equal only for an unconstrained single-endpoint model, so they must
// not be compared: a ZDR- or provider-pinned profile legitimately reports
// total 2 with one available endpoint.
//
// Provider identity is deliberately not part of this proof. A grant's route
// provider may be a routing strategy rather than a provider name ("throughput"),
// and when nothing was selected there is no provider to agree with. Identity is
// asserted on success, where a receipt exists to check it against.
func providerBackpressureMetadataIsPreProvider(payload map[string]any, expectedModel string) bool {
	// Absence fails closed. "No receipt visible" is strictly weaker than
	// "provably no endpoint selected", and only the latter licenses sending a
	// billable request again: a bare error envelope carries no statement about
	// whether the gateway reached a provider. The cost of being wrong is a
	// duplicated provider charge against a frozen scored grant, so a retry
	// requires positive routing evidence, not merely the lack of contrary
	// evidence. Callers that only park or open a circuit lose nothing by this.
	raw, present := payload["openrouter_metadata"]
	if !present {
		return false
	}
	metadata, ok := raw.(map[string]any)
	if !ok || !onlyJSONKeys(metadata,
		"requested", "strategy", "attempt", "endpoints",
		"generation_time", "is_byok", "region", "summary") {
		return false
	}
	requested, requestedOK := metadata["requested"].(string)
	strategy, strategyOK := metadata["strategy"].(string)
	if !requestedOK || requested != expectedModel || !strategyOK || strategy != "direct" {
		return false
	}
	// A negative attempt is contradictory metadata and fails closed; 0 and 1
	// both mean "first dispatch" across gateway versions, and anything above 1
	// means an endpoint was already tried and may have been billed.
	attempt, attemptOK := isIntLiteral(metadata["attempt"])
	if !attemptOK || strings.HasPrefix(attempt.String(), "-") {
		return false
	}
	attemptValue, attemptErr := attempt.Int64()
	if attemptErr != nil || attemptValue > 1 {
		return false
	}
	if _, present := metadata["attempts"]; present {
		return false
	}
	endpoints, ok := metadata["endpoints"].(map[string]any)
	if !ok || !onlyJSONKeys(endpoints, "total", "available") {
		return false
	}
	available, availableOK := endpoints["available"].([]any)
	if !availableOK || len(available) == 0 || len(available) > 100 {
		return false
	}
	for _, rawEndpoint := range available {
		endpoint, ok := rawEndpoint.(map[string]any)
		if !ok || !onlyJSONKeys(endpoint, "provider", "model", "selected") {
			return false
		}
		model, modelOK := endpoint["model"].(string)
		selected, selectedOK := endpoint["selected"].(bool)
		if !modelOK || model != expectedModel || !selectedOK || selected {
			return false
		}
	}
	return true
}

func onlyJSONKeys(payload map[string]any, allowed ...string) bool {
	allowedSet := make(map[string]struct{}, len(allowed))
	for _, key := range allowed {
		allowedSet[key] = struct{}{}
	}
	for key := range payload {
		if _, ok := allowedSet[key]; !ok {
			return false
		}
	}
	return true
}

func providerReceiptFieldPresent(value any) bool {
	forbidden := map[string]struct{}{
		"id": {}, "generation": {}, "generation_id": {}, "provider": {},
		"choices": {}, "usage": {}, "cost": {}, "receipt": {}, "receipt_id": {},
		"attempt": {}, "attempts": {}, "selected": {}, "billed": {}, "charged": {},
		"billing": {}, "invoice": {},
	}
	var walk func(any) bool
	walk = func(current any) bool {
		switch typed := current.(type) {
		case map[string]any:
			for key, child := range typed {
				if _, blocked := forbidden[key]; blocked {
					return true
				}
				if walk(child) {
					return true
				}
			}
		case []any:
			for _, child := range typed {
				if walk(child) {
					return true
				}
			}
		}
		return false
	}
	return walk(value)
}

// isRetryablePreProviderNotFound recognizes OpenRouter's documented
// "No providers available" routing result. An attempt value of zero means the
// request never reached a provider, so retrying the identical frozen request
// cannot duplicate provider work or billing. Every ambiguous shape fails
// closed; ordinary 404s remain terminal.
func isRetryablePreProviderNotFound(result *providerHTTPResult, expectedModel string) bool {
	if result == nil || result.status != http.StatusNotFound || expectedModel == "" {
		return false
	}
	decoded, ok := decodeJSONNumbersRejectDuplicateKeys(result.body)
	if !ok {
		return false
	}
	payload, ok := decoded.(map[string]any)
	if !ok {
		return false
	}
	errorPayload, ok := payload["error"].(map[string]any)
	if !ok {
		return false
	}
	code, ok := isIntLiteral(errorPayload["code"])
	if !ok || code.String() != "404" {
		return false
	}
	metadata, ok := payload["openrouter_metadata"].(map[string]any)
	if !ok || metadata["requested"] != expectedModel {
		return false
	}
	// The 404 "no allowed providers" envelope is a different response shape and
	// no live sample of it has been captured, so its attempt base stays pinned
	// at 0. Relaxing it would be inference from the 429 envelope, not evidence.
	attempt, ok := isIntLiteral(metadata["attempt"])
	if !ok || attempt.String() != "0" {
		return false
	}
	if _, present := metadata["attempts"]; present {
		return false
	}
	endpoints, ok := metadata["endpoints"].(map[string]any)
	if !ok {
		return false
	}
	available, ok := endpoints["available"].([]any)
	if !ok || len(available) == 0 || len(available) > 100 {
		return false
	}
	for _, raw := range available {
		endpoint, ok := raw.(map[string]any)
		if !ok || endpoint["selected"] != false {
			return false
		}
	}
	for _, forbidden := range []string{
		"id", "generation", "generation_id", "model", "provider", "choices", "usage", "cost",
	} {
		if _, present := payload[forbidden]; present {
			return false
		}
	}
	return true
}

// decodeJSONNumbersRejectDuplicateKeys parses one JSON document while
// rejecting duplicate object keys at every depth. The pre-provider retry
// classifier is an accounting boundary: contradictory last-key-wins metadata
// must never authorize another upstream attempt.
func decodeJSONNumbersRejectDuplicateKeys(body []byte) (any, bool) {
	dec := json.NewDecoder(bytes.NewReader(body))
	dec.UseNumber()
	decoded, err := decodeUniqueJSONValue(dec)
	if err != nil {
		return nil, false
	}
	var trailing any
	if err := dec.Decode(&trailing); !errors.Is(err, io.EOF) {
		return nil, false
	}
	return decoded, true
}

func decodeUniqueJSONValue(dec *json.Decoder) (any, error) {
	token, err := dec.Token()
	if err != nil {
		return nil, err
	}
	delim, isDelim := token.(json.Delim)
	if !isDelim {
		return token, nil
	}
	switch delim {
	case '{':
		object := make(map[string]any)
		for dec.More() {
			keyToken, err := dec.Token()
			if err != nil {
				return nil, err
			}
			key, ok := keyToken.(string)
			if !ok {
				return nil, errors.New("JSON object key is not a string")
			}
			if _, duplicate := object[key]; duplicate {
				return nil, errors.New("duplicate JSON object key")
			}
			value, err := decodeUniqueJSONValue(dec)
			if err != nil {
				return nil, err
			}
			object[key] = value
		}
		if closeToken, err := dec.Token(); err != nil || closeToken != json.Delim('}') {
			return nil, errors.New("invalid JSON object terminator")
		}
		return object, nil
	case '[':
		array := make([]any, 0)
		for dec.More() {
			value, err := decodeUniqueJSONValue(dec)
			if err != nil {
				return nil, err
			}
			array = append(array, value)
		}
		if closeToken, err := dec.Token(); err != nil || closeToken != json.Delim(']') {
			return nil, errors.New("invalid JSON array terminator")
		}
		return array, nil
	default:
		return nil, errors.New("unexpected JSON delimiter")
	}
}

func backoffDelay(attempt int) time.Duration {
	return time.Duration(float64(250*time.Millisecond) * math.Pow(2, float64(attempt-1)))
}

// postOnce performs one POST, buffering the response body (like httpx does
// inside client.post). A body-read failure is returned as a transport error,
// exactly as httpx surfaces it from the same call.
func postOnce(ctx context.Context, client *http.Client, url string, body []byte,
	headers map[string]string, maxBody int64, timeoutSeconds int) (*providerHTTPResult, error) {

	attemptCtx, cancel := context.WithTimeout(ctx, time.Duration(timeoutSeconds)*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(attemptCtx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	buffered, err := io.ReadAll(io.LimitReader(resp.Body, maxBody+1))
	if err != nil {
		return nil, err
	}
	return &providerHTTPResult{
		status:        resp.StatusCode,
		header:        resp.Header,
		body:          buffered,
		bodyOverLimit: int64(len(buffered)) > maxBody,
	}, nil
}

// openrouterHeaders mirrors _openrouter_headers. No inbound client headers
// are ever forwarded upstream.
func openrouterHeaders(apiKey string, includeMetadata bool) map[string]string {
	headers := map[string]string{
		"Authorization":      "Bearer " + apiKey,
		"Content-Type":       "application/json",
		"HTTP-Referer":       "https://heyditto.ai/",
		"X-OpenRouter-Title": "Ditto",
	}
	if includeMetadata {
		headers["X-OpenRouter-Metadata"] = "enabled"
	}
	return headers
}

// bf16PriceOrderProviders is the scored bf16_price_order route: DeepInfra's
// bf16 endpoint (the cheapest high-capacity ZDR endpoint) first, then the
// two providers that carried the throughput route (Bedrock, Groq), then
// DekaLLM's bf16 endpoint, which also serves logprobs. "only" pins the same
// set, so a fallback can never land on a 4-bit or fp8 endpoint (AkashML,
// Parasail, SiliconFlow, CoreWeave) the way the throughput sort can.
var bf16PriceOrderProviders = []string{"deepinfra", "amazon-bedrock", "groq", "dekallm"}

// providerPreferences mirrors _provider_preferences. scoredRouting is the
// resolved scored_provider_routing (throughput or bf16_price_order) and only
// affects the aggregate route.
func providerPreferences(routingMode, provider string, quantization string, scoredRouting string) map[string]any {
	if routingMode == config.RoutingModeAggregateThroughput &&
		scoredRouting == config.ScoredProviderRoutingBF16PriceOrder {
		return map[string]any{
			"order":           append([]string{}, bf16PriceOrderProviders...),
			"only":            append([]string{}, bf16PriceOrderProviders...),
			"ignore":          []string{"coreweave"},
			"allow_fallbacks": true,
			"data_collection": "deny",
			"zdr":             true,
		}
	}
	if routingMode == config.RoutingModeAggregateThroughput {
		return map[string]any{
			"sort":            "throughput",
			"ignore":          []string{"coreweave"},
			"allow_fallbacks": true,
			"data_collection": "deny",
			"zdr":             true,
		}
	}
	preferences := map[string]any{
		"only":            []string{provider},
		"allow_fallbacks": false,
		"data_collection": "deny",
		"zdr":             true,
	}
	if quantization != "" {
		preferences["quantizations"] = []string{quantization}
	}
	return preferences
}

// reliabilityProviderPreferences mirrors _reliability_provider_preferences
// (the phase-1 recovery route in aggregate mode).
func reliabilityProviderPreferences() map[string]any {
	return map[string]any{
		"order":           []string{"deepinfra", "groq"},
		"ignore":          []string{"coreweave"},
		"allow_fallbacks": false,
		"data_collection": "deny",
		"zdr":             true,
	}
}

// decodeJSONNumbers parses bytes with UseNumber. ok=false means the body
// was not one JSON document (response.json() raising ValueError); a JSON
// null is ok=true with a nil value, matching Python's None.
func decodeJSONNumbers(body []byte) (any, bool) {
	dec := json.NewDecoder(bytes.NewReader(body))
	dec.UseNumber()
	var decoded any
	if err := dec.Decode(&decoded); err != nil {
		return nil, false
	}
	// Trailing garbage means it was not one JSON document.
	if dec.More() {
		return nil, false
	}
	return decoded, true
}

// openrouterAttemptCount mirrors _openrouter_attempt_count.
func openrouterAttemptCount(decoded any) int {
	payload, ok := decoded.(map[string]any)
	if !ok {
		return 0
	}
	metadata, ok := payload["openrouter_metadata"].(map[string]any)
	if !ok {
		return 0
	}
	if n, ok := isIntLiteral(metadata["attempt"]); ok {
		if v, err := n.Int64(); err == nil && 1 <= v && v <= 100 {
			return int(v)
		}
	}
	if attempts, ok := metadata["attempts"].([]any); ok && len(attempts) <= 100 {
		return len(attempts)
	}
	return 0
}

// openrouterLastAttemptedProvider mirrors _openrouter_last_attempted_provider.
func openrouterLastAttemptedProvider(decoded any) string {
	payload, ok := decoded.(map[string]any)
	if !ok {
		return ""
	}
	metadata, ok := payload["openrouter_metadata"].(map[string]any)
	if !ok {
		return ""
	}
	attempts, ok := metadata["attempts"].([]any)
	if !ok || len(attempts) < 1 || len(attempts) > 100 {
		return ""
	}
	last, ok := attempts[len(attempts)-1].(map[string]any)
	if !ok {
		return ""
	}
	provider, ok := last["provider"].(string)
	if !ok || len(provider) < 1 || len(provider) > 120 {
		return ""
	}
	return provider
}

// upstreamProviderIdentity mirrors _upstream_provider: the actual provider
// from opt-in router metadata, with bounded legacy fallback. An empty string
// with nil error means "unknown" (None).
func upstreamProviderIdentity(payload map[string]any) (string, *httpError) {
	metadata, present := payload["openrouter_metadata"]
	if !present || metadata == nil {
		provider, ok := payload["provider"].(string)
		if ok && len(provider) >= 1 && len(provider) <= 120 {
			return provider, nil
		}
		return "", nil
	}
	metadataMap, ok := metadata.(map[string]any)
	if !ok {
		return "", httpErrorf(502, "provider identity mismatch")
	}
	endpoints, _ := metadataMap["endpoints"].(map[string]any)
	availableRaw, ok := endpoints["available"]
	var available []any
	if ok {
		available, ok = availableRaw.([]any)
	}
	if !ok {
		return "", httpErrorf(502, "provider identity mismatch")
	}
	var selected []any
	for _, raw := range available {
		endpoint, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		if sel, ok := endpoint["selected"].(bool); ok && sel {
			selected = append(selected, endpoint["provider"])
		}
	}
	if len(selected) != 1 {
		return "", httpErrorf(502, "provider identity mismatch")
	}
	provider, ok := selected[0].(string)
	if !ok || len(provider) < 1 || len(provider) > 120 {
		return "", httpErrorf(502, "provider identity mismatch")
	}
	return provider, nil
}

// boundedUsage mirrors _bounded_usage: prompt/completion must be
// non-negative non-bool ints with sum <= 2^62. Provider-supplied cost is
// ignored here.
func boundedUsage(payload map[string]any) (prompt, completion int64, ok bool) {
	usage, isMap := payload["usage"].(map[string]any)
	if !isMap {
		return 0, 0, false
	}
	pn, pInt := isIntLiteral(usage["prompt_tokens"])
	cn, cInt := isIntLiteral(usage["completion_tokens"])
	if !pInt || !cInt {
		return 0, 0, false
	}
	p, perr := pn.Int64()
	c, cerr := cn.Int64()
	if perr != nil || cerr != nil || p < 0 || c < 0 {
		return 0, 0, false
	}
	// Overflow-safe form of Python's arbitrary-precision p+c > 2^62: both
	// operands are non-negative, so p+c can wrap int64 (e.g. p = c = 2^62)
	// and a direct sum comparison would accept what Python rejects.
	if p > (1<<62)-c {
		return 0, 0, false
	}
	return p, c, true
}

// boundedProviderCost mirrors _bounded_provider_cost: usage.cost as bounded
// integer micro-USD (finite, 0 <= cost <= 100 USD), banker's rounding like
// Python round(). -1 means invalid.
func boundedProviderCost(payload map[string]any) int64 {
	usage, isMap := payload["usage"].(map[string]any)
	if !isMap {
		return -1
	}
	n, ok := asNumber(usage["cost"])
	if !ok {
		return -1
	}
	cost := numFloat(n)
	if math.IsNaN(cost) || math.IsInf(cost, 0) || cost < 0 || cost > 100 {
		return -1
	}
	return int64(math.RoundToEven(cost * 1_000_000))
}

// phaseErrorCode mirrors _phase_error_code.
func phaseErrorCode(err *httpError) string {
	switch err.message {
	case "provider response is too large":
		return "response_too_large"
	case "invalid provider response":
		return "invalid_provider_response"
	case "provider identity mismatch":
		return "provider_identity_mismatch"
	case "inference provider unavailable":
		return "provider_unavailable"
	}
	return "provider_unavailable"
}

// providerErrorEnvelope recognizes OpenRouter's body-level error transport.
// Some providers return HTTP 200 while the JSON body carries an upstream 5xx;
// that envelope has no completion identity and must be classified before the
// normal model/provider checks.
func providerErrorEnvelope(payload map[string]any) (int64, string, bool) {
	// OpenRouter also attaches a generation id and provider error metadata to
	// body-level errors. These are diagnostic fields, not a successful model
	// identity. This classifier never authorizes a replay: the receipt-free
	// retry predicate remains separate and rejects envelopes carrying an id.
	if !onlyJSONKeys(payload, "error", "id") {
		return 0, "", false
	}
	if id, present := payload["id"]; present {
		if value, ok := id.(string); !ok || value == "" {
			return 0, "", false
		}
	}
	errorPayload, ok := payload["error"].(map[string]any)
	if !ok || !onlyJSONKeys(errorPayload, "code", "message", "metadata") {
		return 0, "", false
	}
	if metadata, present := errorPayload["metadata"]; present {
		if _, ok := metadata.(map[string]any); !ok {
			return 0, "", false
		}
	}
	codeNumber, ok := isIntLiteral(errorPayload["code"])
	if !ok {
		return 0, "", false
	}
	code, err := codeNumber.Int64()
	if err != nil || code < 400 || code > 599 {
		return 0, "", false
	}
	message, ok := errorPayload["message"].(string)
	if !ok || message == "" {
		return 0, "", false
	}
	return code, message, true
}

// minerRecoverableProviderError is intentionally narrower than a generic
// provider 502. It recognizes only exact response-generation failures that a
// later miner-issued call can repair; transport failures and unrelated provider
// outages remain validator infrastructure failures.
func minerRecoverableProviderError(request, response map[string]any) bool {
	code, message, ok := providerErrorEnvelope(response)
	if !ok || code != http.StatusBadGateway {
		return false
	}
	if strings.Contains(message, "Failed to validate JSON") {
		responseFormat, ok := request["response_format"].(map[string]any)
		if !ok || responseFormat["type"] != "json_schema" {
			return false
		}
		jsonSchema, ok := responseFormat["json_schema"].(map[string]any)
		return ok && jsonSchema["strict"] == true
	}
	if strings.Contains(message, "Tool choice is none, but model called a tool") {
		toolChoice, choicePresent := request["tool_choice"]
		tools, toolsPresent := request["tools"].([]any)
		return (!choicePresent || toolChoice == nil || toolChoice == "none") && (!toolsPresent || len(tools) == 0)
	}
	const undeclaredToolPrefix = "attempted to call tool '"
	const undeclaredToolSuffix = "' which was not in request.tools"
	if start := strings.Index(message, undeclaredToolPrefix); start >= 0 {
		nameStart := start + len(undeclaredToolPrefix)
		nameEnd := strings.Index(message[nameStart:], undeclaredToolSuffix)
		if nameEnd <= 0 {
			return false
		}
		return !requestDeclaresTool(request, message[nameStart:nameStart+nameEnd])
	}
	const invalidToolParametersPrefix = "Upstream error from Groq: Tool call validation failed: tool call validation failed: parameters for tool "
	const invalidToolParametersSuffix = " did not match schema: errors:"
	if start := strings.Index(message, invalidToolParametersPrefix); start >= 0 {
		nameStart := start + len(invalidToolParametersPrefix)
		nameEnd := strings.Index(message[nameStart:], invalidToolParametersSuffix)
		if nameEnd <= 0 {
			return false
		}
		detailStart := nameStart + nameEnd + len(invalidToolParametersSuffix)
		if strings.TrimSpace(message[detailStart:]) == "" {
			return false
		}
		// This exact Groq error is emitted after generation when the model's
		// arguments fail a declared tool's schema. Requiring that declaration
		// distinguishes repairable generated arguments from an invalid or
		// rejected tool definition, which remains infrastructure-owned.
		return requestDeclaresTool(request, message[nameStart:nameStart+nameEnd])
	}
	return false
}

func requestDeclaresTool(request map[string]any, name string) bool {
	tools, _ := request["tools"].([]any)
	for _, rawTool := range tools {
		tool, ok := rawTool.(map[string]any)
		if !ok {
			continue
		}
		function, ok := tool["function"].(map[string]any)
		if ok && function["name"] == name {
			return true
		}
	}
	return false
}

func providerRejectionIsRouteObservable(status int) bool {
	return status >= 400 && status != 400 && status != 422
}

// --- chat sanitization ------------------------------------------------

type publicToolFunction struct {
	Name      string `json:"name"`
	Arguments string `json:"arguments"`
}

type publicToolCall struct {
	ID       string             `json:"id"`
	Type     string             `json:"type"`
	Function publicToolFunction `json:"function"`
}

type publicChatMessage struct {
	Role    string  `json:"role"`
	Content *string `json:"content"`
	// Pointer-to-slice, NOT a bare slice with omitempty: Python emits
	// "tool_calls": [] when the provider sent an empty list (tool_calls is
	// not None) and omits the key only when the provider omitted it. A bare
	// omitempty slice would elide the empty list and change the response
	// shape for harnesses that branch on key presence.
	ToolCalls *[]publicToolCall `json:"tool_calls,omitempty"`
}

type publicChatChoice struct {
	Index        json.RawMessage   `json:"index"`
	FinishReason string            `json:"finish_reason"`
	Message      publicChatMessage `json:"message"`
	Logprobs     json.RawMessage   `json:"logprobs"`
}

type publicChatUsage struct {
	PromptTokens     int64 `json:"prompt_tokens"`
	CompletionTokens int64 `json:"completion_tokens"`
	TotalTokens      int64 `json:"total_tokens"`
}

type publicChatResponse struct {
	ID      string             `json:"id"`
	Object  string             `json:"object"`
	Created json.RawMessage    `json:"created"`
	Model   string             `json:"model"`
	Choices []publicChatChoice `json:"choices"`
	Usage   publicChatUsage    `json:"usage"`
}

// rawChoiceLogprobs extracts choices[0].logprobs from the raw provider body,
// compacted, so the pass-through preserves provider key order. nil means
// absent (rendered as JSON null, matching Python's .get()).
func rawChoiceLogprobs(body []byte) json.RawMessage {
	var top map[string]json.RawMessage
	if err := json.Unmarshal(body, &top); err != nil {
		return nil
	}
	var choices []json.RawMessage
	if err := json.Unmarshal(top["choices"], &choices); err != nil || len(choices) != 1 {
		return nil
	}
	var choice map[string]json.RawMessage
	if err := json.Unmarshal(choices[0], &choice); err != nil {
		return nil
	}
	raw, ok := choice["logprobs"]
	if !ok {
		return nil
	}
	var compact bytes.Buffer
	if err := json.Compact(&compact, raw); err != nil {
		return nil
	}
	return compact.Bytes()
}

// publicProviderResponse mirrors _public_provider_response: the trust
// boundary between OpenRouter's additive surface and the harness contract.
func publicProviderResponse(payload map[string]any, rawBody []byte) (*publicChatResponse, *httpError) {
	responseID, idOk := payload["id"].(string)
	createdNum, createdOk := isIntLiteral(payload["created"])
	model, modelOk := payload["model"].(string)
	choicesRaw, choicesOk := payload["choices"].([]any)
	if !idOk || len(responseID) < 1 || len(responseID) > 256 ||
		!createdOk ||
		payload["object"] != "chat.completion" ||
		!modelOk || !choicesOk || len(choicesRaw) != 1 {
		return nil, httpErrorf(502, "invalid provider response")
	}
	choice, ok := choicesRaw[0].(map[string]any)
	if !ok {
		return nil, httpErrorf(502, "inference provider unavailable")
	}
	if _, hasError := choice["error"]; hasError {
		return nil, httpErrorf(502, "inference provider unavailable")
	}
	indexNum, indexOk := isIntLiteral(choice["index"])
	finishReason, frOk := choice["finish_reason"].(string)
	message, msgOk := choice["message"].(map[string]any)
	validFinish := frOk && (finishReason == "stop" || finishReason == "length" ||
		finishReason == "tool_calls" || finishReason == "content_filter")
	if !indexOk || !validFinish || !msgOk || message["role"] != "assistant" {
		return nil, httpErrorf(502, "invalid provider response")
	}
	var content *string
	if raw, present := message["content"]; present && raw != nil {
		s, ok := raw.(string)
		if !ok {
			return nil, httpErrorf(502, "invalid provider response")
		}
		content = &s
	}
	publicMessage := publicChatMessage{Role: "assistant", Content: content}
	if rawCalls, present := message["tool_calls"]; present && rawCalls != nil {
		calls, ok := rawCalls.([]any)
		if !ok {
			return nil, httpErrorf(502, "invalid provider response")
		}
		publicCalls := make([]publicToolCall, 0, len(calls))
		for _, rawCall := range calls {
			call, ok := rawCall.(map[string]any)
			if !ok {
				return nil, httpErrorf(502, "invalid provider response")
			}
			fn, _ := call["function"].(map[string]any)
			id, idOk := call["id"].(string)
			name, nameOk := "", false
			args, argsOk := "", false
			if fn != nil {
				name, nameOk = fn["name"].(string)
				args, argsOk = fn["arguments"].(string)
			}
			if call["type"] != "function" || !idOk || fn == nil || !nameOk || !argsOk {
				return nil, httpErrorf(502, "invalid provider response")
			}
			publicCalls = append(publicCalls, publicToolCall{
				ID:       id,
				Type:     "function",
				Function: publicToolFunction{Name: name, Arguments: args},
			})
		}
		publicMessage.ToolCalls = &publicCalls
	}
	prompt, completion, usageOk := boundedUsage(payload)
	if !usageOk {
		return nil, httpErrorf(502, "invalid provider response")
	}
	if raw, present := choice["logprobs"]; present && raw != nil {
		if _, ok := raw.(map[string]any); !ok {
			return nil, httpErrorf(502, "invalid provider response")
		}
	}
	return &publicChatResponse{
		ID:      responseID,
		Object:  "chat.completion",
		Created: json.RawMessage(createdNum.String()),
		Model:   model,
		Choices: []publicChatChoice{{
			Index:        json.RawMessage(indexNum.String()),
			FinishReason: finishReason,
			Message:      publicMessage,
			Logprobs:     rawChoiceLogprobs(rawBody),
		}},
		Usage: publicChatUsage{
			PromptTokens:     prompt,
			CompletionTokens: completion,
			TotalTokens:      prompt + completion,
		},
	}, nil
}

// compactJSON marshals without HTML escaping and without the encoder's
// trailing newline — the closest Go equivalent of
// json.dumps(..., separators=(",", ":")).
func compactJSON(v any) ([]byte, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return nil, err
	}
	out := buf.Bytes()
	if n := len(out); n > 0 && out[n-1] == '\n' {
		out = out[:n-1]
	}
	return out, nil
}

// --- chat provider orchestration --------------------------------------

type chatCompletionResult struct {
	raw                []byte
	promptTokens       int64
	completionTokens   int64
	costMicrousd       int64
	upstreamProvider   string
	upstreamAttempts   int
	openrouterAttempts int
	fallbackPhase      int
	phases             []phaseTrace // every phase tried, in order (trace capture)
}

type chatProviderExhausted struct {
	upstreamAttempts   int
	openrouterAttempts int
	fallbackPhase      int
	terminalErrorCode  string
	timedOut           bool
	upstreamProvider   string
	routeObservable    bool
	phases             []phaseTrace
}

// phaseTrace is what one provider-route phase sent and got back, kept for
// the inference trace capture (internal/traces). It is never consulted by
// accounting or routing: those read the typed fields above.
type phaseTrace struct {
	phase     int
	route     string
	payload   []byte // the JSON actually POSTed (provider preferences included)
	status    int
	headers   http.Header
	body      []byte // raw provider body, pre-sanitization (nil on transport failure)
	attempts  int
	errorCode string
	timedOut  bool
}

func (e *chatProviderExhausted) Error() string { return e.terminalErrorCode }

// completeChatWithRecovery retains its wire-compatible name and executes one
// provider phase. It retries one strict receipt-free 429 or generic HTTP 502;
// known response-generation 502s stay miner-owned, and every ambiguous or
// potentially billed outcome remains single-shot.
func completeChatWithRecovery(ctx context.Context, client *http.Client, cfg config.InferenceProxyConfig,
	payload map[string]any, model, expectedProvider, expectedQuantization string,
	expectedPromptPrice, expectedCompletionPrice *float64, sleep sleepFunc) (*chatCompletionResult, *chatProviderExhausted) {

	aggregate := cfg.RoutingMode == config.RoutingModeAggregateThroughput
	type phaseSpec struct {
		phase   int
		payload map[string]any
	}
	primary := make(map[string]any, len(payload)+1)
	for k, v := range payload {
		primary[k] = v
	}
	preferences := providerPreferences(cfg.RoutingMode, expectedProvider, expectedQuantization,
		cfg.ScoredProviderRouting)
	adaptProviderRequestCompatibility(primary, preferences)
	primary["provider"] = preferences
	phases := []phaseSpec{{phase: 0, payload: primary}}
	headers := openrouterHeaders(cfg.OpenRouterAPIKey, true)
	attachOpenRouterSessionHeader(ctx, headers)

	totalAttempts := 0
	routerAttempts := 0
	lastCode := "provider_unavailable"
	lastTimedOut := false
	lastPhase := 0
	lastProvider := ""
	routeObservable := false
	var traced []phaseTrace
phaseLoop:
	for _, spec := range phases {
		lastPhase = spec.phase
		route := "openrouter"
		if spec.phase == 1 {
			route = "reliable"
		}
		sentPayload, _ := json.Marshal(spec.payload)
		for safeAttempt := 1; safeAttempt <= providerSafeRetryMaxAttempts; safeAttempt++ {
			trace := phaseTrace{phase: spec.phase, route: route, payload: sentPayload}
			result, callErr := postProviderWithRetry(ctx, client, cfg.UpstreamURL, spec.payload, headers,
				cfg.ResponseBodyBytes, cfg.TimeoutSeconds, true, "", sleep)
			if callErr != nil {
				totalAttempts += callErr.attempts
				lastTimedOut = callErr.timedOut
				if callErr.timedOut {
					lastCode = "provider_timeout"
				} else {
					lastCode = "provider_transport"
				}
				routeObservable = true
				trace.attempts, trace.timedOut, trace.errorCode = callErr.attempts, callErr.timedOut, lastCode
				traced = append(traced, trace)
				continue phaseLoop
			}
			totalAttempts += result.attempts
			trace.attempts, trace.status, trace.headers, trace.body = result.attempts, result.status, result.header, result.body
			if result.bodyOverLimit {
				lastCode = "response_too_large"
				trace.errorCode = lastCode
				traced = append(traced, trace)
				continue phaseLoop
			}
			decoded, _ := decodeJSONNumbers(result.body)
			routerAttempts += openrouterAttemptCount(decoded)
			if provider := openrouterLastAttemptedProvider(decoded); provider != "" {
				lastProvider = provider
			}
			decodedMap, decodedMapOK := decoded.(map[string]any)
			if decodedMapOK && minerRecoverableProviderError(spec.payload, decodedMap) {
				// Generated-output failures need request-aware repair. Preserve the
				// private recovery class for the miner even when OpenRouter carries
				// the envelope with an outer HTTP 502 instead of HTTP 200.
				lastCode = providerGenerationInvalidCode
				trace.errorCode = lastCode
				traced = append(traced, trace)
				continue phaseLoop
			}
			if result.status >= 400 {
				lastTimedOut = result.status == 408 || result.status == 504
				lastCode = "upstream_http_" + strconv.Itoa(result.status)
				routeObservable = routeObservable || providerRejectionIsRouteObservable(result.status)
				trace.errorCode, trace.timedOut = lastCode, lastTimedOut
				traced = append(traced, trace)
				retryDelay := time.Duration(0)
				if result.status == http.StatusTooManyRequests &&
					providerBackpressureIsReceiptFree(result, model) {
					retryDelay = time.Duration(providerRetryAfterSeconds(result.header)) * time.Second
				} else if providerGatewayIsReceiptFree(result, model) {
					retryDelay = providerGatewayRetryDelay
				}
				if retryDelay > 0 && safeAttempt < providerSafeRetryMaxAttempts {
					sleep(ctx, retryDelay)
					if ctx.Err() != nil {
						continue phaseLoop
					}
					continue
				}
				continue phaseLoop
			}
			routeObservable = true
			if !decodedMapOK {
				lastCode = "invalid_provider_response"
				trace.errorCode = lastCode
				traced = append(traced, trace)
				continue phaseLoop
			}
			if _, _, providerError := providerErrorEnvelope(decodedMap); providerError {
				lastCode = "provider_unavailable"
				trace.errorCode = lastCode
				traced = append(traced, trace)
				continue phaseLoop
			}
			if m, ok := decodedMap["model"].(string); !ok || m != model {
				lastCode = "provider_identity_mismatch"
				trace.errorCode = lastCode
				traced = append(traced, trace)
				continue phaseLoop
			}
			phaseResult, herr := func() (*chatCompletionResult, *httpError) {
				providerValue, herr := upstreamProviderIdentity(decodedMap)
				if herr != nil {
					return nil, herr
				}
				if providerValue == "" || (!aggregate && providerValue != expectedProvider) {
					return nil, httpErrorf(502, "provider identity mismatch")
				}
				prompt, completion, usageOk := boundedUsage(decodedMap)
				if !usageOk {
					return nil, httpErrorf(502, "invalid provider response")
				}
				var cost int64
				if aggregate {
					cost = boundedProviderCost(decodedMap)
					if cost < 0 {
						return nil, httpErrorf(502, "invalid provider response")
					}
				} else {
					if expectedPromptPrice == nil || expectedCompletionPrice == nil {
						return nil, httpErrorf(502, "invalid provider response")
					}
					cost = int64((float64(prompt)**expectedPromptPrice +
						float64(completion)**expectedCompletionPrice) * 1_000_000)
				}
				public, herr := publicProviderResponse(decodedMap, result.body)
				if herr != nil {
					return nil, herr
				}
				raw, err := compactJSON(public)
				if err != nil {
					return nil, httpErrorf(502, "invalid provider response")
				}
				return &chatCompletionResult{
					raw:              raw,
					promptTokens:     prompt,
					completionTokens: completion,
					costMicrousd:     cost,
					upstreamProvider: providerValue,
				}, nil
			}()
			if herr != nil {
				lastCode = phaseErrorCode(herr)
				trace.errorCode = lastCode
				traced = append(traced, trace)
				continue phaseLoop
			}
			traced = append(traced, trace)
			phaseResult.upstreamAttempts = totalAttempts
			phaseResult.openrouterAttempts = routerAttempts
			phaseResult.fallbackPhase = spec.phase
			phaseResult.phases = traced
			return phaseResult, nil
		}
	}
	return nil, &chatProviderExhausted{
		upstreamAttempts:   totalAttempts,
		openrouterAttempts: routerAttempts,
		fallbackPhase:      lastPhase,
		terminalErrorCode:  lastCode,
		timedOut:           lastTimedOut,
		upstreamProvider:   lastProvider,
		routeObservable:    routeObservable,
		phases:             traced,
	}
}

// --- embedding provider ------------------------------------------------

type embeddingProviderResult struct {
	result        *providerHTTPResult
	attempts      int
	direct        bool
	fallbackPhase int
	phases        []embeddingProviderPhase
}

type embeddingProviderPhase struct {
	route   string
	payload []byte
	result  *providerHTTPResult
	callErr *providerCallError
}

func perplexityEmbeddingBackpressureIsReceiptFree(result *providerHTTPResult) bool {
	if result == nil || result.status != http.StatusTooManyRequests ||
		!providerIsBackpressure(result.status, result.header) ||
		trimSpace(result.header.Get("Retry-After")) == "" {
		return false
	}
	decoded, ok := decodeJSONNumbersRejectDuplicateKeys(result.body)
	if !ok {
		return false
	}
	payload, ok := decoded.(map[string]any)
	if !ok || len(payload) != 1 || !onlyJSONKeys(payload, "error") {
		return false
	}
	errorPayload, ok := payload["error"].(map[string]any)
	if !ok || len(errorPayload) != 3 || !onlyJSONKeys(errorPayload, "code", "message", "type") {
		return false
	}
	code, ok := errorPayload["code"].(json.Number)
	if !ok {
		return false
	}
	codeValue, err := code.Int64()
	message, messageOK := errorPayload["message"].(string)
	errorType, typeOK := errorPayload["type"].(string)
	return err == nil && codeValue == http.StatusTooManyRequests &&
		messageOK && strings.TrimSpace(message) != "" &&
		typeOK && errorType == "request_rate_limit_exceeded"
}

// postEmbeddingProvider selects one configured route. It never falls through
// after a possibly cost-bearing provider failure. An explicit backpressure
// response is receipt-free, so the direct route may safely use the configured
// OpenRouter path instead of parking an otherwise healthy scored run.
func postEmbeddingProvider(ctx context.Context, client *http.Client, cfg config.InferenceProxyConfig,
	inputs []string, sleep sleepFunc) (*embeddingProviderResult, *providerCallError) {

	var direct *providerHTTPResult
	var directErr *providerCallError
	var directPayloadBytes []byte
	if cfg.PerplexityAPIKey != "" {
		directPayload := map[string]any{
			"model":           pplxEmbedResponseModel,
			"input":           inputs,
			"dimensions":      cfg.EmbeddingDimensions,
			"encoding_format": "base64_int8",
		}
		directPayloadBytes, _ = json.Marshal(directPayload)
		direct, directErr = postProviderWithRetry(ctx, client, cfg.EmbeddingFallbackURL,
			directPayload,
			map[string]string{
				"Authorization": "Bearer " + cfg.PerplexityAPIKey,
				"Content-Type":  "application/json",
			},
			cfg.EmbeddingResponseBodyBytes, cfg.TimeoutSeconds, false, "", sleep)
		if direct != nil && !perplexityEmbeddingBackpressureIsReceiptFree(direct) {
			return &embeddingProviderResult{
				result: direct, attempts: direct.attempts, direct: true,
				phases: []embeddingProviderPhase{{route: "direct", payload: directPayloadBytes, result: direct}},
			}, nil
		}
		if directErr != nil {
			return nil, directErr
		}
	}
	openRouterPayload := map[string]any{
		"model":           cfg.EmbeddingModel,
		"input":           inputs,
		"dimensions":      cfg.EmbeddingDimensions,
		"encoding_format": "float",
		"provider": map[string]any{
			"order":           []string{cfg.EmbeddingProvider},
			"allow_fallbacks": false,
			"data_collection": "deny",
		},
	}
	openRouterPayloadBytes, _ := json.Marshal(openRouterPayload)
	openrouter, orErr := postProviderWithRetry(ctx, client, cfg.EmbeddingUpstreamURL,
		openRouterPayload,
		openrouterHeaders(cfg.OpenRouterAPIKey, false),
		cfg.EmbeddingResponseBodyBytes, cfg.TimeoutSeconds, false, "", sleep)
	if orErr != nil {
		if direct != nil {
			attempts := direct.attempts + orErr.attempts
			return &embeddingProviderResult{
				attempts:      attempts,
				fallbackPhase: 1,
				phases: []embeddingProviderPhase{
					{route: "direct", payload: directPayloadBytes, result: direct},
					{route: "openrouter", payload: openRouterPayloadBytes, callErr: orErr},
				},
			}, &providerCallError{attempts: attempts, timedOut: orErr.timedOut}
		}
		return nil, orErr
	}
	phases := []embeddingProviderPhase{{route: "openrouter", payload: openRouterPayloadBytes, result: openrouter}}
	attempts := openrouter.attempts
	if direct != nil {
		phases = append([]embeddingProviderPhase{{route: "direct", payload: directPayloadBytes, result: direct}}, phases...)
		attempts += direct.attempts
	}
	return &embeddingProviderResult{
		result:   openrouter,
		attempts: attempts,
		direct:   false,
		fallbackPhase: func() int {
			if direct != nil {
				return 1
			}
			return 0
		}(),
		phases: phases,
	}, nil
}

// --- embedding sanitization --------------------------------------------

// perplexityEmbeddingResponse mirrors _perplexity_embedding_response:
// convert the direct API's base64 signed-INT8 vectors to the reviewed float
// wire (byte/128).
func perplexityEmbeddingResponse(decoded any) (any, *httpError) {
	payload, ok := decoded.(map[string]any)
	if !ok {
		return nil, httpErrorf(502, "invalid provider response")
	}
	data, ok := payload["data"].([]any)
	if !ok {
		return nil, httpErrorf(502, "invalid provider response")
	}
	converted := make([]any, 0, len(data))
	for _, rawItem := range data {
		item, _ := rawItem.(map[string]any)
		var encoded string
		encodedOk := false
		if item != nil {
			encoded, encodedOk = item["embedding"].(string)
		}
		if !encodedOk {
			return nil, httpErrorf(502, "invalid provider response")
		}
		raw, err := base64.StdEncoding.DecodeString(encoded)
		if err != nil {
			return nil, httpErrorf(502, "invalid provider response")
		}
		vector := make([]any, len(raw))
		for i, b := range raw {
			vector[i] = float64(int8(b)) / 128
		}
		out := make(map[string]any, len(item))
		for k, v := range item {
			out[k] = v
		}
		out["embedding"] = vector
		converted = append(converted, out)
	}
	out := make(map[string]any, len(payload))
	for k, v := range payload {
		out[k] = v
	}
	out["data"] = converted
	return out, nil
}

type publicEmbeddingItem struct {
	Object    string `json:"object"`
	Index     int    `json:"index"`
	Embedding []any  `json:"embedding"`
}

type publicEmbeddingUsage struct {
	PromptTokens int64 `json:"prompt_tokens"`
	TotalTokens  int64 `json:"total_tokens"`
}

type publicEmbeddingResponse struct {
	Object string                `json:"object"`
	Model  string                `json:"model"`
	Data   []publicEmbeddingItem `json:"data"`
	Usage  publicEmbeddingUsage  `json:"usage"`
}

// publicEmbeddingResponseFrom mirrors _public_embedding_response: envelope,
// ordering, arity, and vector length are validated; individual elements are
// deliberately NOT float-validated (the broker re-validates them).
func publicEmbeddingResponseFrom(decoded any, model string, dimensions, inputCount int) (*publicEmbeddingResponse, int64, *httpError) {
	payload, ok := decoded.(map[string]any)
	if !ok {
		return nil, 0, httpErrorf(502, "provider identity mismatch")
	}
	responseModel, _ := payload["model"].(string)
	modelOk := responseModel == model
	if model == pplxEmbedContractModel && responseModel == pplxEmbedResponseModel {
		modelOk = true
	}
	if !modelOk {
		return nil, 0, httpErrorf(502, "provider identity mismatch")
	}
	data, dataOk := payload["data"].([]any)
	usage, _ := payload["usage"].(map[string]any)
	var promptTokens int64
	tokensOk := false
	if usage != nil {
		if n, ok := isIntLiteral(usage["prompt_tokens"]); ok {
			if v, err := n.Int64(); err == nil {
				promptTokens = v
				tokensOk = true
			}
		}
	}
	if !dataOk || len(data) != inputCount || !tokensOk || promptTokens < 0 {
		return nil, 0, httpErrorf(502, "invalid provider response")
	}
	publicData := make([]publicEmbeddingItem, 0, len(data))
	for expectedIndex, rawItem := range data {
		item, itemOk := rawItem.(map[string]any)
		var vector []any
		vectorOk := false
		indexOk := false
		if itemOk {
			if n, ok := asNumber(item["index"]); ok {
				indexOk = numFloat(n) == float64(expectedIndex)
			}
			switch v := item["embedding"].(type) {
			case []any:
				vector, vectorOk = v, true
			case []float64:
				vector = make([]any, len(v))
				for i, f := range v {
					vector[i] = f
				}
				vectorOk = true
			}
		}
		if !itemOk || !indexOk || !vectorOk || len(vector) != dimensions {
			return nil, 0, httpErrorf(502, "invalid provider response")
		}
		publicData = append(publicData, publicEmbeddingItem{
			Object:    "embedding",
			Index:     expectedIndex,
			Embedding: vector,
		})
	}
	return &publicEmbeddingResponse{
		Object: "list",
		Model:  model,
		Data:   publicData,
		Usage:  publicEmbeddingUsage{PromptTokens: promptTokens, TotalTokens: promptTokens},
	}, promptTokens, nil
}
