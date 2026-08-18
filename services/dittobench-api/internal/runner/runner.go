// Package runner drives a miner's harness over HTTP: it POSTs one RunRequest
// per dataset case to <harnessURL>/run and collects the RunResponses. Per-case
// failures (timeout, non-200, bad JSON) are recorded as empty responses so a
// single bad case never aborts the whole evaluation.
package runner

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strconv"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/netguard"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// perCaseTimeout bounds a single /run call.
const perCaseTimeout = 120 * time.Second

// v7PerCaseTimeout bounds a single /run call for bench_version >= 7. The v7
// difficulty release ships ~10x denser haystacks and longer multi-hop chains,
// so a legitimate case round-trip (locked-model reasoning included) needs more
// headroom than the historical 120s. Client-side only: the request bytes on
// the wire are unchanged. Overridable via DITTOBENCH_V7_CASE_TIMEOUT (a Go
// duration string such as "8m").
var v7PerCaseTimeout = envDuration("DITTOBENCH_V7_CASE_TIMEOUT", 5*time.Minute)

// perCaseTimeoutFor selects the per-case deadline for a bench version. Pre-v7
// versions keep the frozen 120s so historical replay timing envelopes are
// untouched.
func perCaseTimeoutFor(benchVersion int) time.Duration {
	if benchVersion >= 7 {
		return v7PerCaseTimeout
	}
	return perCaseTimeout
}

// healthTimeout bounds the /health probe.
const healthTimeout = 10 * time.Second

// seedTimeout bounds the /seed call (embedding a haystack can take a while).
// A CPU-only validator embedding a full "small" haystack — hundreds of long
// documents through a local embeddings server (e.g. Ollama) — can legitimately
// take several minutes, so the default is overridable via DITTOBENCH_SEED_TIMEOUT
// (a Go duration string such as "20m"). Values <= 0 or unparseable fall back to
// the default.
var seedTimeout = envDuration("DITTOBENCH_SEED_TIMEOUT", 5*time.Minute)

// ErrSeedStoreLockTimeout is the starter harness's exact bounded failure when
// /seed could not acquire its shared store lock for 600 seconds. It is typed at
// the HTTP boundary so arbitrary downstream error prose cannot mint a no-fault
// validator retry.
var ErrSeedStoreLockTimeout = errors.New("seed store lock timeout")

const seedStoreLockTimeoutMessage = "seed exceeded 600s, aborted to release the store lock"

// envDuration reads a Go duration from key, returning def when unset, empty,
// unparseable, or non-positive.
func envDuration(key string, def time.Duration) time.Duration {
	if v := os.Getenv(key); v != "" {
		if d, err := time.ParseDuration(v); err == nil && d > 0 {
			return d
		}
	}
	return def
}

// client is used for caller-supplied harness URLs. It defaults to a guarded
// client (no private targets); Configure relaxes it only for explicit local dev.
var client = netguard.Client(false)
var sandboxClient = netguard.Client(true)

type sandboxContextKey struct{}

// Configure sets the caller-supplied harness URL policy. allowPrivate=true is
// for explicit local development; hosted deployments leave it false. Validator-
// owned Docker sandboxes use TrustSandbox instead. Call once at startup.
func Configure(allowPrivate bool) { client = netguard.Client(allowPrivate) }

// TrustSandbox marks requests to a harness URL returned by the validator's own
// Sandbox.Run implementation. Those URLs are loopback-bound by construction,
// so they need a private-address-capable client. Caller-supplied harness URLs
// never receive this context and remain protected by the SSRF guard.
func TrustSandbox(ctx context.Context) context.Context {
	return context.WithValue(ctx, sandboxContextKey{}, struct{}{})
}

func clientFor(ctx context.Context) *http.Client {
	if _, ok := ctx.Value(sandboxContextKey{}).(struct{}); ok {
		return sandboxClient
	}
	return client
}

// Health probes <harnessURL>/health and returns nil on a 2xx response.
func Health(ctx context.Context, harnessURL string) error {
	ctx, cancel := context.WithTimeout(ctx, healthTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, harnessURL+"/health", nil)
	if err != nil {
		return fmt.Errorf("build health request: %w", err)
	}
	resp, err := clientFor(ctx).Do(req)
	if err != nil {
		return fmt.Errorf("harness unreachable: %w", err)
	}
	defer resp.Body.Close()
	io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<16))

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("harness health returned %d", resp.StatusCode)
	}
	return nil
}

// SupportsCaseScopedInference reads the additive harness health capability.
// Any legacy, malformed, or non-JSON 2xx response safely means unsupported;
// callers then retain serial case windows.
func SupportsCaseScopedInference(ctx context.Context, harnessURL string) bool {
	ctx, cancel := context.WithTimeout(ctx, healthTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, harnessURL+"/health", nil)
	if err != nil {
		return false
	}
	resp, err := clientFor(ctx).Do(req)
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return false
	}
	var health struct {
		Capabilities []string `json:"capabilities"`
	}
	if json.NewDecoder(io.LimitReader(resp.Body, 1<<16)).Decode(&health) != nil {
		return false
	}
	for _, capability := range health.Capabilities {
		if capability == "case_scoped_inference_v1" {
			return true
		}
	}
	return false
}

// WaitHealthy polls <harnessURL>/health until it returns 2xx or the deadline
// passes. Used by the sandbox path to wait for a freshly started container to
// come up before spending the evaluation on it.
func WaitHealthy(ctx context.Context, harnessURL string, timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	var last error
	for time.Now().Before(deadline) {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		if last = Health(ctx, harnessURL); last == nil {
			return nil
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(time.Second):
		}
	}
	if last == nil {
		last = fmt.Errorf("timeout")
	}
	return fmt.Errorf("harness not healthy after %s: %w", timeout, last)
}

// RunHarness evaluates every case in ds against the harness and returns a map
// keyed by case ID. The returned map always contains an entry for every case
// (failed cases get a zero-value RunResponse). The error is non-nil only for a
// fundamentally unusable input (none currently); per-case errors are swallowed.
func RunHarness(ctx context.Context, harnessURL string, ds protocol.Dataset, tools []protocol.ToolDefinition) (map[string]protocol.RunResponse, error) {
	out := make(map[string]protocol.RunResponse, len(ds.ToolCases))

	for _, c := range ds.ToolCases {
		resp, err := runOne(ctx, harnessURL, c, tools, CaseOptions{})
		if err != nil {
			// Record an empty response; scorer treats absence/zero as a miss.
			out[c.ID] = protocol.RunResponse{}
			continue
		}
		out[c.ID] = resp
	}
	return out, nil
}

// v7SeedTimeout bounds a /seed call for bench_version >= 7 haystacks, which
// are ~10x denser than the historical suites and can take a CPU-only embedder
// well past the 5-minute default. An explicit DITTOBENCH_SEED_TIMEOUT still
// applies to every version via the max() in seedTimeoutFor. Overridable via
// DITTOBENCH_V7_SEED_TIMEOUT.
var v7SeedTimeout = envDuration("DITTOBENCH_V7_SEED_TIMEOUT", 15*time.Minute)

// seedTimeoutFor selects the /seed deadline for a bench version: pre-v7 keeps
// the historical timeout; v7 gets at least v7SeedTimeout (an operator-raised
// DITTOBENCH_SEED_TIMEOUT wins when larger).
func seedTimeoutFor(benchVersion int) time.Duration {
	if benchVersion >= 7 && v7SeedTimeout > seedTimeout {
		return v7SeedTimeout
	}
	return seedTimeout
}

// Seed POSTs a fresh haystack to <harnessURL>/seed and returns the loaded
// counts the harness reports. Used by the run_size pipeline before memory cases.
func Seed(ctx context.Context, harnessURL string, req protocol.SeedRequest) (protocol.SeedResponse, error) {
	return SeedForVersion(ctx, harnessURL, req, 0)
}

// SeedForVersion is Seed with the bench version's deadline envelope. The wire
// bytes are identical to Seed for every version — only the client-side timeout
// differs (v7 haystacks are much larger).
func SeedForVersion(ctx context.Context, harnessURL string, req protocol.SeedRequest, benchVersion int) (protocol.SeedResponse, error) {
	ctx, cancel := context.WithTimeout(ctx, seedTimeoutFor(benchVersion))
	defer cancel()

	buf, err := marshalSeedRequest(req, benchVersion)
	if err != nil {
		return protocol.SeedResponse{}, fmt.Errorf("marshal seed request: %w", err)
	}
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, harnessURL+"/seed", bytes.NewReader(buf))
	if err != nil {
		return protocol.SeedResponse{}, fmt.Errorf("build seed request: %w", err)
	}
	httpReq.Header.Set("Content-Type", "application/json")

	httpResp, err := clientFor(ctx).Do(httpReq)
	if err != nil {
		return protocol.SeedResponse{}, fmt.Errorf("post /seed: %w", err)
	}
	defer httpResp.Body.Close()

	body, err := io.ReadAll(io.LimitReader(httpResp.Body, 1<<20))
	if err != nil {
		return protocol.SeedResponse{}, fmt.Errorf("read /seed body: %w", err)
	}
	if httpResp.StatusCode < 200 || httpResp.StatusCode >= 300 {
		var payload struct {
			Error string `json:"error"`
		}
		if httpResp.StatusCode == http.StatusServiceUnavailable &&
			json.Unmarshal(body, &payload) == nil &&
			payload.Error == seedStoreLockTimeoutMessage {
			return protocol.SeedResponse{}, fmt.Errorf("%w: %s", ErrSeedStoreLockTimeout, payload.Error)
		}
		return protocol.SeedResponse{}, fmt.Errorf("/seed returned %d: %s", httpResp.StatusCode, string(body))
	}
	var out protocol.SeedResponse
	if err := json.Unmarshal(body, &out); err != nil {
		return protocol.SeedResponse{}, fmt.Errorf("decode /seed response: %w", err)
	}
	return out, nil
}

// marshalSeedRequest keeps every historical request byte-identical while V9
// removes wave metadata and makes all collection fields explicit arrays. This
// is a wire-only shape; the canonical SeedRequest remains the artifact model.
func marshalSeedRequest(req protocol.SeedRequest, benchVersion int) ([]byte, error) {
	if benchVersion < protocol.BenchVersionV9 {
		return json.Marshal(req)
	}
	type v9SeedRequest struct {
		UserID   string                 `json:"user_id"`
		Pairs    []protocol.MemoryPair  `json:"pairs"`
		Subjects []protocol.Subject     `json:"subjects"`
		Links    []protocol.SubjectLink `json:"links"`
	}
	if req.Pairs == nil {
		req.Pairs = []protocol.MemoryPair{}
	}
	if req.Subjects == nil {
		req.Subjects = []protocol.Subject{}
	}
	if req.Links == nil {
		req.Links = []protocol.SubjectLink{}
	}
	return json.Marshal(v9SeedRequest{UserID: req.UserID, Pairs: req.Pairs, Subjects: req.Subjects, Links: req.Links})
}

// CaseOptions carries the optional observed-execution per-case wire fields: a
// validator-served mock tool-execution endpoint the harness should route its
// non-memory tool calls through (so the validator observes the trajectory), and
// the user_id the case's memory graph was seeded under (multi-graph isolation).
// The zero value reproduces self-report behavior (no endpoint, default user).
type CaseOptions struct {
	ToolEndpoint        string
	UserID              string
	BenchVersion        int
	InferenceBaseURL    string
	CaseScopedInference bool
}

// AttemptTelemetry is validator-observed execution evidence for one HTTP
// attempt. It deliberately records a bounded outcome class rather than the raw
// error, which may contain host paths or network details that do not belong in
// the public transcript.
type AttemptTelemetry struct {
	Attempt    int    `json:"attempt"`
	DurationMs int64  `json:"duration_ms"`
	Outcome    string `json:"outcome"`
	HTTPStatus int    `json:"http_status,omitempty"`
}

// CaseExecution is trusted runner telemetry for a single benchmark question.
// It is published beside that question in the content-addressed transcript;
// none of these values come from the harness response.
type CaseExecution struct {
	Attempts                 []AttemptTelemetry               `json:"attempts"`
	TotalDurationMs          int64                            `json:"total_duration_ms"`
	TimedOut                 bool                             `json:"timed_out,omitempty"`
	Cancelled                bool                             `json:"cancelled,omitempty"`
	TerminalOutcome          string                           `json:"terminal_outcome"`
	ModelInferenceObserved   bool                             `json:"model_inference_observed,omitempty"`
	ModelAttributionComplete bool                             `json:"model_attribution_complete,omitempty"`
	ToolProvenance           *protocol.ToolProvenanceEvidence `json:"tool_provenance,omitempty"`
	// RelayInjectedDelayMs is the total delay-fingerprint hold the trusted
	// relay imposed inside this case's window; RelayDelayConsistent reports
	// whether the case's wall time can contain it (nil = unmeasured). Shadow
	// evidence only: published for calibration, never consulted by scoring.
	RelayInjectedDelayMs int64 `json:"relay_injected_delay_ms,omitempty"`
	RelayDelayConsistent *bool `json:"relay_delay_consistent,omitempty"`
	// ModelCounterfactualObserved reports that the trusted relay administered the
	// Bench v12 causal-dependence counterfactual for this case: it re-ran the case
	// under FULL model ablation (a completion with no usable content) and graded the
	// result against the same expected answer. ModelCounterfactualDependent is the
	// correctness-based verdict — true when the clean run was correct and the
	// ablated run was INCORRECT (correctness collapsed, so the answer genuinely
	// depended on the model), false when the clean run was correct and the ablated
	// run was STILL correct (the agent recovered the answer with no working model,
	// the launderer signature). It is nil in two distinct cases distinguished by
	// ModelCounterfactualObserved: Observed=false means the counterfactual was NOT
	// administered (pending); Observed=true with a nil verdict means it WAS
	// administered but the clean run was already incorrect, so the case is excluded
	// from the dependent/independent tally while still counting toward
	// slice-attribution completeness. Populated only by the v12 relay path; consumed
	// at cmd/dittobench-api/v9_base.go v9DependenceTelemetryForVersion, the single
	// model-dependence integration point. Until the relay sets these, the v12 gate
	// fails closed.
	ModelCounterfactualObserved  bool  `json:"model_counterfactual_observed,omitempty"`
	ModelCounterfactualDependent *bool `json:"model_counterfactual_dependent,omitempty"`
	// AnswerStuffObserved reports that the trusted relay captured this case's
	// ordered clean-pass model I/O (bounded/normalized) and the scorer settled the
	// Bench v12 Class-D answer-stuffing provenance check for it. AnswerStuffed is
	// the verdict for a COMPUTED-answer case: true when the finished answer value
	// appeared in a model INPUT the harness sent BEFORE it appeared in any model
	// COMPLETION (the harness fed the model its own computed answer to copy), false
	// when the value's provenance is clean (only ever in a completion, or first
	// produced by an earlier completion and reused later -- legit multi-turn). It
	// is nil in two distinct cases distinguished by AnswerStuffObserved:
	// Observed=false means the I/O was NOT captured/settled (pending); Observed=true
	// with a nil verdict means the case is NOT part of the computed slice (its
	// answer is verbatim-recall, or it never reached the model), so it is kept out
	// of the stuffed/clean tally while still counting toward attribution
	// completeness. The provenance value tokens are never stored here: only the
	// bounded verdict is published, so the answer key and prompt never enter the
	// transcript. Populated only by the v12 relay/scorer path; consumed at
	// cmd/dittobench-api/v9_base.go v12AnswerStuffingTelemetry. Until the relay and
	// scorer settle these, the v12 answer-stuffing gate fails OPEN (a detection
	// gate never penalizes an honest run for missing capture).
	AnswerStuffObserved bool  `json:"answer_stuff_observed,omitempty"`
	AnswerStuffed       *bool `json:"answer_stuffed,omitempty"`
	// AnswerStuffLoose is the "loose" answer-stuffing verdict for a COMPUTED case
	// (an answer that is not verbatim-recall), set REGARDLESS of whether the answer
	// value also appears elsewhere in the run's seeded memory: true when the finished
	// answer value appeared in a model INPUT before any model COMPLETION. It is a
	// SUPERSET of AnswerStuffed -- a case whose answer value ALSO appears in memory (a
	// coinciding-value stuffer) cannot be auto-proven against RAG, so AnswerStuffed
	// stays nil (excluded from the provable auto-gate) while this loose verdict is
	// still recorded. Aggregated at cmd/dittobench-api/v9_base.go
	// v12AnswerStuffingTelemetry into the loose systematic-review signal, which only
	// ROUTES a run to human review (never an auto-zero). It is set only for a settled,
	// fully-captured, computed, model-reached case by the v12 scorer path; nil
	// otherwise. Populated only for bench_version>=12.
	AnswerStuffLoose *bool `json:"answer_stuff_loose,omitempty"`
	// AnswerStuffReviewRequired marks a COMPUTED, model-reached case whose clean-pass
	// model I/O was captured but overflowed the per-side value-token ceiling (a
	// prompt larger than the model's full context window -- pathological, not
	// anything a legitimate deep-RAG agent can produce). Provenance could not be
	// settled, so instead of leaving the case pending (fail OPEN, the prior
	// truncation bug) or auto-zeroing (which would risk an honest giant-context
	// agent) the scorer sets this bit; the v12 answer-stuffing gate then routes the
	// whole run to human REVIEW with a full factor (ResultReviewRequired). It is set
	// only alongside AnswerStuffObserved=true with a nil AnswerStuffed verdict, and
	// only by the v12 relay/scorer path; consumed at cmd/dittobench-api/v9_base.go
	// v12AnswerStuffingTelemetry. Prompt size is never itself a signal -- this bit is
	// a last-resort safety net, never expected to fire on normal full-context use.
	AnswerStuffReviewRequired bool `json:"answer_stuff_review_required,omitempty"`
}

// RunCase POSTs one tool OR memory case to <harnessURL>/run. For a tool case,
// pass c (the toolcase) and prompt=c.Prompt; for a memory case, pass a synthetic
// ToolCase with the question as the prompt. Exported so the pipeline can run +
// score cases one at a time (appending partial results). opts carries the
// optional observed-execution fields (tool_endpoint, user_id).
func RunCase(ctx context.Context, harnessURL, caseID, prompt string, tools []protocol.ToolDefinition, opts CaseOptions) (protocol.RunResponse, error) {
	resp, _, err := RunCaseWithTelemetry(ctx, harnessURL, caseID, prompt, tools, opts)
	return resp, err
}

// RunCaseWithTelemetry is RunCase plus validator-observed attempt, timing, and
// terminal-outcome evidence. Callers that publish a transcript should prefer
// this form; the legacy RunCase wrapper remains for compatibility.
func RunCaseWithTelemetry(ctx context.Context, harnessURL, caseID, prompt string, tools []protocol.ToolDefinition, opts CaseOptions) (protocol.RunResponse, CaseExecution, error) {
	return runOneWithTelemetry(ctx, harnessURL, protocol.ToolCase{ID: caseID, Prompt: prompt}, tools, opts)
}

// runAttemptBackoff is the fixed pause before the Nth retry (index 0 is the
// pause before the first retry). No jitter: the delay only bounds retry rate,
// and scores never depend on timing, so determinism is not affected.
var runAttemptBackoff = []time.Duration{250 * time.Millisecond, 750 * time.Millisecond}

// runAttempts is the total number of /run attempts per case (1 = no retry).
// A harness under concurrent load, or the model relay, can return a transient
// 429/5xx or drop the connection; without a retry that case silently scores a
// miss, which becomes common once cases run in parallel. Retries only fire on
// transient failures and stay inside the per-case deadline. Overridable so an
// operator can disable retries (set 1) or widen them.
var runAttempts = envInt("DITTOBENCH_RUN_ATTEMPTS", 3)

// envInt reads a positive int from key, returning def when unset or invalid.
func envInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			return n
		}
	}
	return def
}

func runOne(ctx context.Context, harnessURL string, c protocol.ToolCase, tools []protocol.ToolDefinition, opts CaseOptions) (protocol.RunResponse, error) {
	resp, _, err := runOneWithTelemetry(ctx, harnessURL, c, tools, opts)
	return resp, err
}

func runOneWithTelemetry(ctx context.Context, harnessURL string, c protocol.ToolCase, tools []protocol.ToolDefinition, opts CaseOptions) (protocol.RunResponse, CaseExecution, error) {
	started := time.Now()
	execution := CaseExecution{Attempts: make([]AttemptTelemetry, 0, runAttempts)}
	ctx, cancel := context.WithTimeout(ctx, perCaseTimeoutFor(opts.BenchVersion))
	defer cancel()
	finish := func(outcome string, err error) (protocol.RunResponse, CaseExecution, error) {
		execution.TotalDurationMs = time.Since(started).Milliseconds()
		execution.TerminalOutcome = outcome
		execution.TimedOut = ctx.Err() == context.DeadlineExceeded
		execution.Cancelled = ctx.Err() == context.Canceled
		return protocol.RunResponse{}, execution, err
	}

	wireBenchVersion := harnessWireBenchVersion(opts.BenchVersion)
	reqBody := protocol.RunRequest{
		CaseID:           c.ID,
		SystemPrompt:     "You are Ditto, a helpful assistant with access to tools. Call a tool only when it is the right action for the user's request.",
		UserInput:        c.Prompt,
		Tools:            tools,
		BenchVersion:     wireBenchVersion,
		ToolEndpoint:     opts.ToolEndpoint,
		UserID:           opts.UserID,
		InferenceBaseURL: opts.InferenceBaseURL,
	}
	buf, err := json.Marshal(reqBody)
	if err != nil {
		return finish("request_encode_error", fmt.Errorf("marshal run request: %w", err))
	}

	attempts := runAttempts
	if attempts < 1 {
		attempts = 1
	}
	var lastErr error
	for attempt := 0; attempt < attempts; attempt++ {
		if attempt > 0 {
			// Back off before a retry, but abandon if the per-case deadline is up.
			pause := runAttemptBackoff[min(attempt-1, len(runAttemptBackoff)-1)]
			select {
			case <-ctx.Done():
				outcome := "cancelled"
				if ctx.Err() == context.DeadlineExceeded {
					outcome = "timeout"
				}
				return finish(outcome, lastErr)
			case <-time.After(pause):
			}
		}
		resp, attemptTelemetry, retryable, err := runAttempt(ctx, harnessURL, buf)
		attemptTelemetry.Attempt = attempt + 1
		execution.Attempts = append(execution.Attempts, attemptTelemetry)
		if err == nil {
			execution.TotalDurationMs = time.Since(started).Milliseconds()
			execution.TerminalOutcome = "success"
			return resp, execution, nil
		}
		lastErr = err
		// A non-retryable failure (a 4xx other than 429, or a valid 200 the
		// harness returned as unparseable) will not change on a retry.
		if !retryable {
			return finish(attemptTelemetry.Outcome, err)
		}
	}
	outcome := "retry_exhausted"
	if len(execution.Attempts) > 0 {
		outcome = execution.Attempts[len(execution.Attempts)-1].Outcome
	}
	return finish(outcome, lastErr)
}

// harnessWireBenchVersion keeps unreleased scorer revisions behind the latest
// public harness contract. Bench v10 changes the validator-owned dataset,
// projection, and grader, but it does not require miners to advertise or branch
// on an unreleased protocol number. Sending 10 here would let an otherwise
// correct v9 harness fail closed before exercising any v10 case and turn version
// negotiation into a trivial difficulty signal.
func harnessWireBenchVersion(benchVersion int) int {
	if benchVersion == protocol.BenchVersionV10 {
		return protocol.BenchVersionV9
	}
	if benchVersion >= protocol.BenchVersionV7 {
		return benchVersion
	}
	return 0
}

// runAttempt makes one POST /run. retryable is true when the failure is
// transient (connection error, 429, or 5xx) and a fresh attempt could succeed.
func runAttempt(ctx context.Context, harnessURL string, buf []byte) (protocol.RunResponse, AttemptTelemetry, bool, error) {
	started := time.Now()
	telemetry := AttemptTelemetry{}
	finish := func(outcome string, status int) AttemptTelemetry {
		telemetry.DurationMs = time.Since(started).Milliseconds()
		telemetry.Outcome = outcome
		telemetry.HTTPStatus = status
		return telemetry
	}
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, harnessURL+"/run", bytes.NewReader(buf))
	if err != nil {
		return protocol.RunResponse{}, finish("request_build_error", 0), false, fmt.Errorf("build run request: %w", err)
	}
	httpReq.Header.Set("Content-Type", "application/json")

	httpResp, err := clientFor(ctx).Do(httpReq)
	if err != nil {
		// A network/connection error is transient unless the context itself is
		// done (deadline or cancellation), in which case a retry cannot help.
		outcome := "connection_error"
		if ctx.Err() == context.DeadlineExceeded {
			outcome = "timeout"
		} else if ctx.Err() == context.Canceled {
			outcome = "cancelled"
		}
		return protocol.RunResponse{}, finish(outcome, 0), ctx.Err() == nil, fmt.Errorf("post /run: %w", err)
	}
	defer httpResp.Body.Close()

	body, err := io.ReadAll(io.LimitReader(httpResp.Body, 4<<20))
	if err != nil {
		outcome := "response_read_error"
		if ctx.Err() == context.DeadlineExceeded {
			outcome = "timeout"
		} else if ctx.Err() == context.Canceled {
			outcome = "cancelled"
		}
		return protocol.RunResponse{}, finish(outcome, httpResp.StatusCode), ctx.Err() == nil, fmt.Errorf("read /run body: %w", err)
	}
	if httpResp.StatusCode < 200 || httpResp.StatusCode >= 300 {
		retryable := httpResp.StatusCode == http.StatusTooManyRequests || httpResp.StatusCode >= 500
		outcome := "client_error"
		if httpResp.StatusCode == http.StatusTooManyRequests {
			outcome = "rate_limited"
		} else if httpResp.StatusCode >= 500 {
			outcome = "server_error"
		}
		return protocol.RunResponse{}, finish(outcome, httpResp.StatusCode), retryable, fmt.Errorf("/run returned %d", httpResp.StatusCode)
	}

	var out protocol.RunResponse
	if err := json.Unmarshal(body, &out); err != nil {
		return protocol.RunResponse{}, finish("invalid_response", httpResp.StatusCode), false, fmt.Errorf("decode /run response: %w", err)
	}
	// Measure latency validator-side (the /run round trip) and override any
	// self-reported value: a harness-supplied latency_ms is untrusted and must
	// never reach a score.
	out.LatencyMs = time.Since(started).Milliseconds()
	return out, finish("success", httpResp.StatusCode), false, nil
}
