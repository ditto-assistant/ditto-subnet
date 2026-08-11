package main

// The inference broker is the trusted boundary between an untrusted harness
// and the platform-owned OpenRouter proxy. Platform bearer and DPoP private-key
// material live only in this process's memory: neither is put in a child
// container environment, command line, image, log, or Docker-readable mount.

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"math"
	mathrand "math/rand/v2"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/ablation"
	"github.com/ditto-assistant/dittobench-api/internal/llm"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/google/uuid"
)

const (
	brokerBodyLimit            = 4 << 20
	brokerMaximumSessionTTL    = 2 * time.Hour
	brokerPerSourceConcurrency = 4
	brokerReadHeaderTimeout    = 5 * time.Second
	brokerReadTimeout          = 15 * time.Second
	brokerWriteTimeout         = 2 * time.Minute
	brokerIdleTimeout          = 30 * time.Second
	brokerMaximumHeaderBytes   = 32 << 10
	platformInferenceAPIPath   = "/api/v1/inference/chat/completions"
	platformEmbeddingAPIPath   = "/api/v1/inference/embeddings"
	embeddingAPIPath           = "/api/embed"
	embeddingModel             = "embeddinggemma"
	hostedEmbeddingModel       = "perplexity/pplx-embed-v1-0.6b"
	embeddingDimensions        = 768
	embeddingMaximumInputs     = 256
	embeddingBodyLimit         = 1 << 20
	embeddingResponseLimit     = 16 << 20
	toolRouteBodyLimit         = 64 << 10
	embeddingSessionRequests   = 100000
	embeddingSessionInputs     = 1000000
	embeddingSessionInputBytes = 1 << 30
	brokerAblationTraceBytes   = 256 << 20
)

// usesPlatformEmbedding is the single version boundary for the hosted,
// ticket-scoped embedding route. Bench v2-v6 retain the frozen local Ollama
// lane; v7 and every later negotiated contract use the signed Platform route.
func usesPlatformEmbedding(benchVersion int) bool {
	return benchVersion >= protocol.BenchVersionV7
}

// ticketTransientMaxAttempts bounds the embedding lane's short retry window.
// Chat uses ticketChatFastMaxAttempts because one Platform response already
// represents exhausted throughput and reliability phases; repeating that whole
// stack six more times before announcing a wait would multiply provider work
// without giving the heartbeat a chance to explain the pause.
//
// Why so small. Each broker-level attempt is a NEW nonce and therefore a NEW
// platform reservation (ditto-platform ditto/db/queries/inference.py:460-474:
// `reserved_tokens=token_reservation`, `grant.request_count += 1`). A failed
// attempt is then conservatively charged its FULL reservation to the grant
// (`if not usage_available: prompt_tokens = request.reserved_tokens`,
// inference.py:543-547). So attempt N costs the grant one request and a whole
// reservation whether or not the provider did any work. That ledger is the
// grant's CAPACITY budget -- it is not the miner's scored usage, which this
// broker books separately and only from a successful attempt -- but it is still
// finite, and a large cap here could exhaust a grant mid-run and convert a
// survivable blip into the very failure the retry exists to prevent.
//
// Each extra broker delivery is a NEW reservation. Chat therefore makes one
// fast delivery, then at most four slow recovery deliveries. Even a pathological
// all-retry run stays below the 8,192-request ticket budget, while every
// delivery remains independently reserved, signed, and recorded.
const (
	ticketTransientMaxAttempts = 6
	ticketChatFastMaxAttempts  = 1
	ticketRecoveryMaxAttempts  = 4
	ticketRecoveryBackoff      = 15 * time.Second
)

type brokerTicketIdentity struct {
	GrantID        string
	AgentID        string
	SlotID         string
	TicketDeadline time.Time
}

type brokerSession struct {
	mu               sync.Mutex
	id               string
	activationSecret string
	privateKey       ed25519.PrivateKey
	publicKey        ed25519.PublicKey
	grantID          string
	bearer           string
	proxyURL         string
	legacyGateway    string
	// trustedChatHandler is an in-process confirmation reader route. It is
	// reachable only after this broker has authenticated the sandbox source;
	// unlike a loopback HTTP listener it cannot be scanned or called by another
	// host process to spend the server-owned provider session.
	trustedChatHandler    http.Handler
	generation            int
	expiresAt             time.Time
	expectedSourceIP      string
	provider              string
	model                 string
	requestModel          string
	profileRevision       string
	preparedAt            time.Time
	ticketAgentID         string
	ticketSlotID          string
	ticketDeadline        time.Time
	boundRunID            string
	benchVersion          int
	confirmationSession   bool
	inFlight              int
	embeddingPhaseStarted bool
	embeddingPhaseActive  bool
	embeddingInFlight     int
	embeddingConcurrency  int
	// embeddingCalls tracks every in-flight embedding call so the phase can be
	// ended -- and every one of them revoked -- as a set. It was a single
	// (cancel, done) pair while the lane admitted one request at a time; a
	// hostile harness leaving background calls open must not survive
	// endEmbeddingPhase just because a sibling call replaced the field.
	embeddingCalls      map[chan struct{}]context.CancelFunc
	embeddingRetries    uint64
	embeddingRequests   uint64
	embeddingInputs     uint64
	embeddingInputBytes uint64
	requests            uint64
	successes           uint64
	failures            uint64
	grantDenials        uint64
	usageAvailable      uint64
	usageUnavailable    uint64
	// grantAgentDeclines is the SUBSET of grantDenials the harness caused: it
	// spent the request or token allowance its own ticket granted, or sent one
	// request too large to reserve. Kept as a subset rather than as a split of
	// grantDenials on purpose -- grantDenials keeps its exact previous meaning
	// and wire value, so which runs FAIL is unchanged and only who is CHARGED
	// moves. See relayFinalizeFailure for the tie-break.
	grantAgentDeclines uint64
	// terminalAgentFailureNotified makes the first typed agent-attributable
	// allowance decline end the benchmark exactly once. Once the platform says
	// the grant is spent, every later case can only receive the same refusal.
	terminalAgentFailureNotified bool
	// agentRequestRejections counts pre-reservation 4xx (a 400 the platform's
	// schema refused, a 403 on a model, an oversized body) -- the platform
	// rejecting the harness's request without ever reserving capacity. These
	// already fail the run through usageUnavailable; this counter exists only
	// so the failure can name the harness instead of the relay.
	agentRequestRejections uint64
	// capacityExhaustions counts calls that used up their whole bounded
	// backpressure wait budget and gave up. Deliberately its own counter AND
	// deliberately still infrastructure: a saturated lane is a platform
	// property, and a harness cannot make the platform busy on its own. It gets
	// a distinct code purely so an operator can tell a saturated rail apart
	// from an unreachable relay without reading logs.
	capacityExhaustions uint64
	// recoveryWaits counts logical chat calls that exhausted the fast retry
	// window and entered the slower provider-recovery window. Multiple calls can
	// wait concurrently; recoveryWaiters keeps the public run status paused
	// until the last one resumes.
	recoveryWaits       uint64
	recoveryExhaustions uint64
	recoveryWaiters     int
	promptTokens        uint64
	promptBytes         uint64
	completionTokens    uint64
	providerLatency     uint64
	callerCancels       uint64
	upstreamAttempts    uint64
	cancels             map[string]context.CancelFunc
	// ablation is installed only for one v9 confirmation case at a time. The
	// capability is coordinator-created and revocable; the broker merely adapts
	// the matching HTTP lane to it and replays the paired ordinary response for
	// the other lane.
	ablation       *brokerAblationScope
	ablationTraces map[string]*brokerAblationTrace
	ablationBytes  uint64
}

type brokerAblationScope struct {
	lane                ablation.Lane
	caseID              string
	opaqueUserNamespace string
	responder           ablation.SyntheticResponder
	trace               *brokerAblationTrace
	session             *brokerSession
	chatCursor          int
	embeddingCursor     int
}

type brokerAblationCall struct {
	requestSHA256 string
	response      []byte
}

// brokerAblationTrace captures the paid ordinary sample once, then permits
// exact-response replay for the non-intervened service during both ablation
// rounds. Consequently an inference intervention synthesizes chat and replays
// embeddings, while an embedding intervention replays chat and synthesizes
// embeddings: neither intervention can issue a paid upstream request.
type brokerAblationTrace struct {
	mu          sync.Mutex
	chat        []brokerAblationCall
	embeddings  []brokerAblationCall
	storedBytes uint64
}

func ablationCallSHA256(raw []byte) string {
	digest := sha256.Sum256(raw)
	return hex.EncodeToString(digest[:])
}

func (scope *brokerAblationScope) reserveOrdinaryCall(chat bool, raw []byte) int {
	scope.trace.mu.Lock()
	defer scope.trace.mu.Unlock()
	calls := &scope.trace.embeddings
	if chat {
		calls = &scope.trace.chat
	}
	index := len(*calls)
	*calls = append(*calls, brokerAblationCall{requestSHA256: ablationCallSHA256(raw)})
	return index
}

func (scope *brokerAblationScope) completeOrdinaryCall(chat bool, index int, response []byte) bool {
	if len(response) == 0 || uint64(len(response)) > brokerAblationTraceBytes {
		return false
	}
	scope.session.mu.Lock()
	defer scope.session.mu.Unlock()
	scope.trace.mu.Lock()
	defer scope.trace.mu.Unlock()
	calls := scope.trace.embeddings
	if chat {
		calls = scope.trace.chat
	}
	if index < 0 || index >= len(calls) || len(calls[index].response) != 0 ||
		scope.session.ablationBytes > brokerAblationTraceBytes-uint64(len(response)) {
		return false
	}
	calls[index].response = append([]byte(nil), response...)
	scope.trace.storedBytes += uint64(len(response))
	scope.session.ablationBytes += uint64(len(response))
	return true
}

func (scope *brokerAblationScope) replayCall(chat bool, raw []byte) ([]byte, error) {
	scope.trace.mu.Lock()
	defer scope.trace.mu.Unlock()
	calls, cursor := scope.trace.embeddings, &scope.embeddingCursor
	if chat {
		calls, cursor = scope.trace.chat, &scope.chatCursor
	}
	if *cursor >= len(calls) {
		return nil, fmt.Errorf("ordinary ablation trace is exhausted")
	}
	call := calls[*cursor]
	*cursor++
	if call.requestSHA256 != ablationCallSHA256(raw) || len(call.response) == 0 {
		return nil, fmt.Errorf("ordinary ablation trace does not match request")
	}
	return append([]byte(nil), call.response...), nil
}

// brokerAblationLease prevents a scoped responder from surviving the exact
// RunCase attempt that received it. Close is idempotent and compare-and-clears
// the scope, so a stale defer cannot revoke a later case.
type brokerAblationLease struct {
	once    sync.Once
	session *brokerSession
	scope   *brokerAblationScope
}

func (lease *brokerAblationLease) Close() {
	if lease == nil {
		return
	}
	lease.once.Do(func() {
		lease.session.mu.Lock()
		if lease.session.ablation == lease.scope {
			lease.session.ablation = nil
		}
		lease.session.mu.Unlock()
	})
}

// beginAblationCase attaches one coordinator case to an already source-bound
// v9 confirmation session. The ordinary lane records provider responses. Each
// intervention receives its revocable responder and the matching ordinary
// trace, replaying the non-intervened service so no paid request is made during
// either ablation round.
func (b *inferenceBroker) beginAblationCase(
	id, runID string, request ablation.RunRequest,
) (*brokerAblationLease, error) {
	if request.CaseID == "" || request.OpaqueUserNamespace == "" ||
		(request.Lane != ablation.LaneOrdinary && request.Lane != ablation.LaneInference &&
			request.Lane != ablation.LaneEmbedding) ||
		(request.Lane == ablation.LaneOrdinary && request.Responder != nil) ||
		(request.Lane != ablation.LaneOrdinary && request.Responder == nil) {
		return nil, fmt.Errorf("invalid ablation case scope")
	}
	b.mu.RLock()
	session := b.sessions[id]
	b.mu.RUnlock()
	if session == nil {
		return nil, fmt.Errorf("inference session unavailable")
	}
	session.mu.Lock()
	defer session.mu.Unlock()
	if session.boundRunID != runID || session.benchVersion != ablation.BenchVersionV9 ||
		!session.activeLocked(time.Now()) || session.ablation != nil {
		return nil, fmt.Errorf("inference session is not available for ablation")
	}
	if session.ablationTraces == nil {
		session.ablationTraces = make(map[string]*brokerAblationTrace)
	}
	trace := session.ablationTraces[request.CaseID]
	if request.Lane == ablation.LaneOrdinary {
		if trace != nil {
			trace.mu.Lock()
			released := trace.storedBytes
			trace.mu.Unlock()
			if released > session.ablationBytes {
				return nil, fmt.Errorf("ordinary ablation trace accounting is invalid")
			}
			session.ablationBytes -= released
		}
		trace = &brokerAblationTrace{}
		session.ablationTraces[request.CaseID] = trace
	} else if trace == nil {
		return nil, fmt.Errorf("ordinary ablation trace is unavailable")
	}
	scope := &brokerAblationScope{
		lane: request.Lane, caseID: request.CaseID,
		opaqueUserNamespace: request.OpaqueUserNamespace, responder: request.Responder, trace: trace, session: session,
	}
	session.ablation = scope
	return &brokerAblationLease{session: session, scope: scope}, nil
}

func newInferenceBrokerHTTPServer(addr string, handler http.Handler) *http.Server {
	return &http.Server{
		Addr:              addr,
		Handler:           handler,
		ReadHeaderTimeout: brokerReadHeaderTimeout,
		ReadTimeout:       brokerReadTimeout,
		WriteTimeout:      brokerWriteTimeout,
		IdleTimeout:       brokerIdleTimeout,
		MaxHeaderBytes:    brokerMaximumHeaderBytes,
	}
}

type inferenceBroker struct {
	mu                   sync.RWMutex
	sessions             map[string]*brokerSession
	tools                map[string]toolRoute
	client               *http.Client
	maxSessions          int
	controlToken         string
	platformProxyURL     string
	platformTransportURL string
	embeddingURL         string
	embeddingSlots       chan struct{}
	retry                brokerRetryConfig
	sleep                func(context.Context, time.Duration) error
	relayWait            func(string, bool)
	terminalAgentFailure func(string)
}

// notifyTerminalAgentFailure ends a run after its first typed allowance
// decline. The platform has already refused the reservation, so no provider was
// contacted for that request and no later request can recover on this grant.
// Keeping the callback outside the session lock lets the server cancel the run
// and tear the session down without a lock inversion.
func (b *inferenceBroker) notifyTerminalAgentFailure(session *brokerSession) {
	if b.terminalAgentFailure == nil {
		return
	}
	session.mu.Lock()
	if session.terminalAgentFailureNotified {
		session.mu.Unlock()
		return
	}
	session.terminalAgentFailureNotified = true
	runID := session.boundRunID
	session.mu.Unlock()
	if runID != "" {
		b.terminalAgentFailure(runID)
	}
}

func (b *inferenceBroker) beginRelayWait(session *brokerSession) func() {
	session.mu.Lock()
	session.recoveryWaits++
	session.recoveryWaiters++
	first := session.recoveryWaiters == 1
	runID := session.boundRunID
	if first && b.relayWait != nil && runID != "" {
		b.relayWait(runID, true)
	}
	session.mu.Unlock()

	var once sync.Once
	return func() {
		once.Do(func() {
			session.mu.Lock()
			session.recoveryWaiters--
			last := session.recoveryWaiters == 0
			runID := session.boundRunID
			if last && b.relayWait != nil && runID != "" {
				b.relayWait(runID, false)
			}
			session.mu.Unlock()
		})
	}
}

type toolRoute struct {
	expectedSourceIP      string
	allowNATFallback      bool
	requireCaseCapability bool
	capabilityKey         []byte
	handler               http.Handler
	slots                 chan struct{}
}

// registeredToolRoute is a short-lived broker registration owned by one
// benchmark run. V9 registrations mint case/user-bound capabilities; frozen
// pre-v9 registrations retain their historical bare endpoint. In both modes,
// unregistering the route revokes access at once.
type registeredToolRoute struct {
	id                    string
	requireCaseCapability bool
	capabilityKey         []byte
}

// platformGrantDenied marks a platform inference response that declined to
// reserve capacity for this ticket's grant rather than reporting an upstream
// provider fault.
//
// The distinction is exact, not heuristic. The platform inference proxy answers
// 429 in one place only: when begin_inference_request() refuses the reservation
// (ditto-platform ditto/api_server/endpoints/inference.py `inference grant
// unavailable` / `embedding grant unavailable`, backed by
// ditto/db/queries/inference.py:329-335, which sets `grant.status = "revoked"`
// when the owning ticket is no longer ISSUED, its deadline was rewritten, or
// its deadline has passed). A genuine provider rate limit can never surface
// here as a 429: the platform retries 408/429/5xx upstream itself and converts
// every remaining provider rejection into a 502.
//
// So a 429 on the ticket-scoped path means the validator's LEASE went away --
// platform-side eviction, budget exhaustion, or per-ticket concurrency -- and
// counting it as an `infrastructure_failure` alongside real provider faults is
// what made a mid-run ticket force-expiry look like an upstream provider blip.
// Both still fail the run closed; only the accounting and the operator-visible
// reason change.
type platformGrantDenied struct {
	status int
	// code is the platform's decline code, or 0 when it did not send one.
	// Reporting only -- the action for every terminal decline is identical.
	code int
}

// Error deliberately preserves the historical wording so the marker-based
// classification in trustedEmbeddingInfrastructureFailure keeps matching.
func (e platformGrantDenied) Error() string {
	return fmt.Sprintf("embedding platform returned %d", e.status)
}

// platformEmbeddingTransient marks the narrow class of v7 embedding faults that
// are worth delivering again: no response at all, or a platform 5xx, which is
// what the platform returns once its own bounded provider loop has given up.
// Everything else -- a denied grant, a wrong model, a malformed or
// wrong-dimension vector, an unusable session -- stays terminal, because a
// repeat delivery cannot change any of those and an integrity fault must keep
// failing closed.
type platformEmbeddingTransient struct {
	err error
}

func (e platformEmbeddingTransient) Error() string { return e.err.Error() }
func (e platformEmbeddingTransient) Unwrap() error { return e.err }

// platformEmbeddingAtCapacity marks a platform refusal that is pure
// backpressure: the ticket's lease is healthy and the embedding lane was
// momentarily full. It is NOT a fault, so it is neither counted against the
// transient-retry budget nor written to the retry ledger -- waiting out a
// queue is not the same event as surviving a provider blip, and conflating
// them would make a throttled run indistinguishable from a degraded one.
//
// The platform signals it with 503 plus Retry-After, deliberately distinct from
// the 502 it returns when a provider genuinely failed and the 429 it reserves
// for a revoked lease. Recognising it is what lets an operator lower the
// concurrency board under live runs without destroying them; a build that does
// not recognise it still survives, because 503 already falls in the transient
// class above.
type platformEmbeddingAtCapacity struct {
	retryAfter time.Duration
}

func (platformEmbeddingAtCapacity) Error() string {
	return "embedding platform lane is at capacity"
}

// platformEmbeddingCapacityMaxWaits bounds how long one embedding call will
// queue behind platform backpressure. The request context already caps the call
// at 65s; this is the second bound, so a platform pinned at zero headroom
// surfaces as a failed run in bounded time rather than holding every case open
// until the deadline.
const platformEmbeddingCapacityMaxWaits = 12

// platformChatCapacityMaxWaits bounds how long one chat completion will queue
// behind platform backpressure, for the same reason its embedding twin exists:
// the request context already caps the call, and this is the second bound so a
// platform pinned at zero headroom surfaces as a failed run in bounded time
// rather than holding every check open until the ticket deadline.
const platformChatCapacityMaxWaits = 12

// platformEmbeddingIsAtCapacity recognises the platform's backpressure answer.
// Both conditions are required: 503 alone is the generic transient class, and
// the platform sets Retry-After only when the refusal came from a concurrency
// or rate limit rather than a failed dependency.
func platformEmbeddingIsAtCapacity(response *http.Response) bool {
	return platformIsAtCapacity(response)
}

// retryAfterDuration reads the delta-seconds form of Retry-After, clamped to a
// range a benchmark can actually wait out. A hostile or misconfigured value can
// only cost this one call its bounded pause, never the run's deadline.
func retryAfterDuration(header string) time.Duration {
	seconds, err := strconv.Atoi(strings.TrimSpace(header))
	if err != nil || seconds < 1 {
		return 250 * time.Millisecond
	}
	if seconds > 5 {
		seconds = 5
	}
	return time.Duration(seconds) * time.Second
}

// platformDeniesGrant reports whether a ticket-scoped platform response is a
// grant denial. Legacy relay sessions are excluded: they talk to the frozen
// model-relay, which forwards a real provider 429 verbatim, so on that path a
// 429 IS an upstream fault and must keep its existing accounting.
func platformDeniesGrant(legacyGateway string, status int) bool {
	return legacyGateway == "" && status == http.StatusTooManyRequests
}

// Platform decline codes, from the `error_code` field of the platform's error
// envelope (ditto-platform ditto/api_server/middleware/error_envelope.py).
//
// These exist because the status code was never a wide enough channel. A 429
// on this path has meant three unrelated things -- the lease is dead, the
// lease's request budget is spent, or the lane was momentarily full -- and this
// broker classified on status alone, so all three were read as a dead lease.
// That is what discarded `banblackycat`: 17 capacity declines against a lease
// that was still perfectly alive.
//
// The status remains a coarse hint (retryable vs terminal) and stays correct on
// its own, which is what lets a platform emitting these codes keep working
// against a broker that has never heard of them. The code is the precise
// signal, and it is only ever *additional* information.
const (
	platformDeclineUnspecified          = 4100
	platformDeclineGrantRevoked         = 4101
	platformDeclineBudgetExhausted      = 4102
	platformDeclineAtCapacity           = 4103
	platformDeclineTokenBudgetExhausted = 4104
	platformDeclineLeaseExpired         = 4105
	platformDeclineNonceReplayed        = 4106
	platformDeclineModelNotPermitted    = 4107
	platformDeclineGrantNotExchanged    = 4108
	platformDeclineReservationTooLarge  = 4109
)

// declineFault says who a decline is attributable to. It is the whole point of
// reading the code at all: until now every one of these collapsed into a single
// counter, and that counter's only consumer classified the run as
// validator_infrastructure/retryable -- which mints a retry grant, RAISES the
// attempt cap, and re-leases. A harness that reliably spends its own allowance
// therefore re-leased itself forever, holding validator slots and never scoring.
type declineFault int

const (
	// declineFaultPlatform: the platform (or this broker) caused it
	// unilaterally, and no harness behaviour could have avoided it. Stays
	// no-fault. This is also the DEFAULT for anything unrecognised.
	declineFaultPlatform declineFault = iota
	// declineFaultAgent: the harness's own request volume or request size
	// caused it. The lease is alive and the platform is healthy; the agent
	// simply spent what it was given. Charging a retry grant for this is what
	// created the loop.
	declineFaultAgent
)

// declineFaultFor is the authoritative attribution table, deliberately a table
// and not a ladder of ifs -- the same shape, and for the same reason, as the
// platform's own _TERMINAL_DECLINE_RESPONSES (ditto-platform
// ditto/api_server/middleware/error_envelope.py): a new decline that nobody
// wires up must fall into the SAFE bin, not an arbitrary one.
//
// Only entries present here are agent-attributable. Everything else -- every
// code below, every code this build has never heard of, and the "the platform
// sent no code at all" answer (0) -- returns declineFaultPlatform. That default
// is the safety property: a decline this build cannot attribute keeps its grant.
//
// Explicitly NOT agent-attributable, despite being 429 declines the harness's
// request superficially "caused":
//
//   - 4106 NONCE_REPLAYED and 4107 MODEL_NOT_PERMITTED. A harness cannot reach
//     either. This broker mints a fresh uuid nonce per upstream attempt on both
//     lanes, and it OVERWRITES the caller's model with the ticket's before
//     forwarding (rewriteRequestModel on chat, a hard-coded hostedEmbeddingModel
//     on embeddings). If the platform still says the nonce repeated or the model
//     is not permitted, the disagreement is between the grant and the ticket --
//     platform and broker state the miner never touches. Billing a miner for
//     those would be the exact mirror-image of the bug being fixed here.
//   - 4108 GRANT_NOT_EXCHANGED, 4105 LEASE_EXPIRED, 4101 GRANT_REVOKED. Grant
//     lifecycle, owned entirely by the platform and this broker.
//   - 4100 unattributed. The platform holds an unknown grant id and a failed
//     bearer comparison deliberately indistinguishable so an unauthenticated
//     caller learns nothing. If the platform refuses to say, this must not guess.
//   - 4103 AT_CAPACITY. Never reaches this table (it is a 503 handled as
//     backpressure), and lane saturation is a platform property regardless.
var declineFaultFor = map[int]declineFault{
	// The request-count allowance the ticket granted, spent by the harness's
	// own call volume. The lease is alive and the platform is healthy.
	platformDeclineBudgetExhausted: declineFaultAgent,
	// The token allowance, spent by the harness's own prompt sizes. The
	// platform splits this from the request count precisely because "hit the
	// token wall at call 400" is a different agent behaviour from "hit the
	// request wall at call 8192" -- both are the agent's behaviour.
	platformDeclineTokenBudgetExhausted: declineFaultAgent,
	// A single harness request whose reservation exceeds the entire token
	// allowance. Nothing has been spent and the lease is still good: the
	// request itself is oversized, which is authored by the harness alone.
	platformDeclineReservationTooLarge: declineFaultAgent,
}

// declineIsAgentFault reports whether a platform decline code is attributable
// to the harness. Unknown codes, and the absent code 0, are never agent-fault.
func declineIsAgentFault(code int) bool {
	return declineFaultFor[code] == declineFaultAgent
}

// platformDeclineCode extracts the platform's decline code from an error body,
// returning 0 when the body is absent, unparseable, or carries no code.
//
// Zero is the "say nothing" answer on purpose. Every caller must already have a
// correct status-only behaviour to fall back on, because the fleet runs pinned
// builds against a platform that may be older or newer than this one. A body
// this cannot parse must never be able to change a decision.
func platformDeclineCode(body []byte) int {
	if len(body) == 0 || len(body) > 64<<10 {
		return 0
	}
	var envelope struct {
		ErrorCode int `json:"error_code"`
	}
	if json.Unmarshal(body, &envelope) != nil {
		return 0
	}
	if _, known := platformDeclineReasons[envelope.ErrorCode]; known {
		return envelope.ErrorCode
	}
	return 0
}

// platformRejectionMessageLimit bounds what is copied out of a platform error
// body. Long enough for a schema refusal that names several fields, short
// enough that nothing can use this path to pump bulk text into a harness's
// stderr or this process's logs.
const platformRejectionMessageLimit = 400

// platformRejectionMessage extracts the platform's own explanation of a request
// rejection from its error envelope (ditto-platform
// ditto/api_server/middleware/error_envelope.py: `{error_code, message,
// request_id}`), returning "" when there is nothing usable.
//
// This exists because the broker used to throw that explanation away. A 4xx
// from the platform reached the harness as the fixed string "inference request
// denied", so a miner whose request carried one field the platform's schema did
// not recognise saw a generic refusal with no indication of which field. That
// is exactly what happened to `Cooking`: three submissions burned on a single
// unrecognised `reasoning` key, with no way to discover its name. The platform
// now names the offending key; this makes the name survive the last hop.
//
// Forwarding the string changes no classification. The status, the counters,
// and the agent-fault attribution above are all untouched, and the response
// shape stays `{"error": ...}` -- only the human-readable value differs. A body
// this cannot parse falls back to the old wording rather than failing.
func platformRejectionMessage(body []byte) string {
	if len(body) == 0 || len(body) > 64<<10 {
		return ""
	}
	var envelope struct {
		Message string `json:"message"`
	}
	if json.Unmarshal(body, &envelope) != nil {
		return ""
	}
	// Control characters are stripped rather than escaped: this string is
	// destined for a log line and a harness's stderr, and a newline in either
	// would let a platform-relayed message forge an entry.
	cleaned := strings.Map(func(r rune) rune {
		if r < 0x20 || r == 0x7f {
			return -1
		}
		return r
	}, envelope.Message)
	cleaned = strings.TrimSpace(cleaned)
	if cleaned == "" {
		return ""
	}
	if len(cleaned) > platformRejectionMessageLimit {
		// Truncate on a rune boundary so the result stays valid UTF-8.
		cleaned = strings.ToValidUTF8(cleaned[:platformRejectionMessageLimit], "") + "..."
	}
	return cleaned
}

// platformDeclineReasons is the recognised code set and its operator-facing
// wording in one place, so a code cannot be parseable without also being
// renderable (or attributable, via declineFaultFor above).
//
// This build previously recognised only 4100-4103. The platform has emitted
// 4104-4109 since it split the terminal declines apart, and every one of them
// was landing on the "the platform did not say why" branch -- the discriminating
// signal arrived, was parsed, and was then discarded. That is why a spent TOKEN
// budget (4104), the decline that actually ends heavy v7 runs, has been
// indistinguishable from a revoked lease at this layer.
var platformDeclineReasons = map[int]string{
	platformDeclineUnspecified:          "the platform declined the reservation",
	platformDeclineGrantRevoked:         "the lease was revoked",
	platformDeclineBudgetExhausted:      "the lease spent its request budget",
	platformDeclineAtCapacity:           "the lane is at capacity",
	platformDeclineTokenBudgetExhausted: "the lease spent its token budget",
	platformDeclineLeaseExpired:         "the lease's own clock ran out",
	platformDeclineNonceReplayed:        "the broker replayed a nonce",
	platformDeclineModelNotPermitted:    "the grant does not pin this model",
	platformDeclineGrantNotExchanged:    "the grant was never exchanged for a bearer",
	platformDeclineReservationTooLarge:  "one request exceeded the whole token allowance",
}

// platformDeclineReason renders a decline code for an operator reading logs.
func platformDeclineReason(code int) string {
	if reason, known := platformDeclineReasons[code]; known {
		return reason
	}
	return "the platform did not say why"
}

// platformIsAtCapacity recognises the platform's backpressure answer on any
// lane. Both conditions are required: 503 alone is the generic transient class,
// and the platform sets Retry-After only when the refusal came from a
// concurrency or rate limit rather than a failed dependency.
//
// The body is consulted only to *confirm*, never to reject: a platform that
// sets the header without the code is still telling the truth about
// backpressure, and older platforms did exactly that.
func platformIsAtCapacity(response *http.Response) bool {
	return response.StatusCode == http.StatusServiceUnavailable &&
		strings.TrimSpace(response.Header.Get("Retry-After")) != ""
}

type brokerRetryConfig struct {
	maxAttempts int
	base        time.Duration
	cap         time.Duration
	factor      float64
}

func (c brokerRetryConfig) backoff(attempt int) time.Duration {
	delay := float64(c.base) * math.Pow(c.factor, float64(attempt-1))
	if maximum := float64(c.cap); c.cap > 0 && delay > maximum {
		delay = maximum
	}
	if delay <= 0 {
		return 0
	}
	return time.Duration(delay/2 + mathrand.Float64()*(delay/2))
}

func brokerSleep(ctx context.Context, delay time.Duration) error {
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

func embeddingRequestCanceled(ctx context.Context) bool {
	return errors.Is(ctx.Err(), context.Canceled)
}

func newInferenceBroker(maxSessions int, embeddingCapacity ...int) *inferenceBroker {
	if maxSessions < 1 {
		maxSessions = 1
	}
	capacity := 1
	if len(embeddingCapacity) > 0 && embeddingCapacity[0] > 0 {
		capacity = embeddingCapacity[0]
	}
	platformProxyURL := configuredPlatformProxyURL(
		os.Getenv("DITTOBENCH_PLATFORM_INFERENCE_PROXY_URL"),
	)
	return &inferenceBroker{
		sessions: make(map[string]*brokerSession),
		tools:    make(map[string]toolRoute),
		client: &http.Client{
			Timeout: 100 * time.Second,
			CheckRedirect: func(*http.Request, []*http.Request) error {
				return http.ErrUseLastResponse
			},
		},
		maxSessions:      maxSessions * 2,
		embeddingSlots:   make(chan struct{}, capacity),
		controlToken:     strings.TrimSpace(os.Getenv("DITTOBENCH_BROKER_CONTROL_TOKEN")),
		platformProxyURL: platformProxyURL,
		platformTransportURL: configuredPlatformTransportURL(
			os.Getenv("DITTOBENCH_PLATFORM_INFERENCE_TRANSPORT_URL"), platformProxyURL,
		),
		embeddingURL: configuredEmbeddingURL(
			envOr("DITTOBENCH_EMBEDDING_UPSTREAM_URL", "http://host.docker.internal:11434/api/embed"),
		),
		retry: brokerRetryConfig{
			maxAttempts: envIntDefault("RELAY_RETRY_MAX_ATTEMPTS", 6),
			base:        time.Duration(envIntDefault("RELAY_RETRY_BASE_MS", 200)) * time.Millisecond,
			cap:         time.Duration(envIntDefault("RELAY_RETRY_CAP_MS", 2000)) * time.Millisecond,
			factor:      envFloatDefault("RELAY_RETRY_FACTOR", 2),
		},
		sleep: brokerSleep,
	}
}

// beginEmbeddingPhase opens the locked embedding operation only after the
// scorer has admitted this exact run into its bounded memory phase. A session
// receives one phase for its lifetime; ending it is final, so a late harness
// request cannot reopen validator-owned embedding capacity.
func (b *inferenceBroker) beginEmbeddingPhase(id, runID string) bool {
	b.mu.RLock()
	session := b.sessions[id]
	b.mu.RUnlock()
	if session == nil {
		return false
	}
	session.mu.Lock()
	defer session.mu.Unlock()
	if session.boundRunID != runID || !session.activeLocked(time.Now()) ||
		session.embeddingPhaseStarted {
		return false
	}
	session.embeddingPhaseStarted = true
	session.embeddingPhaseActive = true
	return true
}

func (b *inferenceBroker) endEmbeddingPhase(id, runID string) {
	b.mu.RLock()
	session := b.sessions[id]
	b.mu.RUnlock()
	if session == nil {
		return
	}
	session.mu.Lock()
	var pending []chan struct{}
	if session.boundRunID == runID {
		session.embeddingPhaseActive = false
		for done, cancel := range session.embeddingCalls {
			pending = append(pending, done)
			cancel()
		}
	}
	session.mu.Unlock()
	// A hostile harness may return from its scored request while leaving
	// background embedding calls open. Revoke every one of them and wait for
	// their cleanup to release any historical local-embedding slot before the
	// scorer releases memory-phase admission to a sibling. Cancelling under the
	// lock is safe -- the cancel funcs do not take it -- and it closes the race
	// where a call admitted between the snapshot and the cancels would escape.
	for _, done := range pending {
		<-done
	}
}

func configuredEmbeddingURL(raw string) string {
	value := strings.TrimSpace(raw)
	parsed, err := url.Parse(value)
	if err != nil || value == "" || (parsed.Scheme != "http" && parsed.Scheme != "https") ||
		parsed.Host == "" || parsed.User != nil || parsed.RawQuery != "" ||
		parsed.Fragment != "" || parsed.Path != embeddingAPIPath {
		return ""
	}
	return parsed.String()
}

func configuredPlatformProxyURL(raw string) string {
	value := strings.TrimSpace(raw)
	parsed, err := url.Parse(value)
	if err != nil || value == "" || parsed.Scheme != "https" || parsed.Host == "" ||
		parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" ||
		parsed.Path != platformInferenceAPIPath {
		return ""
	}
	return value
}

// configuredPlatformTransportURL separates the signed grant identity from the
// network route used by the trusted broker. The canonical proxy URL is still
// compared byte-for-byte during activation; only the validator-owned HTTPS
// client uses this independently configured direct origin. An empty override
// preserves the historical single-URL behavior.
func configuredPlatformTransportURL(raw, canonical string) string {
	if strings.TrimSpace(raw) == "" {
		return canonical
	}
	return configuredPlatformProxyURL(raw)
}

func (b *inferenceBroker) controlAuthorized(r *http.Request) bool {
	ip := net.ParseIP(sourceIP(r.RemoteAddr))
	if ip != nil && ip.IsLoopback() {
		return true
	}
	if b.controlToken == "" {
		return false
	}
	provided, ok := strings.CutPrefix(r.Header.Get("Authorization"), "Bearer ")
	return ok && subtle.ConstantTimeCompare([]byte(provided), []byte(b.controlToken)) == 1
}

func (b *inferenceBroker) requireControl(w http.ResponseWriter, r *http.Request) bool {
	if b.controlAuthorized(r) {
		return true
	}
	w.Header().Set("Cache-Control", "no-store")
	writeError(w, http.StatusUnauthorized, "inference control plane unavailable")
	return false
}

// prepareLegacy creates a memory-only, run-bound session in front of a
// reviewed validator-owned compatibility relay. It gives concurrent v2-v6 sandboxes
// independent trusted accounting without putting the provider credential or a
// bearer in the harness. V7 is rejected here and requires a platform grant.
func (b *inferenceBroker) prepareLegacy(
	runID string,
	benchVersion int,
	gateway string,
	relay relayHealthSnapshot,
) (string, error) {
	b.pruneExpired(time.Now())
	if _, err := uuid.Parse(runID); err != nil || benchVersion < 2 || benchVersion > 6 {
		return "", fmt.Errorf("invalid legacy inference run")
	}
	if _, err := relayURL(gateway, "/v1/chat/completions"); err != nil {
		return "", err
	}
	if err := validLegacyRelayIdentity(relay); err != nil {
		return "", err
	}
	id, err := randomToken(18)
	if err != nil {
		return "", err
	}
	session := &brokerSession{
		id:              id,
		legacyGateway:   gateway,
		provider:        relay.Provider,
		profileRevision: relay.ProfileRevision,
		model:           relay.Model,
		requestModel:    llm.HarnessModelForVersion(benchVersion),
		expiresAt:       time.Now().Add(brokerMaximumSessionTTL),
		preparedAt:      time.Now(),
		boundRunID:      runID,
		cancels:         make(map[string]context.CancelFunc),
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if len(b.sessions) >= b.maxSessions {
		return "", fmt.Errorf("inference broker is at capacity")
	}
	b.sessions[id] = session
	return id, nil
}

func validLegacyRelayIdentity(relay relayHealthSnapshot) error {
	valid := (relay.Provider == "chutes" &&
		relay.ProfileRevision == llm.ChutesRelayProfileRevision &&
		relay.Model == llm.LockedUpstreamModel) ||
		(relay.Provider == "openrouter" &&
			relay.ProfileRevision == llm.OpenRouterRelayProfileRevision &&
			relay.Model == llm.LockedHarnessModel)
	if !valid {
		return fmt.Errorf("legacy relay identity is not a reviewed profile")
	}
	return nil
}

func (b *inferenceBroker) registerTool(
	h http.Handler,
	expectedSourceIP string,
	allowNATFallback bool,
	requireCaseCapability bool,
) (registeredToolRoute, func(), error) {
	id, err := randomToken(18)
	if err != nil {
		return registeredToolRoute{}, func() {}, err
	}
	key := make([]byte, sha256.Size)
	if _, err := rand.Read(key); err != nil {
		return registeredToolRoute{}, func() {}, err
	}
	b.mu.Lock()
	b.tools[id] = toolRoute{
		expectedSourceIP:      expectedSourceIP,
		allowNATFallback:      allowNATFallback && requireCaseCapability,
		requireCaseCapability: requireCaseCapability,
		capabilityKey:         key,
		handler:               h,
		slots:                 make(chan struct{}, brokerPerSourceConcurrency),
	}
	b.mu.Unlock()
	stop := func() {
		b.mu.Lock()
		delete(b.tools, id)
		b.mu.Unlock()
	}
	return registeredToolRoute{
		id:                    id,
		requireCaseCapability: requireCaseCapability,
		capabilityKey:         key,
	}, stop, nil
}

func (b *inferenceBroker) handleTool(w http.ResponseWriter, r *http.Request) {
	b.mu.RLock()
	route, ok := b.tools[r.PathValue("id")]
	b.mu.RUnlock()
	if !ok {
		writeError(w, http.StatusNotFound, "tool route not found")
		return
	}
	if r.Method == http.MethodGet {
		if route.requireCaseCapability && !validToolCapability(route.capabilityKey, "health", "", r.URL.Query().Get("cap")) {
			writeError(w, http.StatusUnauthorized, "tool route unavailable")
			return
		}
		w.WriteHeader(http.StatusNoContent)
		return
	}
	caseID := r.URL.Query().Get("case_id")
	userID := r.URL.Query().Get("user_id")
	if route.requireCaseCapability && !validToolCapability(route.capabilityKey, caseID, userID, r.URL.Query().Get("cap")) {
		writeError(w, http.StatusUnauthorized, "tool route unavailable")
		return
	}
	if route.expectedSourceIP == "" ||
		(sourceIP(r.RemoteAddr) != route.expectedSourceIP && !route.allowNATFallback) {
		writeError(w, http.StatusUnauthorized, "tool route unavailable")
		return
	}
	select {
	case route.slots <- struct{}{}:
		defer func() { <-route.slots }()
	default:
		w.Header().Set("Retry-After", "1")
		writeError(w, http.StatusTooManyRequests, "tool source is at capacity")
		return
	}
	if route.requireCaseCapability && !toolRequestMatchesCapability(w, r, caseID, userID) {
		return
	}
	forwarded := r.Clone(r.Context())
	forwarded.URL.Path = "/tool"
	forwarded.URL.RawQuery = ""
	route.handler.ServeHTTP(w, forwarded)
}

func (r registeredToolRoute) endpoint(baseURL, caseID, userID string) string {
	if !r.requireCaseCapability {
		return baseURL
	}
	values := url.Values{
		"case_id": {caseID},
		"user_id": {userID},
		"cap":     {toolCapability(r.capabilityKey, caseID, userID)},
	}
	return baseURL + "?" + values.Encode()
}

func (r registeredToolRoute) healthEndpoint(baseURL string) string {
	if !r.requireCaseCapability {
		return baseURL
	}
	return baseURL + "?cap=" + url.QueryEscape(toolCapability(r.capabilityKey, "health", ""))
}

func toolCapability(key []byte, caseID, userID string) string {
	mac := hmac.New(sha256.New, key)
	_, _ = fmt.Fprintf(mac, "dittobench-tool-v1\n%d:%s\n%d:%s", len(caseID), caseID, len(userID), userID)
	return base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}

func validToolCapability(key []byte, caseID, userID, provided string) bool {
	want := toolCapability(key, caseID, userID)
	return len(provided) == len(want) && subtle.ConstantTimeCompare([]byte(provided), []byte(want)) == 1
}

func toolRequestMatchesCapability(w http.ResponseWriter, r *http.Request, caseID, userID string) bool {
	body, err := io.ReadAll(io.LimitReader(r.Body, toolRouteBodyLimit+1))
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid tool request")
		return false
	}
	if len(body) > toolRouteBodyLimit {
		writeError(w, http.StatusRequestEntityTooLarge, "tool request too large")
		return false
	}
	var identity struct {
		CaseID string `json:"case_id"`
		UserID string `json:"user_id"`
	}
	if err := json.Unmarshal(body, &identity); err != nil {
		writeError(w, http.StatusBadRequest, "invalid tool request")
		return false
	}
	if identity.CaseID != caseID || identity.UserID != userID {
		writeError(w, http.StatusUnauthorized, "tool route unavailable")
		return false
	}
	r.Body = io.NopCloser(bytes.NewReader(body))
	return true
}

func randomToken(n int) (string, error) {
	raw := make([]byte, n)
	if _, err := rand.Read(raw); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(raw), nil
}

func (b *inferenceBroker) prepare(w http.ResponseWriter, r *http.Request) {
	if !b.requireControl(w, r) {
		return
	}
	b.pruneExpired(time.Now())
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "inference broker unavailable")
		return
	}
	id, err := randomToken(18)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "inference broker unavailable")
		return
	}
	activation, err := randomToken(24)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "inference broker unavailable")
		return
	}
	session := &brokerSession{
		id: id, activationSecret: activation, privateKey: private, publicKey: public,
		preparedAt: time.Now(), cancels: make(map[string]context.CancelFunc),
	}
	b.mu.Lock()
	if len(b.sessions) >= b.maxSessions {
		b.mu.Unlock()
		writeError(w, http.StatusTooManyRequests, "inference broker is at capacity")
		return
	}
	b.sessions[id] = session
	b.mu.Unlock()
	w.Header().Set("Cache-Control", "no-store")
	writeJSON(w, http.StatusCreated, map[string]any{
		"session_id":        id,
		"activation_secret": activation,
		"broker_public_key": base64.RawURLEncoding.EncodeToString(public),
	})
}

type brokerActivation struct {
	ActivationSecret string    `json:"activation_secret"`
	GrantID          string    `json:"grant_id"`
	AgentID          string    `json:"agent_id,omitempty"`
	SlotID           string    `json:"slot_id,omitempty"`
	TicketDeadline   time.Time `json:"ticket_deadline,omitempty"`
	Bearer           string    `json:"bearer"`
	ProxyURL         string    `json:"proxy_url"`
	Generation       int       `json:"generation"`
	ExpiresAt        time.Time `json:"expires_at"`
	Provider         string    `json:"provider"`
	ProfileRevision  string    `json:"profile_revision"`
	Model            string    `json:"model"`
}

func (b *inferenceBroker) activate(w http.ResponseWriter, r *http.Request) {
	if !b.requireControl(w, r) {
		return
	}
	id := r.PathValue("id")
	b.mu.RLock()
	session := b.sessions[id]
	b.mu.RUnlock()
	if session == nil {
		writeError(w, http.StatusNotFound, "inference session not found")
		return
	}
	var activation brokerActivation
	if err := json.NewDecoder(io.LimitReader(r.Body, 16<<10)).Decode(&activation); err != nil {
		writeError(w, http.StatusBadRequest, "invalid inference activation")
		return
	}
	session.mu.Lock()
	defer session.mu.Unlock()
	now := time.Now()
	_, grantErr := uuid.Parse(activation.GrantID)
	ticketIdentity := brokerTicketIdentity{
		GrantID: activation.GrantID, AgentID: activation.AgentID,
		SlotID: activation.SlotID, TicketDeadline: activation.TicketDeadline,
	}
	hasRouteIdentity := activation.Provider != "" || activation.ProfileRevision != "" || activation.Model != ""
	transportURL := b.platformTransportURL
	if transportURL == "" {
		transportURL = b.platformProxyURL
	}
	secretMatches := subtle.ConstantTimeCompare(
		[]byte(activation.ActivationSecret), []byte(session.activationSecret),
	) == 1
	if !secretMatches || activation.Bearer == "" ||
		len(activation.Bearer) > 4096 || grantErr != nil || activation.Generation < 1 ||
		!activation.ExpiresAt.After(now) || activation.ExpiresAt.After(now.Add(brokerMaximumSessionTTL)) ||
		b.platformProxyURL == "" || transportURL == "" || activation.ProxyURL != b.platformProxyURL ||
		(hasRouteIdentity && (!validBrokerTicketIdentity(ticketIdentity, now) ||
			activation.ExpiresAt.After(activation.TicketDeadline) || activation.Provider == "" ||
			activation.ProfileRevision == "" || activation.Model == "")) {
		writeError(w, http.StatusUnauthorized, "invalid inference activation")
		return
	}
	session.activationSecret = ""
	session.grantID = activation.GrantID
	session.bearer = activation.Bearer
	// Keep activation.ProxyURL as the authenticated identity check above, but
	// send ticket traffic over the direct transport origin. The grant proof is
	// still verified by the same Platform endpoint and redirects stay disabled.
	session.proxyURL = transportURL
	session.generation = activation.Generation
	session.expiresAt = activation.ExpiresAt
	session.provider = activation.Provider
	session.profileRevision = activation.ProfileRevision
	session.model = activation.Model
	session.requestModel = activation.Model
	session.ticketAgentID = activation.AgentID
	session.ticketSlotID = activation.SlotID
	session.ticketDeadline = activation.TicketDeadline
	w.Header().Set("Cache-Control", "no-store")
	writeJSON(w, http.StatusOK, map[string]bool{"active": true})
}

func validBrokerTicketIdentity(identity brokerTicketIdentity, now time.Time) bool {
	_, grantErr := uuid.Parse(identity.GrantID)
	_, agentErr := uuid.Parse(identity.AgentID)
	validSlot := len(identity.SlotID) == len("slot-0") && strings.HasPrefix(identity.SlotID, "slot-") &&
		identity.SlotID[len(identity.SlotID)-1] >= '0' && identity.SlotID[len(identity.SlotID)-1] <= '7'
	return grantErr == nil && agentErr == nil && validSlot && identity.TicketDeadline.After(now)
}

func (b *inferenceBroker) claimRun(id, runID string, identity brokerTicketIdentity, benchVersion int) bool {
	b.mu.RLock()
	session := b.sessions[id]
	b.mu.RUnlock()
	if session == nil {
		return false
	}
	if _, err := uuid.Parse(runID); err != nil {
		return false
	}
	session.mu.Lock()
	defer session.mu.Unlock()
	now := time.Now()
	if session.boundRunID != "" || session.bearer == "" || !session.expiresAt.After(now) {
		return false
	}
	if benchVersion < 7 {
		// Bounded transition compatibility: old platform/subnet clients do not
		// carry route identity. Historical OpenRouter scoring keeps its original
		// aggregate provider/profile key so the reviewed v5/v6 baseline remains
		// valid even after this broker learns the v7 route fields.
		session.provider = "openrouter"
		session.profileRevision = llm.OpenRouterRelayProfileRevision
		session.model = llm.LockedHarnessModel
	}
	if benchVersion >= 7 {
		expected := brokerTicketIdentity{
			GrantID: session.grantID, AgentID: session.ticketAgentID,
			SlotID: session.ticketSlotID, TicketDeadline: session.ticketDeadline,
		}
		if !validBenchmarkRouteProfile(benchVersion, session.profileRevision) || !validBrokerTicketIdentity(identity, now) ||
			identity.GrantID != expected.GrantID || identity.AgentID != expected.AgentID ||
			identity.SlotID != expected.SlotID || !identity.TicketDeadline.Equal(expected.TicketDeadline) {
			return false
		}
	}
	if session.model != llm.HarnessModelForVersion(benchVersion) || session.provider == "" || session.profileRevision == "" {
		return false
	}
	session.requestModel = session.model
	session.boundRunID = runID
	session.benchVersion = benchVersion
	// Only the hosted lane widens. v2-v6 embeddings still land on the one
	// Ollama container this host runs, so they keep the single-slot lane they
	// have always had; leaving embeddingConcurrency at zero is that lane.
	if usesPlatformEmbedding(benchVersion) {
		session.embeddingConcurrency = v8EmbeddingSessionConcurrency
	}
	return true
}

func (b *inferenceBroker) bindSource(id, runID, sourceIP string) bool {
	b.mu.RLock()
	session := b.sessions[id]
	b.mu.RUnlock()
	if session == nil || net.ParseIP(sourceIP) == nil {
		return false
	}
	session.mu.Lock()
	defer session.mu.Unlock()
	if session.boundRunID != runID || session.expectedSourceIP != "" ||
		(session.bearer == "" && session.legacyGateway == "" && session.trustedChatHandler == nil) ||
		!session.expiresAt.After(time.Now()) {
		return false
	}
	session.expectedSourceIP = sourceIP
	return true
}

// replaceBoundSource atomically moves one ticket-bound run to a replacement
// sandbox after the old container has been stopped. It is intentionally a CAS:
// only the same run and exact prior source may replace the binding, so the
// compatibility restart cannot widen the ticket to two live containers.
func (b *inferenceBroker) replaceBoundSource(id, runID, oldSourceIP, newSourceIP string) bool {
	b.mu.RLock()
	session := b.sessions[id]
	b.mu.RUnlock()
	if session == nil || net.ParseIP(oldSourceIP) == nil || net.ParseIP(newSourceIP) == nil {
		return false
	}
	session.mu.Lock()
	defer session.mu.Unlock()
	if session.boundRunID != runID || session.expectedSourceIP != oldSourceIP ||
		(session.bearer == "" && session.legacyGateway == "" && session.trustedChatHandler == nil) ||
		!session.expiresAt.After(time.Now()) {
		return false
	}
	session.expectedSourceIP = newSourceIP
	return true
}

// embeddingLaneLocked is how many embedding calls this session may have in
// flight. Caller holds session.mu.
//
// Zero means "never set", which is every pre-v7 and legacy session, and it
// resolves to one -- byte-identical to the single-slot bool this replaced. Only
// claimRun widens it, and only for v7.
func (session *brokerSession) embeddingLaneLocked() int {
	if session.embeddingConcurrency < 1 {
		return 1
	}
	return session.embeddingConcurrency
}

func (session *brokerSession) activeLocked(now time.Time) bool {
	return session.boundRunID != "" && session.expectedSourceIP != "" &&
		session.expiresAt.After(now) &&
		(session.bearer != "" || session.legacyGateway != "" || session.trustedChatHandler != nil)
}

func destroyBrokerSession(session *brokerSession) {
	if session == nil {
		return
	}
	session.mu.Lock()
	pending := make([]chan struct{}, 0, len(session.embeddingCalls))
	for done, cancel := range session.embeddingCalls {
		pending = append(pending, done)
		cancel()
	}
	for _, cancel := range session.cancels {
		cancel()
	}
	clear(session.cancels)
	for i := range session.privateKey {
		session.privateKey[i] = 0
	}
	session.activationSecret = ""
	session.bearer = ""
	session.legacyGateway = ""
	session.trustedChatHandler = nil
	session.requestModel = ""
	session.embeddingPhaseActive = false
	session.mu.Unlock()
	for _, done := range pending {
		<-done
	}
}

func (b *inferenceBroker) remove(id string) {
	b.mu.Lock()
	session := b.sessions[id]
	delete(b.sessions, id)
	b.mu.Unlock()
	destroyBrokerSession(session)
}

func (b *inferenceBroker) removeRun(id, runID string) bool {
	b.mu.Lock()
	session := b.sessions[id]
	if session == nil {
		b.mu.Unlock()
		return false
	}
	session.mu.Lock()
	owned := session.boundRunID != "" && session.boundRunID == runID
	session.mu.Unlock()
	if !owned {
		b.mu.Unlock()
		return false
	}
	delete(b.sessions, id)
	b.mu.Unlock()
	destroyBrokerSession(session)
	return true
}

func (b *inferenceBroker) pruneExpired(now time.Time) {
	b.mu.RLock()
	ids := make([]string, 0)
	for id, session := range b.sessions {
		session.mu.Lock()
		expired := (!session.expiresAt.IsZero() && !session.expiresAt.After(now)) ||
			(session.expiresAt.IsZero() && now.Sub(session.preparedAt) > 2*time.Minute)
		session.mu.Unlock()
		if expired {
			ids = append(ids, id)
		}
	}
	b.mu.RUnlock()
	for _, id := range ids {
		b.remove(id)
	}
}

func (b *inferenceBroker) cancel(w http.ResponseWriter, r *http.Request) {
	if !b.requireControl(w, r) {
		return
	}
	b.remove(r.PathValue("id"))
	w.WriteHeader(http.StatusNoContent)
}

func (b *inferenceBroker) handle(w http.ResponseWriter, r *http.Request) {
	b.pruneExpired(time.Now())
	session := b.sessionForSource(sourceIP(r.RemoteAddr))
	if session == nil {
		writeError(w, http.StatusUnauthorized, "inference session unavailable")
		return
	}
	rest := "/" + strings.TrimLeft(r.PathValue("rest"), "/")
	if rest == "/health" && r.Method == http.MethodGet {
		b.health(w, session)
		return
	}
	// The frozen starter-kit Chutes adapter appends /chat/completions to its
	// configured base URL, while newer OpenAI-compatible clients append
	// /v1/chat/completions. Accept both at this trusted, source-bound boundary;
	// both routes are forwarded to the one locked upstream relay endpoint.
	if (rest != "/chat/completions" && rest != "/v1/chat/completions") || r.Method != http.MethodPost {
		writeError(w, http.StatusNotFound, "inference route not found")
		return
	}
	b.handleChat(w, r, session)
}

// handleOpenRouterShim is the HTTPS compatibility door for harnesses that
// compile https://openrouter.ai/api/v1 into their source and therefore cannot
// consume the injected ticket broker URL. Docker resolves only that hostname
// to the validator gateway and preserves the sandbox source IP through the
// local REDIRECT, so this reaches the same source-bound session and the same
// locked-model/accounting path as the documented broker endpoint.
func (b *inferenceBroker) handleOpenRouterShim(w http.ResponseWriter, r *http.Request) {
	host := strings.ToLower(strings.TrimSpace(r.Host))
	if parsedHost, _, err := net.SplitHostPort(host); err == nil {
		host = parsedHost
	}
	if host != "openrouter.ai" || r.Method != http.MethodPost || r.URL.Path != "/api/v1/chat/completions" {
		writeError(w, http.StatusNotFound, "inference route not found")
		return
	}
	b.pruneExpired(time.Now())
	session := b.sessionForSource(sourceIP(r.RemoteAddr))
	if session == nil {
		writeError(w, http.StatusUnauthorized, "inference session unavailable")
		return
	}
	b.handleChat(w, r, session)
}

func (b *inferenceBroker) handleChat(w http.ResponseWriter, r *http.Request, session *brokerSession) {
	session.mu.Lock()
	if session.inFlight >= brokerPerSourceConcurrency {
		session.mu.Unlock()
		w.Header().Set("Retry-After", "1")
		writeError(w, http.StatusTooManyRequests, "inference source is at capacity")
		return
	}
	session.inFlight++
	session.mu.Unlock()
	defer func() {
		session.mu.Lock()
		session.inFlight--
		session.mu.Unlock()
	}()
	b.proxy(w, r, session)
}

type embeddingRequest struct {
	Model string   `json:"model"`
	Input []string `json:"input"`
}

type embeddingResponse struct {
	Embeddings      [][]float64 `json:"embeddings"`
	PromptEvalCount int         `json:"prompt_eval_count,omitempty"`
}

// handleEmbedding exposes only the deterministic embedding operation to the
// source-bound harness. Provider discovery, model management, generation, and
// administrative APIs remain unreachable from the sandbox.
func (b *inferenceBroker) handleEmbedding(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost || r.URL.Path != embeddingAPIPath {
		writeError(w, http.StatusNotFound, "embedding route not found")
		return
	}
	b.pruneExpired(time.Now())
	session := b.sessionForSource(sourceIP(r.RemoteAddr))
	if session == nil {
		writeError(w, http.StatusUnauthorized, "embedding session unavailable")
		return
	}
	session.mu.Lock()
	benchVersion := session.benchVersion
	session.mu.Unlock()
	if !usesPlatformEmbedding(benchVersion) && b.embeddingURL == "" {
		writeError(w, http.StatusServiceUnavailable, "embedding service unavailable")
		return
	}
	requestContext, cancelRequest := context.WithTimeout(r.Context(), 65*time.Second)
	var cancelOnce sync.Once
	cancel := func() {
		cancelOnce.Do(func() {
			cancelRequest()
			_ = r.Body.Close()
		})
	}
	session.mu.Lock()
	if !session.embeddingPhaseActive {
		session.mu.Unlock()
		cancel()
		writeError(w, http.StatusConflict, "embedding phase unavailable")
		return
	}
	if session.embeddingInFlight >= session.embeddingLaneLocked() {
		session.mu.Unlock()
		cancel()
		w.Header().Set("Retry-After", "1")
		writeError(w, http.StatusTooManyRequests, "embedding source is at capacity")
		return
	}
	done := make(chan struct{})
	session.embeddingInFlight++
	if session.embeddingCalls == nil {
		session.embeddingCalls = make(map[chan struct{}]context.CancelFunc)
	}
	session.embeddingCalls[done] = cancel
	session.mu.Unlock()
	slotAcquired := false
	defer func() {
		if slotAcquired {
			<-b.embeddingSlots
		}
		session.mu.Lock()
		if _, tracked := session.embeddingCalls[done]; tracked {
			delete(session.embeddingCalls, done)
			session.embeddingInFlight--
		}
		session.mu.Unlock()
		cancel()
		close(done)
	}()

	body, err := io.ReadAll(io.LimitReader(r.Body, embeddingBodyLimit+1))
	if err != nil || len(body) > embeddingBodyLimit {
		writeError(w, http.StatusRequestEntityTooLarge, "embedding request too large")
		return
	}
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.DisallowUnknownFields()
	var payload embeddingRequest
	// Everything about the payload is still validated strictly -- unknown
	// fields, trailing JSON, input count, per-input size -- except the model
	// name, which is no longer an allowlist. See servedModel below.
	if decoder.Decode(&payload) != nil || decoder.Decode(&struct{}{}) != io.EOF ||
		len(payload.Input) == 0 || len(payload.Input) > embeddingMaximumInputs {
		writeError(w, http.StatusBadRequest, "invalid embedding request")
		return
	}
	for _, input := range payload.Input {
		if input == "" || len(input) > embeddingBodyLimit {
			writeError(w, http.StatusBadRequest, "invalid embedding request")
			return
		}
	}
	inputBytes := 0
	for _, input := range payload.Input {
		inputBytes += len(input)
	}
	session.mu.Lock()
	if !session.embeddingPhaseActive {
		session.mu.Unlock()
		writeError(w, http.StatusConflict, "embedding phase unavailable")
		return
	}
	if session.embeddingRequests+1 > embeddingSessionRequests ||
		session.embeddingInputs+uint64(len(payload.Input)) > embeddingSessionInputs ||
		session.embeddingInputBytes+uint64(inputBytes) > embeddingSessionInputBytes {
		session.mu.Unlock()
		writeError(w, http.StatusTooManyRequests, "embedding session budget exhausted")
		return
	}
	session.embeddingRequests++
	session.embeddingInputs += uint64(len(payload.Input))
	session.embeddingInputBytes += uint64(inputBytes)
	runID := session.boundRunID
	ablationScope := session.ablation
	ordinaryAblationCall := -1
	if ablationScope != nil && ablationScope.lane == ablation.LaneOrdinary {
		ordinaryAblationCall = ablationScope.reserveOrdinaryCall(false, body)
	}
	session.mu.Unlock()

	// The matching synthetic lane returns before an upstream slot, provider
	// request, or provider-accounting counter is touched. A revoked responder is
	// terminal for the attempt and cannot silently degrade into ordinary
	// embeddings.
	if ablationScope != nil && requestContext.Err() != nil {
		writeError(w, http.StatusRequestTimeout, "ablation embedding cancelled")
		return
	}
	if ablationScope != nil && ablationScope.lane == ablation.LaneEmbedding {
		decoded, responseErr := ablationScope.responder.Embeddings(payload.Input)
		if responseErr != nil {
			writeError(w, http.StatusServiceUnavailable, "synthetic embedding unavailable")
			return
		}
		writeJSON(w, http.StatusOK, decoded)
		return
	}
	if ablationScope != nil && ablationScope.lane == ablation.LaneInference {
		replayed, replayErr := ablationScope.replayCall(false, body)
		if replayErr != nil {
			writeError(w, http.StatusServiceUnavailable, "ordinary embedding replay unavailable")
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Cache-Control", "no-store")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(replayed)
		return
	}

	// The model is a property of the ticket, not of the request -- the same
	// premise #102 applied to the chat door, now applied to this one.
	//
	// It is a stronger premise here than it is there. The chat door at least
	// forwards the caller's body and has to rewrite the field out of it; this
	// door never forwards the caller's body at all. Both lanes below marshal a
	// brand-new upstream request pinned to a constant -- embeddinggemma for the
	// v2-v6 local Ollama lane, perplexity/pplx-embed-v1-0.6b for hosted v7 --
	// so payload.Model has never reached an upstream and is not read again past
	// this point. Rejecting on it bought no isolation whatsoever; it only meant
	// the two doors disagreed about the same miner-authored string, and a
	// harness naming an ordinary Ollama tag (`embeddinggemma:latest`) lost its
	// entire run at the first of ~671 embedding calls with a 400 it could not
	// act on.
	//
	// Substituting is not a relaxation of the lock: what the broker SENDS is
	// unchanged, and the platform proxy re-locks the same value independently
	// (ditto-platform#428). A mismatch is logged exactly as chat logs it,
	// because a harness that names a model it was not granted is a signal worth
	// keeping even once it stops being an error.
	//
	// The mismatch is measured against embeddingModel, not against servedModel.
	// embeddingModel is the name this door has always published -- it is what
	// the harness contract asks for and what all twelve shipped harnesses send
	// verbatim -- whereas servedModel is the implementation behind it, and under
	// v7 no conforming harness can name that. Comparing against the served name
	// would log all ~671 calls of every well-behaved v7 run. Comparing against
	// the published one logs exactly the harnesses that named something nobody
	// told them to name, which is the signal.
	servedModel := embeddingModel
	if usesPlatformEmbedding(benchVersion) {
		servedModel = hostedEmbeddingModel
	}
	if payload.Model != embeddingModel {
		logSubstitutedModel(runID, payload.Model, servedModel)
	}

	// v2-v6 retain the frozen global Ollama lane. Hosted v7 requests are already
	// isolated and serialized per ticket above, so unrelated evaluations must
	// not queue behind an obsolete host-global embedding bottleneck.
	if !usesPlatformEmbedding(benchVersion) {
		select {
		case b.embeddingSlots <- struct{}{}:
			slotAcquired = true
		default:
			w.Header().Set("Retry-After", "1")
			writeError(w, http.StatusTooManyRequests, "embedding service is at capacity")
			return
		}
	}

	var decoded embeddingResponse
	if ablationScope != nil && ablationScope.lane == ablation.LaneOrdinary ||
		(ablationScope == nil && session.confirmationSession) {
		// Confirmation uses the existing local, fixed embedding seam for the
		// paid-free ordinary/reference trace. Both intervention rounds either
		// replay this response or synthesize the targeted lane above.
		decoded, err = b.forwardLocalEmbedding(requestContext, payload.Input)
	} else if usesPlatformEmbedding(benchVersion) {
		decoded, err = b.forwardPlatformEmbeddingWithRetry(requestContext, session, payload.Input)
	} else {
		decoded, err = b.forwardLocalEmbedding(requestContext, payload.Input)
	}
	if err != nil {
		if usesPlatformEmbedding(benchVersion) {
			// #97 made a v7 embedding fault fail the run closed, and it still
			// does. What changes is only WHICH counter it lands in: a platform
			// grant denial is recorded as a lost lease rather than as an
			// upstream provider failure. Embeddings are roughly two thirds of a
			// v7 run's inference requests, so an evicted ticket is most likely
			// to be discovered here first -- which is exactly how a platform
			// eviction came to be reported as "1 upstream failure".
			var denied platformGrantDenied
			agentDecline := false
			session.mu.Lock()
			var saturated platformEmbeddingAtCapacity
			if embeddingRequestCanceled(requestContext) {
				// endEmbeddingPhase deliberately cancels and drains every call
				// that a harness left in flight. A client can cancel its own
				// request for the same reason. Neither event says anything about
				// provider health, so keep it in the same caller-cancellation
				// ledger the chat lane already uses. Counting this as an upstream
				// failure made a concurrent harness fail closed at finalization
				// after all of its delivered requests had succeeded.
				session.callerCancels++
			} else if errors.As(err, &denied) {
				session.grantDenials++
				attribution := "platform fault: the lease is gone"
				if declineIsAgentFault(denied.code) {
					session.grantAgentDeclines++
					agentDecline = true
					attribution = "AGENT fault: the harness spent its own allowance"
				}
				log.Printf(
					"run %s: platform declined the embedding grant (429: %s -- %s); ticket deadline held locally is %s (in %s) -- this is a lease denial, not a provider fault (denial #%d, agent-attributable #%d)",
					session.boundRunID, platformDeclineReason(denied.code), attribution,
					session.ticketDeadline.UTC().Format(time.RFC3339),
					time.Until(session.ticketDeadline).Truncate(time.Second),
					session.grantDenials, session.grantAgentDeclines,
				)
			} else if errors.As(err, &saturated) {
				// The whole bounded wait budget went by and the lane was still
				// full. Distinct from a provider fault, but still the
				// platform's -- see capacityExhaustions.
				session.capacityExhaustions++
				session.failures++
			} else {
				session.failures++
			}
			session.mu.Unlock()
			if agentDecline {
				b.notifyTerminalAgentFailure(session)
			}
		}
		writeError(w, http.StatusBadGateway, "embedding service unavailable")
		return
	}
	for _, vector := range decoded.Embeddings {
		if len(vector) != embeddingDimensions {
			writeError(w, http.StatusBadGateway, "invalid embedding response")
			return
		}
		for _, value := range vector {
			if math.IsNaN(value) || math.IsInf(value, 0) {
				writeError(w, http.StatusBadGateway, "invalid embedding response")
				return
			}
		}
	}
	if ordinaryAblationCall >= 0 {
		responseBody, marshalErr := json.Marshal(decoded)
		if marshalErr != nil {
			writeError(w, http.StatusBadGateway, "invalid embedding response")
			return
		}
		if !ablationScope.completeOrdinaryCall(false, ordinaryAblationCall, responseBody) {
			writeError(w, http.StatusServiceUnavailable, "ordinary embedding trace unavailable")
			return
		}
	}
	writeJSON(w, http.StatusOK, decoded)
}

// forwardPlatformEmbeddingWithRetry gives the v7 embedding lane the same tiny
// second line of defence the chat lane has. Embeddings are roughly two thirds
// of a v7 run's ~1,067 inference requests and had NO retry at all, so they were
// the most likely place for a single transient fault to discard a whole run.
//
// The retryable class is exactly the transient one: a transport failure, or a
// platform 5xx (which is what the platform returns after its own 3-attempt
// provider loop gives up). A grant denial is auth class and returns
// immediately -- retrying a revoked lease cannot succeed and would burn a fresh
// reservation each time. Every extra delivery is recorded in the session's
// retry ledger so a run that survived a fault stays distinguishable from one
// that never faulted.
func (b *inferenceBroker) forwardPlatformEmbeddingWithRetry(
	ctx context.Context, session *brokerSession, inputs []string,
) (embeddingResponse, error) {
	var lastErr error
	capacityWaits := 0
	for attempt := 1; attempt <= ticketTransientMaxAttempts; {
		decoded, err := b.forwardPlatformEmbedding(ctx, session, inputs)
		if err == nil {
			return decoded, nil
		}
		lastErr = err

		// Backpressure first. A capacity wait does NOT consume a transient
		// attempt and does NOT touch the retry ledger: the platform is telling
		// this call to queue, which is a scheduling event, not a fault. Folding
		// it into the fault budget would spend the run's three real recovery
		// attempts on a healthy platform that was merely busy.
		var atCapacity platformEmbeddingAtCapacity
		if errors.As(err, &atCapacity) {
			if capacityWaits >= platformEmbeddingCapacityMaxWaits || ctx.Err() != nil {
				return embeddingResponse{}, err
			}
			capacityWaits++
			session.mu.Lock()
			runID := session.boundRunID
			session.mu.Unlock()
			log.Printf(
				"run %s: platform embedding lane is at capacity; waiting %s (wait %d/%d) -- healthy lease, not a fault",
				runID, atCapacity.retryAfter, capacityWaits, platformEmbeddingCapacityMaxWaits,
			)
			if b.sleep(ctx, atCapacity.retryAfter) != nil {
				break
			}
			continue
		}

		var transient platformEmbeddingTransient
		if !errors.As(err, &transient) || ctx.Err() != nil {
			return embeddingResponse{}, err
		}
		attempt++
		if attempt > ticketTransientMaxAttempts {
			break
		}
		if b.sleep(ctx, b.retry.backoff(attempt-1)) != nil {
			break
		}
		session.mu.Lock()
		session.embeddingRetries++
		retries := session.embeddingRetries
		runID := session.boundRunID
		session.mu.Unlock()
		log.Printf(
			"run %s: retrying v7 embedding attempt %d/%d after a transient platform fault (%v); run retry ledger=%d",
			runID, attempt, ticketTransientMaxAttempts, lastErr, retries,
		)
	}
	return embeddingResponse{}, lastErr
}

func (b *inferenceBroker) forwardLocalEmbedding(ctx context.Context, inputs []string) (embeddingResponse, error) {
	lockedBody, err := json.Marshal(embeddingRequest{Model: embeddingModel, Input: inputs})
	if err != nil {
		return embeddingResponse{}, err
	}
	upstream, err := http.NewRequestWithContext(ctx, http.MethodPost, b.embeddingURL, bytes.NewReader(lockedBody))
	if err != nil {
		return embeddingResponse{}, err
	}
	upstream.Header.Set("Content-Type", "application/json")
	response, err := b.client.Do(upstream)
	if err != nil {
		return embeddingResponse{}, err
	}
	defer response.Body.Close()
	responseBody, err := io.ReadAll(io.LimitReader(response.Body, embeddingResponseLimit+1))
	if err != nil || len(responseBody) > embeddingResponseLimit || response.StatusCode < 200 || response.StatusCode >= 300 {
		return embeddingResponse{}, fmt.Errorf("embedding upstream returned %d", response.StatusCode)
	}
	var decoded embeddingResponse
	if json.Unmarshal(responseBody, &decoded) != nil || len(decoded.Embeddings) != len(inputs) || decoded.PromptEvalCount < 0 {
		return embeddingResponse{}, fmt.Errorf("invalid embedding response")
	}
	return decoded, nil
}

type platformEmbeddingRequest struct {
	Model          string   `json:"model"`
	Input          []string `json:"input"`
	Dimensions     int      `json:"dimensions"`
	EncodingFormat string   `json:"encoding_format"`
}

type platformEmbeddingResponse struct {
	Model string `json:"model"`
	Data  []struct {
		Index     int       `json:"index"`
		Embedding []float64 `json:"embedding"`
	} `json:"data"`
	Usage struct {
		PromptTokens int `json:"prompt_tokens"`
		TotalTokens  int `json:"total_tokens"`
	} `json:"usage"`
}

func platformEmbeddingURL(chatProxyURL string) (string, error) {
	parsed, err := url.Parse(chatProxyURL)
	if err != nil || parsed.Scheme == "" || parsed.Host == "" || parsed.RawQuery != "" || parsed.Fragment != "" ||
		parsed.Path != platformInferenceAPIPath {
		return "", fmt.Errorf("invalid platform inference route")
	}
	parsed.Path = platformEmbeddingAPIPath
	return parsed.String(), nil
}

func (b *inferenceBroker) forwardPlatformEmbedding(ctx context.Context, session *brokerSession, inputs []string) (embeddingResponse, error) {
	body, err := json.Marshal(platformEmbeddingRequest{
		Model: hostedEmbeddingModel, Input: inputs,
		Dimensions: embeddingDimensions, EncodingFormat: "float",
	})
	if err != nil {
		return embeddingResponse{}, err
	}
	session.mu.Lock()
	if !usesPlatformEmbedding(session.benchVersion) || !session.activeLocked(time.Now()) {
		session.mu.Unlock()
		return embeddingResponse{}, fmt.Errorf("embedding session unavailable")
	}
	grantID, bearer, proxyURL, generation := session.grantID, session.bearer, session.proxyURL, session.generation
	privateKey := append(ed25519.PrivateKey(nil), session.privateKey...)
	session.mu.Unlock()
	endpoint, err := platformEmbeddingURL(proxyURL)
	if err != nil {
		return embeddingResponse{}, err
	}
	nonce := uuid.NewString()
	requested := time.Now().UTC().Format("2006-01-02T15:04:05.000000+00:00")
	digest := sha256.Sum256(body)
	message := fmt.Sprintf("ditto-inference:v1:%s:%d:%s:%s:%s", grantID, generation, nonce, requested, hex.EncodeToString(digest[:]))
	proof := base64.RawURLEncoding.EncodeToString(ed25519.Sign(privateKey, []byte(message)))
	upstream, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(body))
	if err != nil {
		return embeddingResponse{}, err
	}
	upstream.Header.Set("Content-Type", "application/json")
	upstream.Header.Set("Authorization", "Bearer "+bearer)
	upstream.Header.Set("X-Ditto-Grant", grantID)
	upstream.Header.Set("X-Ditto-Generation", fmt.Sprint(generation))
	upstream.Header.Set("X-Ditto-Nonce", nonce)
	upstream.Header.Set("X-Ditto-Requested-At", requested)
	upstream.Header.Set("X-Ditto-Proof", proof)
	response, err := b.client.Do(upstream)
	if err != nil {
		// No response at all: transport/connection fault, the transient class.
		return embeddingResponse{}, platformEmbeddingTransient{err: err}
	}
	defer response.Body.Close()
	responseBody, err := io.ReadAll(io.LimitReader(response.Body, embeddingResponseLimit+1))
	if err != nil || len(responseBody) > embeddingResponseLimit || response.StatusCode < 200 || response.StatusCode >= 300 {
		if err == nil && platformEmbeddingIsAtCapacity(response) {
			// Backpressure, not a fault. Checked before the 5xx branch below
			// because it IS a 5xx; the Retry-After header is what tells the two
			// apart, and the platform sets it on this path only.
			return embeddingResponse{}, platformEmbeddingAtCapacity{
				retryAfter: retryAfterDuration(response.Header.Get("Retry-After")),
			}
		}
		if err != nil || response.StatusCode >= 500 {
			// The platform returns 5xx after its own bounded provider loop has
			// already given up, and a truncated read produced no outcome
			// either. Both are transient and worth one more delivery.
			return embeddingResponse{}, platformEmbeddingTransient{
				err: fmt.Errorf("embedding platform returned %d", response.StatusCode),
			}
		}
		if response.StatusCode == http.StatusTooManyRequests {
			// Auth class, not a provider fault: the platform's embedding proxy
			// answers 429 only from begin_inference_request declining the
			// reservation. Typed so the caller neither retries it nor books it
			// as an upstream failure; the message is unchanged so the existing
			// marker-based classification still matches.
			return embeddingResponse{}, platformGrantDenied{
				status: response.StatusCode,
				code:   platformDeclineCode(responseBody),
			}
		}
		return embeddingResponse{}, fmt.Errorf("embedding platform returned %d", response.StatusCode)
	}
	var platformResponse platformEmbeddingResponse
	if json.Unmarshal(responseBody, &platformResponse) != nil || platformResponse.Model != hostedEmbeddingModel ||
		len(platformResponse.Data) != len(inputs) || platformResponse.Usage.PromptTokens < 0 ||
		platformResponse.Usage.TotalTokens != platformResponse.Usage.PromptTokens {
		return embeddingResponse{}, fmt.Errorf("invalid platform embedding response")
	}
	decoded := embeddingResponse{
		Embeddings: make([][]float64, len(inputs)), PromptEvalCount: platformResponse.Usage.PromptTokens,
	}
	for index, item := range platformResponse.Data {
		if item.Index != index || len(item.Embedding) != embeddingDimensions {
			return embeddingResponse{}, fmt.Errorf("invalid platform embedding response")
		}
		for _, value := range item.Embedding {
			if math.IsNaN(value) || math.IsInf(value, 0) {
				return embeddingResponse{}, fmt.Errorf("invalid platform embedding response")
			}
		}
		decoded.Embeddings[index] = item.Embedding
	}
	return decoded, nil
}

func (b *inferenceBroker) sessionForSource(ip string) *brokerSession {
	if net.ParseIP(ip) == nil {
		return nil
	}
	b.mu.RLock()
	defer b.mu.RUnlock()
	for _, session := range b.sessions {
		session.mu.Lock()
		matches := session.expectedSourceIP == ip && session.activeLocked(time.Now())
		session.mu.Unlock()
		if matches {
			return session
		}
	}
	return nil
}

func (b *inferenceBroker) health(w http.ResponseWriter, session *brokerSession) {
	session.mu.Lock()
	defer session.mu.Unlock()
	status := "ok"
	if !session.activeLocked(time.Now()) {
		status = "unavailable"
	}
	writeJSON(w, http.StatusOK, relayHealthSnapshot{
		AccountingVersion:      2,
		Status:                 status,
		Requests:               session.requests,
		Successes:              session.successes,
		InfrastructureFailures: session.failures,
		GrantDenials:           session.grantDenials,
		GrantAgentDeclines:     session.grantAgentDeclines,
		AgentRequestRejections: session.agentRequestRejections,
		CapacityExhaustions:    session.capacityExhaustions,
		RecoveryWaits:          session.recoveryWaits,
		RecoveryExhaustions:    session.recoveryExhaustions,
		EmbeddingRetries:       session.embeddingRetries,
		CallerCancellations:    session.callerCancels,
		UpstreamAttempts:       session.upstreamAttempts,
		Provider:               session.provider,
		ProfileRevision:        session.profileRevision,
		Model:                  session.model,
		UsageAvailable:         session.usageAvailable,
		UsageUnavailable:       session.usageUnavailable,
		PromptTokens:           session.promptTokens,
		PromptBytes:            session.promptBytes,
		CompletionTokens:       session.completionTokens,
		ProviderLatencyMs:      session.providerLatency,
		TTFTStatus:             "not_streamed",
	})
}

func sourceIP(remote string) string {
	host, _, err := net.SplitHostPort(remote)
	if err != nil {
		return ""
	}
	return host
}

func callTrustedChatHandler(ctx context.Context, handler http.Handler, body []byte) (*http.Response, error) {
	if handler == nil {
		return nil, errors.New("trusted chat handler is unavailable")
	}
	request, err := http.NewRequestWithContext(
		ctx, http.MethodPost, "http://confirmation-reader.invalid/v1/chat/completions", bytes.NewReader(body),
	)
	if err != nil {
		return nil, err
	}
	request.Header.Set("Content-Type", "application/json")
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, request)
	return recorder.Result(), nil
}

func (b *inferenceBroker) proxy(w http.ResponseWriter, r *http.Request, session *brokerSession) {
	body, err := io.ReadAll(io.LimitReader(r.Body, brokerBodyLimit+1))
	if err != nil || len(body) > brokerBodyLimit {
		writeError(w, http.StatusRequestEntityTooLarge, "inference request too large")
		return
	}
	var modelRequest struct {
		Model string `json:"model"`
	}
	if json.Unmarshal(body, &modelRequest) != nil {
		writeError(w, http.StatusBadRequest, "invalid inference request")
		return
	}
	session.mu.Lock()
	if sourceIP(r.RemoteAddr) != session.expectedSourceIP || !session.activeLocked(time.Now()) {
		session.mu.Unlock()
		writeError(w, http.StatusUnauthorized, "inference session unavailable")
		return
	}
	// The model is a property of the ticket, not of the request. The harness
	// that produced this body is miner-authored, so its model field is at best
	// advisory: substitute the ticket's model rather than rejecting, so a
	// harness carrying a stale default (every pre-v7 fork of the starter kit
	// defaults to qwen/qwen3-32b) is scored on the locked model instead of
	// failing closed with an error it cannot act on. The platform proxy re-locks
	// the same value independently, so this is convenience, not the boundary.
	requestedModel := modelRequest.Model
	if requestedModel != session.requestModel || session.benchVersion == protocol.BenchVersionV9 {
		rewritten, rewriteErr := normalizeChatRequest(body, session.requestModel, session.benchVersion)
		if rewriteErr != nil {
			if session.benchVersion == protocol.BenchVersionV9 {
				session.agentRequestRejections++
			}
			session.mu.Unlock()
			writeError(w, http.StatusBadRequest, rewriteErr.Error())
			return
		}
		if requestedModel != session.requestModel {
			logSubstitutedModel(session.boundRunID, requestedModel, session.requestModel)
		}
		body = rewritten
	}
	// A v9 intervention is served before any provider accounting, proof
	// construction, or HTTP client is reachable. The coordinator's responder is
	// scoped to this exact case attempt; if it has already been revoked, the
	// request fails here and is never allowed to fall through to paid inference.
	ablationScope := session.ablation
	requestModel := session.requestModel
	if ablationScope != nil && ablationScope.lane == ablation.LaneInference {
		responder := ablationScope.responder
		session.mu.Unlock()
		if r.Context().Err() != nil {
			writeError(w, http.StatusRequestTimeout, "synthetic inference cancelled")
			return
		}
		completion, responseErr := responder.Chat(requestModel, uint64(len(body)))
		if responseErr != nil {
			writeError(w, http.StatusServiceUnavailable, "synthetic inference unavailable")
			return
		}
		w.Header().Set("Cache-Control", "no-store")
		writeJSON(w, http.StatusOK, completion)
		return
	}
	if ablationScope != nil && ablationScope.lane == ablation.LaneEmbedding {
		session.mu.Unlock()
		if r.Context().Err() != nil {
			writeError(w, http.StatusRequestTimeout, "ordinary inference replay cancelled")
			return
		}
		replayed, replayErr := ablationScope.replayCall(true, body)
		if replayErr != nil {
			writeError(w, http.StatusServiceUnavailable, "ordinary inference replay unavailable")
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Cache-Control", "no-store")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(replayed)
		return
	}
	ordinaryAblationCall := -1
	if ablationScope != nil && ablationScope.lane == ablation.LaneOrdinary {
		ordinaryAblationCall = ablationScope.reserveOrdinaryCall(true, body)
	}
	grantID, bearer, proxyURL, generation := session.grantID, session.bearer, session.proxyURL, session.generation
	legacyGateway := session.legacyGateway
	trustedChatHandler := session.trustedChatHandler
	grantDenialRoute := legacyGateway
	if trustedChatHandler != nil {
		grantDenialRoute = "trusted-in-process"
	}
	privateKey := append(ed25519.PrivateKey(nil), session.privateKey...)
	session.requests++
	session.promptBytes += uint64(len(body))
	session.mu.Unlock()

	requestCtx, cancel := context.WithCancel(r.Context())
	cancelID := uuid.NewString()
	session.mu.Lock()
	session.cancels[cancelID] = cancel
	session.mu.Unlock()
	defer func() {
		cancel()
		session.mu.Lock()
		delete(session.cancels, cancelID)
		session.mu.Unlock()
	}()
	if trustedChatHandler != nil {
		proxyURL = "http://confirmation-reader.invalid/v1/chat/completions"
	} else if legacyGateway != "" {
		var routeErr error
		proxyURL, routeErr = relayURL(legacyGateway, "/v1/chat/completions")
		if routeErr != nil {
			writeError(w, http.StatusBadGateway, "inference provider unavailable")
			return
		}
	}
	var responseBody []byte
	var responseStatus int
	var totalLatency uint64
	// The platform owns the FIRST line of provider retry for ticket-scoped v7
	// inference (_PROVIDER_MAX_ATTEMPTS=3 over 408/429/5xx, all under one
	// reservation), so #97 collapsed this loop to a single attempt rather than
	// multiply provider work. That is still the right default, but one attempt
	// means a fault the platform could not absorb discards ~18 minutes of work,
	// so v7 keeps a deliberately tiny second line of defence. See
	// ticketTransientMaxAttempts for why the cap spans the complete backoff window.
	maxAttempts := ticketChatFastMaxAttempts + ticketRecoveryMaxAttempts
	if trustedChatHandler != nil {
		maxAttempts = 1
	} else if legacyGateway != "" {
		maxAttempts = b.retry.maxAttempts
	}
	if maxAttempts < 1 {
		maxAttempts = 1
	}
	capacityWaits := 0
	var endRelayWait func()
	defer func() {
		if endRelayWait != nil {
			endRelayWait()
		}
	}()
	for attempt := 1; attempt <= maxAttempts; attempt++ {
		if attempt > 1 {
			delay := b.retry.backoff(attempt - 1)
			if legacyGateway == "" && attempt > ticketChatFastMaxAttempts {
				if endRelayWait == nil {
					endRelayWait = b.beginRelayWait(session)
				}
				delay = ticketRecoveryBackoff
			}
			if b.sleep(requestCtx, delay) != nil {
				break
			}
		}
		nonce := uuid.NewString()
		requested := time.Now().UTC().Format("2006-01-02T15:04:05.000000+00:00")
		req, buildErr := http.NewRequestWithContext(requestCtx, http.MethodPost, proxyURL, bytes.NewReader(body))
		if buildErr != nil {
			break
		}
		req.Header.Set("Content-Type", "application/json")
		if legacyGateway == "" && trustedChatHandler == nil {
			digest := sha256.Sum256(body)
			message := fmt.Sprintf("ditto-inference:v1:%s:%d:%s:%s:%s", grantID, generation, nonce, requested, hex.EncodeToString(digest[:]))
			proof := base64.RawURLEncoding.EncodeToString(ed25519.Sign(privateKey, []byte(message)))
			req.Header.Set("Authorization", "Bearer "+bearer)
			req.Header.Set("X-Ditto-Grant", grantID)
			req.Header.Set("X-Ditto-Generation", fmt.Sprint(generation))
			req.Header.Set("X-Ditto-Nonce", nonce)
			req.Header.Set("X-Ditto-Requested-At", requested)
			req.Header.Set("X-Ditto-Proof", proof)
		}
		session.mu.Lock()
		session.upstreamAttempts++
		session.mu.Unlock()
		started := time.Now()
		var resp *http.Response
		var requestErr error
		if trustedChatHandler != nil {
			resp, requestErr = callTrustedChatHandler(requestCtx, trustedChatHandler, body)
		} else {
			resp, requestErr = b.client.Do(req)
		}
		totalLatency += uint64(time.Since(started).Milliseconds())
		if requestErr != nil {
			if requestCtx.Err() != nil {
				break
			}
			continue
		}
		atCapacity := legacyGateway == "" && trustedChatHandler == nil && platformIsAtCapacity(resp)
		capacityPause := retryAfterDuration(resp.Header.Get("Retry-After"))
		candidateBody, readErr := io.ReadAll(io.LimitReader(resp.Body, (16<<20)+1))
		_ = resp.Body.Close()
		responseStatus = resp.StatusCode
		if readErr != nil || len(candidateBody) > 16<<20 {
			continue
		}
		responseBody = candidateBody
		// Backpressure, not a fault: the lease is healthy and the lane was
		// momentarily full. Handled before the transient branch below because
		// it IS a 5xx, and waiting out a queue is not the same event as
		// surviving a provider blip -- charging it to the transient
		// transient budget would spend a run's whole margin on a busy minute.
		// This is the chat-lane twin of the embedding path, which has answered
		// backpressure this way since dittobench-api #103.
		if atCapacity {
			if capacityWaits >= platformChatCapacityMaxWaits {
				// The whole bounded wait budget went by and the lane was still
				// full. This is genuinely ambiguous -- a saturated rail can be
				// an under-provisioned platform OR one embed-heavy ticket
				// crowding out its neighbours -- and it is deliberately NOT
				// reclassified as the agent's fault. A miner cannot provision
				// the lane, and blaming one for a platform running at its limit
				// is the same error as this change is fixing, pointed the other
				// way. It gets its own CODE so an operator can see it; it keeps
				// the infrastructure CLASS so it keeps its grant.
				session.mu.Lock()
				session.capacityExhaustions++
				session.mu.Unlock()
				break
			}
			capacityWaits++
			if b.sleep(requestCtx, capacityPause) != nil {
				break
			}
			attempt--
			continue
		}
		// Auth class: the platform declined the grant. The lease is gone, or its
		// budget is spent, so every further attempt would fail identically while
		// still consuming a fresh reservation and one more request from a grant
		// that cannot serve it. Stop immediately -- both terminal reasons want
		// the same action here, and only the reporting below tells them apart.
		if platformDeniesGrant(grantDenialRoute, responseStatus) {
			break
		}
		if responseStatus == http.StatusRequestTimeout || responseStatus == http.StatusTooManyRequests || responseStatus >= 500 {
			continue
		}
		break
	}
	if requestCtx.Err() != nil {
		session.mu.Lock()
		session.callerCancels++
		session.providerLatency += totalLatency
		session.mu.Unlock()
		writeError(w, http.StatusConflict, "inference session unavailable")
		return
	}
	if responseStatus >= 400 && responseStatus < 500 && responseStatus != http.StatusTooManyRequests {
		// The platform rejected the harness's REQUEST before reserving any
		// capacity: a body its schema refused, an oversized payload, a model
		// the grant will not serve. No reservation was made and no provider was
		// contacted, so nothing here is an upstream fault -- the only thing
		// that went wrong is the bytes the harness sent.
		//
		// usageUnavailable is still incremented, unchanged, so the run fails
		// exactly as before via requireCompleteV7Usage. The new counter is
		// purely attributive.
		session.mu.Lock()
		session.usageUnavailable++
		session.agentRequestRejections++
		session.providerLatency += totalLatency
		rejections, runID := session.agentRequestRejections, session.boundRunID
		session.mu.Unlock()
		// The platform's own explanation is the only thing that can tell a
		// miner WHICH of their bytes was refused, and this is its last hop
		// before the harness. Discarding it here is what left `Cooking` unable
		// to discover the offending field across three submissions. It goes to
		// both destinations: the harness's stderr, and the operator's log.
		detail := platformRejectionMessage(responseBody)
		if detail == "" {
			detail = "inference request denied"
		}
		log.Printf(
			"run %s: platform rejected the harness's inference request with %d before any reservation -- AGENT fault, no provider was contacted (rejection #%d): %s",
			runID, responseStatus, rejections, detail,
		)
		writeError(w, responseStatus, detail)
		return
	}
	// A platform grant denial is a lost lease, not a provider fault. It is
	// counted separately so the run's failure names the real cause; the harness
	// still sees the byte-identical 502 it saw before, because this run is going
	// to be discarded either way and its remaining requests must not observe a
	// changed gateway contract mid-benchmark.
	if platformDeniesGrant(grantDenialRoute, responseStatus) {
		session.mu.Lock()
		session.grantDenials++
		session.providerLatency += totalLatency
		declineCode := platformDeclineCode(responseBody)
		attribution := "platform fault: the lease is gone"
		agentDecline := false
		if declineIsAgentFault(declineCode) {
			session.grantAgentDeclines++
			agentDecline = true
			attribution = "AGENT fault: the harness spent its own allowance"
		}
		denials, agentDenials := session.grantDenials, session.grantAgentDeclines
		runID, deadline := session.boundRunID, session.ticketDeadline
		session.mu.Unlock()
		if agentDecline {
			b.notifyTerminalAgentFailure(session)
		}
		// The code, when the platform sends one, is the difference between "the
		// validator lost this lease" and "the agent spent its allowance" -- two
		// findings that call for opposite follow-up, and which until now were
		// not merely the same log line but the same CLASSIFICATION. Older
		// platforms send no code, get the old wording, and stay no-fault.
		log.Printf(
			"run %s: platform declined the inference grant (429: %s -- %s); ticket deadline held locally is %s (in %s) -- this is a lease denial, not a provider fault (denial #%d, agent-attributable #%d)",
			runID, platformDeclineReason(declineCode), attribution,
			deadline.UTC().Format(time.RFC3339), time.Until(deadline).Truncate(time.Second),
			denials, agentDenials,
		)
		writeError(w, http.StatusBadGateway, "inference provider unavailable")
		return
	}
	if responseStatus < 200 || responseStatus >= 300 || len(responseBody) == 0 {
		session.mu.Lock()
		session.failures++
		if endRelayWait != nil {
			session.recoveryExhaustions++
		}
		session.providerLatency += totalLatency
		session.mu.Unlock()
		writeError(w, http.StatusBadGateway, "inference provider unavailable")
		return
	}
	var decoded struct {
		Usage *struct {
			PromptTokens     int `json:"prompt_tokens"`
			CompletionTokens int `json:"completion_tokens"`
		} `json:"usage"`
	}
	usageOK := json.Unmarshal(responseBody, &decoded) == nil && decoded.Usage != nil && decoded.Usage.PromptTokens >= 0 && decoded.Usage.CompletionTokens >= 0
	session.mu.Lock()
	session.successes++
	session.providerLatency += totalLatency
	if usageOK {
		session.usageAvailable++
		session.promptTokens += uint64(decoded.Usage.PromptTokens)
		session.completionTokens += uint64(decoded.Usage.CompletionTokens)
	} else {
		session.usageUnavailable++
	}
	session.mu.Unlock()
	if ordinaryAblationCall >= 0 {
		if !ablationScope.completeOrdinaryCall(true, ordinaryAblationCall, responseBody) {
			writeError(w, http.StatusServiceUnavailable, "ordinary inference trace unavailable")
			return
		}
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(responseBody)
}

func (b *inferenceBroker) trustedProbe(ctx context.Context, id string) error {
	b.mu.RLock()
	session := b.sessions[id]
	b.mu.RUnlock()
	if session == nil {
		return fmt.Errorf("inference session unavailable")
	}
	body, _ := json.Marshal(map[string]any{
		"model":      session.requestModel,
		"messages":   []map[string]string{{"role": "user", "content": "Reply OK."}},
		"max_tokens": 1, "temperature": 0, "stream": false,
	})
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", bytes.NewReader(body)).WithContext(ctx)
	session.mu.Lock()
	req.RemoteAddr = net.JoinHostPort(session.expectedSourceIP, "1")
	session.mu.Unlock()
	recorder := httptest.NewRecorder()
	b.proxy(recorder, req, session)
	if recorder.Code < 200 || recorder.Code >= 300 {
		return fmt.Errorf("ticket inference probe returned %d", recorder.Code)
	}
	var decoded struct {
		Choices []json.RawMessage `json:"choices"`
	}
	if json.Unmarshal(recorder.Body.Bytes(), &decoded) != nil || len(decoded.Choices) == 0 {
		return fmt.Errorf("ticket inference probe returned no completion")
	}
	session.mu.Lock()
	benchVersion := session.benchVersion
	session.mu.Unlock()
	if usesPlatformEmbedding(benchVersion) {
		embedding, err := b.forwardPlatformEmbedding(ctx, session, []string{"validator embedding preflight"})
		if err != nil {
			return fmt.Errorf("ticket embedding probe failed: %w", err)
		}
		if len(embedding.Embeddings) != 1 || len(embedding.Embeddings[0]) != embeddingDimensions {
			return fmt.Errorf("ticket embedding probe returned no vector")
		}
	}
	return nil
}

func (b *inferenceBroker) snapshot(id string) (relayHealthSnapshot, error) {
	b.mu.RLock()
	session := b.sessions[id]
	b.mu.RUnlock()
	if session == nil {
		return relayHealthSnapshot{}, fmt.Errorf("inference session unavailable")
	}
	session.mu.Lock()
	defer session.mu.Unlock()
	return relayHealthSnapshot{
		AccountingVersion: 2, Status: "ok", Requests: session.requests,
		Successes: session.successes, InfrastructureFailures: session.failures,
		GrantDenials: session.grantDenials, EmbeddingRetries: session.embeddingRetries,
		GrantAgentDeclines:     session.grantAgentDeclines,
		AgentRequestRejections: session.agentRequestRejections,
		CapacityExhaustions:    session.capacityExhaustions,
		RecoveryWaits:          session.recoveryWaits,
		RecoveryExhaustions:    session.recoveryExhaustions,
		CallerCancellations:    session.callerCancels, UpstreamAttempts: session.upstreamAttempts,
		Provider: session.provider, ProfileRevision: session.profileRevision,
		Model: session.model, UsageAvailable: session.usageAvailable,
		UsageUnavailable: session.usageUnavailable, PromptTokens: session.promptTokens,
		PromptBytes: session.promptBytes, CompletionTokens: session.completionTokens,
		ProviderLatencyMs: session.providerLatency, TTFTStatus: "not_streamed",
	}, nil
}

type brokerCaseSnapshot struct {
	Requests  uint64
	Successes uint64
	InFlight  int
}

// caseSnapshot is the ticket-scoped boundary used to attribute model use to
// one ordinary benchmark case. It deliberately stays internal instead of
// extending the aggregate relay health wire: only the source-bound broker can
// prove that no sibling request overlaps the case window.
func (b *inferenceBroker) caseSnapshot(id string) (brokerCaseSnapshot, error) {
	b.mu.RLock()
	session := b.sessions[id]
	b.mu.RUnlock()
	if session == nil {
		return brokerCaseSnapshot{}, fmt.Errorf("inference session unavailable")
	}
	session.mu.Lock()
	defer session.mu.Unlock()
	return brokerCaseSnapshot{
		Requests: session.requests, Successes: session.successes, InFlight: session.inFlight,
	}, nil
}

// rewriteRequestModel replaces the caller's `model` with the ticket's, leaving
// every other field of the request untouched. Decoding into a generic map and
// re-encoding is deliberate: it normalises exactly one field and cannot smuggle
// an unmodelled field past the schema the platform proxy validates downstream.
func rewriteRequestModel(body []byte, model string) ([]byte, error) {
	return normalizeChatRequest(body, model, 0)
}

const benchV9DefaultReasoningEffort = "medium"

var benchV9ReasoningEfforts = map[string]struct{}{
	"low": {}, "medium": {}, "high": {},
}

// normalizeChatRequest pins the ticket model and, for v9, canonicalizes the
// agent-owned reasoning strategy before either a Platform reservation or a
// local relay is reachable. The flat OpenAI alias and nested OpenRouter form
// collapse to one nested block. Provider-only controls never survive the
// boundary: the trusted side owns exclude=true for privacy, while the agent
// owns only low/medium/high effort. V7/V8 retain byte-for-byte reasoning
// semantics and their fixed-medium Platform contract.
func normalizeChatRequest(body []byte, model string, benchVersion int) ([]byte, error) {
	var decoded map[string]any
	if err := json.Unmarshal(body, &decoded); err != nil || decoded == nil {
		return nil, fmt.Errorf("inference request is not a JSON object")
	}
	decoded["model"] = model
	if benchVersion != protocol.BenchVersionV9 {
		return json.Marshal(decoded)
	}

	nestedRaw, nestedPresent := decoded["reasoning"]
	flatRaw, flatPresent := decoded["reasoning_effort"]
	nestedEffort := ""
	flatEffort := ""
	if nestedPresent {
		nested, ok := nestedRaw.(map[string]any)
		if !ok || len(nested) != 1 {
			return nil, fmt.Errorf("invalid reasoning")
		}
		candidate, ok := nested["effort"].(string)
		if !ok {
			return nil, fmt.Errorf("invalid reasoning effort")
		}
		if _, ok = benchV9ReasoningEfforts[candidate]; !ok {
			return nil, fmt.Errorf("invalid reasoning effort")
		}
		nestedEffort = candidate
	}
	if flatPresent {
		candidate, ok := flatRaw.(string)
		if !ok {
			return nil, fmt.Errorf("invalid reasoning_effort")
		}
		if _, ok = benchV9ReasoningEfforts[candidate]; !ok {
			return nil, fmt.Errorf("invalid reasoning_effort")
		}
		flatEffort = candidate
	}
	if nestedEffort != "" && flatEffort != "" && nestedEffort != flatEffort {
		return nil, fmt.Errorf("conflicting reasoning effort")
	}
	effort := nestedEffort
	if effort == "" {
		effort = flatEffort
	}
	if effort == "" {
		effort = benchV9DefaultReasoningEffort
	}
	delete(decoded, "reasoning_effort")
	decoded["reasoning"] = map[string]any{"effort": effort, "exclude": true}
	return json.Marshal(decoded)
}

// logSubstitutedModel records that a harness asked for a model other than the
// one its ticket serves. Both broker doors call this and emit the identical
// sentence, because on both doors the caller's model string is decorative: chat
// rewrites it out of the body before forwarding (rewriteRequestModel above),
// and embeddings discard the caller's body entirely and build a fresh upstream
// request pinned to their own constant. What the string is still good for is
// telling an operator that a harness is not reading its injected configuration,
// or is probing for a model it was not granted -- so it is worth one line, and
// worth having that line be the same line on both routes.
func logSubstitutedModel(runID, requested, served string) {
	log.Printf("run %s: harness requested model %q; serving the ticket model %q",
		runID, requested, served)
}
