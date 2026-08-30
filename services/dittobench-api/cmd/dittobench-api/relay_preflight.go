package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/efficiency"
	"github.com/ditto-assistant/dittobench-api/internal/llm"
	"github.com/ditto-assistant/dittobench-api/internal/runner"
	"github.com/ditto-assistant/dittobench-api/internal/store"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

const relayProbeTimeout = 30 * time.Second

type relayHealthSnapshot struct {
	AccountingVersion      int    `json:"accounting_version"`
	Status                 string `json:"status"`
	Requests               uint64 `json:"requests"`
	Successes              uint64 `json:"successes"`
	InfrastructureFailures uint64 `json:"infrastructure_failures"`
	// MinerRecoverableFailures counts exact response-generation failures returned
	// to the miner. A successful later request can complete the run; the scorer
	// does not relabel these as validator infrastructure.
	MinerRecoverableFailures uint64 `json:"miner_recoverable_failures"`
	// GrantDenials counts platform-side refusals to reserve capacity for this
	// ticket's grant (revoked lease, rewritten/passed ticket deadline,
	// exhausted budget, per-ticket concurrency). Deliberately separate from
	// InfrastructureFailures: those are upstream PROVIDER faults, and merging
	// the two makes a validator losing its lease indistinguishable from a
	// provider blip. A legacy relay never reports this.
	GrantDenials uint64 `json:"grant_denials"`
	// GrantAgentDeclines is the SUBSET of GrantDenials the harness caused: it
	// spent the request-count or token allowance its own ticket granted
	// (platform codes 4102/4104), or sent a single request too large to reserve
	// (4109). The lease was alive and the platform healthy in every one of
	// these; nothing the validator or platform did unilaterally lands here.
	//
	// A subset rather than a split so GrantDenials keeps its exact previous
	// meaning: which runs fail is unchanged by this field's existence, only who
	// is charged. A legacy relay reports neither.
	GrantAgentDeclines uint64 `json:"grant_agent_declines"`
	// DeclineEvidenceMismatches is the subset of grant denials where the
	// Platform named an agent allowance code but the broker's independent
	// request/charge upper bounds proved that code impossible.
	DeclineEvidenceMismatches uint64 `json:"decline_evidence_mismatches"`
	// BudgetEvidenceAbsences is the subset of grant denials where the Platform
	// named an agent allowance code but this session had no authenticated
	// request/token budgets. Missing evidence cannot confirm the code.
	BudgetEvidenceAbsences uint64 `json:"budget_evidence_absences"`
	// AgentRequestRejections counts pre-reservation 4xx: the platform refusing
	// the harness's request bytes (schema, size, model) without reserving
	// capacity or contacting a provider. Already fails the run via
	// UsageUnavailable; this exists so finalize can name a rejected request
	// instead of a spent grant.
	AgentRequestRejections uint64 `json:"agent_request_rejections"`
	// CapacityExhaustions counts calls that spent their entire bounded
	// backpressure wait budget and gave up. Reported separately from
	// InfrastructureFailures so a saturated rail is legible, but deliberately
	// still INFRASTRUCTURE: a miner cannot provision the lane.
	CapacityExhaustions uint64 `json:"capacity_exhaustions"`
	// RecoveryWaits counts logical calls that paused after the fast retry
	// window. RecoveryExhaustions is the subset that still could not complete.
	RecoveryWaits       uint64 `json:"recovery_waits"`
	RecoveryExhaustions uint64 `json:"recovery_exhaustions"`
	// EmbeddingRetries is the v7 embedding lane's retry ledger, the counterpart
	// of UpstreamAttempts-minus-Requests on the chat lane.
	EmbeddingRetries    uint64 `json:"embedding_retries"`
	CallerCancellations uint64 `json:"caller_cancellations"`
	UpstreamAttempts    uint64 `json:"upstream_attempts"`
	Provider            string `json:"provider"`
	ProfileRevision     string `json:"profile_revision"`
	Model               string `json:"model"`
	UsageAvailable      uint64 `json:"usage_available"`
	UsageUnavailable    uint64 `json:"usage_unavailable"`
	PromptTokens        uint64 `json:"prompt_tokens"`
	PromptBytes         uint64 `json:"prompt_bytes"`
	CompletionTokens    uint64 `json:"completion_tokens"`
	ProviderLatencyMs   uint64 `json:"provider_latency_ms"`
	TTFTStatus          string `json:"ttft_status"`
}

// relayExecutionSummary is the run's delivery ledger. It is embedded in the
// content-addressed transcript artifact, so the retry counts below are hashed
// into transcript_sha256 and travel with the signed score: a run that survived
// a transient fault stays permanently distinguishable from one that never
// faulted, and neither count can be edited after the fact.
//
// The retry fields are `omitempty` so a clean run's transcript bytes -- and
// therefore its digest -- are byte-identical to what this code produced before
// retries existed. Only a run that actually retried carries the extra keys.
type relayExecutionSummary struct {
	Requests                  uint64 `json:"requests"`
	Successes                 uint64 `json:"successes"`
	InfrastructureFailures    uint64 `json:"infrastructure_failures"`
	MinerRecoverableFailures  uint64 `json:"miner_recoverable_failures,omitempty"`
	GrantDenials              uint64 `json:"grant_denials,omitempty"`
	GrantAgentDeclines        uint64 `json:"grant_agent_declines,omitempty"`
	DeclineEvidenceMismatches uint64 `json:"decline_evidence_mismatches,omitempty"`
	BudgetEvidenceAbsences    uint64 `json:"budget_evidence_absences,omitempty"`
	AgentRequestRejections    uint64 `json:"agent_request_rejections,omitempty"`
	CapacityExhaustions       uint64 `json:"capacity_exhaustions,omitempty"`
	RecoveryWaits             uint64 `json:"recovery_waits,omitempty"`
	RecoveryExhaustions       uint64 `json:"recovery_exhaustions,omitempty"`
	CallerCancellations       uint64 `json:"caller_cancellations"`
	UpstreamAttempts          uint64 `json:"upstream_attempts"`
	Retries                   uint64 `json:"retries"`
	EmbeddingRetries          uint64 `json:"embedding_retries,omitempty"`
	RouteProbeAttempts        uint64 `json:"route_probe_attempts,omitempty"`
	RouteProbeRouted          uint64 `json:"route_probe_routed,omitempty"`
}

// provesV9RouteDisposition distinguishes a genuine zero-request scored
// interval from a compatibility miss. One routed challenge proves the agent
// can reach the ticket broker but chose not to during the scored cases. If the
// preferred selector misses, both supported selectors must be challenged
// before a zero can be signed.
//
// Named for the v9 contract that introduced it, but the counters it reads are
// populated for every version >= v8 and requireCompleteV7Usage consults it for
// every version >= v9. Nothing here is v9-specific.
func (s relayExecutionSummary) provesV9RouteDisposition() bool {
	return (s.RouteProbeAttempts > 0 &&
		s.RouteProbeRouted > 0 &&
		s.RouteProbeRouted <= s.RouteProbeAttempts) ||
		(s.RouteProbeAttempts >= 2 && s.RouteProbeRouted == 0)
}

// routeChallengeUnfinished separates the two ways provesV9RouteDisposition can
// be false, because only one of them is the platform's fault.
//
//   - Unfinished (routed == 0 && attempts < 2): we stopped asking. One routed
//     probe would have proved the harness can reach the broker; two missed
//     probes would have proved the adapter cannot. Neither happened, so the
//     interval is ambiguous purely because the challenge is incomplete -- and
//     completing it is the platform's job.
//   - Incoherent (routed > attempts): the counters contradict themselves, so
//     the evidence itself is untrustworthy. That must stay fail-closed and
//     terminal; treating corrupt evidence as a platform gap would hand out
//     retries on exactly the signal an attacker would want to forge.
func (s relayExecutionSummary) routeChallengeUnfinished() bool {
	return s.RouteProbeRouted == 0 && s.RouteProbeAttempts < 2
}

// lockScoredRelayRun serializes the portion of scored jobs that can call the
// single model relay owned by this API instance. The relay's health accounting
// is intentionally aggregate, so isolation must begin before the harness
// preflight can make a model request and remain in force through the final
// counter snapshot. The returned closure makes early-return paths release the
// lease reliably.
func (s *server) lockScoredRelayRun() func() {
	s.relayRunMu.Lock()
	return s.relayRunMu.Unlock
}

// probeLockedModelRelay makes one deliberately tiny completion through the
// same operator-owned gateway injected into every scored harness. The harness
// /health and tool-endpoint preflight do not exercise this dependency, so both
// can pass while a provider outage turns every model-backed case into a silent
// miss.
func probeLockedModelRelay(ctx context.Context, gateway string, benchVersion int) error {
	ctx, cancel := context.WithTimeout(ctx, relayProbeTimeout)
	defer cancel()

	body, err := json.Marshal(map[string]any{
		"model": llm.HarnessModelForVersion(benchVersion),
		"messages": []map[string]string{{
			"role":    "user",
			"content": "Reply OK.",
		}},
		"max_tokens":  1,
		"temperature": 0,
		"stream":      false,
	})
	if err != nil {
		return fmt.Errorf("encode probe")
	}

	endpoint, err := relayURL(gateway, "/v1/chat/completions")
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("build probe request")
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return fmt.Errorf("relay unreachable")
	}
	defer resp.Body.Close()
	responseBody, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return fmt.Errorf("read relay response")
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("relay returned %d", resp.StatusCode)
	}
	var decoded struct {
		Choices []json.RawMessage `json:"choices"`
	}
	if err := json.Unmarshal(responseBody, &decoded); err != nil || len(decoded.Choices) == 0 {
		return fmt.Errorf("relay returned no completion")
	}
	return nil
}

func relayURL(gateway, path string) (string, error) {
	raw := strings.TrimSpace(gateway)
	if raw == "" {
		return "", fmt.Errorf("gateway is not configured")
	}
	u, err := url.Parse(raw)
	if err != nil || u.Scheme == "" || u.Host == "" {
		return "", fmt.Errorf("gateway is invalid")
	}
	basePath := strings.TrimRight(u.Path, "/")
	basePath = strings.TrimSuffix(basePath, "/v1")
	u.Path = basePath + path
	u.RawPath = ""
	u.RawQuery = ""
	u.Fragment = ""
	return u.String(), nil
}

func readRelayHealth(ctx context.Context, gateway string) (relayHealthSnapshot, error) {
	endpoint, err := relayURL(gateway, "/health")
	if err != nil {
		return relayHealthSnapshot{}, err
	}
	ctx, cancel := context.WithTimeout(ctx, relayProbeTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return relayHealthSnapshot{}, fmt.Errorf("build relay health request")
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return relayHealthSnapshot{}, fmt.Errorf("relay health unavailable")
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return relayHealthSnapshot{}, fmt.Errorf("relay health returned %d", resp.StatusCode)
	}
	var snapshot relayHealthSnapshot
	if err := json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&snapshot); err != nil {
		return relayHealthSnapshot{}, fmt.Errorf("relay health malformed")
	}
	if snapshot.Status != "ok" {
		return relayHealthSnapshot{}, fmt.Errorf("relay health is not ok")
	}
	if snapshot.AccountingVersion != 1 && snapshot.AccountingVersion != 2 {
		return relayHealthSnapshot{}, fmt.Errorf("relay health lacks run accounting")
	}
	return snapshot, nil
}

func validV7RouteProfile(profile string) bool {
	const prefix = "openrouter-route-"
	const suffix = "-v1"
	if !strings.HasPrefix(profile, prefix) || !strings.HasSuffix(profile, suffix) {
		return false
	}
	digest := strings.TrimSuffix(strings.TrimPrefix(profile, prefix), suffix)
	if len(digest) != 16 || digest != strings.ToLower(digest) {
		return false
	}
	for _, r := range digest {
		if (r < '0' || r > '9') && (r < 'a' || r > 'f') {
			return false
		}
	}
	return true
}

func validBenchmarkRouteProfile(benchVersion int, profile string) bool {
	if benchVersion >= protocol.BenchVersionV9 {
		return profile == llm.V9AggregateProfileRevision
	}
	return validV7RouteProfile(profile)
}

// requireTokenAccounting is the admission gate on relay identity before a run.
// v5/v6 additionally require a reviewed absolute token baseline (their
// composite is discounted against it). v7 runs under the QUALITY-ONLY contract:
// token usage never moves the composite, so no reviewed token baseline is
// required to admit or score a v7 run. The v7 gate still fail-closes on the
// things that matter for identity — trusted accounting (v2), the immutable
// provider identity, the locked harness model, and a well-formed versioned
// route profile — and complete metered usage is separately enforced by
// requireCompleteV7Usage at run result.
func requireTokenAccounting(snapshot relayHealthSnapshot, benchVersion int, runSize string) error {
	if snapshot.AccountingVersion != 2 {
		return fmt.Errorf("relay health lacks trusted token accounting")
	}
	if snapshot.Provider == "" || snapshot.ProfileRevision == "" || snapshot.Model == "" {
		return fmt.Errorf("relay health lacks immutable provider identity")
	}
	if benchVersion >= protocol.BenchVersionV7 {
		if snapshot.Model != llm.V7HarnessModel {
			return fmt.Errorf("relay model does not match benchmark v7")
		}
		if !validBenchmarkRouteProfile(benchVersion, snapshot.ProfileRevision) {
			return fmt.Errorf("relay profile does not match benchmark v%d", benchVersion)
		}
	}
	return nil
}

// requireCompleteV7Usage accepts efficiency.ValidUsage and the one explicitly
// proven zero-use shape. Every other incomplete interval fails closed.
//
// The proven zero-use branch is pinned to `>= BenchVersionV9`, not `== 9`, and
// that is the whole point of it. A `== 9` pin meant a v10/v11 agent that ran
// zero inference got errAgentModelUseMissing -- sandbox_failure /
// model_inference_required / retryable:false -- so the submission never
// received a v11 score at all. The ledger is single-version by construction
// (see the authority filter in ditto/db/queries/scores.py), so an agent with no
// v11 quorum simply keeps being ranked on its last authoritative version. With
// v10 saturated at the 0.997012 ceiling, a zero-inference template-fitter was
// therefore holding a near-perfect composite that the anti-template-fitting
// bench could never measure -- the exact inversion of what v11 exists to do.
//
// Everything the accepted branch needs already carries forward to v10/v11 and
// is NOT v9-only: the discarded route probe runs for `>= BenchVersionV8`
// (main.go), scoregates.SupportedVersion spans 9..11, and applyV9BaseEvidence
// assembles the signed root for every version >= 9. So returning nil
// here lands the run on the ordinary scoring path, where the model-use gate
// publishes `zero_inference` with factor 0 and the enforce multiplier makes the
// composite exactly 0.00 -- an accepted, signed, finalizable score rather than
// a terminal error. That is the outcome the fold needs: a real v11 row, ranked
// ineligible (ranking_composite > 0.0 fails), which stops the stale v10
// ceiling from being carried forward once the pool flips to v11.
//
// What deliberately does NOT change: the proof requirement itself. An
// UNPROVEN zero-use interval stays errAgentModelUseMissing on v10/v11 exactly
// as it does on v9. Scoring an unproven zero as 0.00 would charge a miner the
// worst possible score for a platform-side adapter mismatch -- precisely the
// confusion the discarded route challenge exists to rule out. And neither
// branch may become retryable: minting a grant here is the mnemox unbounded
// re-lease loop (see relayFinalizeFailure below and
// ditto/validator/dittobench.py).
//
// UsageUnavailable has always had two unrelated causes sharing one counter. One
// is provider-side: a call succeeded but the provider's response carried no
// usage block, which nothing the harness did can cause or prevent. The other is
// the platform rejecting the harness's own request bytes with a pre-reservation
// 4xx -- schema, size, or model -- before any provider was contacted. Both
// failed the run as validator_infrastructure/retryable, so a harness reliably
// sending one malformed request per case was minting itself retry grants.
//
// execution.AgentRequestRejections is the second cause, counted separately.
// When it accounts for the ENTIRE usage shortfall, the run is the agent's --
// a rejected request, not a spent grant. Any provider-side gap at all, and
// the run keeps its grant -- the same infrastructure-wins-ties rule
// relayDegradedSince applies to grant denials.
func requireCompleteV7Usage(benchVersion int, usage protocol.TokenUsage, execution relayExecutionSummary) error {
	if benchVersion < protocol.BenchVersionV7 || efficiency.ValidUsage(usage) {
		return nil
	}
	if unusedV8ChatLane(benchVersion, usage, execution) {
		if benchVersion >= protocol.BenchVersionV9 {
			if execution.provesV9RouteDisposition() {
				// The transcript commits both the discarded route challenges and
				// the complete zero-request scored interval. The signed model-use
				// gate can therefore publish zero_inference without confusing an
				// adapter mismatch for an agent strategy.
				return nil
			}
			// A COMPLETED challenge is never ambiguous: one routed probe proves
			// the harness can reach the broker, two missed probes prove the
			// adapter cannot. So an unfinished challenge is the platform's gap,
			// not an agent strategy.
			//
			// Charging the agent for it made an unfinished probe look identical
			// to a template-fitter, and model_inference_required is both
			// non-retryable and non-zeroable -- so one skipped challenge
			// stranded a submission at zero scores permanently, which in turn
			// left its rollout cohort unable to converge.
			if execution.routeChallengeUnfinished() {
				return fmt.Errorf(
					"%w: benchmark v%d zero use lacks a signed model-route proof "+
						"(route probe attempts=%d routed=%d)",
					errRouteProofUnavailable,
					benchVersion,
					execution.RouteProbeAttempts,
					execution.RouteProbeRouted,
				)
			}
			// Incoherent counters: fail closed on the agent bin exactly as
			// before. The evidence contradicts itself, so nothing here may be
			// read as proof of platform fault, and nothing may mint a retry.
			return fmt.Errorf(
				"%w: benchmark v%d zero use has incoherent route-proof counters "+
					"(attempts=%d routed=%d)",
				errAgentModelUseMissing,
				benchVersion,
				execution.RouteProbeAttempts,
				execution.RouteProbeRouted,
			)
		}
		return fmt.Errorf(
			"%w: benchmark v%d requires at least one authoritative model call",
			errAgentModelUseMissing,
			benchVersion,
		)
	}
	if execution.AgentRequestRejections > 0 && execution.AgentRequestRejections >= usage.UsageUnavailable {
		return fmt.Errorf(
			"%w: the platform rejected %d of the harness's inference request(s) outright, before reserving any capacity — no provider was contacted",
			errAgentRequestRejected, execution.AgentRequestRejections,
		)
	}
	return fmt.Errorf("benchmark v7 requires complete provider token usage")
}

// unusedV8ChatLane identifies the one complete accounting shape that
// efficiency.ValidUsage intentionally rejects without explaining why: a Bench
// v8 harness that made no authoritative chat request at all. Policy v9 requires
// the scored response path to use a real model; deterministic or embedding-only
// answers are not a valid scored run.
//
// This stays deliberately narrower than "zero tokens": the relay snapshot
// must call the interval complete, and both the usage and execution deltas must
// prove that no chat request was attempted. That lets the caller classify the
// run as terminal and agent-attributable instead of minting a retry under the
// generic relay-infrastructure fallback. A rejected, cancelled, failed, or
// usage-less request still goes through the existing fail-closed classifier.
func unusedV8ChatLane(benchVersion int, usage protocol.TokenUsage, execution relayExecutionSummary) bool {
	return benchVersion >= protocol.BenchVersionV8 &&
		usage.Status == "complete" &&
		usage.Requests == 0 &&
		usage.Successes == 0 &&
		usage.UsageAvailable == 0 &&
		usage.UsageUnavailable == 0 &&
		usage.PromptTokens == 0 &&
		usage.CompletionTokens == 0 &&
		usage.TotalTokens == 0 &&
		execution.Requests == 0 &&
		execution.Successes == 0 &&
		execution.InfrastructureFailures == 0 &&
		execution.MinerRecoverableFailures == 0 &&
		execution.GrantDenials == 0 &&
		execution.AgentRequestRejections == 0 &&
		execution.CapacityExhaustions == 0 &&
		execution.RecoveryExhaustions == 0 &&
		execution.CallerCancellations == 0
}

// relayDegradedSince preserves the fail-closed rule: a run must never be scored
// on degraded inference. What changed is only the DIAGNOSIS. A lost lease and a
// provider fault used to produce the same "upstream failure" sentence, so a
// platform-side ticket force-expiry -- which revokes the grant and makes the
// scorer's next request fail -- was reported as if the provider had blipped.
// Grant denials are therefore reported first and by their own name: when both
// are present, the denial is the cause and the provider counter is downstream
// noise.
func relayDegradedSince(start, end relayHealthSnapshot) error {
	if relayRestarted(start, end) {
		return fmt.Errorf("relay restarted during benchmark")
	}
	if end.GrantDenials > start.GrantDenials {
		denials := end.GrantDenials - start.GrantDenials
		agentDeclines := end.GrantAgentDeclines - start.GrantAgentDeclines
		evidenceMismatches := end.DeclineEvidenceMismatches - start.DeclineEvidenceMismatches
		// Infrastructure wins ties, and this is the tie-break that makes the
		// whole change safe: the run is charged to the agent ONLY when every
		// single denial it saw was agent-attributable. One revoked lease, one
		// expired grant, one decline this build could not attribute -- any of
		// them, mixed in with a hundred budget declines -- and the run keeps
		// its grant. A platform failure can therefore never start billing a
		// miner, no matter what else happened alongside it.
		absences := end.BudgetEvidenceAbsences - start.BudgetEvidenceAbsences
		if agentDeclines == denials {
			return fmt.Errorf(
				"%w: the harness exhausted its own inference allowance %d time(s) during benchmark — the lease was alive and the platform healthy",
				errAgentInferenceDeclined, denials,
			)
		}
		if absences > 0 {
			return fmt.Errorf(
				"%w: platform sent %d terminal allowance decline(s) while this session had no authenticated budget evidence",
				errBudgetEvidenceAbsent, absences,
			)
		}
		if evidenceMismatches > 0 {
			return fmt.Errorf(
				"%w: platform sent %d terminal allowance decline(s) contradicted by the broker's authenticated budget evidence",
				errGrantDeclineEvidenceMismatch, evidenceMismatches,
			)
		}
		return fmt.Errorf(
			"platform declined this run's inference grant %d time(s) during benchmark (lease revoked, deadline rewritten, or budget exhausted) — not an upstream provider fault",
			denials,
		)
	}
	if end.CapacityExhaustions > start.CapacityExhaustions {
		// Reported before the generic upstream-failure line because a capacity
		// exhaustion also increments InfrastructureFailures, and the saturated
		// lane is the cause while the generic counter is downstream noise --
		// the same ordering rule grant denials already follow above.
		return fmt.Errorf(
			"%w: %d inference call(s) spent their whole backpressure budget waiting on a full platform lane",
			errInferenceLaneSaturated, end.CapacityExhaustions-start.CapacityExhaustions,
		)
	}
	if end.RecoveryExhaustions > start.RecoveryExhaustions {
		return fmt.Errorf(
			"%w: %d inference call(s) exhausted the slow provider-recovery window",
			errRelayRecoveryExhausted, end.RecoveryExhaustions-start.RecoveryExhaustions,
		)
	}
	if end.InfrastructureFailures > start.InfrastructureFailures {
		return fmt.Errorf("relay recorded %d upstream failure(s) during benchmark", end.InfrastructureFailures-start.InfrastructureFailures)
	}
	return nil
}

// relayRestarted reports whether any monotonic counter went backwards, which
// can only mean the relay's accounting was reset underneath this run. Shared by
// relayDegradedSince and relayExecutionSince so a newly added counter cannot be
// guarded in one and forgotten in the other.
func relayRestarted(start, end relayHealthSnapshot) bool {
	return end.Requests < start.Requests || end.Successes < start.Successes ||
		end.InfrastructureFailures < start.InfrastructureFailures ||
		end.MinerRecoverableFailures < start.MinerRecoverableFailures ||
		end.GrantDenials < start.GrantDenials ||
		end.GrantAgentDeclines < start.GrantAgentDeclines ||
		end.DeclineEvidenceMismatches < start.DeclineEvidenceMismatches ||
		end.BudgetEvidenceAbsences < start.BudgetEvidenceAbsences ||
		end.AgentRequestRejections < start.AgentRequestRejections ||
		end.CapacityExhaustions < start.CapacityExhaustions ||
		end.RecoveryWaits < start.RecoveryWaits ||
		end.RecoveryExhaustions < start.RecoveryExhaustions ||
		end.EmbeddingRetries < start.EmbeddingRetries ||
		end.CallerCancellations < start.CallerCancellations ||
		end.UpstreamAttempts < start.UpstreamAttempts ||
		end.MinerRecoverableFailures-start.MinerRecoverableFailures > end.Requests-start.Requests ||
		// A subset counter that outran its total means the two were updated
		// inconsistently; refuse to classify on it rather than guess.
		end.GrantAgentDeclines-start.GrantAgentDeclines > end.GrantDenials-start.GrantDenials ||
		end.DeclineEvidenceMismatches-start.DeclineEvidenceMismatches > end.GrantDenials-start.GrantDenials ||
		end.BudgetEvidenceAbsences-start.BudgetEvidenceAbsences > end.GrantDenials-start.GrantDenials ||
		(end.GrantAgentDeclines-start.GrantAgentDeclines)+(end.DeclineEvidenceMismatches-start.DeclineEvidenceMismatches)+(end.BudgetEvidenceAbsences-start.BudgetEvidenceAbsences) > end.GrantDenials-start.GrantDenials
}

func relayExecutionSince(start, end relayHealthSnapshot) (relayExecutionSummary, error) {
	if relayRestarted(start, end) {
		return relayExecutionSummary{}, fmt.Errorf("relay restarted during benchmark")
	}
	summary := relayExecutionSummary{
		Requests:                  end.Requests - start.Requests,
		Successes:                 end.Successes - start.Successes,
		InfrastructureFailures:    end.InfrastructureFailures - start.InfrastructureFailures,
		MinerRecoverableFailures:  end.MinerRecoverableFailures - start.MinerRecoverableFailures,
		GrantDenials:              end.GrantDenials - start.GrantDenials,
		GrantAgentDeclines:        end.GrantAgentDeclines - start.GrantAgentDeclines,
		DeclineEvidenceMismatches: end.DeclineEvidenceMismatches - start.DeclineEvidenceMismatches,
		BudgetEvidenceAbsences:    end.BudgetEvidenceAbsences - start.BudgetEvidenceAbsences,
		AgentRequestRejections:    end.AgentRequestRejections - start.AgentRequestRejections,
		CapacityExhaustions:       end.CapacityExhaustions - start.CapacityExhaustions,
		RecoveryWaits:             end.RecoveryWaits - start.RecoveryWaits,
		RecoveryExhaustions:       end.RecoveryExhaustions - start.RecoveryExhaustions,
		EmbeddingRetries:          end.EmbeddingRetries - start.EmbeddingRetries,
		CallerCancellations:       end.CallerCancellations - start.CallerCancellations,
		UpstreamAttempts:          end.UpstreamAttempts - start.UpstreamAttempts,
	}
	if summary.UpstreamAttempts > summary.Requests {
		summary.Retries = summary.UpstreamAttempts - summary.Requests
	}
	return summary, nil
}

func relayUsageSince(start, end relayHealthSnapshot) (protocol.TokenUsage, error) {
	usage := protocol.TokenUsage{
		AccountingVersion: end.AccountingVersion,
		Status:            "unavailable",
		Source:            "model_proxy_provider_response",
		Provider:          end.Provider,
		ProfileRevision:   end.ProfileRevision,
		Model:             end.Model,
		TTFTStatus:        end.TTFTStatus,
	}
	if start.AccountingVersion != end.AccountingVersion {
		return usage, fmt.Errorf("relay accounting version changed during benchmark")
	}
	if start.Provider != end.Provider || start.ProfileRevision != end.ProfileRevision || start.Model != end.Model {
		return usage, fmt.Errorf("relay provider profile changed during benchmark")
	}
	if end.Requests < start.Requests || end.Successes < start.Successes ||
		end.UsageAvailable < start.UsageAvailable || end.UsageUnavailable < start.UsageUnavailable ||
		end.PromptTokens < start.PromptTokens || end.PromptBytes < start.PromptBytes ||
		end.CompletionTokens < start.CompletionTokens ||
		end.ProviderLatencyMs < start.ProviderLatencyMs {
		return usage, fmt.Errorf("relay restarted during benchmark")
	}
	usage.Requests = end.Requests - start.Requests
	usage.Successes = end.Successes - start.Successes
	usage.UsageAvailable = end.UsageAvailable - start.UsageAvailable
	usage.UsageUnavailable = end.UsageUnavailable - start.UsageUnavailable
	usage.PromptTokens = end.PromptTokens - start.PromptTokens
	usage.PromptBytes = end.PromptBytes - start.PromptBytes
	usage.CompletionTokens = end.CompletionTokens - start.CompletionTokens
	usage.TotalTokens = usage.PromptTokens + usage.CompletionTokens
	usage.ProviderLatencyMs = end.ProviderLatencyMs - start.ProviderLatencyMs
	if usage.Successes == usage.UsageAvailable && usage.UsageUnavailable == 0 {
		usage.Status = "complete"
	}
	return usage, nil
}

func (s *server) relayRunStart(ctx context.Context, runID string, benchVersion int, runSize, gateway, sessionID string) (relayHealthSnapshot, bool) {
	var probeErr error
	if sessionID != "" {
		probeErr = s.broker.trustedProbe(ctx, sessionID)
	} else {
		probeErr = probeLockedModelRelay(ctx, gateway, benchVersion)
	}
	if probeErr != nil {
		s.failRelayUnavailableForContext(ctx, runID, probeErr)
		return relayHealthSnapshot{}, false
	}
	var snapshot relayHealthSnapshot
	var err error
	if sessionID != "" {
		snapshot, err = s.broker.snapshot(sessionID)
	} else {
		snapshot, err = readRelayHealth(ctx, gateway)
	}
	if err != nil {
		s.failRelayUnavailableForContext(ctx, runID, err)
		return relayHealthSnapshot{}, false
	}
	if benchVersion >= protocol.BenchVersionV5 {
		if err := requireTokenAccounting(snapshot, benchVersion, runSize); err != nil {
			s.failRelayUnavailableForContext(ctx, runID, err)
			return relayHealthSnapshot{}, false
		}
	}
	return snapshot, true
}

func (s *server) relayRunResult(ctx context.Context, runID string, start relayHealthSnapshot, gateway, sessionID string) (protocol.TokenUsage, relayExecutionSummary, bool) {
	var end relayHealthSnapshot
	var err error
	if sessionID != "" {
		end, err = s.broker.snapshot(sessionID)
	} else {
		end, err = readRelayHealth(ctx, gateway)
	}
	if err == nil {
		err = relayDegradedSince(start, end)
	}
	if err != nil {
		s.failRelayUnavailableForContext(ctx, runID, err)
		return protocol.TokenUsage{}, relayExecutionSummary{}, false
	}
	usage, usageErr := relayUsageSince(start, end)
	if usageErr != nil {
		s.failRelayUnavailableForContext(ctx, runID, usageErr)
		return protocol.TokenUsage{}, relayExecutionSummary{}, false
	}
	execution, executionErr := relayExecutionSince(start, end)
	if executionErr != nil {
		s.failRelayUnavailableForContext(ctx, runID, executionErr)
		return protocol.TokenUsage{}, relayExecutionSummary{}, false
	}
	return usage, execution, true
}

// handleRelayPreflight lets an off-netns caller verify the locked model relay is
// reachable on the SAME path a scored run uses. The subnet validator reaches this
// scorer at sandbox-docker:8000 but is NOT in sandbox-docker's network namespace,
// so it cannot itself resolve host.docker.internal — its own embed preflight hits
// a service name and therefore stays green even when the scorer's
// host.docker.internal:11435 relay path is broken (e.g. a managed stack whose
// compose predates the extra_hosts-on-sandbox-docker fix). This endpoint runs the
// same relay-health read the scored path runs at run start, inside this container's
// shared netns, so a mis-wired stack fails the validator's preflight and claims no
// ticket instead of leasing tickets it will fast-fail. Cheap: a relay /health GET,
// no provider completion — provider degradation is caught per-run, not here. The
// failure envelope mirrors the scored path's classification (model_relay_unavailable)
// so the caller maps it to a validator-infrastructure back-off.
func (s *server) handleRelayPreflight(w http.ResponseWriter, r *http.Request) {
	gateway := envOr("HARNESS_GATEWAY_URL", "http://host.docker.internal:11434")
	snapshot, err := readRelayHealth(r.Context(), gateway)
	if err != nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]any{
			"status": "unavailable",
			"failure": map[string]any{
				"kind":      "validator_infrastructure",
				"code":      "model_relay_unavailable",
				"retryable": true,
			},
			"error": err.Error(),
		})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"status":           "ok",
		"provider":         snapshot.Provider,
		"model":            snapshot.Model,
		"profile_revision": snapshot.ProfileRevision,
	})
}

// errAgentInferenceDeclined and errInferenceLaneSaturated are typed sentinels,
// not message markers: the harness's own output and its own error strings flow through this
// process, and a classification that can be produced by printing the right
// words is a classification a harness can forge. `errors.Is` cannot be forged
// by any string a miner controls -- only code in this package can wrap these.
//
// The direction of each is deliberate and opposite:
//
//   - errAgentInferenceDeclined moves a run OUT of the no-fault class. Only
//     conditions the AGENT controls are ever wrapped in it. Forging it would
//     let a miner take blame it does not deserve, which is a strange attack;
//     the real risk is code drift wrapping too much, which the table in
//     inference_broker.go (declineFaultFor) bounds.
//   - errInferenceLaneSaturated KEEPS a run in the no-fault class and only
//     renames it. Forging it would be worth something -- it mints a retry
//     grant -- which is precisely why it is a sentinel and not a substring.
var (
	errAgentInferenceDeclined       = errors.New("agent-attributable inference decline")
	errAgentRequestRejected         = errors.New("agent-attributable inference request rejection")
	errAgentModelUseMissing         = errors.New("agent made no authoritative model call")
	errRouteProofUnavailable        = errors.New("platform did not complete the route challenge")
	errInferenceLaneSaturated       = errors.New("platform inference lane saturated")
	errRelayRecoveryExhausted       = errors.New("provider recovery exhausted")
	errGrantDeclineEvidenceMismatch = errors.New("grant decline contradicted broker budget evidence")
	errBudgetEvidenceAbsent         = errors.New("grant decline lacked authenticated budget evidence")
)

// relayFinalizeFailure classifies a finalize-path relay failure.
//
// The DEFAULT is unchanged and stays validator_infrastructure/retryable. That
// is the correct default here and must not be narrowed: relay unreachable, a
// snapshot that could not be read, a provider fault, a revoked grant, an
// expired lease, a restarted relay, and every condition this build has never
// seen are all things the platform can cause unilaterally, and a run must keep
// its grant for all of them.
//
// What changed is that the no-fault class is no longer the ONLY outcome. Every
// finalize failure used to land here, so nine unrelated conditions -- several
// of them entirely the harness's doing -- became "validator_infrastructure,
// retryable: true", which mints a retry grant, RAISES the attempt cap, and
// re-leases. An agent that failed the same way every run therefore re-leased
// itself without bound, holding validator slots and never scoring.
//
// Only a typed sentinel can leave the default. Nothing derived from harness
// output, harness error text, or an unrecognised platform code can.
func relayFinalizeFailure(err error) *store.Failure {
	switch {
	case errors.Is(err, errAgentModelUseMissing):
		// Narrower than it looks, and deliberately so. On v8 this is any
		// zero-chat run. On v9+ it is ONLY a zero-chat run whose route
		// disposition could not be proven -- a PROVEN zero-inference run does
		// not reach here at all, because requireCompleteV7Usage returns nil and
		// the run is scored as an accepted, signed 0.00 through the
		// zero_inference model-use gate. So this bin now means "we could not
		// tell an agent strategy from an adapter mismatch", not "the agent ran
		// no inference".
		//
		// It stays terminal either way. Retrying re-runs the same image against
		// the same selectors, and making it retryable would mint a grant, raise
		// the attempt cap, and re-lease without bound.
		return &store.Failure{
			Kind:      "sandbox_failure",
			Code:      "model_inference_required",
			Retryable: false,
		}
	case errors.Is(err, errRouteProofUnavailable):
		// The platform did not finish challenging the route, so it cannot tell
		// an agent strategy from an adapter mismatch -- and that gap is ours.
		// Deliberately NOT `model_inference_required`: that bin is terminal and
		// agent-attributable, which permanently strands a submission at zero
		// scores for something it did not do.
		//
		// Retryable, because unlike the agent bins a re-lease genuinely can
		// resolve this: the next run challenges the route again. It is also
		// deliberately NOT one of the validator's no-fault
		// `_SANDBOX_INFRASTRUCTURE_CODES`, so it does NOT mint a retry grant or
		// raise the attempt cap. It re-runs inside the ordinary bounded attempt
		// budget, which keeps the mnemox unbounded re-lease loop closed while
		// still letting a real proof gap heal itself.
		return &store.Failure{
			Kind:      "validator_infrastructure",
			Code:      "route_proof_unavailable",
			Retryable: true,
		}
	case errors.Is(err, errAgentInferenceDeclined):
		// Terminal and the agent's: retrying cannot help, because the harness
		// would spend the same allowance the same way on a fresh grant. This is
		// one of two bins in this function that costs a miner an attempt, and
		// every condition reaching it is one the agent controls outright.
		return &store.Failure{
			Kind:      "sandbox_failure",
			Code:      "inference_allowance_exhausted",
			Retryable: false,
			Diagnostics: map[string]any{
				"budget_evidence_armed": true,
			},
		}
	case errors.Is(err, errAgentRequestRejected):
		// Terminal and the agent's, but not a spent grant: the platform refused
		// the request bytes before reserving capacity (schema, size, unsupported
		// field). A fresh grant cannot repair the same harness bytes, so this
		// stays retryable=false. It MUST NOT share inference_allowance_exhausted:
		// operators group that code as "the 75M/8192 wall", and a 400 is not
		// that wall.
		return &store.Failure{
			Kind:      "sandbox_failure",
			Code:      "inference_request_rejected",
			Retryable: false,
		}
	case errors.Is(err, errInferenceLaneSaturated):
		// Saturation is distinguished in DIAGNOSTICS, not in the code, and the
		// asymmetry is deliberate.
		//
		// The two new classifications are not equally safe to put on the wire.
		// The agent one is: a validator that has never heard of
		// `inference_allowance_exhausted` sees sandbox_failure/retryable=false,
		// which its terminal default already handles correctly. Old validators
		// do the right thing with it by construction.
		//
		// The no-fault direction is NOT. ditto-subnet's
		// `_sandbox_infrastructure_failure_code` returns None for any code
		// outside its allowlist, and None becomes fail_job("scoring_error") --
		// so shipping a NEW infrastructure code charges a miner on every
		// validator that predates the matching release. The fleet lags scorer
		// releases by design, which means a new code here would bill miners for
		// a saturated platform rail for as long as the skew lasts: precisely
		// the mistake this whole change exists to stop, arriving through the
		// rollout instead of through the classifier.
		//
		// `model_relay_unavailable` is already in every deployed validator's
		// allowlist, so this stays no-fault on EVERY fleet version, including
		// ones that will never be upgraded. Diagnostics carry the distinction
		// for the operator; that field is not consulted by the subnet's
		// classifier, so it cannot change the class anywhere.
		return &store.Failure{
			Kind:      "validator_infrastructure",
			Code:      "model_relay_unavailable",
			Retryable: true,
			Diagnostics: map[string]any{
				"relay_cause": "inference_lane_saturated",
			},
		}
	case errors.Is(err, errRelayRecoveryExhausted):
		return &store.Failure{
			Kind:      "validator_infrastructure",
			Code:      "model_relay_unavailable",
			Retryable: true,
			Diagnostics: map[string]any{
				"relay_cause": "provider_recovery_exhausted",
			},
		}
	case errors.Is(err, errGrantDeclineEvidenceMismatch):
		return &store.Failure{
			Kind:      "validator_infrastructure",
			Code:      "model_relay_unavailable",
			Retryable: true,
			Diagnostics: map[string]any{
				"relay_cause":           "grant_decline_evidence_mismatch",
				"budget_evidence_armed": true,
			},
		}
	case errors.Is(err, errBudgetEvidenceAbsent):
		return &store.Failure{
			Kind:      "validator_infrastructure",
			Code:      "model_relay_unavailable",
			Retryable: true,
			Diagnostics: map[string]any{
				"relay_cause":           "budget_evidence_absent",
				"budget_evidence_armed": false,
			},
		}
	default:
		return &store.Failure{
			Kind:      "validator_infrastructure",
			Code:      "model_relay_unavailable",
			Retryable: true,
		}
	}
}

func relaySandboxFailurePrefix(failure *store.Failure) string {
	if failure != nil && failure.Code == "inference_request_rejected" {
		return "harness inference request rejected: "
	}
	return "harness exhausted its inference allowance: "
}

func (s *server) failRelayUnavailable(runID string, err error) {
	failure := relayFinalizeFailure(err)
	// The prose has to follow the classification. "locked model relay
	// unavailable" was the only sentence this path could produce, and it was a
	// false statement for every agent-caused finalize failure -- which is how a
	// harness spending its own token budget came to read, in the operator's
	// logs and in errors.failure_detail(), as a broken relay.
	prefix := "locked model relay unavailable: "
	if failure.Kind == "sandbox_failure" {
		prefix = relaySandboxFailurePrefix(failure)
	}
	s.store.FailWith(runID, prefix+err.Error(), failure)
}

func (s *server) failRelayUnavailableForContext(ctx context.Context, runID string, err error) {
	// DELETE /v1/runs/{id} owns context.Canceled. Do not translate that caller
	// lifecycle signal into a validator-infrastructure verdict while the worker
	// unwinds relay preflight or final accounting. Deadlines remain fail-closed.
	if errors.Is(ctx.Err(), context.Canceled) {
		return
	}
	s.failRelayUnavailable(runID, err)
}

func trustedEmbeddingInfrastructureFailure(err error) *store.Failure {
	if err == nil {
		return nil
	}
	message := strings.ToLower(err.Error())
	for _, marker := range []string{
		"embedding service unavailable",
		"embedding platform returned",
		"ticket embedding probe failed",
	} {
		if strings.Contains(message, marker) {
			return &store.Failure{
				Kind:      "validator_infrastructure",
				Code:      "embedding_provider_unavailable",
				Retryable: true,
			}
		}
	}
	return nil
}

func trustedSeedingInfrastructureFailure(err error) *store.Failure {
	if errors.Is(err, runner.ErrSeedStoreLockTimeout) {
		return &store.Failure{
			Kind:      "validator_infrastructure",
			Code:      "seed_store_lock_timeout",
			Retryable: true,
		}
	}
	return trustedEmbeddingInfrastructureFailure(err)
}

func (s *server) failV7Seeding(runID, message string, err error) {
	failure := trustedSeedingInfrastructureFailure(err)
	s.store.FailWith(runID, message+err.Error(), failure)
}
