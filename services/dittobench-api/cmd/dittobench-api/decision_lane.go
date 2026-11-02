package main

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
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
	session.mu.Lock()
	session.decisionDispatches++
	session.mu.Unlock()
	response, err := b.client.Do(upstream)
	if err != nil {
		// No response at all: the same no-fault relay-unavailable class the
		// chat lane reports.
		writeError(w, http.StatusBadGateway, "decision lane unavailable")
		return
	}
	defer response.Body.Close()
	responseBody, err := io.ReadAll(io.LimitReader(response.Body, decisionResponseLimit+1))
	if err != nil || len(responseBody) > decisionResponseLimit {
		writeError(w, http.StatusBadGateway, "decision lane unavailable")
		return
	}
	if retryAfter := response.Header.Get("Retry-After"); retryAfter != "" {
		w.Header().Set("Retry-After", retryAfter)
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(response.StatusCode)
	_, _ = w.Write(responseBody)
}
