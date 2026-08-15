package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/llm"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// The nine-into-one collapse this file exists to prevent.
//
// Every finalize failure used to produce exactly one envelope --
// validator_infrastructure / model_relay_unavailable / retryable:true -- which
// is the platform's NO-FAULT class: it mints a retry grant, RAISES the attempt
// cap, and re-leases. Several of the conditions funnelled into it are entirely
// the harness's doing, so an agent that failed the same way every run re-leased
// itself without bound, holding validator slots and never scoring.
//
// The table below is the whole contract in one place: for each condition, which
// class it lands in and why. A change that moves a row is a change to who pays
// for it, and should have to edit this test to do so.
func TestFinalizeFailureAttributionContract(t *testing.T) {
	for _, testCase := range []struct {
		name      string
		err       error
		kind      string
		code      string
		retryable bool
		why       string
	}{
		{
			name: "relay unreachable", err: errors.New("relay unreachable"),
			kind: "validator_infrastructure", code: "model_relay_unavailable", retryable: true,
			why: "the validator's own relay; the harness cannot reach it",
		},
		{
			name: "snapshot unreadable", err: errors.New("inference session unavailable"),
			kind: "validator_infrastructure", code: "model_relay_unavailable", retryable: true,
			why: "broker state, not harness behaviour",
		},
		{
			name: "relay restarted", err: errors.New("relay restarted during benchmark"),
			kind: "validator_infrastructure", code: "model_relay_unavailable", retryable: true,
			why: "accounting reset underneath the run",
		},
		{
			name: "provider fault", err: errors.New("relay recorded 1 upstream failure(s) during benchmark"),
			kind: "validator_infrastructure", code: "model_relay_unavailable", retryable: true,
			why: "upstream provider; nothing the miner controls",
		},
		{
			name: "grant revoked", err: errors.New("platform declined this run's inference grant 1 time(s) during benchmark (lease revoked, deadline rewritten, or budget exhausted) — not an upstream provider fault"),
			kind: "validator_infrastructure", code: "model_relay_unavailable", retryable: true,
			why: "the platform acted unilaterally; it must never start billing a miner",
		},
		{
			name: "incomplete provider usage", err: errors.New("benchmark v7 requires complete provider token usage"),
			kind: "validator_infrastructure", code: "model_relay_unavailable", retryable: true,
			why: "the provider omitted usage; the harness cannot make it appear",
		},
		{
			name: "lane saturated", err: fmt.Errorf("%w: 1 inference call(s) spent their whole backpressure budget", errInferenceLaneSaturated),
			kind: "validator_infrastructure", code: "model_relay_unavailable", retryable: true,
			why: "DELIBERATELY still no-fault, and deliberately NOT a new code: a new infrastructure code is scoring_error on every validator that predates it",
		},
		{
			name: "agent spent its allowance", err: fmt.Errorf("%w: the harness exhausted its own inference allowance 3 time(s)", errAgentInferenceDeclined),
			kind: "sandbox_failure", code: "inference_allowance_exhausted", retryable: false,
			why: "the lease was alive and the platform healthy; the agent spent what it was given",
		},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			failure := relayFinalizeFailure(testCase.err)
			if failure.Kind != testCase.kind || failure.Code != testCase.code ||
				failure.Retryable != testCase.retryable {
				t.Fatalf("%s\nwant %s/%s retryable=%v\ngot  %+v",
					testCase.why, testCase.kind, testCase.code, testCase.retryable, failure)
			}
		})
	}
}

// THE GUARD, in the direction that matters for THIS change.
//
// #118's guard ran the other way: its default was terminal, it was moving
// conditions INTO the no-fault class, so the thing to protect was "nothing
// reaches no-fault by falling through a default". Here the default IS no-fault
// and conditions are moving OUT of it, so the mirror-image property is what
// needs a guard: nothing reaches the AGENT class by falling through, and in
// particular nothing reaches it via text.
//
// This matters more than the arrow direction suggests. The agent class costs a
// miner one of a small number of finite attempts, and the strings below all
// flow through this process from places a miner influences -- harness stderr,
// harness-authored error messages, a provider echoing a request back. If any of
// them could mint `sandbox_failure`, a competitor's harness output could bill
// somebody else's run, and an operator's own log line could bill a real
// platform outage to whoever happened to be running.
func TestUnrecognisedFinalizeFailuresCannotReachTheAgentClass(t *testing.T) {
	for _, err := range []error{
		nil,
		errors.New(""),
		errors.New("relay unreachable"),
		errors.New("relay health malformed"),
		errors.New("relay accounting version changed during benchmark"),
		errors.New("context deadline exceeded"),
		// The sentinels' own words, verbatim, as plain prose. A substring match
		// would let a harness print either on stderr and change its own
		// verdict; `errors.Is` cannot be forged from text.
		errors.New("agent-attributable inference decline"),
		errors.New("platform inference lane saturated"),
		errors.New("the harness exhausted its own inference allowance 400 time(s)"),
		errors.New("inference_allowance_exhausted"),
		errors.New("sandbox_failure: inference_allowance_exhausted retryable=false"),
		// A harness echoing the platform's decline envelope back at the relay.
		errors.New(`{"error_code": 4102, "detail": "budget exhausted"}`),
		errors.New("platform declined the inference grant (429: the lease spent its token budget)"),
	} {
		name := "nil"
		if err != nil {
			name = err.Error()
		}
		t.Run(name, func(t *testing.T) {
			failure := relayFinalizeFailure(err)
			if failure.Kind == "sandbox_failure" || !failure.Retryable {
				t.Fatalf(
					"an unrecognised failure reached the AGENT class and would spend a miner's attempt: %+v",
					failure,
				)
			}
		})
	}
}

// The forgery bound stated as its own property, on the one input a miner most
// directly controls: the bytes of a platform error body. `platformDeclineCode`
// is the only way harness-adjacent bytes can reach the attribution table, so a
// body a miner could induce must not be able to produce an agent verdict for a
// condition that is not theirs -- and, symmetrically, must not be able to hide
// a real one behind an unparseable envelope.
func TestDeclineAttributionCannotBeForgedFromABody(t *testing.T) {
	for _, testCase := range []struct {
		name  string
		body  string
		agent bool
	}{
		{name: "genuine budget decline", body: `{"error_code": 4102}`, agent: true},
		{name: "genuine token budget decline", body: `{"error_code": 4104}`, agent: true},
		{name: "genuine oversized reservation", body: `{"error_code": 4109}`, agent: true},
		{name: "revoked grant", body: `{"error_code": 4101}`, agent: false},
		{name: "expired lease", body: `{"error_code": 4105}`, agent: false},
		{name: "unattributed", body: `{"error_code": 4100}`, agent: false},
		// Codes the platform emits that the broker deliberately does NOT
		// attribute to the agent: this broker mints every nonce itself and
		// overwrites the caller's model before forwarding, so a harness cannot
		// cause either. Billing them would be the mirror-image mistake.
		{name: "nonce replayed is the broker's", body: `{"error_code": 4106}`, agent: false},
		{name: "model not permitted is the ticket's", body: `{"error_code": 4107}`, agent: false},
		{name: "grant never exchanged is the broker's", body: `{"error_code": 4108}`, agent: false},
		// Backpressure never reaches the denial path at all, and is not the
		// agent's on any path.
		{name: "at capacity", body: `{"error_code": 4103}`, agent: false},
		// Anything outside the contract says nothing, and "nothing" is never
		// agent-fault. A build that met a code from a newer platform must keep
		// the run's grant rather than guess at a class.
		{name: "unknown future code", body: `{"error_code": 4199}`, agent: false},
		{name: "an unrelated 4xx envelope", body: `{"error_code": 4001}`, agent: false},
		{name: "no code", body: `{"detail": "budget exhausted"}`, agent: false},
		{name: "empty", body: ``, agent: false},
		{name: "not json", body: `budget exhausted`, agent: false},
		{name: "prose naming the code", body: `error_code 4102 budget exhausted`, agent: false},
		{name: "string not number", body: `{"error_code": "4102"}`, agent: false},
		{name: "nested out of contract", body: `{"error": {"error_code": 4102}}`, agent: false},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			if got := declineIsAgentFault(platformDeclineCode([]byte(testCase.body))); got != testCase.agent {
				t.Fatalf("declineIsAgentFault(%q) = %v, want %v", testCase.body, got, testCase.agent)
			}
		})
	}
}

// Every code the platform can emit must be both parseable and renderable here.
// The broker recognised only 4100-4103 while the platform had been emitting
// 4100-4109 for months, so 4104 TOKEN_BUDGET_EXHAUSTED -- the decline that
// actually ends heavy v7 runs -- arrived, was parsed, and was thrown away on
// the "the platform did not say why" branch.
func TestEveryPlatformDeclineCodeIsRecognised(t *testing.T) {
	for code := 4100; code <= 4109; code++ {
		t.Run(fmt.Sprint(code), func(t *testing.T) {
			body := fmt.Sprintf(`{"error_code": %d}`, code)
			if got := platformDeclineCode([]byte(body)); got != code {
				t.Fatalf("platformDeclineCode(%d) = %d; a code the platform emits is being discarded", code, got)
			}
			if reason := platformDeclineReason(code); reason == "the platform did not say why" {
				t.Fatalf("code %d parses but renders as unknown", code)
			}
		})
	}
}

// Infrastructure wins ties. This is the property that makes the whole change
// safe in the stated direction: a run is charged to the agent only when EVERY
// denial it saw was agent-attributable. Any platform-caused denial mixed in --
// even one, even against a hundred agent declines -- and the run keeps its
// grant.
func TestAMixedRunKeepsItsGrant(t *testing.T) {
	base := relayHealthSnapshot{Requests: 100, Successes: 100}
	for _, testCase := range []struct {
		name     string
		denials  uint64
		agent    uint64
		expected bool // true == agent class
	}{
		{name: "every denial the agent's", denials: 5, agent: 5, expected: true},
		{name: "one denial the agent's", denials: 1, agent: 1, expected: true},
		{name: "one platform denial among many agent ones", denials: 100, agent: 99, expected: false},
		{name: "no attribution at all", denials: 5, agent: 0, expected: false},
		{name: "half and half", denials: 4, agent: 2, expected: false},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			end := base
			end.GrantDenials = testCase.denials
			end.GrantAgentDeclines = testCase.agent
			err := relayDegradedSince(base, end)
			if err == nil {
				t.Fatal("a run with grant denials was allowed to score")
			}
			if got := errors.Is(err, errAgentInferenceDeclined); got != testCase.expected {
				t.Fatalf("agent-attributed=%v, want %v (%v)", got, testCase.expected, err)
			}
		})
	}
}

// A subset counter cannot exceed its total. If it does, the two were updated
// inconsistently and this must refuse to classify rather than conclude "all
// denials were the agent's" from corrupt arithmetic -- which is exactly the
// shape of input that would turn a bug into a wrongly-billed miner.
func TestAnImpossibleSubsetIsRefusedRatherThanBlamed(t *testing.T) {
	start := relayHealthSnapshot{Requests: 10}
	end := relayHealthSnapshot{Requests: 20, GrantDenials: 2, GrantAgentDeclines: 3}
	err := relayDegradedSince(start, end)
	if err == nil || errors.Is(err, errAgentInferenceDeclined) {
		t.Fatalf("an impossible counter split produced an agent verdict: %v", err)
	}
	if _, err := relayExecutionSince(start, end); err == nil {
		t.Fatal("an impossible counter split produced an execution summary")
	}
}

// Lane saturation keeps its grant. Stated separately from the table above
// because it is the one reclassification in this change that deliberately does
// NOT move to the agent, and a future edit that "finishes the job" by moving it
// should fail here with the reason attached.
func TestLaneSaturationStaysNoFault(t *testing.T) {
	start := relayHealthSnapshot{Requests: 10, Successes: 10}
	end := relayHealthSnapshot{Requests: 20, Successes: 19, InfrastructureFailures: 1, CapacityExhaustions: 1}
	err := relayDegradedSince(start, end)
	if err == nil {
		t.Fatal("a run that exhausted its backpressure budget was allowed to score")
	}
	if !errors.Is(err, errInferenceLaneSaturated) {
		t.Fatalf("saturation was not named: %v", err)
	}
	failure := relayFinalizeFailure(err)
	if failure.Kind != "validator_infrastructure" || !failure.Retryable {
		t.Fatalf("a saturated platform lane was charged to the miner: %+v", failure)
	}
	// The code must stay one every DEPLOYED validator already allowlists.
	// ditto-subnet maps an unrecognised infrastructure code to scoring_error,
	// so a new code here would bill miners for a saturated platform rail on the
	// whole fleet until it caught up -- the bug this change exists to stop,
	// arriving through the rollout instead of through the classifier.
	if failure.Code != "model_relay_unavailable" {
		t.Fatalf("saturation shipped a code old validators charge to the miner: %+v", failure)
	}
	if failure.Diagnostics["relay_cause"] != "inference_lane_saturated" {
		t.Fatalf("saturation is indistinguishable from an unreachable relay: %+v", failure)
	}
}

func TestProviderRecoveryExhaustionStaysNoFaultAndIsNamed(t *testing.T) {
	start := relayHealthSnapshot{Requests: 10, Successes: 10}
	end := relayHealthSnapshot{
		Requests: 11, Successes: 10, InfrastructureFailures: 1,
		RecoveryWaits: 1, RecoveryExhaustions: 1,
	}
	err := relayDegradedSince(start, end)
	if !errors.Is(err, errRelayRecoveryExhausted) {
		t.Fatalf("recovery exhaustion was not named: %v", err)
	}
	failure := relayFinalizeFailure(err)
	if failure.Kind != "validator_infrastructure" || failure.Code != "model_relay_unavailable" || !failure.Retryable {
		t.Fatalf("recovery exhaustion was charged to the miner: %+v", failure)
	}
	if failure.Diagnostics["relay_cause"] != "provider_recovery_exhausted" {
		t.Fatalf("recovery exhaustion diagnostics = %+v", failure.Diagnostics)
	}
}

// Pre-reservation 4xx: the platform refused the harness's request bytes without
// reserving capacity or contacting a provider. Which runs FAIL is unchanged --
// the predicate is still efficiency.ValidUsage -- only who is charged moves.
func TestPreReservationRejectionsAreTheAgents(t *testing.T) {
	incomplete := protocol.TokenUsage{
		Status: "unavailable", Requests: 10, Successes: 8,
		UsageAvailable: 8, UsageUnavailable: 2,
		PromptTokens: 100, CompletionTokens: 10, TotalTokens: 110,
	}
	for _, testCase := range []struct {
		name      string
		execution relayExecutionSummary
		agent     bool
		why       string
	}{
		{
			name:      "every gap is a rejected request",
			execution: relayExecutionSummary{AgentRequestRejections: 2},
			agent:     true,
			why:       "the harness sent 2 requests the platform would not accept",
		},
		{
			name:      "no rejections, so the provider omitted usage",
			execution: relayExecutionSummary{},
			agent:     false,
			why:       "nothing the harness did can make a provider report usage",
		},
		{
			name:      "a provider gap mixed in keeps the grant",
			execution: relayExecutionSummary{AgentRequestRejections: 1},
			agent:     false,
			why:       "infrastructure wins ties, exactly as it does for grant denials",
		},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			err := requireCompleteV7Usage(protocol.BenchVersionV8, incomplete, testCase.execution)
			if err == nil {
				t.Fatalf("incomplete usage was allowed to score (%s)", testCase.why)
			}
			if got := errors.Is(err, errAgentInferenceDeclined); got != testCase.agent {
				t.Fatalf("%s: agent-attributed=%v, want %v", testCase.why, got, testCase.agent)
			}
		})
	}
}

// The narrowness bound in the other direction: a CLEAN run is untouched, and a
// pre-v7 run is untouched, no matter what the new counters say.
func TestACleanRunIsUnaffected(t *testing.T) {
	complete := protocol.TokenUsage{
		Status: "complete", Requests: 10, Successes: 10, UsageAvailable: 10,
		PromptTokens: 100, CompletionTokens: 10, TotalTokens: 110,
	}
	noisy := relayExecutionSummary{AgentRequestRejections: 5, GrantAgentDeclines: 5}
	if err := requireCompleteV7Usage(protocol.BenchVersionV8, complete, noisy); err != nil {
		t.Fatalf("a run with complete usage was failed: %v", err)
	}
	if err := requireCompleteV7Usage(protocol.BenchVersionV6, protocol.TokenUsage{}, noisy); err != nil {
		t.Fatalf("a pre-v7 run was subjected to the v7 usage contract: %v", err)
	}
	start := relayHealthSnapshot{Requests: 10, Successes: 10}
	end := relayHealthSnapshot{Requests: 20, Successes: 20}
	if err := relayDegradedSince(start, end); err != nil {
		t.Fatalf("a clean run was reported as degraded: %v", err)
	}
}

// --- end-to-end through the real broker ------------------------------------

// The pure-function tests above pin the contract; this pins the WIRING. Every
// counter they reason about has to actually be incremented at the right site,
// on both lanes, by a real request against a real (test) platform. A table that
// classifies correctly on a counter nobody increments fixes nothing.
func TestBudgetDeclineIsAttributedToTheAgentEndToEnd(t *testing.T) {
	for _, lane := range []string{"chat", "embedding"} {
		for _, testCase := range []struct {
			name  string
			body  string
			agent bool
		}{
			{name: "token budget exhausted", body: `{"error_code": 4104}`, agent: true},
			{name: "request budget exhausted", body: `{"error_code": 4102}`, agent: true},
			{name: "reservation too large", body: `{"error_code": 4109}`, agent: true},
			{name: "grant revoked", body: `{"error_code": 4101}`, agent: false},
			{name: "lease expired", body: `{"error_code": 4105}`, agent: false},
			// An older platform that sends no code at all keeps the old
			// behaviour exactly: no attribution, grant retained.
			{name: "legacy platform sends no code", body: `{"detail": "denied"}`, agent: false},
		} {
			t.Run(lane+"/"+testCase.name, func(t *testing.T) {
				upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
					w.Header().Set("Content-Type", "application/json")
					w.WriteHeader(http.StatusTooManyRequests)
					_, _ = w.Write([]byte(testCase.body))
				}))
				defer upstream.Close()

				broker := newInferenceBroker(1)
				terminalFailures := 0
				broker.terminalAgentFailure = func(string) { terminalFailures++ }
				broker.sleep = func(context.Context, time.Duration) error { return nil }
				proxyURL := configureBrokerUpstream(broker, upstream)
				prepared := prepareBrokerSession(t, broker)
				activateBrokerSessionFor(t, broker, prepared, proxyURL, "openrouter", "openrouter-route-0123456789abcdef-v1", llm.V7HarnessModel)
				sourceIP := "192.0.2.101"
				if lane == "embedding" {
					sourceIP = "192.0.2.102"
				}
				runID := claimAndBindBrokerSession(t, broker, prepared["session_id"], sourceIP, protocol.BenchVersionV8)
				start, err := broker.snapshot(prepared["session_id"])
				if err != nil {
					t.Fatal(err)
				}

				if lane == "chat" {
					request := httptest.NewRequest(http.MethodPost, "/v1/inference/id/v1/chat/completions", bytes.NewBufferString(`{"model":"openai/gpt-oss-20b"}`))
					request.RemoteAddr = sourceIP + ":4321"
					request.SetPathValue("rest", "v1/chat/completions")
					recorder := httptest.NewRecorder()
					broker.handle(recorder, request)
					// Unchanged harness-visible contract: the run is discarded
					// either way, and its remaining requests must not observe a
					// different gateway mid-benchmark just because blame moved.
					if recorder.Code != http.StatusBadGateway {
						t.Fatalf("harness-visible status=%d, want an unchanged 502", recorder.Code)
					}
				} else {
					if !broker.beginEmbeddingPhase(prepared["session_id"], runID) {
						t.Fatal("failed to admit v7 embedding phase")
					}
					defer broker.endEmbeddingPhase(prepared["session_id"], runID)
					if response := callEmbedding(broker, sourceIP, "hosted text"); response.Code != http.StatusBadGateway {
						t.Fatalf("harness-visible status=%d, want an unchanged 502", response.Code)
					}
				}

				end, err := broker.snapshot(prepared["session_id"])
				if err != nil {
					t.Fatal(err)
				}
				// The total keeps its exact previous meaning on every path, so
				// which runs fail is unchanged by this whole change.
				if end.GrantDenials != 1 {
					t.Fatalf("grant denial was not recorded: %+v", end)
				}
				if end.InfrastructureFailures != 0 {
					t.Fatalf("a lease denial was miscounted as a provider fault: %+v", end)
				}
				wantAgent := uint64(0)
				if testCase.agent {
					wantAgent = 1
				}
				if end.GrantAgentDeclines != wantAgent {
					t.Fatalf("agent attribution=%d, want %d: %+v", end.GrantAgentDeclines, wantAgent, end)
				}
				if terminalFailures != int(wantAgent) {
					t.Fatalf("terminal callbacks=%d, want %d", terminalFailures, wantAgent)
				}

				degraded := relayDegradedSince(start, end)
				if degraded == nil {
					t.Fatal("a denied grant no longer fails the run closed")
				}
				failure := relayFinalizeFailure(degraded)
				if testCase.agent {
					if failure.Kind != "sandbox_failure" || failure.Retryable {
						t.Fatalf("the agent's own exhausted allowance still mints a retry grant: %+v", failure)
					}
					if failure.Code != "inference_allowance_exhausted" {
						t.Fatalf("unexpected agent code: %+v", failure)
					}
				} else {
					if failure.Kind != "validator_infrastructure" || !failure.Retryable {
						t.Fatalf("a platform-caused denial started billing the miner: %+v", failure)
					}
				}
			})
		}
	}
}

func TestTerminalAgentFailureNotificationIsOncePerRun(t *testing.T) {
	broker := newInferenceBroker(1)
	notifications := 0
	broker.terminalAgentFailure = func(runID string) {
		if runID != "run-1" {
			t.Fatalf("notified run %q, want run-1", runID)
		}
		notifications++
	}
	session := &brokerSession{boundRunID: "run-1"}

	broker.notifyTerminalAgentFailure(session)
	broker.notifyTerminalAgentFailure(session)

	if notifications != 1 {
		t.Fatalf("terminal notifications=%d, want 1", notifications)
	}
}

// The pre-reservation 4xx path, end to end. The platform refuses the harness's
// request bytes outright; no reservation is made and no provider is contacted.
func TestPreReservationRejectionIsAttributedToTheAgentEndToEnd(t *testing.T) {
	for _, status := range []int{http.StatusBadRequest, http.StatusForbidden, http.StatusRequestEntityTooLarge} {
		t.Run(fmt.Sprint(status), func(t *testing.T) {
			upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				http.Error(w, "rejected", status)
			}))
			defer upstream.Close()

			broker := newInferenceBroker(1)
			broker.sleep = func(context.Context, time.Duration) error { return nil }
			proxyURL := configureBrokerUpstream(broker, upstream)
			prepared := prepareBrokerSession(t, broker)
			activateBrokerSessionFor(t, broker, prepared, proxyURL, "openrouter", "openrouter-route-0123456789abcdef-v1", llm.V7HarnessModel)
			sourceIP := "192.0.2.103"
			claimAndBindBrokerSession(t, broker, prepared["session_id"], sourceIP, protocol.BenchVersionV8)
			start, err := broker.snapshot(prepared["session_id"])
			if err != nil {
				t.Fatal(err)
			}

			request := httptest.NewRequest(http.MethodPost, "/v1/inference/id/v1/chat/completions", bytes.NewBufferString(`{"model":"openai/gpt-oss-20b"}`))
			request.RemoteAddr = sourceIP + ":4321"
			request.SetPathValue("rest", "v1/chat/completions")
			recorder := httptest.NewRecorder()
			broker.handle(recorder, request)
			if recorder.Code != status {
				t.Fatalf("harness-visible status=%d, want an unchanged %d", recorder.Code, status)
			}

			end, err := broker.snapshot(prepared["session_id"])
			if err != nil {
				t.Fatal(err)
			}
			// Unchanged: this is what still fails the run.
			if end.UsageUnavailable != 1 {
				t.Fatalf("the usage shortfall stopped being recorded: %+v", end)
			}
			if end.AgentRequestRejections != 1 {
				t.Fatalf("a rejected harness request was not attributed: %+v", end)
			}
			if end.InfrastructureFailures != 0 || end.GrantDenials != 0 {
				t.Fatalf("a rejected request leaked into a platform counter: %+v", end)
			}

			execution, err := relayExecutionSince(start, end)
			if err != nil {
				t.Fatal(err)
			}
			usage, err := relayUsageSince(start, end)
			if err != nil {
				t.Fatal(err)
			}
			usageErr := requireCompleteV7Usage(protocol.BenchVersionV8, usage, execution)
			if usageErr == nil {
				t.Fatal("a run with a rejected request was allowed to score")
			}
			failure := relayFinalizeFailure(usageErr)
			if failure.Kind != "sandbox_failure" || failure.Retryable {
				t.Fatalf("the harness's own malformed request still mints a retry grant: %+v", failure)
			}
		})
	}
}

// Capacity exhaustion, end to end, on the lane the operator was just tuning.
// The platform answers 503 + Retry-After forever; the call spends its whole
// bounded wait budget and gives up. It must be counted, named, and STILL keep
// its grant.
func TestCapacityExhaustionIsNamedButKeepsItsGrantEndToEnd(t *testing.T) {
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Retry-After", "1")
		http.Error(w, "at capacity", http.StatusServiceUnavailable)
	}))
	defer upstream.Close()

	broker := newInferenceBroker(1)
	broker.sleep = func(context.Context, time.Duration) error { return nil }
	broker.embeddingRequestTTL = 100 * time.Millisecond
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSessionFor(t, broker, prepared, proxyURL, "openrouter", "openrouter-route-0123456789abcdef-v1", llm.V7HarnessModel)
	sourceIP := "192.0.2.104"
	runID := claimAndBindBrokerSession(t, broker, prepared["session_id"], sourceIP, protocol.BenchVersionV8)
	start, err := broker.snapshot(prepared["session_id"])
	if err != nil {
		t.Fatal(err)
	}
	if !broker.beginEmbeddingPhase(prepared["session_id"], runID) {
		t.Fatal("failed to admit v7 embedding phase")
	}
	defer broker.endEmbeddingPhase(prepared["session_id"], runID)
	if response := callEmbedding(broker, sourceIP, "hosted text"); response.Code != http.StatusBadGateway {
		t.Fatalf("harness-visible status=%d, want 502", response.Code)
	}

	end, err := broker.snapshot(prepared["session_id"])
	if err != nil {
		t.Fatal(err)
	}
	if end.CapacityExhaustions != 1 {
		t.Fatalf("a saturated lane was not recorded: %+v", end)
	}
	if end.GrantAgentDeclines != 0 || end.AgentRequestRejections != 0 {
		t.Fatalf("a saturated platform lane was attributed to the harness: %+v", end)
	}
	degraded := relayDegradedSince(start, end)
	if degraded == nil {
		t.Fatal("an exhausted backpressure budget no longer fails the run closed")
	}
	failure := relayFinalizeFailure(degraded)
	if failure.Kind != "validator_infrastructure" || !failure.Retryable {
		t.Fatalf("a miner was billed for a saturated platform rail: %+v", failure)
	}
	if failure.Code != "model_relay_unavailable" {
		t.Fatalf("saturation shipped a code old validators charge to the miner: %+v", failure)
	}
	if failure.Diagnostics["relay_cause"] != "inference_lane_saturated" {
		t.Fatalf("saturation is still indistinguishable from an unreachable relay: %+v", failure)
	}
}

// The rollout invariant, which is a different property from the classification
// invariant and is easy to satisfy the first and break the second.
//
// ditto-subnet's _sandbox_infrastructure_failure_code returns None for any code
// outside its allowlist, and None becomes fail_job("scoring_error"). So the
// no-fault class is only no-fault on validators that recognise the specific
// code: introducing a NEW infrastructure code silently charges miners on every
// validator that predates the matching release, for as long as the fleet lags.
// It lags by design.
//
// The two directions are therefore NOT symmetric, and this pins the asymmetry:
//
//   - New AGENT codes are skew-safe. An old validator sees
//     sandbox_failure/retryable=false and its terminal default is already the
//     intended outcome, so it needs to know nothing about the code.
//   - New INFRASTRUCTURE codes are not. Every no-fault failure this package can
//     produce must use a code already deployed everywhere.
//
// `model_relay_unavailable` has been in that allowlist since the code existed.
// If a future change wants a genuinely new no-fault code, the validator side
// has to ship and the fleet has to roll FIRST, and this test is where that
// conversation starts.
func TestNoFaultFailuresOnlyUseCodesTheDeployedFleetAllowlists(t *testing.T) {
	// ditto/validator/dittobench.py::_SANDBOX_INFRASTRUCTURE_CODES as deployed
	// on the current fleet (v0.36.0 / v0.34.1).
	deployed := map[string]bool{
		"sandbox_oom":                    true,
		"sandbox_tmpfs_exhausted":        true,
		"sandbox_network_unavailable":    true,
		"model_relay_unavailable":        true,
		"embedding_provider_unavailable": true,
		"seed_store_lock_timeout":        true,
	}
	for _, err := range []error{
		nil,
		errors.New("relay unreachable"),
		errors.New("benchmark v7 requires complete provider token usage"),
		errors.New("relay restarted during benchmark"),
		fmt.Errorf("%w: lane full", errInferenceLaneSaturated),
		fmt.Errorf("%w: allowance spent", errAgentInferenceDeclined),
	} {
		failure := relayFinalizeFailure(err)
		if failure.Kind != "validator_infrastructure" {
			continue // agent-class failures are skew-safe by construction
		}
		if !deployed[failure.Code] {
			t.Fatalf(
				"no-fault code %q is not in the deployed fleet's allowlist; every validator that predates it would charge the miner instead",
				failure.Code,
			)
		}
	}
}
