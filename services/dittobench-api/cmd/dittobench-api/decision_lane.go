package main

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"time"

	"github.com/google/uuid"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Bench v15 decision lane at the harness boundary.
//
// From bench_version 15 a scored harness may call the locked decision model
// (TypeSafe Jev, pinned by the operator on the inference board) through the
// same source-bound broker that serves chat and embeddings:
//
//	POST $DITTOBENCH_DECISIONS_URL   (= $DITTOBENCH_INFERENCE_BASE_URL/decisions)
//
// The body is the System One body ({state, questions, assets?}); the relay
// forces the model to the grant's pin and meters it as ledger cost. How a
// harness uses the lane is unrestricted. The broker adds no policy of its own:
// it authenticates the call with the run's grant exactly as it does chat,
// bounds the in-flight count, and passes the relay's answer (including its
// 4xx/5xx shapes) back verbatim. Below v15, and on confirmation or ablation
// sessions, the route does not exist.

const (
	platformDecisionAPIPath = "/api/v1/inference/decisions"
	// decisionRequestLimit and decisionResponseLimit bound the bodies the
	// broker relays; the relay applies its own, tighter request limit.
	decisionRequestLimit  = 1 << 20
	decisionResponseLimit = 1 << 20
	// decisionInFlightPerSession bounds concurrent decision calls of one run at
	// the broker. The relay's per-ticket rail is the authoritative limit.
	decisionInFlightPerSession = 16
	harnessDecisionsEnvKey     = "DITTOBENCH_DECISIONS_URL"
)

// usesDecisionLane reports whether a bench version exposes the decision lane.
func usesDecisionLane(benchVersion int) bool {
	return benchVersion >= protocol.BenchVersionV15
}

func platformDecisionURL(chatProxyURL string) (string, error) {
	parsed, err := url.Parse(chatProxyURL)
	if err != nil || parsed.Scheme == "" || parsed.Host == "" || parsed.RawQuery != "" || parsed.Fragment != "" ||
		parsed.Path != platformInferenceAPIPath {
		return "", fmt.Errorf("invalid platform inference route")
	}
	parsed.Path = platformDecisionAPIPath
	return parsed.String(), nil
}

func (b *inferenceBroker) handleDecision(w http.ResponseWriter, r *http.Request, lease brokerSourceLease) {
	session := lease.session
	session.mu.Lock()
	if !usesDecisionLane(session.benchVersion) || session.confirmationSession || session.ablation != nil {
		session.mu.Unlock()
		writeError(w, http.StatusNotFound, "inference route not found")
		return
	}
	if session.sourceEpoch != lease.epoch || session.expectedSourceIP != lease.sourceIP ||
		!session.activeLocked(time.Now()) {
		session.mu.Unlock()
		writeError(w, http.StatusUnauthorized, "inference session unavailable")
		return
	}
	if session.decisionInFlight >= decisionInFlightPerSession {
		session.mu.Unlock()
		w.Header().Set("Retry-After", "1")
		writeError(w, http.StatusTooManyRequests, "decision lane is at capacity")
		return
	}
	session.decisionInFlight++
	// Register the call's cancel in the same critical section that admitted
	// it, like chat, so destroying the session (revoke, expiry, run end) can
	// never miss an admitted decision call.
	requestCtx, cancel := context.WithCancel(r.Context())
	cancelID := uuid.NewString()
	if session.cancels == nil {
		session.cancels = make(map[string]context.CancelFunc)
	}
	session.cancels[cancelID] = cancel
	grantID, bearer, proxyURL, generation := session.grantID, session.bearer, session.proxyURL, session.generation
	privateKey := append(ed25519.PrivateKey(nil), session.privateKey...)
	traceCtx := traceContextLocked(session, session.activeCaseGeneration, "", "", "")
	session.mu.Unlock()
	defer func() {
		cancel()
		session.mu.Lock()
		session.decisionInFlight--
		delete(session.cancels, cancelID)
		session.mu.Unlock()
	}()

	body, err := io.ReadAll(io.LimitReader(r.Body, decisionRequestLimit+1))
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid decision request")
		return
	}
	if len(body) > decisionRequestLimit {
		writeError(w, http.StatusRequestEntityTooLarge, "decision request is too large")
		return
	}
	endpoint, err := platformDecisionURL(proxyURL)
	if err != nil {
		writeError(w, http.StatusBadGateway, "decision lane unavailable")
		return
	}
	nonce := uuid.NewString()
	requested := time.Now().UTC().Format("2006-01-02T15:04:05.000000+00:00")
	digest := sha256.Sum256(body)
	message := fmt.Sprintf("ditto-inference:v1:%s:%d:%s:%s:%s", grantID, generation, nonce, requested, hex.EncodeToString(digest[:]))
	proof := base64.RawURLEncoding.EncodeToString(ed25519.Sign(privateKey, []byte(message)))
	upstream, err := http.NewRequestWithContext(requestCtx, http.MethodPost, endpoint, bytes.NewReader(body))
	if err != nil {
		writeError(w, http.StatusBadGateway, "decision lane unavailable")
		return
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
	// Bench v16 priced cost: until the call settles, its request size stands
	// in for its input tokens (an upper bound: a token is at least one byte),
	// so a call still in flight when the run is priced is never free.
	pending := uint64(len(body))
	session.mu.Lock()
	session.decisionDispatches++
	session.decisionPendingBytes += pending
	session.mu.Unlock()
	response, err := b.client.Do(upstream)
	if err != nil {
		// No response at all: the same no-fault relay-unavailable class the
		// chat lane reports. A call cancelled by the harness or by run
		// teardown may still have been metered, so it keeps its upper bound;
		// a transport failure is not charged.
		settleDecisionWithoutUsage(session, pending, requestCtx.Err() != nil)
		writeError(w, http.StatusBadGateway, "decision lane unavailable")
		return
	}
	defer response.Body.Close()
	responseBody, err := io.ReadAll(io.LimitReader(response.Body, decisionResponseLimit+1))
	if err != nil || len(responseBody) > decisionResponseLimit {
		// The relay answered, so it may have metered the call; with the body
		// unreadable its usage is unknown. Keep the request-size bound, and
		// a 2xx also counts as usage missing (recordDecisionUsage with nil).
		if response.StatusCode >= 200 && response.StatusCode < 300 {
			recordDecisionUsage(session, response.StatusCode, nil, pending)
		} else {
			settleDecisionWithoutUsage(session, pending, true)
		}
		writeError(w, http.StatusBadGateway, "decision lane unavailable")
		return
	}
	recordDecisionUsage(session, response.StatusCode, responseBody, pending)
	if retryAfter := response.Header.Get("Retry-After"); retryAfter != "" {
		w.Header().Set("Retry-After", retryAfter)
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(response.StatusCode)
	_, _ = w.Write(responseBody)
}

// recordDecisionUsage settles one dispatched decision call (pending is its
// request size, booked at dispatch) with the relay-reported usage
// ({usage: {input_tokens, output_tokens}}, both required) for the bench v16
// priced cost (v16_priced_cost.go). Usage is booked whatever the status, so a
// call the relay metered and then answered with an error is still priced. A
// 2xx without complete usage (including an unreadable body, passed as nil) is
// counted as missing, which makes the run's priced cost insufficient evidence
// rather than silently cheaper; an unreadable body also keeps the call's
// request-size upper bound.
func recordDecisionUsage(session *brokerSession, status int, responseBody []byte, pending uint64) {
	var decoded struct {
		Usage *struct {
			InputTokens  *int64 `json:"input_tokens"`
			OutputTokens *int64 `json:"output_tokens"`
		} `json:"usage"`
	}
	usageOK := responseBody != nil && json.Unmarshal(responseBody, &decoded) == nil && decoded.Usage != nil &&
		decoded.Usage.InputTokens != nil && *decoded.Usage.InputTokens >= 0 &&
		decoded.Usage.OutputTokens != nil && *decoded.Usage.OutputTokens >= 0
	success := status >= 200 && status < 300
	session.mu.Lock()
	defer session.mu.Unlock()
	releaseDecisionPendingLocked(session, pending)
	if success {
		session.decisionCompleted++
		if !usageOK {
			session.decisionUsageMissing++
		}
	}
	if usageOK {
		session.decisionInputTokens += uint64(*decoded.Usage.InputTokens)
		session.decisionOutputTokens += uint64(*decoded.Usage.OutputTokens)
	} else if responseBody == nil {
		// An unreadable body: keep the call's request-size upper bound.
		session.decisionInputTokens += pending
	}
}

// settleDecisionWithoutUsage settles a dispatched call that produced no usage.
// keepBound books its request-size upper bound as input tokens (a cancelled
// call the relay may still have metered); otherwise it is not charged.
func settleDecisionWithoutUsage(session *brokerSession, pending uint64, keepBound bool) {
	session.mu.Lock()
	defer session.mu.Unlock()
	releaseDecisionPendingLocked(session, pending)
	if keepBound {
		session.decisionInputTokens += pending
	}
}

// releaseDecisionPendingLocked removes a settled call's upper bound from the
// in-flight total. The caller holds session.mu.
func releaseDecisionPendingLocked(session *brokerSession, pending uint64) {
	if pending > session.decisionPendingBytes {
		pending = session.decisionPendingBytes
	}
	session.decisionPendingBytes -= pending
}
