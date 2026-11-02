package main

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/llm"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/google/uuid"
)

func protectedBrokerSession(t *testing.T, sourceIP string) (*inferenceBroker, string, string, string) {
	t.Helper()
	broker := newInferenceBroker(1)
	sessionID, runID := "capability-session", "00000000-0000-0000-0000-000000000111"
	broker.sessions[sessionID] = &brokerSession{
		id: sessionID, boundRunID: runID, bearer: "platform-ticket",
		expiresAt: time.Now().Add(time.Hour), legacyGateway: "active",
	}
	token, err := broker.installSourceCapability(sessionID, runID)
	if err != nil {
		t.Fatal(err)
	}
	if !broker.bindSourceCapability(sessionID, runID, sourceIP, token) {
		t.Fatal("bind protected source")
	}
	return broker, sessionID, runID, token
}

func capabilityRequest(method, target, sourceIP, host, bearer string) *http.Request {
	request := httptest.NewRequest(method, target, bytes.NewBufferString(`{}`))
	request.RemoteAddr = sourceIP + ":4321"
	request.Host = host
	if bearer != "" {
		request.Header.Set("Authorization", "Bearer "+bearer)
	}
	return request
}

func TestBrokerSourceCapabilityCanonicalHost(t *testing.T) {
	token, err := newBrokerSourceCapability()
	if err != nil {
		t.Fatal(err)
	}
	if len(token) != brokerSourceCapabilityChars || !canonicalBrokerSourceCapability(token) {
		t.Fatalf("generated capability is not canonical: %q", token)
	}
	host, err := brokerSourceCapabilityHost(token)
	if err != nil {
		t.Fatal(err)
	}
	if want := "c-" + token + ".host.docker.internal"; host != want {
		t.Fatalf("host=%q want=%q", host, want)
	}
	parsed, shaped := brokerSourceCapabilityFromHost(strings.ToUpper(host) + ":11436")
	if !shaped || parsed != token {
		t.Fatalf("parsed=%q shaped=%v", parsed, shaped)
	}
	for _, invalid := range []string{
		"", strings.Repeat("a", brokerSourceCapabilityChars-1),
		strings.Repeat("a", brokerSourceCapabilityChars-1) + "0",
		strings.ToUpper(token), token + "a",
	} {
		if canonicalBrokerSourceCapability(invalid) {
			t.Fatalf("accepted non-canonical capability %q", invalid)
		}
	}
	if parsed, shaped := brokerSourceCapabilityFromHost("c-not-a-capability.host.docker.internal"); !shaped || parsed != "" {
		t.Fatalf("malformed capability host parsed=%q shaped=%v", parsed, shaped)
	}
}

func TestDirectBrokerCapabilitySupportsStarterPlaceholderBearer(t *testing.T) {
	const sourceIP = "192.0.2.80"
	broker, _, _, token := protectedBrokerSession(t, sourceIP)
	host, err := brokerSourceCapabilityHost(token)
	if err != nil {
		t.Fatal(err)
	}
	request := capabilityRequest(http.MethodGet, "http://"+host+":11436/v1/inference/health", sourceIP, host+":11436", brokerPlaceholderKey)
	request.SetPathValue("rest", "health")
	response := httptest.NewRecorder()
	broker.handle(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", response.Code, response.Body.String())
	}
}

func TestBrokerCapabilityRejectsMismatchAndStaysRequiredAfterRevoke(t *testing.T) {
	const sourceIP = "192.0.2.81"
	broker, sessionID, runID, token := protectedBrokerSession(t, sourceIP)
	host, err := brokerSourceCapabilityHost(token)
	if err != nil {
		t.Fatal(err)
	}
	other, err := newBrokerSourceCapability()
	if err != nil {
		t.Fatal(err)
	}

	request := capabilityRequest(http.MethodGet, "http://"+host+":11436/v1/inference/health", sourceIP, host+":11436", other)
	request.SetPathValue("rest", "health")
	response := httptest.NewRecorder()
	broker.handle(response, request)
	if response.Code != http.StatusUnauthorized {
		t.Fatalf("mismatched bearer status=%d", response.Code)
	}

	if !broker.revokeSourceCapability(sessionID, runID, token) {
		t.Fatal("revoke capability")
	}
	request = capabilityRequest(http.MethodGet, "http://host.docker.internal:11436/v1/inference/health", sourceIP, "host.docker.internal:11436", "")
	request.SetPathValue("rest", "health")
	response = httptest.NewRecorder()
	broker.handle(response, request)
	if response.Code != http.StatusUnauthorized {
		t.Fatalf("source fallback reopened after revoke: status=%d", response.Code)
	}
	if broker.bindSource(sessionID, runID, sourceIP) {
		t.Fatal("legacy bind reopened after capability installation")
	}
}

func TestOldBrokerCapabilityCannotEnterReusedSource(t *testing.T) {
	const sourceIP = "192.0.2.82"
	broker, sessionID, runID, oldToken := protectedBrokerSession(t, sourceIP)
	oldHost, err := brokerSourceCapabilityHost(oldToken)
	if err != nil {
		t.Fatal(err)
	}
	if !broker.revokeSourceCapability(sessionID, runID, oldToken) || !broker.unbindSource(sessionID, runID, sourceIP) {
		t.Fatal("close old capability binding")
	}
	newToken, err := broker.installSourceCapability(sessionID, runID)
	if err != nil {
		t.Fatal(err)
	}
	if !broker.bindSourceCapability(sessionID, runID, sourceIP, newToken) {
		t.Fatal("bind replacement capability")
	}

	assertUnchanged := func(t *testing.T, invoke func(*httptest.ResponseRecorder)) {
		t.Helper()
		session := broker.sessions[sessionID]
		session.mu.Lock()
		before := []uint64{session.requests, session.embeddingRequests, session.upstreamAttempts}
		session.mu.Unlock()
		response := httptest.NewRecorder()
		invoke(response)
		if response.Code != http.StatusUnauthorized {
			t.Fatalf("status=%d body=%s", response.Code, response.Body.String())
		}
		session.mu.Lock()
		after := []uint64{session.requests, session.embeddingRequests, session.upstreamAttempts}
		session.mu.Unlock()
		for index := range before {
			if before[index] != after[index] {
				t.Fatalf("counter %d changed: before=%d after=%d", index, before[index], after[index])
			}
		}
	}

	assertUnchanged(t, func(response *httptest.ResponseRecorder) {
		request := capabilityRequest(http.MethodPost, "http://"+oldHost+":11436/v1/inference/chat/completions", sourceIP, oldHost+":11436", brokerPlaceholderKey)
		request.SetPathValue("rest", "chat/completions")
		broker.handle(response, request)
	})
	assertUnchanged(t, func(response *httptest.ResponseRecorder) {
		request := capabilityRequest(http.MethodPost, "http://"+oldHost+":11436/api/embed", sourceIP, oldHost+":11436", "")
		broker.handleEmbedding(response, request)
	})
	assertUnchanged(t, func(response *httptest.ResponseRecorder) {
		request := capabilityRequest(http.MethodPost, "https://openrouter.ai/api/v1/chat/completions", sourceIP, "openrouter.ai", oldToken)
		broker.handleOpenRouterShim(response, request)
	})
}

func TestResolvedBrokerCapabilityLeaseCannotEnterAfterRevocation(t *testing.T) {
	const sourceIP = "192.0.2.83"
	broker, sessionID, runID, token := protectedBrokerSession(t, sourceIP)
	lease := broker.sessionLeaseForCapability(sourceIP, token)
	if lease.session == nil {
		t.Fatal("resolve capability lease")
	}
	if !broker.revokeSourceCapability(sessionID, runID, token) {
		t.Fatal("revoke capability")
	}
	session := broker.sessions[sessionID]

	for name, invoke := range map[string]func(*httptest.ResponseRecorder){
		"chat": func(response *httptest.ResponseRecorder) {
			request := capabilityRequest(http.MethodPost, "http://broker/v1/inference/chat/completions", sourceIP, "broker", "")
			request.Body = http.NoBody
			broker.handleChat(response, request, lease, "")
		},
		"embedding": func(response *httptest.ResponseRecorder) {
			request := capabilityRequest(http.MethodPost, "http://broker/api/embed", sourceIP, "broker", "")
			broker.handleEmbeddingWithLease(response, request, lease)
		},
	} {
		t.Run(name, func(t *testing.T) {
			session.mu.Lock()
			before := []uint64{session.requests, session.embeddingRequests, session.upstreamAttempts}
			session.mu.Unlock()
			response := httptest.NewRecorder()
			invoke(response)
			if response.Code != http.StatusUnauthorized {
				t.Fatalf("status=%d body=%s", response.Code, response.Body.String())
			}
			session.mu.Lock()
			after := []uint64{session.requests, session.embeddingRequests, session.upstreamAttempts}
			session.mu.Unlock()
			for index := range before {
				if before[index] != after[index] {
					t.Fatalf("counter %d changed: before=%d after=%d", index, before[index], after[index])
				}
			}
		})
	}
}

func TestOrdinaryCompatibilityReplacementRotatesCapabilityAtReusedSource(t *testing.T) {
	const sourceIP = "192.0.2.86"
	broker, sessionID, runID, oldToken := protectedBrokerSession(t, sourceIP)
	oldLease := broker.sessionLeaseForCapability(sourceIP, oldToken)
	if oldLease.session == nil {
		t.Fatal("resolve ordinary capability lease")
	}
	oldEnv, err := harnessSandboxEnvWithCapability(
		nil, protocol.BenchVersionV8, platformLockedProvider, sessionID, oldToken,
	)
	if err != nil {
		t.Fatal(err)
	}

	newToken, retiredEpoch, err := broker.rotateSourceCapability(sessionID, runID, oldToken)
	if err != nil {
		t.Fatal(err)
	}
	if oldToken == newToken || !canonicalBrokerSourceCapability(newToken) {
		t.Fatalf("replacement capability old=%q new=%q", oldToken, newToken)
	}
	if retiredEpoch == 0 {
		t.Fatal("replacement did not identify retired source epoch")
	}
	newEnv, err := harnessSandboxEnvWithCapability(
		nil, protocol.BenchVersionV8, v8CompatLockedProvider, sessionID, newToken,
	)
	if err != nil {
		t.Fatal(err)
	}
	if oldEnv["OLLAMA_BASE_URL"] == newEnv["OLLAMA_BASE_URL"] ||
		oldEnv["OPENROUTER_API_KEY"] == newEnv["OPENROUTER_API_KEY"] {
		t.Fatal("replacement sandbox reused broker route capability")
	}
	if !broker.replaceBoundSourceCapability(sessionID, runID, sourceIP, sourceIP, newToken) {
		t.Fatal("move ordinary replacement to reused source")
	}

	response := httptest.NewRecorder()
	request := capabilityRequest(http.MethodPost, "http://broker/api/embed", sourceIP, "broker", "")
	broker.handleEmbeddingWithLease(response, request, oldLease)
	if response.Code != http.StatusUnauthorized {
		t.Fatalf("old pre-rotation lease status=%d body=%s", response.Code, response.Body.String())
	}
	newLease := broker.sessionLeaseForCapability(sourceIP, newToken)
	if newLease.session == nil {
		t.Fatal("replacement capability did not resolve")
	}
	if !broker.revokeSourceCapability(sessionID, runID, newToken) {
		t.Fatal("revoke replacement capability")
	}
	if broker.sessionLeaseForSource(sourceIP).session != nil {
		t.Fatal("replacement cleanup reopened source-only admission")
	}
}

func TestCompatibilityReplacementWaitsForRetiredEpochHandlers(t *testing.T) {
	const sourceIP = "192.0.2.87"
	broker, sessionID, runID, oldToken := protectedBrokerSession(t, sourceIP)
	lease := broker.sessionLeaseForCapability(sourceIP, oldToken)
	reader, writer := io.Pipe()
	request := capabilityRequest(http.MethodPost, "http://broker/v1/inference/chat/completions", sourceIP, "broker", "")
	request.Body = reader
	response := httptest.NewRecorder()
	handlerDone := make(chan struct{})
	go func() {
		broker.handleChat(response, request, lease, "")
		close(handlerDone)
	}()
	session := broker.sessions[sessionID]
	deadline := time.Now().Add(time.Second)
	for {
		session.mu.Lock()
		active := session.sourceActiveHandlers[lease.epoch]
		session.mu.Unlock()
		if active == 1 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("broker handler did not enter retired epoch")
		}
		time.Sleep(time.Millisecond)
	}

	newToken, retiredEpoch, err := broker.rotateSourceCapability(sessionID, runID, oldToken)
	if err != nil || retiredEpoch != lease.epoch {
		t.Fatalf("rotate token=%q epoch=%d err=%v", newToken, retiredEpoch, err)
	}
	blockedCtx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	err = broker.waitSourceEpochDrained(blockedCtx, sessionID, retiredEpoch)
	cancel()
	if err == nil {
		t.Fatal("retired epoch drained while admitted handler was blocked")
	}
	_ = writer.CloseWithError(errors.New("retired container removed"))
	select {
	case <-handlerDone:
	case <-time.After(time.Second):
		t.Fatal("retired handler did not return")
	}
	drainedCtx, cancel := context.WithTimeout(context.Background(), time.Second)
	err = broker.waitSourceEpochDrained(drainedCtx, sessionID, retiredEpoch)
	cancel()
	if err != nil {
		t.Fatal(err)
	}
}

func TestOldAndMissingCapabilitiesCannotCrossSessionsAtReusedSource(t *testing.T) {
	const sourceIP = "192.0.2.84"
	broker, oldID, oldRunID, oldToken := protectedBrokerSession(t, sourceIP)
	if !broker.revokeSourceCapability(oldID, oldRunID, oldToken) || !broker.unbindSource(oldID, oldRunID, sourceIP) {
		t.Fatal("close old protected source")
	}
	broker.removeRun(oldID, oldRunID)

	newID, newRunID := "ordinary-replacement-session", uuid.NewString()
	broker.sessions[newID] = &brokerSession{
		id: newID, boundRunID: newRunID, bearer: "new-platform-ticket",
		expiresAt: time.Now().Add(time.Hour), legacyGateway: "active",
	}
	newToken, err := broker.installSourceCapability(newID, newRunID)
	if err != nil || !broker.bindSourceCapability(newID, newRunID, sourceIP, newToken) {
		t.Fatalf("bind new protected source: %v", err)
	}
	oldHost, _ := brokerSourceCapabilityHost(oldToken)
	for name, request := range map[string]*http.Request{
		"old token":     capabilityRequest(http.MethodPost, "http://"+oldHost+":11436/api/embed", sourceIP, oldHost, ""),
		"missing token": capabilityRequest(http.MethodPost, "http://host.docker.internal:11436/api/embed", sourceIP, "host.docker.internal", ""),
	} {
		t.Run(name, func(t *testing.T) {
			response := httptest.NewRecorder()
			broker.handleEmbedding(response, request)
			if response.Code != http.StatusUnauthorized {
				t.Fatalf("status=%d body=%s", response.Code, response.Body.String())
			}
		})
	}
}

func TestBrokerCapabilityIsNeverForwardedAsProviderBearer(t *testing.T) {
	var upstreamAuthorization string
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		upstreamAuthorization = request.Header.Get("Authorization")
		response.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(response, `{"usage":{"prompt_tokens":1,"completion_tokens":1},"choices":[{"message":{"content":"ok"}}]}`)
	}))
	defer upstream.Close()

	broker := newInferenceBroker(1)
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSessionFor(t, broker, prepared, proxyURL, "openrouter", llm.OpenRouterRelayProfileRevision, llm.LockedHarnessModel)
	runID := uuid.NewString()
	if !broker.claimRun(prepared["session_id"], runID, brokerTicketIdentity{}, protocol.BenchVersionV6) {
		t.Fatal("claim run")
	}
	capability, err := broker.installSourceCapability(prepared["session_id"], runID)
	if err != nil || !broker.bindSourceCapability(prepared["session_id"], runID, "192.0.2.85", capability) {
		t.Fatalf("bind capability: %v", err)
	}
	host, _ := brokerSourceCapabilityHost(capability)
	request := capabilityRequest(
		http.MethodPost, "http://"+host+":11436/v1/inference/chat/completions", "192.0.2.85", host, capability,
	)
	request.SetPathValue("rest", "chat/completions")
	request.Body = io.NopCloser(strings.NewReader(`{"model":"ignored","messages":[{"role":"user","content":"hello"}],"max_tokens":8}`))
	response := httptest.NewRecorder()
	broker.handle(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", response.Code, response.Body.String())
	}
	if upstreamAuthorization != "Bearer platform-bearer-never-given-to-harness" {
		t.Fatalf("upstream bearer=%q", upstreamAuthorization)
	}
	if strings.Contains(upstreamAuthorization, capability) {
		t.Fatal("broker capability reached provider Authorization")
	}
}
