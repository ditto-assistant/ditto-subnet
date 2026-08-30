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
	"encoding/base32"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"math"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/ablation"
	"github.com/ditto-assistant/dittobench-api/internal/llm"
	"github.com/ditto-assistant/dittobench-api/internal/longmemeval"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/google/uuid"
)

const (
	minerRecoverableFailureHeader = "X-Ditto-Inference-Failure-Class"
	minerRecoverableGeneration    = "miner_recoverable_generation"
)

func minerRecoverablePlatformFailure(legacyGateway string, trustedChatHandler http.Handler, status int, class string) bool {
	return legacyGateway == "" && trustedChatHandler == nil &&
		status == http.StatusBadGateway && class == minerRecoverableGeneration
}

const (
	brokerBodyLimit = 4 << 20
	// Must exceed the longest live Platform grant. Canonical scoring is
	// 180 minutes; confirmation is 4h; in-flight 430-minute tickets still
	// activate until they drain. Inference grant expiry is that deadline,
	// so a shorter cap rejects every activation.
	brokerMaximumSessionTTL    = 8 * time.Hour
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

type brokerTicketIdentity struct {
	GrantID        string
	AgentID        string
	SlotID         string
	TicketDeadline time.Time
}

type brokerConfirmationGrant struct {
	Lane               string
	GrantID            string
	Bearer             string
	ProxyURL           string
	Generation         int
	ExpiresAt          time.Time
	Provider           string
	RouteProvider      string
	ReceiptProvider    string
	ProfileRevision    string
	Model              string
	RequestBudget      uint64
	TokenBudget        uint64
	CostBudgetMicrousd uint64
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
	// delayFingerprintKey seeds the deterministic response-delay schedule
	// (delay_fingerprint.go). Minted with the session, never serialized, never
	// shown to the harness or the control plane -- the same custody rule as
	// privateKey above.
	delayFingerprintKey []byte
	delayFP             delayFingerprintConfig
	// trustedChatHandler is an in-process confirmation reader route. It is
	// reachable only after this broker has authenticated the sandbox source;
	// unlike a loopback HTTP listener it cannot be scanned or called by another
	// host process to spend the server-owned provider session.
	trustedChatHandler http.Handler
	generation         int
	expiresAt          time.Time
	expectedSourceIP   string
	sourceEpoch        uint64
	// sourceActiveHandlers counts every provider-capable broker handler after
	// source/capability admission and before it returns. Compatibility rotation
	// advances sourceEpoch, strictly stops the retired process, then waits the
	// retired epoch to drain before a replacement process may start.
	sourceActiveHandlers map[uint64]uint64
	// sourceCapabilityDigest authenticates exactly one fresh submitted
	// sandbox container. Only its SHA-256 digest is retained; the opaque
	// token is injected into that container's compatibility URLs/keys and is
	// rotated before another container can be source-bound.
	sourceCapabilityDigest   [sha256.Size]byte
	sourceCapabilityRequired bool
	sourceCapabilityActive   bool
	provider                 string
	model                    string
	requestModel             string
	// Budget evidence is copied from the authenticated Platform exchange. It is
	// optional during a rolling upgrade; when present it lets this independent
	// broker reject an impossible 4102/4104/4109 attribution instead of trusting
	// one terminal response enough to bill a miner.
	requestBudget             uint64
	tokenBudget               uint64
	embeddingRequestBudget    uint64
	embeddingTokenBudget      uint64
	maxOutputTokens           uint64
	chatDispatches            uint64
	chatChargeUpperBound      uint64
	embeddingDispatches       uint64
	embeddingChargeUpperBound uint64
	profileRevision           string
	preparedAt                time.Time
	ticketAgentID             string
	ticketSlotID              string
	ticketDeadline            time.Time
	boundRunID                string
	benchVersion              int
	confirmationSession       bool
	confirmationGrants        map[string]brokerConfirmationGrant
	embeddingGrant            brokerConfirmationGrant
	inFlight                  int
	// chatConcurrency is the per-source in-flight chat admission for this
	// session. Zero means brokerPerSourceConcurrency; the scorer raises it to
	// the run's effective case concurrency so overlapping /run calls are not
	// starved by a cap sized for serial scoring (see configureCaseConcurrency).
	chatConcurrency int
	// caseGeneration binds every admitted v9+ chat request to the ordinary case
	// window in which it started. A harness may return its /run response while
	// a background request is still inside the broker's bounded recovery loop;
	// run-wide counter deltas cannot distinguish that tail from the next case.
	// Generation-local counters keep the tail attached to its original window.
	caseGeneration       uint64
	activeCaseGeneration uint64
	activeCaseID         string
	caseSnapshots        map[uint64]brokerCaseSnapshot
	caseToolCalls        map[uint64][]brokerModelToolCall
	caseCapabilities     map[string]uint64
	caseCapabilityTokens map[uint64]string
	caseIDs              map[string]uint64
	// runCases is the set of ordinary /run cases currently in flight on this
	// session (refcounted: the scorer may re-enter a case id on retry). It is
	// attribution evidence for the inference trace capture only -- never an
	// admission or accounting input -- and it is what lets a concurrent v10+
	// run still tell the relay which cases a call could belong to.
	runCases map[string]int
	// Session-scoped v10+ tool provenance. Concurrent /run opens no exclusive
	// case windows, so every ordinary chat completion is admitted at
	// caseGeneration 0: its model-emitted tool calls are recorded here,
	// session-wide, and a tool_endpoint request consumes one matching
	// unconsumed emission regardless of which case's prompt produced it (the
	// consumed flag still forbids double-spend). Per-case outcomes are keyed by
	// the wire case id the route's HMAC capability authenticated.
	sessionToolCalls            []brokerModelToolCall
	sessionToolEmitted          uint64
	sessionToolConsumed         uint64
	sessionToolInvalidEmissions uint64
	sessionToolCases            map[string]brokerSessionToolLedger
	// Bench v12 answer-stuffing capture. answerIO records, per active case
	// generation, the ordered bounded/normalized clean-pass model I/O (value
	// tokens only -- never the answer key or raw prose). answerIOByCaseID holds
	// the SAME pointers keyed by wire case id so the scorer can read one case's
	// log post-run. Both are populated only for bench_version>=12, so v9..v11 are
	// byte-identical and unaffected.
	answerIO              map[uint64]*caseModelIOLog
	answerIOByCaseID      map[string]*caseModelIOLog
	embeddingPhaseStarted bool
	embeddingPhaseActive  bool
	embeddingInFlight     int
	embeddingConcurrency  int
	// embeddingQueueChanged wakes calls waiting behind this session's local
	// lane whenever capacity is released or the phase is revoked. Excess
	// harness concurrency is queued inside the trusted broker instead of being
	// reflected back as a fatal Ollama-compatible 429.
	embeddingQueueChanged chan struct{}
	// embeddingCalls tracks every in-flight embedding call so the phase can be
	// ended -- and every one of them revoked -- as a set. It was a single
	// (cancel, done) pair while the lane admitted one request at a time; a
	// hostile harness leaving background calls open must not survive
	// endEmbeddingPhase just because a sibling call replaced the field.
	embeddingCalls      map[chan struct{}]context.CancelFunc
	embeddingRetries    uint64
	embeddingRequests   uint64
	embeddingTokens     uint64
	embeddingInputs     uint64
	embeddingInputBytes uint64
	requests            uint64
	successes           uint64
	failures            uint64
	// minerRecoverableFailures counts authenticated Platform responses that
	// the harness can handle itself. They remain failed requests and are
	// returned unchanged, but do not make the validator infrastructure degraded.
	minerRecoverableFailures uint64
	grantDenials             uint64
	usageAvailable           uint64
	usageUnavailable         uint64
	// grantAgentDeclines is the SUBSET of grantDenials the harness caused: it
	// spent the request or token allowance its own ticket granted, or sent one
	// request too large to reserve. Kept as a subset rather than as a split of
	// grantDenials on purpose -- grantDenials keeps its exact previous meaning
	// and wire value, so which runs FAIL is unchanged and only who is CHARGED
	// moves. See relayFinalizeFailure for the tie-break.
	grantAgentDeclines uint64
	// declineEvidenceMismatches counts terminal allowance codes contradicted by
	// the broker's independently observed request/charge upper bounds. Such a
	// run still fails closed, but it is validator infrastructure and keeps the
	// miner's attempt.
	declineEvidenceMismatches uint64
	// budgetEvidenceAbsences counts terminal allowance codes received while
	// this session had no authenticated request/token budgets. Missing evidence
	// is not proof the harness spent the grant: it is a transport or mixed-
	// rollout gap, and it must not bill a miner.
	budgetEvidenceAbsences uint64
	// terminalAgentFailureNotified makes the first typed agent-attributable
	// allowance decline end the benchmark exactly once. Once the platform says
	// the grant is spent, every later case can only receive the same refusal.
	terminalAgentFailureNotified bool
	// agentRequestRejections counts pre-reservation 4xx (a 400 the platform's
	// schema refused, a 403 on a model, an oversized body) -- the platform
	// rejecting the harness's request without ever reserving capacity. These
	// already fail the run through usageUnavailable; this counter exists so
	// finalize can name a rejected request instead of a spent grant.
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
	draining            bool
	activeHandlers      int
	// counterfactual marks a Bench v12 causal-dependence LaneInference scope. It
	// substitutes ONLY the chat completion (the perturbed model output) and
	// leaves embeddings on their live platform lane, because the v12 scored path
	// records no paired ordinary trace to replay from. The v9 confirmation
	// coordinator never sets this, so its replay-based inference lane is
	// byte-identical.
	counterfactual bool
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

func (lease *brokerAblationLease) beginDrain() error {
	if lease == nil || lease.session == nil || lease.scope == nil {
		return fmt.Errorf("ablation scope unavailable")
	}
	lease.session.mu.Lock()
	defer lease.session.mu.Unlock()
	if lease.session.ablation != lease.scope {
		return fmt.Errorf("ablation scope unavailable")
	}
	lease.scope.draining = true
	return nil
}

func (lease *brokerAblationLease) waitDrained(ctx context.Context) error {
	if ctx == nil || lease == nil || lease.session == nil || lease.scope == nil {
		return fmt.Errorf("ablation drain unavailable")
	}
	ticker := time.NewTicker(10 * time.Millisecond)
	defer ticker.Stop()
	for {
		lease.session.mu.Lock()
		current := lease.session.ablation == lease.scope
		draining := lease.scope.draining
		active := lease.scope.activeHandlers
		lease.session.mu.Unlock()
		if !current || !draining {
			return fmt.Errorf("ablation drain unavailable")
		}
		if active == 0 {
			return nil
		}
		select {
		case <-ctx.Done():
			return fmt.Errorf("ablation broker drain unavailable")
		case <-ticker.C:
		}
	}
}

func (lease *brokerAblationLease) Close() {
	if lease == nil {
		return
	}
	lease.once.Do(func() {
		lease.session.mu.Lock()
		if lease.session.ablation == lease.scope && lease.scope.activeHandlers == 0 {
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
	now := time.Now()
	// Bench v12 admits the LaneInference counterfactual on the scored path in
	// addition to the confirmation ablation. Both still require the run-bound,
	// source-active session. The counterfactual is a SCORED-path construct, so a
	// confirmation session is never a counterfactual even at v12 — otherwise its
	// embedding ablation lane (below) would be rejected. A confirmation session
	// runs the paired inference+embedding ablation under the frozen v9-named
	// contract at any supported confirmation version ({9, 12}); the v9 branch's
	// admission is unchanged (v9 short-circuits before the confirmation clause).
	counterfactual := session.benchVersion >= ablation.BenchVersionV12 && !session.confirmationSession
	allowedVersion := session.benchVersion == ablation.BenchVersionV9 || counterfactual ||
		(session.confirmationSession && ablation.ConfirmationBenchVersionSupported(session.benchVersion))
	if session.boundRunID != runID || !allowedVersion ||
		(!session.activeLocked(now) && !session.confirmationUnboundActiveLocked(now)) || session.ablation != nil {
		return nil, fmt.Errorf("inference session is not available for ablation")
	}
	// The v12 counterfactual substitutes only chat and never replays an ordinary
	// trace, so it neither requires nor records one. It is LaneInference only.
	if counterfactual && request.Lane != ablation.LaneInference {
		return nil, fmt.Errorf("v12 counterfactual scope requires the inference lane")
	}
	if session.ablationTraces == nil {
		session.ablationTraces = make(map[string]*brokerAblationTrace)
	}
	trace := session.ablationTraces[request.CaseID]
	if !counterfactual {
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
	} else if trace == nil {
		// A counterfactual scope needs a trace object only so the shared replay
		// plumbing has a non-nil target; it is never populated or replayed from.
		trace = &brokerAblationTrace{}
	}
	scope := &brokerAblationScope{
		lane: request.Lane, caseID: request.CaseID,
		opaqueUserNamespace: request.OpaqueUserNamespace, responder: request.Responder,
		trace: trace, session: session, counterfactual: counterfactual,
	}
	session.ablation = scope
	return &brokerAblationLease{session: session, scope: scope}, nil
}

func newInferenceBrokerHTTPServer(addr string, handler http.Handler, brokers ...*inferenceBroker) *http.Server {
	server := &http.Server{
		Addr:              addr,
		Handler:           handler,
		ReadHeaderTimeout: brokerReadHeaderTimeout,
		ReadTimeout:       brokerReadTimeout,
		WriteTimeout:      brokerWriteTimeout,
		IdleTimeout:       brokerIdleTimeout,
		MaxHeaderBytes:    brokerMaximumHeaderBytes,
	}
	if len(brokers) == 1 && brokers[0] != nil {
		server.ConnContext = brokers[0].connectionContext
	}
	return server
}

// listenInferenceBrokerTCP4 binds the miner-facing compatibility plane to the
// address family used by Docker's host-gateway mapping. A generic ":port"
// listener is allowed to become an IPv6 wildcard and rely on IPv4-mapped IPv6
// behavior from the validator host. That host sysctl is outside the signed
// stack contract, while every sandbox receives an IPv4 host-gateway address.
// Bind the contract directly instead of depending on host IPv6 policy.
func listenInferenceBrokerTCP4(addr string) (net.Listener, error) {
	return net.Listen("tcp4", addr)
}

type inferenceBroker struct {
	mu                    sync.RWMutex
	sessions              map[string]*brokerSession
	tools                 map[string]toolRoute
	client                *http.Client
	maxSessions           int
	controlToken          string
	platformProxyURL      string
	platformTransportURL  string
	embeddingURL          string
	embeddingSlots        chan struct{}
	embeddingBackpressure embeddingBackpressureGate
	embeddingRequestTTL   time.Duration
	delayFP               delayFingerprintConfig
	sleep                 func(context.Context, time.Duration) error
	relayWait             func(string, bool)
	terminalAgentFailure  func(string)
}

// embeddingBackpressureGate is a validator-wide circuit breaker for the
// hosted embedding provider. The first 503 + Retry-After closes the gate for
// every ticket on this validator. Once the cooldown expires, exactly one call
// probes recovery; peers remain queued until that probe succeeds. This avoids
// the synchronized retry wave that otherwise turns one provider throttle into
// dozens of fresh reservations and duplicate upstream deliveries.
type embeddingBackpressureGate struct {
	mu            sync.Mutex
	retryAt       time.Time
	probeInFlight bool
	generation    uint64
	changed       chan struct{}
}

// embeddingBackpressureProbe identifies the exact cooldown generation whose
// recovery one call is probing. A zero value means the gate was already open.
// The generation prevents a late probe result from clearing newer
// backpressure reported by an older request that was already in flight.
type embeddingBackpressureProbe uint64

func (p embeddingBackpressureProbe) active() bool { return p != 0 }

func (g *embeddingBackpressureGate) changedLocked() <-chan struct{} {
	if g.changed == nil {
		g.changed = make(chan struct{})
	}
	return g.changed
}

func (g *embeddingBackpressureGate) signalLocked() {
	if g.changed != nil {
		close(g.changed)
		g.changed = nil
	}
}

func (g *embeddingBackpressureGate) wait(ctx context.Context) (embeddingBackpressureProbe, error) {
	for {
		g.mu.Lock()
		now := time.Now()
		if g.retryAt.IsZero() {
			g.mu.Unlock()
			return 0, nil
		}
		if !now.Before(g.retryAt) && !g.probeInFlight {
			g.probeInFlight = true
			probe := embeddingBackpressureProbe(g.generation)
			g.mu.Unlock()
			return probe, nil
		}
		changed := g.changedLocked()
		delay := time.Duration(0)
		if now.Before(g.retryAt) {
			delay = g.retryAt.Sub(now)
		}
		g.mu.Unlock()

		if delay > 0 {
			timer := time.NewTimer(delay)
			select {
			case <-ctx.Done():
				if !timer.Stop() {
					select {
					case <-timer.C:
					default:
					}
				}
				return 0, ctx.Err()
			case <-changed:
				if !timer.Stop() {
					select {
					case <-timer.C:
					default:
					}
				}
			case <-timer.C:
			}
			continue
		}

		select {
		case <-ctx.Done():
			return 0, ctx.Err()
		case <-changed:
		}
	}
}

func (g *embeddingBackpressureGate) backpressure(retryAfter time.Duration) {
	if retryAfter <= 0 {
		retryAfter = 250 * time.Millisecond
	}
	g.mu.Lock()
	until := time.Now().Add(retryAfter)
	if until.After(g.retryAt) {
		g.retryAt = until
	}
	g.generation++
	if g.generation == 0 {
		g.generation = 1
	}
	g.probeInFlight = false
	g.signalLocked()
	g.mu.Unlock()
}

func (g *embeddingBackpressureGate) finishProbe(probe embeddingBackpressureProbe) {
	if !probe.active() {
		return
	}
	g.mu.Lock()
	if uint64(probe) != g.generation || !g.probeInFlight {
		g.mu.Unlock()
		return
	}
	g.retryAt = time.Time{}
	g.probeInFlight = false
	g.signalLocked()
	g.mu.Unlock()
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
	provenanceSessionID   string
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

type brokerModelToolCall struct {
	id         string
	name       string
	argsSHA256 string
	consumed   bool
}

// brokerSessionToolLedger is one wire case's tool_endpoint outcome under
// session-scoped provenance: attempts, matches consumed from the session-wide
// emission ledger, and rejections with their finding bits.
type brokerSessionToolLedger struct {
	EndpointAttempts   uint64
	MatchedToolCalls   uint64
	UnmatchedToolCalls uint64
	ToolFindings       uint64
}

// sessionToolProvenanceTotals is the run-level view of the session-wide
// emission ledger, read once after the last case has returned. Emissions that
// were never consumed are the run's model_selected_not_executed count: they
// cannot be attributed to one case without exclusive windows.
type sessionToolProvenanceTotals struct {
	ModelEmitted     uint64
	Consumed         uint64
	InvalidEmissions uint64
}

const (
	toolFindingUnbacked uint64 = 1 << iota
	toolFindingNameArgumentMismatch
	toolFindingDuplicateExecution
	toolFindingCrossCaseReplay
	toolFindingInvalidModelEmission
)

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
// here as a 429: the platform maps every provider rejection into a 502 after
// its single upstream dispatch.
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
	// reservationUpperBound is the tokenizer-independent maximum this one
	// refused request could have charged. It lets the caller disprove 4109 and
	// impossible near-empty 4104 responses without trusting provider usage.
	reservationUpperBound uint64
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
	// The token allowance, spent by the harness's own receipted prompt and
	// completion tokens. The platform splits this from the request count
	// because "hit the token wall" is a different agent behaviour from
	// "hit the request wall at call 8192" -- both are the agent's behaviour.
	platformDeclineTokenBudgetExhausted: declineFaultAgent,
	// Historical: a single request whose *estimate* exceeded the whole grant.
	// Admission no longer emits 4109; leftover codes cannot be confirmed
	// from a byte ceiling and stay platform-fault via the evidence guard.
	platformDeclineReservationTooLarge: declineFaultAgent,
}

// declineIsAgentFault reports whether a platform decline code is attributable
// to the harness. Unknown codes, and the absent code 0, are never agent-fault.
func declineIsAgentFault(code int) bool {
	return declineFaultFor[code] == declineFaultAgent
}

// sessionHasBudgetEvidenceLocked reports whether this session received the
// authenticated Platform budgets needed to confirm a 4102/4104/4109. Missing
// evidence is a transport or mixed-rollout gap, not proof the harness spent
// the grant.
func sessionHasBudgetEvidenceLocked(session *brokerSession, embedding bool) bool {
	if session == nil {
		return false
	}
	if embedding {
		return session.embeddingRequestBudget > 0 && session.embeddingTokenBudget > 0
	}
	return session.requestBudget > 0 && session.tokenBudget > 0 && session.maxOutputTokens > 0
}

// attributePlatformDeclineLocked classifies one typed 429. The session lock
// must already be held. Agent-fault codes without authenticated budgets, or
// with budgets that cannot have been crossed, stay platform-fault.
func attributePlatformDeclineLocked(session *brokerSession, code int, embedding bool, currentUpperBound uint64) (agentDecline bool, attribution string) {
	attribution = "platform fault: the lease is gone"
	if !declineIsAgentFault(code) {
		return false, attribution
	}
	if !sessionHasBudgetEvidenceLocked(session, embedding) {
		session.budgetEvidenceAbsences++
		return false, "platform fault: no authenticated budget evidence; will not charge the miner"
	}
	if declineMatchesBudgetEvidenceLocked(session, code, embedding, currentUpperBound) {
		session.grantAgentDeclines++
		return true, "AGENT fault: the harness spent its own allowance"
	}
	session.declineEvidenceMismatches++
	return false, "platform fault: terminal allowance code contradicted the broker's budget evidence"
}

// declineMatchesBudgetEvidenceLocked independently checks whether the typed
// Platform decline is even possible under what this broker observed. The
// Platform remains authoritative for the exact accounting. Missing evidence
// cannot confirm the code: treat it as impossible so the miner keeps the
// attempt (the same default as an unknown decline code).
//
// These bounds exist only to DISPROVE an impossible attribution. Confirm
// 4104 from settled receipted tokens only. A body-byte + max_output ceiling
// is not spend: Crown-v11 / gatev58 died as inference_allowance_exhausted
// at a few percent of the 75M wall because the old sums treated "cannot
// disprove" as "charge the miner".
func declineMatchesBudgetEvidenceLocked(session *brokerSession, code int, embedding bool, currentUpperBound uint64) bool {
	if !sessionHasBudgetEvidenceLocked(session, embedding) {
		return false
	}
	if embedding {
		switch code {
		case platformDeclineBudgetExhausted:
			// embeddingRequests increments once per handle, including this call,
			// before the platform is contacted. It is the reservation lower
			// bound we have: the 1B-token / 100k-request embedding grant is
			// not crossed by a body-byte sum the way chat is.
			return session.embeddingRequests >= session.embeddingRequestBudget
		case platformDeclineTokenBudgetExhausted:
			return session.embeddingTokens >= session.embeddingTokenBudget
		case platformDeclineReservationTooLarge:
			return false
		default:
			return false
		}
	}
	switch code {
	case platformDeclineBudgetExhausted:
		// Platform fires 4102 when already-reserved count >= budget. Successes
		// are completed reservations; inFlight includes this call and any
		// sibling that may still hold a slot (including a just-lost admission).
		return session.successes+uint64(session.inFlight) >= session.requestBudget
	case platformDeclineTokenBudgetExhausted:
		// Platform fires 4104 when settled receipted tokens already meet the
		// grant. Do not add a body-byte + max_output ceiling: that overestimate
		// is why Crown-v11 / gatev58 died at a few percent of the 75M wall.
		return session.promptTokens+session.completionTokens >= session.tokenBudget
	case platformDeclineReservationTooLarge:
		return false
	default:
		return false
	}
}

// platformChatChargeUpperBound mirrors the Platform's tokenizer-independent
// charge ceiling: UTF-8 request bytes plus the clamped output allowance. It is
// not scored usage and intentionally overestimates; if even this bound is below
// the ticket budget, a token-exhaustion response cannot be true.
func platformChatChargeUpperBound(body []byte, maximum uint64) uint64 {
	output := maximum
	var payload struct {
		MaxTokens           *int64 `json:"max_tokens"`
		MaxCompletionTokens *int64 `json:"max_completion_tokens"`
	}
	if json.Unmarshal(body, &payload) == nil {
		for _, candidate := range []*int64{payload.MaxTokens, payload.MaxCompletionTokens} {
			if candidate != nil && *candidate > 0 && uint64(*candidate) < output {
				output = uint64(*candidate)
			}
		}
	}
	return uint64(len(body)) + output
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
		embeddingRequestTTL: 65 * time.Second,
		delayFP:             parseDelayFingerprintConfig(),
		sleep:               brokerSleep,
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
	now := time.Now()
	if session.boundRunID != runID ||
		(!session.activeLocked(now) && !session.confirmationUnboundActiveLocked(now)) ||
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
		session.signalEmbeddingQueueLocked()
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
		delayFP:         b.delayFP,
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
	return b.registerToolWithProvenance(
		h, expectedSourceIP, allowNATFallback, requireCaseCapability, "",
	)
}

func (b *inferenceBroker) registerToolWithProvenance(
	h http.Handler,
	expectedSourceIP string,
	allowNATFallback bool,
	requireCaseCapability bool,
	provenanceSessionID string,
) (registeredToolRoute, func(), error) {
	return b.registerToolRoute(
		h, expectedSourceIP, allowNATFallback, requireCaseCapability, provenanceSessionID,
		brokerPerSourceConcurrency,
	)
}

// sourceConcurrencyFor sizes a per-source admission cap for a run that overlaps
// caseConcurrency /run calls: at least one in-flight call per overlapping case,
// never below the serial-era floor.
func sourceConcurrencyFor(caseConcurrency int) int {
	if caseConcurrency > brokerPerSourceConcurrency {
		return caseConcurrency
	}
	return brokerPerSourceConcurrency
}

func (s *brokerSession) chatConcurrencyLimit() int {
	return sourceConcurrencyFor(s.chatConcurrency)
}

// configureCaseConcurrency raises the bound run's per-source chat admission to
// cover caseConcurrency overlapping /run calls. It never lowers the cap below
// brokerPerSourceConcurrency and is a no-op for an unbound or foreign run.
func (b *inferenceBroker) configureCaseConcurrency(id, runID string, caseConcurrency int) bool {
	b.mu.RLock()
	session := b.sessions[id]
	b.mu.RUnlock()
	if session == nil {
		return false
	}
	session.mu.Lock()
	defer session.mu.Unlock()
	if session.boundRunID != runID {
		return false
	}
	session.chatConcurrency = sourceConcurrencyFor(caseConcurrency)
	return true
}

// registerToolRoute registers a tool route whose per-source slot count covers
// caseConcurrency overlapping /run calls (floor brokerPerSourceConcurrency).
func (b *inferenceBroker) registerToolRoute(
	h http.Handler,
	expectedSourceIP string,
	allowNATFallback bool,
	requireCaseCapability bool,
	provenanceSessionID string,
	caseConcurrency int,
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
		provenanceSessionID:   provenanceSessionID,
		capabilityKey:         key,
		handler:               h,
		slots:                 make(chan struct{}, sourceConcurrencyFor(caseConcurrency)),
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
	var requestBody []byte
	if route.requireCaseCapability {
		var matches bool
		requestBody, matches = toolRequestMatchesCapability(w, r, caseID, userID)
		if !matches {
			return
		}
	}
	if route.provenanceSessionID != "" {
		var call protocol.ToolExecRequest
		if json.Unmarshal(requestBody, &call) != nil ||
			!b.consumeModelToolCall(route.provenanceSessionID, caseID, call) {
			writeError(w, http.StatusConflict, "tool provenance unavailable")
			return
		}
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

func toolRequestMatchesCapability(w http.ResponseWriter, r *http.Request, caseID, userID string) ([]byte, bool) {
	body, err := io.ReadAll(io.LimitReader(r.Body, toolRouteBodyLimit+1))
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid tool request")
		return nil, false
	}
	if len(body) > toolRouteBodyLimit {
		writeError(w, http.StatusRequestEntityTooLarge, "tool request too large")
		return nil, false
	}
	var identity struct {
		CaseID string `json:"case_id"`
		UserID string `json:"user_id"`
	}
	if err := json.Unmarshal(body, &identity); err != nil {
		writeError(w, http.StatusBadRequest, "invalid tool request")
		return nil, false
	}
	if identity.CaseID != caseID || identity.UserID != userID {
		writeError(w, http.StatusUnauthorized, "tool route unavailable")
		return nil, false
	}
	r.Body = io.NopCloser(bytes.NewReader(body))
	return body, true
}

func canonicalToolArguments(raw []byte) (string, error) {
	if len(bytes.TrimSpace(raw)) == 0 || bytes.Equal(bytes.TrimSpace(raw), []byte("null")) {
		raw = []byte("{}")
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var value any
	if err := decoder.Decode(&value); err != nil || decoder.Decode(&struct{}{}) != io.EOF {
		return "", fmt.Errorf("invalid tool arguments")
	}
	if _, ok := value.(map[string]any); !ok {
		return "", fmt.Errorf("tool arguments must be an object")
	}
	canonical, err := json.Marshal(value)
	if err != nil {
		return "", fmt.Errorf("canonicalize tool arguments: %w", err)
	}
	digest := sha256.Sum256(canonical)
	return hex.EncodeToString(digest[:]), nil
}

func (b *inferenceBroker) consumeModelToolCall(
	sessionID string,
	caseID string,
	call protocol.ToolExecRequest,
) bool {
	b.mu.RLock()
	session := b.sessions[sessionID]
	b.mu.RUnlock()
	if session == nil {
		return false
	}
	argsSHA256, argsErr := canonicalToolArguments(call.Args)
	session.mu.Lock()
	defer session.mu.Unlock()
	generation := session.caseIDs[caseID]
	capabilityBound := generation != 0
	if generation == 0 {
		generation = session.activeCaseGeneration
	}
	if generation == 0 {
		// No exclusive case window is open (concurrent /run): match against the
		// session-wide emission ledger.
		return consumeSessionModelToolCallLocked(session, caseID, call.Name, argsSHA256, argsErr)
	}
	snapshot := session.caseSnapshots[generation]
	snapshot.EndpointAttempts++
	if !capabilityBound && session.activeCaseID != caseID {
		snapshot.UnmatchedToolCalls++
		snapshot.ToolFindings |= toolFindingCrossCaseReplay
		session.caseSnapshots[generation] = snapshot
		return false
	}
	if call.Name == "" || argsErr != nil {
		snapshot.UnmatchedToolCalls++
		snapshot.ToolFindings |= toolFindingNameArgumentMismatch
		session.caseSnapshots[generation] = snapshot
		return false
	}
	calls := session.caseToolCalls[generation]
	sameName := false
	consumedMatch := false
	for index := range calls {
		candidate := &calls[index]
		if candidate.name != call.Name {
			continue
		}
		sameName = true
		if candidate.argsSHA256 != argsSHA256 {
			continue
		}
		if candidate.consumed {
			consumedMatch = true
			continue
		}
		candidate.consumed = true
		session.caseToolCalls[generation] = calls
		snapshot.MatchedToolCalls++
		session.caseSnapshots[generation] = snapshot
		return true
	}
	snapshot.UnmatchedToolCalls++
	switch {
	case consumedMatch:
		snapshot.ToolFindings |= toolFindingDuplicateExecution
	case sameName:
		snapshot.ToolFindings |= toolFindingNameArgumentMismatch
	default:
		snapshot.ToolFindings |= toolFindingUnbacked
	}
	session.caseSnapshots[generation] = snapshot
	return false
}

func decodeModelToolCalls(responseBody []byte) ([]brokerModelToolCall, error) {
	var response struct {
		Choices []struct {
			Message struct {
				ToolCalls []struct {
					ID       string `json:"id"`
					Type     string `json:"type"`
					Function struct {
						Name      string `json:"name"`
						Arguments string `json:"arguments"`
					} `json:"function"`
				} `json:"tool_calls"`
			} `json:"message"`
		} `json:"choices"`
	}
	if err := json.Unmarshal(responseBody, &response); err != nil || len(response.Choices) == 0 {
		return nil, fmt.Errorf("invalid chat completion")
	}
	calls := response.Choices[0].Message.ToolCalls
	out := make([]brokerModelToolCall, 0, len(calls))
	seen := make(map[string]struct{}, len(calls))
	for _, call := range calls {
		if call.ID == "" || call.Function.Name == "" || (call.Type != "" && call.Type != "function") {
			return nil, fmt.Errorf("invalid model tool call")
		}
		if _, duplicate := seen[call.ID]; duplicate {
			return nil, fmt.Errorf("duplicate model tool call id")
		}
		seen[call.ID] = struct{}{}
		argsSHA256, err := canonicalToolArguments([]byte(call.Function.Arguments))
		if err != nil {
			return nil, err
		}
		out = append(out, brokerModelToolCall{
			id: call.ID, name: call.Function.Name, argsSHA256: argsSHA256,
		})
	}
	return out, nil
}

// consumeSessionModelToolCallLocked is the session-scoped half of
// consumeModelToolCall: with no case window open it books the attempt on the
// wire case's ledger and consumes the first unconsumed session-wide emission
// with the same name and canonical argument digest, whichever case's prompt
// produced it. A call with no backing emission, changed arguments, or an
// already-consumed match is rejected and its finding bit recorded. Caller holds
// session.mu.
func consumeSessionModelToolCallLocked(
	session *brokerSession,
	caseID string,
	name string,
	argsSHA256 string,
	argsErr error,
) bool {
	if session.benchVersion < protocol.BenchVersionV10 {
		return false
	}
	if session.sessionToolCases == nil {
		session.sessionToolCases = make(map[string]brokerSessionToolLedger)
	}
	ledger := session.sessionToolCases[caseID]
	defer func() { session.sessionToolCases[caseID] = ledger }()
	ledger.EndpointAttempts++
	if name == "" || argsErr != nil {
		ledger.UnmatchedToolCalls++
		ledger.ToolFindings |= toolFindingNameArgumentMismatch
		return false
	}
	sameName := false
	consumedMatch := false
	for index := range session.sessionToolCalls {
		candidate := &session.sessionToolCalls[index]
		if candidate.name != name {
			continue
		}
		sameName = true
		if candidate.argsSHA256 != argsSHA256 {
			continue
		}
		if candidate.consumed {
			consumedMatch = true
			continue
		}
		candidate.consumed = true
		session.sessionToolConsumed++
		ledger.MatchedToolCalls++
		return true
	}
	ledger.UnmatchedToolCalls++
	switch {
	case consumedMatch:
		ledger.ToolFindings |= toolFindingDuplicateExecution
	case sameName:
		ledger.ToolFindings |= toolFindingNameArgumentMismatch
	default:
		ledger.ToolFindings |= toolFindingUnbacked
	}
	return false
}

// recordSessionModelToolCallsLocked books one successful v10+ chat completion's
// model-emitted tool calls on the session-wide ledger. A response whose
// tool_calls cannot be decoded records nothing -- an emission the broker cannot
// attest can never be consumed, so a harness executing from it is rejected as
// unbacked -- and is counted so the run's evidence carries the fact. Caller
// holds session.mu.
func recordSessionModelToolCallsLocked(session *brokerSession, responseBody []byte) {
	calls, err := decodeModelToolCalls(responseBody)
	if err != nil {
		session.sessionToolInvalidEmissions++
		return
	}
	session.sessionToolCalls = append(session.sessionToolCalls, calls...)
	session.sessionToolEmitted += uint64(len(calls))
}

// sessionToolProvenance returns one wire case's tool provenance under
// session-scoped matching, or nil when no v10+ session ledger exists (the
// caller fails closed). ModelEmitted counts the emissions THIS case consumed:
// without exclusive windows an emission cannot be attributed to a case, so the
// unconsumed remainder is reported run-wide by sessionToolProvenanceTotals and
// ModelSelectedNotExecuted is always 0 here. Complete is true: every emission
// is booked under the session lock before the upstream response is released to
// the harness, and every endpoint attempt is booked before the mock tool runs,
// so a read taken after /run returned has seen every fact that could have
// informed that response. A session-wide invalid emission is surfaced as an
// informational finding on every later case; it does not affect validity.
func (b *inferenceBroker) sessionToolProvenance(id, caseID string) *protocol.ToolProvenanceEvidence {
	b.mu.RLock()
	session := b.sessions[id]
	b.mu.RUnlock()
	if session == nil {
		return nil
	}
	session.mu.Lock()
	defer session.mu.Unlock()
	if session.benchVersion < protocol.BenchVersionV10 {
		return nil
	}
	ledger := session.sessionToolCases[caseID]
	findings := toolFindingNames(ledger.ToolFindings)
	if session.sessionToolInvalidEmissions > 0 {
		findings = append(findings, "invalid_model_tool_emission")
	}
	if len(findings) == 0 {
		findings = nil
	}
	return &protocol.ToolProvenanceEvidence{
		ModelEmitted:     int(ledger.MatchedToolCalls),
		EndpointAttempts: int(ledger.EndpointAttempts),
		Matched:          int(ledger.MatchedToolCalls),
		Unmatched:        int(ledger.UnmatchedToolCalls),
		Complete:         true,
		Findings:         findings,
	}
}

// sessionToolProvenanceTotals reads the session-wide emission ledger for the
// run summary. ok=false when no v10+ session exists.
func (b *inferenceBroker) sessionToolProvenanceTotals(id string) (sessionToolProvenanceTotals, bool) {
	b.mu.RLock()
	session := b.sessions[id]
	b.mu.RUnlock()
	if session == nil {
		return sessionToolProvenanceTotals{}, false
	}
	session.mu.Lock()
	defer session.mu.Unlock()
	if session.benchVersion < protocol.BenchVersionV10 {
		return sessionToolProvenanceTotals{}, false
	}
	return sessionToolProvenanceTotals{
		ModelEmitted:     session.sessionToolEmitted,
		Consumed:         session.sessionToolConsumed,
		InvalidEmissions: session.sessionToolInvalidEmissions,
	}, true
}

// recordModelToolCallsLocked books a successful v10+ chat completion's
// model-emitted tool calls: on the open case window's ledger when one exists
// (confirmation and legacy case-scoped paths), otherwise session-wide.
func recordModelToolCallsLocked(
	session *brokerSession,
	caseGeneration uint64,
	responseBody []byte,
) {
	if session.benchVersion < protocol.BenchVersionV10 {
		return
	}
	if caseGeneration == 0 {
		recordSessionModelToolCallsLocked(session, responseBody)
		return
	}
	snapshot := session.caseSnapshots[caseGeneration]
	calls, err := decodeModelToolCalls(responseBody)
	if err != nil {
		snapshot.ToolEvidenceComplete = false
		snapshot.ToolFindings |= toolFindingInvalidModelEmission
		session.caseSnapshots[caseGeneration] = snapshot
		return
	}
	if session.caseToolCalls == nil {
		session.caseToolCalls = make(map[uint64][]brokerModelToolCall)
	}
	session.caseToolCalls[caseGeneration] = append(session.caseToolCalls[caseGeneration], calls...)
	snapshot.ModelToolCalls += uint64(len(calls))
	session.caseSnapshots[caseGeneration] = snapshot
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
	fingerprintKey := make([]byte, delayFingerprintKeySize)
	if _, err := rand.Read(fingerprintKey); err != nil {
		writeError(w, http.StatusInternalServerError, "inference broker unavailable")
		return
	}
	session := &brokerSession{
		id: id, activationSecret: activation, privateKey: private, publicKey: public,
		delayFingerprintKey: fingerprintKey,
		delayFP:             b.delayFP,
		preparedAt:          time.Now(), cancels: make(map[string]context.CancelFunc),
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
	ActivationSecret       string    `json:"activation_secret"`
	GrantID                string    `json:"grant_id"`
	AgentID                string    `json:"agent_id,omitempty"`
	SlotID                 string    `json:"slot_id,omitempty"`
	TicketDeadline         time.Time `json:"ticket_deadline,omitempty"`
	Bearer                 string    `json:"bearer"`
	ProxyURL               string    `json:"proxy_url"`
	Generation             int       `json:"generation"`
	ExpiresAt              time.Time `json:"expires_at"`
	Provider               string    `json:"provider"`
	ProfileRevision        string    `json:"profile_revision"`
	Model                  string    `json:"model"`
	RequestBudget          uint64    `json:"request_budget,omitempty"`
	TokenBudget            uint64    `json:"token_budget,omitempty"`
	EmbeddingRequestBudget uint64    `json:"embedding_request_budget,omitempty"`
	EmbeddingTokenBudget   uint64    `json:"embedding_token_budget,omitempty"`
	MaxOutputTokens        uint64    `json:"max_output_tokens,omitempty"`
}

type brokerConfirmationGrantWire struct {
	Lane               string    `json:"lane"`
	GrantID            string    `json:"grant_id"`
	Bearer             string    `json:"bearer"`
	ProxyURL           string    `json:"proxy_url"`
	Generation         int       `json:"generation"`
	ExpiresAt          time.Time `json:"expires_at"`
	Provider           string    `json:"provider"`
	RouteProvider      string    `json:"route_provider"`
	ReceiptProvider    string    `json:"receipt_provider"`
	ProfileRevision    string    `json:"profile_revision"`
	Model              string    `json:"model"`
	RequestBudget      uint64    `json:"request_budget"`
	TokenBudget        uint64    `json:"token_budget"`
	CostBudgetMicrousd uint64    `json:"cost_budget_microusd"`
}

type brokerConfirmationActivation struct {
	ActivationSecret string                        `json:"activation_secret"`
	AgentID          string                        `json:"agent_id"`
	SlotID           string                        `json:"slot_id"`
	TicketDeadline   time.Time                     `json:"ticket_deadline"`
	Grants           []brokerConfirmationGrantWire `json:"grants"`
}

type brokerConfirmationAuthorizer struct {
	broker    *inferenceBroker
	sessionID string
}

func (a brokerConfirmationAuthorizer) Authorize(
	_ context.Context,
	lane string,
	request *http.Request,
) error {
	if a.broker == nil || request == nil {
		return errors.New("confirmation platform capability is unavailable")
	}
	a.broker.mu.RLock()
	session := a.broker.sessions[a.sessionID]
	a.broker.mu.RUnlock()
	if session == nil {
		return errors.New("confirmation platform capability is unavailable")
	}
	session.mu.Lock()
	grant, ok := session.confirmationGrants[lane]
	privateKey := append(ed25519.PrivateKey(nil), session.privateKey...)
	traceCtx := traceContextLocked(session, session.activeCaseGeneration, lane, "")
	active := session.confirmationSession && session.expiresAt.After(time.Now())
	session.mu.Unlock()
	if !ok || !active || request.URL.String() != grant.ProxyURL || len(privateKey) != ed25519.PrivateKeySize {
		return errors.New("confirmation platform capability is unavailable")
	}
	body, err := io.ReadAll(io.LimitReader(request.Body, brokerBodyLimit+1))
	if err != nil || len(body) > brokerBodyLimit {
		return errors.New("confirmation platform request is invalid")
	}
	request.Body = io.NopCloser(bytes.NewReader(body))
	nonce := uuid.NewString()
	requested := time.Now().UTC().Format("2006-01-02T15:04:05.000000+00:00")
	digest := sha256.Sum256(body)
	message := fmt.Sprintf("ditto-inference:v1:%s:%d:%s:%s:%s", grant.GrantID, grant.Generation, nonce, requested, hex.EncodeToString(digest[:]))
	request.Header.Set("Authorization", "Bearer "+grant.Bearer)
	request.Header.Set("X-Ditto-Grant", grant.GrantID)
	request.Header.Set("X-Ditto-Generation", fmt.Sprint(grant.Generation))
	request.Header.Set("X-Ditto-Nonce", nonce)
	request.Header.Set("X-Ditto-Requested-At", requested)
	request.Header.Set("X-Ditto-Proof", base64.RawURLEncoding.EncodeToString(ed25519.Sign(privateKey, []byte(message))))
	if traceCtx != "" {
		request.Header.Set(traceContextHeader, traceCtx)
	}
	return nil
}

func (b *inferenceBroker) confirmationProviderRuntime(
	sessionID string,
	profile confirmationExecutionProfileWire,
) (longmemeval.ProviderRuntimeConfig, error) {
	b.mu.RLock()
	session := b.sessions[sessionID]
	b.mu.RUnlock()
	if session == nil {
		return longmemeval.ProviderRuntimeConfig{}, errors.New("confirmation platform capability is unavailable")
	}
	session.mu.Lock()
	defer session.mu.Unlock()
	if !session.confirmationSession || len(session.confirmationGrants) != 3 ||
		len(session.privateKey) != ed25519.PrivateKeySize || !session.expiresAt.After(time.Now()) {
		return longmemeval.ProviderRuntimeConfig{}, errors.New("confirmation platform capability is unavailable")
	}
	policies := make(map[string]confirmationProviderLaneProfile, 2)
	for _, policy := range profile.ProviderLanes {
		policies[policy.Lane] = policy
	}
	lanes := make([]longmemeval.ProviderLaneRuntimeConfig, 0, 2)
	for _, lane := range []string{longmemeval.ReaderLane, longmemeval.JudgeLane} {
		grant := session.confirmationGrants[lane]
		policy, found := policies[lane]
		if !found || grant.Model != policy.Model || grant.Provider != policy.Provider ||
			grant.RouteProvider != policy.RouteProvider || grant.ReceiptProvider != policy.ReceiptProvider ||
			grant.ProfileRevision != policy.ProfileRevision {
			return longmemeval.ProviderRuntimeConfig{}, errors.New("confirmation platform capability drift")
		}
		lanes = append(lanes, longmemeval.ProviderLaneRuntimeConfig{
			Lane: lane, UpstreamURL: grant.ProxyURL, RouteProvider: grant.RouteProvider,
			ReceiptProvider: grant.ReceiptProvider,
			RequestTimeout:  10 * time.Minute,
		})
	}
	authorizer := brokerConfirmationAuthorizer{broker: b, sessionID: sessionID}
	return longmemeval.ProviderRuntimeConfig{Lanes: lanes, Authorizer: authorizer}, nil
}

func confirmationProxyPath(lane string) string {
	if lane == "embedding" {
		return "/api/v1/inference/confirmation/embeddings"
	}
	return "/api/v1/inference/confirmation/chat/completions"
}

func validConfirmationProxyURL(raw, lane string) bool {
	parsed, err := url.Parse(raw)
	return err == nil && parsed.Scheme == "https" && parsed.Host != "" && parsed.User == nil &&
		parsed.RawQuery == "" && parsed.Fragment == "" && parsed.Path == confirmationProxyPath(lane)
}

func (b *inferenceBroker) activateConfirmation(w http.ResponseWriter, r *http.Request) {
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
	var activation brokerConfirmationActivation
	decoder := json.NewDecoder(io.LimitReader(r.Body, 64<<10))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&activation) != nil || decoder.Decode(&struct{}{}) != io.EOF {
		writeError(w, http.StatusBadRequest, "invalid confirmation inference activation")
		return
	}
	now := time.Now()
	identity := brokerTicketIdentity{AgentID: activation.AgentID, SlotID: activation.SlotID, TicketDeadline: activation.TicketDeadline}
	secretMatches := subtle.ConstantTimeCompare([]byte(activation.ActivationSecret), []byte(session.activationSecret)) == 1
	if !secretMatches || len(activation.Grants) != 3 || !validBrokerTicketIdentity(
		brokerTicketIdentity{GrantID: activation.Grants[0].GrantID, AgentID: identity.AgentID, SlotID: identity.SlotID, TicketDeadline: identity.TicketDeadline}, now,
	) {
		writeError(w, http.StatusUnauthorized, "invalid confirmation inference activation")
		return
	}
	grants := make(map[string]brokerConfirmationGrant, 3)
	for _, offer := range activation.Grants {
		if (offer.Lane != "reader" && offer.Lane != "judge" && offer.Lane != "embedding") || grants[offer.Lane].Lane != "" ||
			offer.Bearer == "" || len(offer.Bearer) > 4096 || offer.Generation < 1 || offer.RequestBudget < 1 ||
			offer.TokenBudget < 1 || offer.CostBudgetMicrousd < 1 || offer.ExpiresAt.After(activation.TicketDeadline) ||
			!offer.ExpiresAt.After(now) || !validConfirmationProxyURL(offer.ProxyURL, offer.Lane) ||
			strings.TrimSpace(offer.Provider) == "" || strings.TrimSpace(offer.RouteProvider) == "" ||
			strings.TrimSpace(offer.ReceiptProvider) == "" || strings.TrimSpace(offer.ProfileRevision) == "" ||
			strings.TrimSpace(offer.Model) == "" {
			writeError(w, http.StatusUnauthorized, "invalid confirmation inference activation")
			return
		}
		if _, err := uuid.Parse(offer.GrantID); err != nil {
			writeError(w, http.StatusUnauthorized, "invalid confirmation inference activation")
			return
		}
		grants[offer.Lane] = brokerConfirmationGrant{
			Lane: offer.Lane, GrantID: offer.GrantID, Bearer: offer.Bearer, ProxyURL: offer.ProxyURL,
			Generation: offer.Generation, ExpiresAt: offer.ExpiresAt, Provider: offer.Provider,
			RouteProvider: offer.RouteProvider, ReceiptProvider: offer.ReceiptProvider,
			ProfileRevision: offer.ProfileRevision, Model: offer.Model, RequestBudget: offer.RequestBudget,
			TokenBudget: offer.TokenBudget, CostBudgetMicrousd: offer.CostBudgetMicrousd,
		}
	}
	if len(grants) != 3 {
		writeError(w, http.StatusUnauthorized, "invalid confirmation inference activation")
		return
	}
	session.mu.Lock()
	defer session.mu.Unlock()
	if session.activationSecret == "" || session.boundRunID != "" {
		writeError(w, http.StatusConflict, "confirmation inference session is unavailable")
		return
	}
	session.activationSecret = ""
	session.confirmationSession = true
	session.confirmationGrants = grants
	session.ticketAgentID = activation.AgentID
	session.ticketSlotID = activation.SlotID
	session.ticketDeadline = activation.TicketDeadline
	session.expiresAt = activation.TicketDeadline
	session.embeddingConcurrency = v8EmbeddingSessionConcurrency
	w.Header().Set("Cache-Control", "no-store")
	writeJSON(w, http.StatusOK, map[string]bool{"active": true})
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
	hasBudgetEvidence := activation.RequestBudget != 0 || activation.TokenBudget != 0 ||
		activation.EmbeddingRequestBudget != 0 || activation.EmbeddingTokenBudget != 0 ||
		activation.MaxOutputTokens != 0
	validBudgetEvidence := !hasBudgetEvidence || (activation.RequestBudget > 0 &&
		activation.TokenBudget > 0 && activation.EmbeddingRequestBudget > 0 &&
		activation.EmbeddingTokenBudget > 0 && activation.MaxOutputTokens > 0)
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
		!validBudgetEvidence ||
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
	session.requestBudget = activation.RequestBudget
	session.tokenBudget = activation.TokenBudget
	session.embeddingRequestBudget = activation.EmbeddingRequestBudget
	session.embeddingTokenBudget = activation.EmbeddingTokenBudget
	session.maxOutputTokens = activation.MaxOutputTokens
	session.ticketAgentID = activation.AgentID
	session.ticketSlotID = activation.SlotID
	session.ticketDeadline = activation.TicketDeadline
	if sessionHasBudgetEvidenceLocked(session, false) && sessionHasBudgetEvidenceLocked(session, true) {
		log.Printf(
			"inference session %s: budget evidence armed request=%d token=%d embedding_request=%d embedding_token=%d max_output=%d",
			id, session.requestBudget, session.tokenBudget,
			session.embeddingRequestBudget, session.embeddingTokenBudget, session.maxOutputTokens,
		)
	} else {
		log.Printf("inference session %s: budget evidence absent; will not attribute 4102/4104/4109 to the agent", id)
	}
	w.Header().Set("Cache-Control", "no-store")
	writeJSON(w, http.StatusOK, map[string]bool{"active": true})
}

func validBrokerTicketIdentity(identity brokerTicketIdentity, now time.Time) bool {
	_, grantErr := uuid.Parse(identity.GrantID)
	_, agentErr := uuid.Parse(identity.AgentID)
	return grantErr == nil && agentErr == nil && validBrokerSlot(identity.SlotID) && identity.TicketDeadline.After(now)
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

// configureRun installs the Platform-stamped runtime controls after claimRun
// has bound the trusted session to this exact run. It is intentionally not an
// HTTP surface: only the scorer's authenticated /v2/score request can reach it.
func (b *inferenceBroker) configureRun(id, runID string, delay delayFingerprintConfig) bool {
	b.mu.RLock()
	session := b.sessions[id]
	b.mu.RUnlock()
	if session == nil {
		return false
	}
	session.mu.Lock()
	defer session.mu.Unlock()
	if session.boundRunID != runID || session.benchVersion < protocol.BenchVersionV10 {
		return false
	}
	session.delayFP = delay
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
	if session.boundRunID != runID || session.expectedSourceIP != "" || session.sourceCapabilityRequired ||
		(session.bearer == "" && session.legacyGateway == "" && session.trustedChatHandler == nil) ||
		!session.expiresAt.After(time.Now()) {
		return false
	}
	session.expectedSourceIP = sourceIP
	session.sourceEpoch++
	return true
}

const (
	brokerSourceCapabilityBytes = 32
	brokerSourceCapabilityChars = 52
	brokerCapabilityHostPrefix  = "c-"
	brokerCapabilityHostSuffix  = ".host.docker.internal"
)

func canonicalBrokerSourceCapability(raw string) bool {
	if len(raw) != brokerSourceCapabilityChars {
		return false
	}
	for index := range raw {
		character := raw[index]
		if !((character >= 'a' && character <= 'z') || (character >= '2' && character <= '7')) {
			return false
		}
	}
	decoded, err := base32.StdEncoding.WithPadding(base32.NoPadding).DecodeString(strings.ToUpper(raw))
	if err != nil || len(decoded) != brokerSourceCapabilityBytes {
		return false
	}
	canonical := strings.ToLower(base32.StdEncoding.WithPadding(base32.NoPadding).EncodeToString(decoded))
	return subtle.ConstantTimeCompare([]byte(canonical), []byte(raw)) == 1
}

func newBrokerSourceCapability() (string, error) {
	raw := make([]byte, brokerSourceCapabilityBytes)
	if _, err := rand.Read(raw); err != nil {
		return "", err
	}
	token := strings.ToLower(base32.StdEncoding.WithPadding(base32.NoPadding).EncodeToString(raw))
	if !canonicalBrokerSourceCapability(token) {
		return "", fmt.Errorf("invalid generated broker source capability")
	}
	return token, nil
}

func brokerSourceCapabilityHost(token string) (string, error) {
	if !canonicalBrokerSourceCapability(token) {
		return "", fmt.Errorf("invalid broker source capability")
	}
	return brokerCapabilityHostPrefix + token + brokerCapabilityHostSuffix, nil
}

func brokerSourceCapabilityFromHost(hostPort string) (string, bool) {
	host := strings.ToLower(strings.TrimSpace(hostPort))
	if parsed, _, err := net.SplitHostPort(host); err == nil {
		host = parsed
	}
	if !strings.HasPrefix(host, brokerCapabilityHostPrefix) || !strings.HasSuffix(host, brokerCapabilityHostSuffix) {
		return "", false
	}
	token := strings.TrimSuffix(strings.TrimPrefix(host, brokerCapabilityHostPrefix), brokerCapabilityHostSuffix)
	if !canonicalBrokerSourceCapability(token) {
		return "", true
	}
	return token, true
}

func (b *inferenceBroker) installSourceCapability(id, runID string) (string, error) {
	token, err := newBrokerSourceCapability()
	if err != nil || !canonicalBrokerSourceCapability(token) {
		return "", fmt.Errorf("broker source capability unavailable")
	}
	b.mu.RLock()
	session := b.sessions[id]
	b.mu.RUnlock()
	if session == nil {
		return "", fmt.Errorf("inference session unavailable")
	}
	session.mu.Lock()
	defer session.mu.Unlock()
	if session.boundRunID != runID || session.expectedSourceIP != "" ||
		session.sourceCapabilityActive || !session.expiresAt.After(time.Now()) {
		return "", fmt.Errorf("broker source capability unavailable")
	}
	session.sourceCapabilityDigest = sha256.Sum256([]byte(token))
	session.sourceCapabilityRequired = true
	session.sourceCapabilityActive = true
	return token, nil
}

func (b *inferenceBroker) bindSourceCapability(id, runID, sourceIP, token string) bool {
	if net.ParseIP(sourceIP) == nil || !canonicalBrokerSourceCapability(token) {
		return false
	}
	b.mu.RLock()
	session := b.sessions[id]
	b.mu.RUnlock()
	if session == nil {
		return false
	}
	digest := sha256.Sum256([]byte(token))
	session.mu.Lock()
	defer session.mu.Unlock()
	if session.boundRunID != runID || session.expectedSourceIP != "" ||
		!session.sourceCapabilityRequired || !session.sourceCapabilityActive ||
		subtle.ConstantTimeCompare(digest[:], session.sourceCapabilityDigest[:]) != 1 ||
		(session.bearer == "" && session.legacyGateway == "" && session.trustedChatHandler == nil) ||
		!session.expiresAt.After(time.Now()) {
		return false
	}
	session.expectedSourceIP = sourceIP
	session.sourceEpoch++
	return true
}

func (b *inferenceBroker) revokeSourceCapability(id, runID, token string) bool {
	if !canonicalBrokerSourceCapability(token) {
		return false
	}
	b.mu.RLock()
	session := b.sessions[id]
	b.mu.RUnlock()
	if session == nil {
		return false
	}
	digest := sha256.Sum256([]byte(token))
	session.mu.Lock()
	defer session.mu.Unlock()
	if session.boundRunID != runID || !session.sourceCapabilityActive ||
		subtle.ConstantTimeCompare(digest[:], session.sourceCapabilityDigest[:]) != 1 {
		return false
	}
	for index := range session.sourceCapabilityDigest {
		session.sourceCapabilityDigest[index] = 0
	}
	session.sourceCapabilityActive = false
	session.sourceEpoch++
	return true
}

func (b *inferenceBroker) rotateSourceCapability(id, runID, oldToken string) (string, uint64, error) {
	if !canonicalBrokerSourceCapability(oldToken) {
		return "", 0, fmt.Errorf("broker source capability unavailable")
	}
	newToken, err := newBrokerSourceCapability()
	if err != nil {
		return "", 0, fmt.Errorf("broker source capability unavailable")
	}
	b.mu.RLock()
	session := b.sessions[id]
	b.mu.RUnlock()
	if session == nil {
		return "", 0, fmt.Errorf("inference session unavailable")
	}
	oldDigest := sha256.Sum256([]byte(oldToken))
	session.mu.Lock()
	defer session.mu.Unlock()
	if session.boundRunID != runID || !session.sourceCapabilityRequired || !session.sourceCapabilityActive ||
		subtle.ConstantTimeCompare(oldDigest[:], session.sourceCapabilityDigest[:]) != 1 ||
		!session.expiresAt.After(time.Now()) {
		return "", 0, fmt.Errorf("broker source capability unavailable")
	}
	retiredEpoch := session.sourceEpoch
	session.sourceCapabilityDigest = sha256.Sum256([]byte(newToken))
	session.sourceCapabilityActive = true
	session.sourceEpoch++
	return newToken, retiredEpoch, nil
}

func (b *inferenceBroker) waitSourceEpochDrained(ctx context.Context, id string, epoch uint64) error {
	if ctx == nil || epoch == 0 {
		return fmt.Errorf("broker source drain unavailable")
	}
	ticker := time.NewTicker(10 * time.Millisecond)
	defer ticker.Stop()
	for {
		b.mu.RLock()
		session := b.sessions[id]
		b.mu.RUnlock()
		if session == nil {
			return fmt.Errorf("inference session unavailable")
		}
		session.mu.Lock()
		active := session.sourceActiveHandlers[epoch]
		currentEpoch := session.sourceEpoch
		session.mu.Unlock()
		if currentEpoch <= epoch {
			return fmt.Errorf("broker source drain unavailable")
		}
		if active == 0 {
			return nil
		}
		select {
		case <-ctx.Done():
			return fmt.Errorf("broker source drain unavailable")
		case <-ticker.C:
		}
	}
}

func (b *inferenceBroker) replaceBoundSourceCapability(id, runID, oldSourceIP, newSourceIP, token string) bool {
	if net.ParseIP(oldSourceIP) == nil || net.ParseIP(newSourceIP) == nil || !canonicalBrokerSourceCapability(token) {
		return false
	}
	b.mu.RLock()
	session := b.sessions[id]
	b.mu.RUnlock()
	if session == nil {
		return false
	}
	digest := sha256.Sum256([]byte(token))
	session.mu.Lock()
	defer session.mu.Unlock()
	if session.boundRunID != runID || session.expectedSourceIP != oldSourceIP ||
		!session.sourceCapabilityRequired || !session.sourceCapabilityActive ||
		subtle.ConstantTimeCompare(digest[:], session.sourceCapabilityDigest[:]) != 1 ||
		!session.activeLocked(time.Now()) {
		return false
	}
	session.expectedSourceIP = newSourceIP
	session.sourceEpoch++
	return true
}

// unbindSource is the stop-verified half of the confirmation sandbox lease.
// Incrementing the epoch makes a handler that resolved the old source before
// revocation fail its locked admission check even when Docker reuses the same
// IP for the next case.
func (b *inferenceBroker) unbindSource(id, runID, sourceIP string) bool {
	b.mu.RLock()
	session := b.sessions[id]
	b.mu.RUnlock()
	if session == nil || net.ParseIP(sourceIP) == nil {
		return false
	}
	session.mu.Lock()
	defer session.mu.Unlock()
	if session.boundRunID != runID || session.expectedSourceIP != sourceIP ||
		(session.bearer == "" && session.legacyGateway == "" && session.trustedChatHandler == nil) {
		return false
	}
	session.expectedSourceIP = ""
	session.sourceEpoch++
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
	session.sourceEpoch++
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

func (session *brokerSession) embeddingQueueChangedLocked() <-chan struct{} {
	if session.embeddingQueueChanged == nil {
		session.embeddingQueueChanged = make(chan struct{})
	}
	return session.embeddingQueueChanged
}

func (session *brokerSession) signalEmbeddingQueueLocked() {
	if session.embeddingQueueChanged != nil {
		close(session.embeddingQueueChanged)
		session.embeddingQueueChanged = nil
	}
}

func (session *brokerSession) activeLocked(now time.Time) bool {
	return session.boundRunID != "" && session.expectedSourceIP != "" &&
		session.expiresAt.After(now) &&
		(session.bearer != "" || session.legacyGateway != "" || session.trustedChatHandler != nil)
}

func (session *brokerSession) confirmationUnboundActiveLocked(now time.Time) bool {
	return session.confirmationSession && session.boundRunID != "" && session.expectedSourceIP == "" &&
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
	clear(session.sourceActiveHandlers)
	for i := range session.privateKey {
		session.privateKey[i] = 0
	}
	session.activationSecret = ""
	session.bearer = ""
	session.legacyGateway = ""
	session.trustedChatHandler = nil
	session.requestModel = ""
	for index := range session.sourceCapabilityDigest {
		session.sourceCapabilityDigest[index] = 0
	}
	session.sourceCapabilityActive = false
	session.embeddingPhaseActive = false
	session.signalEmbeddingQueueLocked()
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
	lease := b.requestSourceLease(r)
	if lease.session == nil {
		writeError(w, http.StatusUnauthorized, "inference session unavailable")
		return
	}
	session := lease.session
	rest := "/" + strings.TrimLeft(r.PathValue("rest"), "/")
	caseGeneration := uint64(0)
	if strings.HasPrefix(rest, "/cases/") {
		parts := strings.SplitN(strings.TrimPrefix(rest, "/cases/"), "/", 2)
		if len(parts) != 2 {
			writeError(w, http.StatusNotFound, "inference route not found")
			return
		}
		session.mu.Lock()
		caseGeneration = session.caseCapabilities[parts[0]]
		session.mu.Unlock()
		if caseGeneration == 0 {
			writeError(w, http.StatusUnauthorized, "inference case capability unavailable")
			return
		}
		rest = "/" + strings.TrimLeft(parts[1], "/")
	}
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
	b.handleChat(w, r, lease, caseGeneration)
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
	lease := b.requestCapabilityLease(r, true)
	if lease.session == nil {
		writeError(w, http.StatusUnauthorized, "inference session unavailable")
		return
	}
	b.handleChat(w, r, lease)
}

func (b *inferenceBroker) handleChat(
	w http.ResponseWriter, r *http.Request, lease brokerSourceLease, explicitGeneration ...uint64,
) {
	session := lease.session
	session.mu.Lock()
	caseGeneration := session.activeCaseGeneration
	if len(explicitGeneration) > 0 && explicitGeneration[0] != 0 {
		caseGeneration = explicitGeneration[0]
	}
	if session.sourceEpoch != lease.epoch || session.expectedSourceIP != lease.sourceIP ||
		!session.activeLocked(time.Now()) {
		session.mu.Unlock()
		writeError(w, http.StatusUnauthorized, "inference session unavailable")
		return
	}
	if session.sourceActiveHandlers == nil {
		session.sourceActiveHandlers = make(map[uint64]uint64)
	}
	sourceEpoch := lease.epoch
	session.sourceActiveHandlers[sourceEpoch]++
	defer func() {
		session.mu.Lock()
		if active := session.sourceActiveHandlers[sourceEpoch]; active > 1 {
			session.sourceActiveHandlers[sourceEpoch] = active - 1
		} else {
			delete(session.sourceActiveHandlers, sourceEpoch)
		}
		session.mu.Unlock()
	}()
	if session.confirmationSession && caseGeneration == 0 && session.ablation == nil {
		session.mu.Unlock()
		writeError(w, http.StatusConflict, "confirmation case unavailable")
		return
	}
	ablationScope := session.ablation
	if ablationScope != nil {
		if ablationScope.draining {
			session.mu.Unlock()
			writeError(w, http.StatusConflict, "ablation case unavailable")
			return
		}
		ablationScope.activeHandlers++
	}
	confirmationCase := session.confirmationSession && caseGeneration != 0
	if confirmationCase {
		if session.caseSnapshots == nil {
			session.caseSnapshots = make(map[uint64]brokerCaseSnapshot)
		}
		snapshot := session.caseSnapshots[caseGeneration]
		if snapshot.Draining {
			session.mu.Unlock()
			writeError(w, http.StatusConflict, "confirmation case unavailable")
			return
		}
		snapshot.ActiveHandlers++
		snapshot.ReaderAttempts++
		session.caseSnapshots[caseGeneration] = snapshot
	}
	defer func() {
		session.mu.Lock()
		if confirmationCase {
			snapshot := session.caseSnapshots[caseGeneration]
			snapshot.ActiveHandlers--
			session.caseSnapshots[caseGeneration] = snapshot
		}
		if ablationScope != nil {
			ablationScope.activeHandlers--
		}
		session.mu.Unlock()
	}()
	if session.inFlight >= session.chatConcurrencyLimit() {
		session.mu.Unlock()
		w.Header().Set("Retry-After", "1")
		writeError(w, http.StatusTooManyRequests, "inference source is at capacity")
		return
	}
	if caseGeneration != 0 {
		if session.caseSnapshots == nil {
			session.caseSnapshots = make(map[uint64]brokerCaseSnapshot)
		}
		snapshot := session.caseSnapshots[caseGeneration]
		snapshot.InFlight++
		if confirmationCase {
			snapshot.ReaderInFlight++
		}
		session.caseSnapshots[caseGeneration] = snapshot
	}
	session.inFlight++
	session.mu.Unlock()
	defer func() {
		session.mu.Lock()
		session.inFlight--
		if caseGeneration != 0 {
			snapshot := session.caseSnapshots[caseGeneration]
			snapshot.InFlight--
			if confirmationCase {
				snapshot.ReaderInFlight--
				if r.Context().Err() != nil {
					snapshot.ReaderCancellations++
				}
			}
			session.caseSnapshots[caseGeneration] = snapshot
		}
		session.mu.Unlock()
	}()
	b.proxy(w, r, session, caseGeneration)
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
	lease := b.requestSourceLease(r)
	if lease.session == nil {
		writeError(w, http.StatusUnauthorized, "embedding session unavailable")
		return
	}
	b.handleEmbeddingWithLease(w, r, lease)
}

func (b *inferenceBroker) handleEmbeddingWithLease(w http.ResponseWriter, r *http.Request, lease brokerSourceLease) {
	session := lease.session
	session.mu.Lock()
	benchVersion := session.benchVersion
	caseGeneration := session.activeCaseGeneration
	if session.sourceEpoch != lease.epoch || session.expectedSourceIP != lease.sourceIP ||
		!session.activeLocked(time.Now()) {
		session.mu.Unlock()
		writeError(w, http.StatusUnauthorized, "embedding session unavailable")
		return
	}
	if session.sourceActiveHandlers == nil {
		session.sourceActiveHandlers = make(map[uint64]uint64)
	}
	sourceEpoch := lease.epoch
	session.sourceActiveHandlers[sourceEpoch]++
	defer func() {
		session.mu.Lock()
		if active := session.sourceActiveHandlers[sourceEpoch]; active > 1 {
			session.sourceActiveHandlers[sourceEpoch] = active - 1
		} else {
			delete(session.sourceActiveHandlers, sourceEpoch)
		}
		session.mu.Unlock()
	}()
	if session.confirmationSession && caseGeneration == 0 && session.ablation == nil {
		session.mu.Unlock()
		writeError(w, http.StatusConflict, "confirmation case unavailable")
		return
	}
	ablationScope := session.ablation
	if ablationScope != nil {
		if ablationScope.draining {
			session.mu.Unlock()
			writeError(w, http.StatusConflict, "ablation case unavailable")
			return
		}
		ablationScope.activeHandlers++
	}
	confirmationCase := session.confirmationSession && caseGeneration != 0
	if confirmationCase {
		if session.caseSnapshots == nil {
			session.caseSnapshots = make(map[uint64]brokerCaseSnapshot)
		}
		snapshot := session.caseSnapshots[caseGeneration]
		if snapshot.Draining {
			session.mu.Unlock()
			writeError(w, http.StatusConflict, "confirmation case unavailable")
			return
		}
		snapshot.ActiveHandlers++
		snapshot.EmbeddingAttempts++
		session.caseSnapshots[caseGeneration] = snapshot
	}
	session.mu.Unlock()
	defer func() {
		session.mu.Lock()
		if confirmationCase {
			snapshot := session.caseSnapshots[caseGeneration]
			snapshot.ActiveHandlers--
			session.caseSnapshots[caseGeneration] = snapshot
		}
		if ablationScope != nil {
			ablationScope.activeHandlers--
		}
		session.mu.Unlock()
	}()
	if !usesPlatformEmbedding(benchVersion) && b.embeddingURL == "" {
		writeError(w, http.StatusServiceUnavailable, "embedding service unavailable")
		return
	}
	requestContext, cancelRequest := context.WithTimeout(r.Context(), b.embeddingRequestTTL)
	var cancelOnce sync.Once
	cancel := func() {
		cancelOnce.Do(func() {
			cancelRequest()
			_ = r.Body.Close()
		})
	}
	var done chan struct{}
	for {
		session.mu.Lock()
		if !session.embeddingPhaseActive {
			session.mu.Unlock()
			cancel()
			writeError(w, http.StatusConflict, "embedding phase unavailable")
			return
		}
		if session.embeddingInFlight < session.embeddingLaneLocked() {
			done = make(chan struct{})
			session.embeddingInFlight++
			if confirmationCase {
				snapshot := session.caseSnapshots[caseGeneration]
				snapshot.EmbeddingInFlight++
				session.caseSnapshots[caseGeneration] = snapshot
			}
			if session.embeddingCalls == nil {
				session.embeddingCalls = make(map[chan struct{}]context.CancelFunc)
			}
			session.embeddingCalls[done] = cancel
			session.mu.Unlock()
			break
		}
		changed := session.embeddingQueueChangedLocked()
		session.mu.Unlock()

		select {
		case <-requestContext.Done():
			requestErr := requestContext.Err()
			callerErr := r.Context().Err()
			session.mu.Lock()
			phaseActive := session.embeddingPhaseActive
			if phaseActive {
				if errors.Is(requestErr, context.DeadlineExceeded) && callerErr == nil {
					session.capacityExhaustions++
					session.failures++
				} else {
					session.callerCancels++
				}
			}
			session.mu.Unlock()
			cancel()
			if !phaseActive {
				writeError(w, http.StatusConflict, "embedding phase unavailable")
				return
			}
			w.Header().Set("Retry-After", "1")
			writeError(w, http.StatusServiceUnavailable, "embedding source queue timed out")
			return
		case <-changed:
		}
	}
	slotAcquired := false
	defer func() {
		if slotAcquired {
			<-b.embeddingSlots
		}
		session.mu.Lock()
		if _, tracked := session.embeddingCalls[done]; tracked {
			delete(session.embeddingCalls, done)
			session.embeddingInFlight--
			session.signalEmbeddingQueueLocked()
		}
		if confirmationCase {
			snapshot := session.caseSnapshots[caseGeneration]
			snapshot.EmbeddingInFlight--
			if r.Context().Err() != nil {
				snapshot.EmbeddingCancellations++
			}
			session.caseSnapshots[caseGeneration] = snapshot
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
	if ablationScope != nil && ablationScope.lane == ablation.LaneInference && !ablationScope.counterfactual {
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
	// A v12 counterfactual inference scope perturbs only the model completion; its
	// embeddings fall through to the live platform lane below, unchanged.

	// The model is a property of the ticket, not of the request -- the same
	// same premise applied to the chat door, now applied to this one.
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
	// independently. A mismatch is logged exactly as chat logs it,
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
	if ablationScope != nil && ablationScope.lane == ablation.LaneOrdinary {
		// The ordinary confirmation trace is still provider-backed.  Its
		// response is then replayed by the paid-free intervention round; only
		// that replay/synthetic work is allowed to avoid the purpose-bound
		// Platform grant.
		decoded, err = b.forwardPlatformEmbeddingOnce(requestContext, session, payload.Input, caseGeneration)
	} else if usesPlatformEmbedding(benchVersion) {
		decoded, err = b.forwardPlatformEmbeddingOnce(requestContext, session, payload.Input, caseGeneration)
	} else {
		decoded, err = b.forwardLocalEmbedding(requestContext, payload.Input)
	}
	if err != nil {
		if usesPlatformEmbedding(benchVersion) {
			// A v7 embedding fault fails the run closed. What changes is only
			// WHICH counter it lands in: a platform
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
				var attribution string
				agentDecline, attribution = attributePlatformDeclineLocked(session, denied.code, true, denied.reservationUpperBound)
				log.Printf(
					"run %s: platform declined the embedding grant (429: %s -- %s); ticket deadline held locally is %s (in %s) -- this is a lease denial, not a provider fault (denial #%d, agent-attributable #%d, evidence-mismatch #%d, evidence-absent #%d, signed dispatches=%d/%d, dispatched charge upper bound=%d/%d)",
					session.boundRunID, platformDeclineReason(denied.code), attribution,
					session.ticketDeadline.UTC().Format(time.RFC3339),
					time.Until(session.ticketDeadline).Truncate(time.Second),
					session.grantDenials, session.grantAgentDeclines, session.declineEvidenceMismatches,
					session.budgetEvidenceAbsences,
					session.embeddingDispatches, session.embeddingRequestBudget,
					session.embeddingChargeUpperBound, session.embeddingTokenBudget,
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
	session.mu.Lock()
	if decoded.PromptEvalCount > 0 {
		session.embeddingTokens += uint64(decoded.PromptEvalCount)
	}
	if confirmationCase {
		snapshot := session.caseSnapshots[caseGeneration]
		snapshot.EmbeddingDelivered++
		session.caseSnapshots[caseGeneration] = snapshot
	}
	session.mu.Unlock()
	writeJSON(w, http.StatusOK, decoded)
}

// forwardPlatformEmbeddingOnce performs one dispatch. The shared gate may delay
// before dispatch when another
// request already observed saturation; once this request receives any failure,
// the score attempt parks until Backroom authorizes another ticket.
func (b *inferenceBroker) forwardPlatformEmbeddingOnce(
	ctx context.Context, session *brokerSession, inputs []string, caseGeneration ...uint64,
) (embeddingResponse, error) {
	generation := uint64(0)
	if len(caseGeneration) > 0 {
		generation = caseGeneration[0]
	}
	probe, waitErr := b.embeddingBackpressure.wait(ctx)
	if waitErr != nil {
		return embeddingResponse{}, waitErr
	}
	decoded, err := b.forwardPlatformEmbedding(ctx, session, inputs, generation)
	if err == nil {
		b.embeddingBackpressure.finishProbe(probe)
		return decoded, nil
	}
	var atCapacity platformEmbeddingAtCapacity
	if errors.As(err, &atCapacity) {
		b.embeddingBackpressure.backpressure(atCapacity.retryAfter)
		return embeddingResponse{}, err
	}
	b.embeddingBackpressure.finishProbe(probe)
	return embeddingResponse{}, err
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

func (b *inferenceBroker) forwardPlatformEmbedding(
	ctx context.Context, session *brokerSession, inputs []string, caseGeneration uint64,
) (embeddingResponse, error) {
	session.mu.Lock()
	if !usesPlatformEmbedding(session.benchVersion) || !session.activeLocked(time.Now()) {
		session.mu.Unlock()
		return embeddingResponse{}, fmt.Errorf("embedding session unavailable")
	}
	grantID, bearer, proxyURL, generation := session.grantID, session.bearer, session.proxyURL, session.generation
	model := hostedEmbeddingModel
	dimensions := embeddingDimensions
	if session.confirmationSession {
		grant := session.confirmationGrants["embedding"]
		grantID, bearer, proxyURL, generation = grant.GrantID, grant.Bearer, grant.ProxyURL, grant.Generation
		model = grant.Model
		dimensions = embeddingDimensions
	}
	privateKey := append(ed25519.PrivateKey(nil), session.privateKey...)
	traceCtx := traceContextLocked(session, session.activeCaseGeneration, "", "")
	session.mu.Unlock()
	body, err := json.Marshal(platformEmbeddingRequest{
		Model: model, Input: inputs,
		Dimensions: dimensions, EncodingFormat: "float",
	})
	if err != nil {
		return embeddingResponse{}, err
	}
	endpoint := proxyURL
	if !session.confirmationSession {
		endpoint, err = platformEmbeddingURL(proxyURL)
		if err != nil {
			return embeddingResponse{}, err
		}
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
	if traceCtx != "" {
		upstream.Header.Set(traceContextHeader, traceCtx)
	}
	// Count every signed dispatch before transport. A request can commit its
	// Platform reservation and then lose its response, or still be in provider
	// flight while a concurrent sibling receives a terminal budget decline.
	// This deliberately overestimates possible spend: the evidence guard only
	// quarantines a decline when exhaustion is impossible even under this bound.
	session.mu.Lock()
	session.embeddingDispatches++
	if session.confirmationSession && caseGeneration != 0 {
		snapshot := session.caseSnapshots[caseGeneration]
		snapshot.EmbeddingDispatches++
		session.caseSnapshots[caseGeneration] = snapshot
	}
	session.embeddingChargeUpperBound += uint64(len(body))
	session.mu.Unlock()
	response, err := b.client.Do(upstream)
	if err != nil {
		// No response at all: transport/connection fault, the transient class.
		return embeddingResponse{}, platformEmbeddingTransient{err: err}
	}
	defer response.Body.Close()
	responseBody, err := io.ReadAll(io.LimitReader(response.Body, embeddingResponseLimit+1))
	atCapacity := err == nil && platformEmbeddingIsAtCapacity(response)
	if err != nil || len(responseBody) > embeddingResponseLimit || response.StatusCode < 200 || response.StatusCode >= 300 {
		if atCapacity {
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
				status:                response.StatusCode,
				code:                  platformDeclineCode(responseBody),
				reservationUpperBound: uint64(len(body)),
			}
		}
		return embeddingResponse{}, fmt.Errorf("embedding platform returned %d", response.StatusCode)
	}
	var platformResponse platformEmbeddingResponse
	if json.Unmarshal(responseBody, &platformResponse) != nil || platformResponse.Model != model ||
		len(platformResponse.Data) != len(inputs) || platformResponse.Usage.PromptTokens < 0 ||
		platformResponse.Usage.TotalTokens != platformResponse.Usage.PromptTokens {
		return embeddingResponse{}, fmt.Errorf("invalid platform embedding response")
	}
	decoded := embeddingResponse{
		Embeddings: make([][]float64, len(inputs)), PromptEvalCount: platformResponse.Usage.PromptTokens,
	}
	for index, item := range platformResponse.Data {
		if item.Index != index || len(item.Embedding) != dimensions {
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

type brokerSourceLease struct {
	session  *brokerSession
	sourceIP string
	epoch    uint64
}

type brokerConnectionLeaseContextKey struct{}

// connectionContext snapshots the source lease when the TCP connection is
// accepted. A request queued on an old keep-alive connection must never look
// up a newly rebound session merely because Docker reused the same source IP
// before its HTTP handler was scheduled.
func (b *inferenceBroker) connectionContext(ctx context.Context, connection net.Conn) context.Context {
	if ctx == nil {
		ctx = context.Background()
	}
	lease := brokerSourceLease{}
	if connection != nil && connection.RemoteAddr() != nil {
		lease = b.sessionLeaseForSource(sourceIP(connection.RemoteAddr().String()))
	}
	return context.WithValue(ctx, brokerConnectionLeaseContextKey{}, lease)
}

func (b *inferenceBroker) requestSourceLease(request *http.Request) brokerSourceLease {
	return b.requestCapabilityLease(request, false)
}

func (b *inferenceBroker) sessionLeaseForSource(ip string) brokerSourceLease {
	if net.ParseIP(ip) == nil {
		return brokerSourceLease{}
	}
	b.mu.RLock()
	defer b.mu.RUnlock()
	for _, session := range b.sessions {
		session.mu.Lock()
		matches := !session.sourceCapabilityRequired && session.expectedSourceIP == ip && session.activeLocked(time.Now())
		epoch := session.sourceEpoch
		session.mu.Unlock()
		if matches {
			return brokerSourceLease{session: session, sourceIP: ip, epoch: epoch}
		}
	}
	return brokerSourceLease{}
}

func (b *inferenceBroker) sessionLeaseForCapability(ip, token string) brokerSourceLease {
	if net.ParseIP(ip) == nil || !canonicalBrokerSourceCapability(token) {
		return brokerSourceLease{}
	}
	digest := sha256.Sum256([]byte(token))
	b.mu.RLock()
	defer b.mu.RUnlock()
	for _, session := range b.sessions {
		session.mu.Lock()
		matches := session.sourceCapabilityActive &&
			subtle.ConstantTimeCompare(digest[:], session.sourceCapabilityDigest[:]) == 1 &&
			session.expectedSourceIP == ip && session.activeLocked(time.Now())
		epoch := session.sourceEpoch
		session.mu.Unlock()
		if matches {
			return brokerSourceLease{session: session, sourceIP: ip, epoch: epoch}
		}
	}
	return brokerSourceLease{}
}

func bearerCapability(request *http.Request) string {
	provided, ok := strings.CutPrefix(request.Header.Get("Authorization"), "Bearer ")
	if !ok || !canonicalBrokerSourceCapability(provided) {
		return ""
	}
	return provided
}

func sameBrokerSourceCapability(left, right string) bool {
	if !canonicalBrokerSourceCapability(left) || !canonicalBrokerSourceCapability(right) {
		return false
	}
	return subtle.ConstantTimeCompare([]byte(left), []byte(right)) == 1
}

func (b *inferenceBroker) requestCapabilityLease(request *http.Request, requireBearer bool) brokerSourceLease {
	if request == nil {
		return brokerSourceLease{}
	}
	ip := sourceIP(request.RemoteAddr)
	hostCapability, capabilityHost := brokerSourceCapabilityFromHost(request.Host)
	bearer := bearerCapability(request)
	if requireBearer {
		if bearer != "" {
			return b.sessionLeaseForCapability(ip, bearer)
		}
		// Explicit loopback/test sessions retain their historical source-only
		// compatibility. Production sandboxes install sourceCapabilityRequired,
		// so this fallback stays closed even between revoke and unbind.
		return b.requestLegacySourceLease(request)
	}
	if capabilityHost {
		if hostCapability == "" || (bearer != "" && !sameBrokerSourceCapability(hostCapability, bearer)) {
			return brokerSourceLease{}
		}
		return b.sessionLeaseForCapability(ip, hostCapability)
	}
	if bearer != "" {
		return b.sessionLeaseForCapability(ip, bearer)
	}
	return b.requestLegacySourceLease(request)
}

func (b *inferenceBroker) requestLegacySourceLease(request *http.Request) brokerSourceLease {
	if request == nil {
		return brokerSourceLease{}
	}
	if lease, ok := request.Context().Value(brokerConnectionLeaseContextKey{}).(brokerSourceLease); ok {
		return lease
	}
	return b.sessionLeaseForSource(sourceIP(request.RemoteAddr))
}

func (b *inferenceBroker) sessionForSource(ip string) *brokerSession {
	return b.sessionLeaseForSource(ip).session
}

func (b *inferenceBroker) health(w http.ResponseWriter, session *brokerSession) {
	session.mu.Lock()
	defer session.mu.Unlock()
	status := "ok"
	if !session.activeLocked(time.Now()) {
		status = "unavailable"
	}
	writeJSON(w, http.StatusOK, relayHealthSnapshot{
		AccountingVersion:         2,
		Status:                    status,
		Requests:                  session.requests,
		Successes:                 session.successes,
		InfrastructureFailures:    session.failures,
		MinerRecoverableFailures:  session.minerRecoverableFailures,
		GrantDenials:              session.grantDenials,
		GrantAgentDeclines:        session.grantAgentDeclines,
		DeclineEvidenceMismatches: session.declineEvidenceMismatches,
		BudgetEvidenceAbsences:    session.budgetEvidenceAbsences,
		AgentRequestRejections:    session.agentRequestRejections,
		CapacityExhaustions:       session.capacityExhaustions,
		RecoveryWaits:             session.recoveryWaits,
		RecoveryExhaustions:       session.recoveryExhaustions,
		EmbeddingRetries:          session.embeddingRetries,
		CallerCancellations:       session.callerCancels,
		UpstreamAttempts:          session.upstreamAttempts,
		Provider:                  session.provider,
		ProfileRevision:           session.profileRevision,
		Model:                     session.model,
		UsageAvailable:            session.usageAvailable,
		UsageUnavailable:          session.usageUnavailable,
		PromptTokens:              session.promptTokens,
		PromptBytes:               session.promptBytes,
		CompletionTokens:          session.completionTokens,
		ProviderLatencyMs:         session.providerLatency,
		TTFTStatus:                "not_streamed",
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

func (b *inferenceBroker) proxy(
	w http.ResponseWriter,
	r *http.Request,
	session *brokerSession,
	caseGeneration uint64,
) {
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
	confirmationCase := session.confirmationSession && caseGeneration != 0
	// The model is a property of the ticket, not of the request. The harness
	// that produced this body is miner-authored, so its model field is at best
	// advisory: substitute the ticket's model rather than rejecting, so a
	// harness carrying a stale default (every pre-v7 fork of the starter kit
	// defaults to qwen/qwen3-32b) is scored on the locked model instead of
	// failing closed with an error it cannot act on. The platform proxy re-locks
	// the same value independently, so this is convenience, not the boundary.
	requestedModel := modelRequest.Model
	if requestedModel != session.requestModel || session.benchVersion >= protocol.BenchVersionV9 {
		rewritten, rewriteErr := normalizeChatRequest(body, session.requestModel, session.benchVersion)
		if rewriteErr != nil {
			if session.benchVersion >= protocol.BenchVersionV9 {
				session.agentRequestRejections++
				if confirmationCase {
					snapshot := session.caseSnapshots[caseGeneration]
					snapshot.ReaderAgentRejections++
					session.caseSnapshots[caseGeneration] = snapshot
				}
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
		counterfactual := ablationScope.counterfactual
		session.mu.Unlock()
		if r.Context().Err() != nil {
			writeError(w, http.StatusRequestTimeout, "synthetic inference cancelled")
			return
		}
		// A Bench v12 counterfactual scope FULLY ABLATES the model: it serves a
		// completion with no usable content so a harness that genuinely reasons over
		// the model output cannot recover the answer, while the v9 confirmation lane
		// keeps serving its neutral prose via Chat (byte-identical).
		completion, responseErr := responder.Chat(requestModel, uint64(len(body)))
		if counterfactual {
			completion, responseErr = responder.AblatedChat(requestModel, uint64(len(body)))
		}
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
	maxOutputTokens := session.maxOutputTokens
	grantDenialRoute := legacyGateway
	if trustedChatHandler != nil {
		grantDenialRoute = "trusted-in-process"
	}
	privateKey := append(ed25519.PrivateKey(nil), session.privateKey...)
	currentChargeUpperBound := platformChatChargeUpperBound(body, maxOutputTokens)
	traceCtx := traceContextLocked(session, caseGeneration, "", r.Header.Get(harnessCaseHeader))
	session.requests++
	if caseGeneration != 0 {
		snapshot := session.caseSnapshots[caseGeneration]
		snapshot.Requests++
		if confirmationCase {
			snapshot.ReaderDispatches++
		}
		session.caseSnapshots[caseGeneration] = snapshot
	}
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
	var responseFailureClass string
	var preReservationReaderRejection bool
	var totalLatency uint64
	// Every ticket-scoped provider call is single-shot. Platform and the broker
	// both park failures; only a Backroom ticket retry may repeat paid work.
	func() {
		nonce := uuid.NewString()
		requested := time.Now().UTC().Format("2006-01-02T15:04:05.000000+00:00")
		req, buildErr := http.NewRequestWithContext(requestCtx, http.MethodPost, proxyURL, bytes.NewReader(body))
		if buildErr != nil {
			return
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
			if traceCtx != "" {
				req.Header.Set(traceContextHeader, traceCtx)
			}
			// Count before transport for the same reason as embedding dispatches:
			// concurrent or response-lost admissions must remain in the
			// conservative upper bound when a sibling receives 4102/4104.
			session.mu.Lock()
			session.chatDispatches++
			session.chatChargeUpperBound += currentChargeUpperBound
			session.mu.Unlock()
		}
		session.mu.Lock()
		session.upstreamAttempts++
		session.mu.Unlock()
		started := time.Now()
		var resp *http.Response
		var requestErr error
		if trustedChatHandler != nil {
			resp, requestErr = callTrustedChatHandler(requestCtx, trustedChatHandler, body)
			preReservationReaderRejection = longmemeval.IsPreReservationReaderRejection(trustedChatHandler, resp)
		} else {
			resp, requestErr = b.client.Do(req)
		}
		totalLatency += uint64(time.Since(started).Milliseconds())
		if requestErr != nil {
			if requestCtx.Err() != nil {
				return
			}
			return
		}
		atCapacity := legacyGateway == "" && trustedChatHandler == nil && platformIsAtCapacity(resp)
		candidateBody, readErr := io.ReadAll(io.LimitReader(resp.Body, (16<<20)+1))
		_ = resp.Body.Close()
		responseStatus = resp.StatusCode
		responseFailureClass = resp.Header.Get(minerRecoverableFailureHeader)
		if readErr != nil || len(candidateBody) > 16<<20 {
			return
		}
		responseBody = candidateBody
		// Backpressure is terminal for this ticket-scoped request. Record its
		// distinct infrastructure cause, then park the score attempt without a
		// second nonce or provider reservation.
		if atCapacity {
			session.mu.Lock()
			session.capacityExhaustions++
			session.mu.Unlock()
			return
		}
		// Auth class: the platform declined the grant. The lease is gone, or its
		// budget is spent, so every further attempt would fail identically while
		// still consuming a fresh reservation and one more request from a grant
		// that cannot serve it. Stop immediately -- both terminal reasons want
		// the same action here, and only the reporting below tells them apart.
		if platformDeniesGrant(grantDenialRoute, responseStatus) {
			return
		}
		if responseStatus == http.StatusRequestTimeout || responseStatus == http.StatusTooManyRequests || responseStatus >= 500 {
			return
		}
	}()
	if requestCtx.Err() != nil {
		session.mu.Lock()
		session.callerCancels++
		session.providerLatency += totalLatency
		session.mu.Unlock()
		writeError(w, http.StatusConflict, "inference session unavailable")
		return
	}
	if responseStatus >= 400 && responseStatus < 500 && responseStatus != http.StatusTooManyRequests {
		// An ordinary Platform route owns its established 4xx attribution. The
		// in-process confirmation reader is stricter: only its private
		// pre-reservation provenance marker proves that the harness request was
		// rejected before provider accounting. A provider-returned receipted
		// 400/413 has the same status but must remain unattributed here.
		//
		// usageUnavailable is still incremented, unchanged, so the run fails
		// exactly as before via requireCompleteV7Usage. The new counter is
		// purely attributive.
		agentAttributedRejection := trustedChatHandler == nil || preReservationReaderRejection
		session.mu.Lock()
		session.usageUnavailable++
		if agentAttributedRejection {
			session.agentRequestRejections++
			if confirmationCase && preReservationReaderRejection {
				snapshot := session.caseSnapshots[caseGeneration]
				snapshot.ReaderAgentRejections++
				session.caseSnapshots[caseGeneration] = snapshot
			}
		} else {
			session.failures++
		}
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
		if agentAttributedRejection {
			log.Printf(
				"run %s: platform rejected the harness's inference request with %d before any reservation -- AGENT fault, no provider was contacted (rejection #%d): %s",
				runID, responseStatus, rejections, detail,
			)
		} else {
			log.Printf(
				"run %s: trusted reader returned receipted or otherwise unattributed status %d; preserving provider/accounting failure semantics: %s",
				runID, responseStatus, detail,
			)
		}
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
		agentDecline, attribution := attributePlatformDeclineLocked(session, declineCode, false, currentChargeUpperBound)
		denials, agentDenials := session.grantDenials, session.grantAgentDeclines
		mismatches := session.declineEvidenceMismatches
		absences := session.budgetEvidenceAbsences
		dispatches, requestBudget := session.chatDispatches, session.requestBudget
		chargeUpperBound, tokenBudget := session.chatChargeUpperBound, session.tokenBudget
		settledTokens := session.promptTokens + session.completionTokens
		successes, inFlight := session.successes, session.inFlight
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
			"run %s: platform declined the inference grant (429: %s -- %s); ticket deadline held locally is %s (in %s) -- this is a lease denial, not a provider fault (denial #%d, agent-attributable #%d, evidence-mismatch #%d, evidence-absent #%d, signed dispatches=%d/%d, successes=%d in_flight=%d, settled_tokens=%d, this_request_upper_bound=%d, dispatched charge upper bound=%d/%d)",
			runID, platformDeclineReason(declineCode), attribution,
			deadline.UTC().Format(time.RFC3339), time.Until(deadline).Truncate(time.Second),
			denials, agentDenials, mismatches, absences, dispatches, requestBudget, successes, inFlight,
			settledTokens, currentChargeUpperBound, chargeUpperBound, tokenBudget,
		)
		writeError(w, http.StatusBadGateway, "inference provider unavailable")
		return
	}
	if responseStatus < 200 || responseStatus >= 300 || len(responseBody) == 0 {
		session.mu.Lock()
		// Only the authenticated Platform proxy may classify a response as
		// miner-recoverable. Legacy gateways and in-process readers cannot spoof
		// this opt-out from fail-closed infrastructure accounting.
		if minerRecoverablePlatformFailure(legacyGateway, trustedChatHandler, responseStatus, responseFailureClass) {
			session.minerRecoverableFailures++
		} else {
			session.failures++
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
	// The delay fingerprint is computed and booked in the same locked section
	// that books the success, so the scorer's case-window deltas see the two
	// facts move together. The sleep itself happens after the lock is
	// released: only this handler's response waits, never a sibling call.
	var injectedDelay time.Duration
	if caseGeneration != 0 {
		snapshot := session.caseSnapshots[caseGeneration]
		snapshot.Successes++
		if confirmationCase {
			snapshot.ReaderReceipted++
		}
		if session.benchVersion >= protocol.BenchVersionV9 {
			injectedDelay = delayFingerprintFor(
				session.delayFingerprintKey, caseGeneration, snapshot.DelayedRequests, session.delayFP,
			)
			if injectedDelay > 0 {
				snapshot.DelayedRequests++
				snapshot.InjectedDelayMS += uint64(injectedDelay / time.Millisecond)
			}
		}
		session.caseSnapshots[caseGeneration] = snapshot
	}
	recordModelToolCallsLocked(session, caseGeneration, responseBody)
	// Bench v12 answer-stuffing capture: record this call's bounded/normalized
	// input and completion value tokens in call order. `body` is the normalized
	// model INPUT; `responseBody` is the COMPLETION. No-op for bench_version<12.
	recordAnswerIOLocked(session, caseGeneration, body, responseBody)
	session.providerLatency += totalLatency
	if usageOK {
		session.usageAvailable++
		session.promptTokens += uint64(decoded.Usage.PromptTokens)
		session.completionTokens += uint64(decoded.Usage.CompletionTokens)
	} else {
		session.usageUnavailable++
	}
	session.mu.Unlock()
	if injectedDelay > 0 {
		// Hold the completed upstream response for the scheduled fingerprint
		// delay before releasing it to the harness. The upstream work, the
		// platform accounting, and the case-window booking above are all done;
		// a caller cancel during the hold is honored by the shared sleep and
		// simply abandons a response that was already recorded as delivered
		// evidence -- exactly like a cancel during an upstream read today.
		_ = b.sleep(requestCtx, injectedDelay)
	}
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
	b.proxy(recorder, req, session, 0)
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
		embedding, err := b.forwardPlatformEmbeddingOnce(ctx, session, []string{"validator embedding preflight"})
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
		MinerRecoverableFailures: session.minerRecoverableFailures,
		GrantDenials:             session.grantDenials, EmbeddingRetries: session.embeddingRetries,
		GrantAgentDeclines:        session.grantAgentDeclines,
		DeclineEvidenceMismatches: session.declineEvidenceMismatches,
		BudgetEvidenceAbsences:    session.budgetEvidenceAbsences,
		AgentRequestRejections:    session.agentRequestRejections,
		CapacityExhaustions:       session.capacityExhaustions,
		RecoveryWaits:             session.recoveryWaits,
		RecoveryExhaustions:       session.recoveryExhaustions,
		CallerCancellations:       session.callerCancels, UpstreamAttempts: session.upstreamAttempts,
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
	// Confirmation counters are isolated from the ordinary v9 model-use
	// counters above. They cover both purpose-bound reader chat and embedding
	// calls admitted while one LongMem /run generation is active. Delivered is
	// incremented only after the Platform capability endpoint returned a
	// validated HTTP 200 response. It corroborates agent activity but is not
	// canonical signed provider receipt evidence.
	ReaderAttempts         uint64
	ReaderDispatches       uint64
	ReaderReceipted        uint64
	ReaderAgentRejections  uint64
	ReaderInFlight         int
	ReaderCancellations    uint64
	EmbeddingAttempts      uint64
	EmbeddingDispatches    uint64
	EmbeddingDelivered     uint64
	EmbeddingInFlight      int
	EmbeddingCancellations uint64
	ActiveHandlers         int
	Draining               bool
	// DelayedRequests and InjectedDelayMS record the delay-fingerprint
	// schedule realized inside this case window (delay_fingerprint.go): how
	// many successful completions were held, and for how long in total. Booked
	// under the session lock at the same moment as Successes, so the scorer's
	// begin/end deltas attribute them with the same exactness.
	DelayedRequests      uint64
	InjectedDelayMS      uint64
	ModelToolCalls       uint64
	EndpointAttempts     uint64
	MatchedToolCalls     uint64
	UnmatchedToolCalls   uint64
	ToolEvidenceComplete bool
	ToolFindings         uint64
}

// beginCaseSnapshot advances the source-bound generation before one ordinary
// v9 case starts. Requests admitted before this point remain attached to the
// preceding generation even if their provider recovery finishes later.
// Inference trace context. Every request the broker forwards to the platform
// relay carries X-Ditto-Trace-Context: what this broker knows about WHICH run,
// agent, slot and benchmark case the call serves, so the relay's trace capture
// can file the call as training data instead of as an anonymous completion.
//
// Attribution is best-effort and honest about its source: an exclusive case
// window (v9 serial / confirmation reader) names the case exactly; under
// concurrent /run the broker reports the cases in flight and, when the
// harness sent X-Ditto-Case-Id, that claim (verified only if it names an
// in-flight case). None of this feeds admission, scoring or accounting; the
// relay records it verbatim and the proof does not cover it.
const (
	traceContextHeader = "X-Ditto-Trace-Context"
	harnessCaseHeader  = "X-Ditto-Case-Id"
	traceContextMaxIn  = 64 // cases_in_flight is capped (case_concurrency max is 64)
)

type traceContext struct {
	Version        int      `json:"v"`
	RunID          string   `json:"run_id,omitempty"`
	SessionID      string   `json:"session_id,omitempty"`
	AgentID        string   `json:"agent_id,omitempty"`
	SlotID         string   `json:"slot_id,omitempty"`
	BenchVersion   int      `json:"bench_version,omitempty"`
	Lane           string   `json:"lane,omitempty"` // confirmation lane when applicable
	CaseID         string   `json:"case_id,omitempty"`
	CaseSource     string   `json:"case_source,omitempty"` // window | claim | in_flight
	CaseVerified   bool     `json:"case_verified"`
	ClaimedCaseID  string   `json:"claimed_case_id,omitempty"`
	CaseGeneration uint64   `json:"case_generation,omitempty"`
	CasesInFlight  []string `json:"cases_in_flight,omitempty"`
}

// traceContextLocked builds the header value. Caller holds session.mu.
func traceContextLocked(session *brokerSession, caseGeneration uint64, lane, claimed string) string {
	tc := traceContext{
		Version:        1,
		RunID:          session.boundRunID,
		SessionID:      session.id,
		AgentID:        session.ticketAgentID,
		SlotID:         session.ticketSlotID,
		BenchVersion:   session.benchVersion,
		Lane:           lane,
		CaseGeneration: caseGeneration,
	}
	if len(session.runCases) > 0 {
		tc.CasesInFlight = make([]string, 0, len(session.runCases))
		for caseID := range session.runCases {
			tc.CasesInFlight = append(tc.CasesInFlight, caseID)
		}
		sort.Strings(tc.CasesInFlight)
		if len(tc.CasesInFlight) > traceContextMaxIn {
			tc.CasesInFlight = tc.CasesInFlight[:traceContextMaxIn]
		}
	}
	claimed = strings.TrimSpace(claimed)
	if len(claimed) > 128 {
		claimed = claimed[:128]
	}
	tc.ClaimedCaseID = claimed
	switch {
	case caseGeneration != 0 && session.activeCaseGeneration == caseGeneration && session.activeCaseID != "":
		tc.CaseID, tc.CaseSource, tc.CaseVerified = session.activeCaseID, "window", true
	case claimed != "" && session.runCases[claimed] > 0:
		tc.CaseID, tc.CaseSource, tc.CaseVerified = claimed, "claim", true
	case claimed != "":
		tc.CaseID, tc.CaseSource = claimed, "claim"
	case len(session.runCases) == 1:
		for caseID := range session.runCases {
			tc.CaseID, tc.CaseSource, tc.CaseVerified = caseID, "in_flight", true
		}
	}
	encoded, err := json.Marshal(tc)
	if err != nil {
		return ""
	}
	return string(encoded)
}

// beginRunCase marks caseID in flight on the session for trace attribution.
// Returns false when the session is unknown (the scorer ignores that: the
// run proceeds, the traces are merely less attributed).
func (b *inferenceBroker) beginRunCase(id, caseID string) bool {
	if caseID == "" {
		return false
	}
	b.mu.RLock()
	session := b.sessions[id]
	b.mu.RUnlock()
	if session == nil {
		return false
	}
	session.mu.Lock()
	defer session.mu.Unlock()
	if session.runCases == nil {
		session.runCases = make(map[string]int)
	}
	session.runCases[caseID]++
	return true
}

// endRunCase releases one beginRunCase.
func (b *inferenceBroker) endRunCase(id, caseID string) {
	b.mu.RLock()
	session := b.sessions[id]
	b.mu.RUnlock()
	if session == nil {
		return
	}
	session.mu.Lock()
	defer session.mu.Unlock()
	if session.runCases[caseID] <= 1 {
		delete(session.runCases, caseID)
		return
	}
	session.runCases[caseID]--
}

func (b *inferenceBroker) beginCaseSnapshot(id string, caseIDs ...string) (uint64, brokerCaseSnapshot, error) {
	b.mu.RLock()
	session := b.sessions[id]
	b.mu.RUnlock()
	if session == nil {
		return 0, brokerCaseSnapshot{}, fmt.Errorf("inference session unavailable")
	}
	session.mu.Lock()
	defer session.mu.Unlock()
	if session.activeCaseGeneration != 0 {
		return 0, brokerCaseSnapshot{}, fmt.Errorf("inference case generation already active")
	}
	caseID := ""
	if len(caseIDs) > 0 {
		caseID = caseIDs[0]
	}
	if session.benchVersion >= protocol.BenchVersionV10 && caseID == "" {
		return 0, brokerCaseSnapshot{}, fmt.Errorf("v10 inference case id unavailable")
	}
	session.caseGeneration++
	if session.caseSnapshots == nil {
		session.caseSnapshots = make(map[uint64]brokerCaseSnapshot)
	}
	generation := session.caseGeneration
	session.activeCaseGeneration = generation
	session.activeCaseID = caseID
	session.caseSnapshots[generation] = brokerCaseSnapshot{
		ToolEvidenceComplete: session.benchVersion >= protocol.BenchVersionV10,
	}
	registerAnswerIOLocked(session, generation, caseID)
	snapshot := session.caseSnapshots[generation]
	return generation, snapshot, nil
}

// endCaseSnapshot atomically closes one ordinary case before returning its
// counters. A request admitted after the harness response is therefore never
// mistaken for evidence that informed that response, while a request admitted
// earlier retains the captured generation until it finishes.
func (b *inferenceBroker) endCaseSnapshot(id string, generation uint64) (brokerCaseSnapshot, error) {
	b.mu.RLock()
	session := b.sessions[id]
	b.mu.RUnlock()
	if session == nil {
		return brokerCaseSnapshot{}, fmt.Errorf("inference session unavailable")
	}
	session.mu.Lock()
	defer session.mu.Unlock()
	if generation == 0 || session.activeCaseGeneration != generation {
		return brokerCaseSnapshot{}, fmt.Errorf("inference case generation unavailable")
	}
	session.activeCaseGeneration = 0
	session.activeCaseID = ""
	return session.caseSnapshots[generation], nil
}

func (b *inferenceBroker) beginConfirmationCaseDrain(id string, generation uint64) error {
	b.mu.RLock()
	session := b.sessions[id]
	b.mu.RUnlock()
	if session == nil {
		return fmt.Errorf("inference session unavailable")
	}
	session.mu.Lock()
	defer session.mu.Unlock()
	if !session.confirmationSession || generation == 0 || session.activeCaseGeneration != generation {
		return fmt.Errorf("confirmation case generation unavailable")
	}
	snapshot := session.caseSnapshots[generation]
	if snapshot.Draining {
		return nil
	}
	snapshot.Draining = true
	session.caseSnapshots[generation] = snapshot
	return nil
}

// waitConfirmationCaseDrained waits after the submitted process has been
// strictly removed until every broker handler admitted under this generation's
// source epoch has returned. The caller then CAS-unbinds that dead source
// before closing the generation.
func (b *inferenceBroker) waitConfirmationCaseDrained(
	ctx context.Context,
	id string,
	generation uint64,
) (brokerCaseSnapshot, error) {
	if ctx == nil {
		return brokerCaseSnapshot{}, fmt.Errorf("confirmation case drain context unavailable")
	}
	ticker := time.NewTicker(10 * time.Millisecond)
	defer ticker.Stop()
	for {
		snapshot, err := b.generationCaseSnapshot(id, generation)
		if err != nil {
			return brokerCaseSnapshot{}, err
		}
		if snapshot.ActiveHandlers == 0 && snapshot.InFlight == 0 &&
			snapshot.ReaderInFlight == 0 && snapshot.EmbeddingInFlight == 0 {
			return snapshot, nil
		}
		select {
		case <-ctx.Done():
			return brokerCaseSnapshot{}, fmt.Errorf("confirmation case broker drain unavailable")
		case <-ticker.C:
		}
	}
}

// recordAnswerIOLocked captures one successful clean-pass model call's bounded,
// normalized value tokens for the Bench v12 answer-stuffing gate. It runs under
// the session lock (called from proxy's success path) and only for
// bench_version>=12 with an active case generation that opened a capture log. It
// records ONLY value tokens (never the answer key or raw prose), and marks the
// log truncated -- which the scorer reads as unsettled (fail open) -- when a body
// is unparseable or any capture bound is exceeded. requestBody is the normalized
// model INPUT the harness sent; responseBody is the model COMPLETION.
func recordAnswerIOLocked(session *brokerSession, caseGeneration uint64, requestBody, responseBody []byte) {
	if session.benchVersion < protocol.BenchVersionV12 || caseGeneration == 0 || session.answerIO == nil {
		return
	}
	log := session.answerIO[caseGeneration]
	if log == nil {
		return
	}
	if len(log.calls) >= answerIOMaxCalls {
		log.truncated = true
		return
	}
	inputText, okIn := parseChatInputText(requestBody)
	completionText, okOut := parseChatCompletionText(responseBody)
	if !okIn || !okOut {
		log.truncated = true
		return
	}
	inputTokens, trIn := valueTokenSet(inputText)
	completionTokens, trOut := valueTokenSet(completionText)
	if trIn || trOut {
		log.truncated = true
	}
	log.calls = append(log.calls, caseModelCall{inputTokens: inputTokens, completionTokens: completionTokens})
}

// registerAnswerIOLocked opens a v12 answer-stuffing capture log for one case
// generation, keyed by both generation (for proxy-time appends) and wire case id
// (for the post-run scorer read). Both maps hold the same pointer. It is a no-op
// for bench_version<12 or a missing case id, so v9..v11 sessions never allocate
// capture state.
func registerAnswerIOLocked(session *brokerSession, generation uint64, caseID string) {
	if session.benchVersion < protocol.BenchVersionV12 || generation == 0 || caseID == "" {
		return
	}
	if session.answerIO == nil {
		session.answerIO = make(map[uint64]*caseModelIOLog)
		session.answerIOByCaseID = make(map[string]*caseModelIOLog)
	}
	log := &caseModelIOLog{}
	session.answerIO[generation] = log
	session.answerIOByCaseID[caseID] = log
}

// caseModelIO returns the trusted clean-pass model I/O the broker recorded for
// one case. ok=false when no capture log exists (pre-v12, unknown case, or a case
// that never reached the model), which a consumer treats as unsettled (fail
// open). The returned log is a shallow snapshot; the caller only reads it.
//
// The v12 answer-stuffing scorer pass that consumed this was removed when /run
// became concurrent (it needed exclusive per-case windows). The read seam is kept
// beside the still-live capture so a restored per-case pass can consume it.
func (b *inferenceBroker) caseModelIO(id, caseID string) (caseModelIOLog, bool) {
	b.mu.RLock()
	session := b.sessions[id]
	b.mu.RUnlock()
	if session == nil {
		return caseModelIOLog{}, false
	}
	session.mu.Lock()
	defer session.mu.Unlock()
	log := session.answerIOByCaseID[caseID]
	if log == nil {
		return caseModelIOLog{}, false
	}
	calls := make([]caseModelCall, len(log.calls))
	copy(calls, log.calls)
	return caseModelIOLog{calls: calls, truncated: log.truncated}, true
}

// toolFindingNames renders a tool-finding bitmask as the wire finding names,
// in a fixed order, with room left for the count-derived findings appended by
// the callers.
func toolFindingNames(bits uint64) []string {
	findings := make([]string, 0, 7)
	for _, finding := range []struct {
		bit  uint64
		name string
	}{
		{toolFindingUnbacked, "unbacked_harness_execution"},
		{toolFindingNameArgumentMismatch, "name_argument_mismatch"},
		{toolFindingDuplicateExecution, "duplicate_tool_execution"},
		{toolFindingCrossCaseReplay, "cross_case_replay"},
		{toolFindingInvalidModelEmission, "invalid_model_tool_emission"},
	} {
		if bits&finding.bit != 0 {
			findings = append(findings, finding.name)
		}
	}
	return findings
}

// generationCaseSnapshot returns only calls admitted during one ordinary case
// generation. InFlight is intentionally preserved for audit: a request still
// running after the harness has returned cannot have informed that response,
// so the scorer excludes it without letting its eventual completion bleed into
// the next case.
func (b *inferenceBroker) generationCaseSnapshot(
	id string,
	generation uint64,
) (brokerCaseSnapshot, error) {
	b.mu.RLock()
	session := b.sessions[id]
	b.mu.RUnlock()
	if session == nil {
		return brokerCaseSnapshot{}, fmt.Errorf("inference session unavailable")
	}
	session.mu.Lock()
	defer session.mu.Unlock()
	if _, ok := session.caseSnapshots[generation]; generation == 0 || !ok {
		return brokerCaseSnapshot{}, fmt.Errorf("inference case generation unavailable")
	}
	return session.caseSnapshots[generation], nil
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

// settledCaseSnapshot waits until every chat request already admitted for this
// source has finished before returning its monotonic counters. A harness may
// finish its /run response a few milliseconds before the broker handler has
// copied the final upstream bytes and decremented inFlight. Treating that
// harmless tail as an overlapping case makes the entire v9 run's distinct-case
// evidence fail closed even though the trusted request succeeded. The caller
// supplies a bounded context, so a genuinely stuck/background request remains
// incomplete rather than being attributed to the next case.
func (b *inferenceBroker) settledCaseSnapshot(ctx context.Context, id string) (brokerCaseSnapshot, error) {
	const pollInterval = 5 * time.Millisecond
	for {
		snapshot, err := b.caseSnapshot(id)
		if err != nil || snapshot.InFlight == 0 {
			return snapshot, err
		}
		timer := time.NewTimer(pollInterval)
		select {
		case <-ctx.Done():
			if !timer.Stop() {
				<-timer.C
			}
			return brokerCaseSnapshot{}, ctx.Err()
		case <-timer.C:
		}
	}
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

// normalizeChatRequest pins the ticket model and, for v9 and later, canonicalizes the
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
	if benchVersion < protocol.BenchVersionV9 {
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
	// Nested wins on conflict. OpenRouter 400s when both aliases disagree;
	// dropping the flat sibling is the same heal as matching aliases.
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
