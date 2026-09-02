package main

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/llm"
	"github.com/ditto-assistant/dittobench-api/internal/longmemeval"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/google/uuid"
)

const testBrokerAgentID = "00000000-0000-0000-0000-000000000002"

type brokerTestAuthorizer struct{}

func (brokerTestAuthorizer) Authorize(context.Context, string, *http.Request) error { return nil }

func newBrokerTestReaderSession(t *testing.T, upstreamStatus int) (*longmemeval.ProviderSession, string) {
	t.Helper()
	profile, _ := validInstalledConfirmationProfile(t)
	for index := range profile.ProviderLanes {
		profile.ProviderLanes[index].Provider = "openrouter"
		profile.ProviderLanes[index].MaxPromptTokens = 10_000
		profile.ProviderLanes[index].MaxCompletionTokens = 1_000
		profile.ProviderLanes[index].MaxTotalTokens = 11_000
		profile.ProviderLanes[index].MaxCostUSDmicros = 100_000
		if profile.ProviderLanes[index].Lane == longmemeval.ReaderLane {
			profile.ProviderLanes[index].RouteProvider = "deepinfra"
			profile.ProviderLanes[index].ReceiptProvider = "DeepInfra"
			profile.ProviderLanes[index].ProfileRevision = "fixture-reader-v1"
			profile.ProviderLanes[index].Model = "openai/gpt-oss-20b"
		} else {
			profile.ProviderLanes[index].RouteProvider = "azure"
			profile.ProviderLanes[index].ReceiptProvider = "Azure"
			profile.ProviderLanes[index].ProfileRevision = "longmemeval-official-gpt4o-azure-zdr-v2"
			profile.ProviderLanes[index].Model = "openai/gpt-4o-2024-08-06"
		}
	}
	readerModel := ""
	readerReceiptProvider := ""
	upstream := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		writer.Header().Set("Content-Type", "application/json")
		writer.WriteHeader(upstreamStatus)
		_, _ = io.WriteString(writer, `{"id":"reader-receipt","model":`+strconv.Quote(readerModel)+`,"provider":`+strconv.Quote(readerReceiptProvider)+`,"usage":{"prompt_tokens":1,"completion_tokens":0,"total_tokens":1,"cost":0.0000001},"error":{"message":"upstream rejected"}}`)
	}))
	t.Cleanup(upstream.Close)
	config := longmemeval.ProviderRuntimeConfig{
		HTTPClient: upstream.Client(), Authorizer: brokerTestAuthorizer{},
	}
	for _, lane := range profile.ProviderLanes {
		if lane.Lane == longmemeval.ReaderLane {
			readerModel = lane.Model
			readerReceiptProvider = lane.ReceiptProvider
		}
		config.Lanes = append(config.Lanes, longmemeval.ProviderLaneRuntimeConfig{
			Lane: lane.Lane, UpstreamURL: upstream.URL + "/v1/chat/completions",
			RouteProvider: lane.RouteProvider, ReceiptProvider: lane.ReceiptProvider, RequestTimeout: time.Second,
		})
	}
	session, err := longmemeval.NewProviderSession(context.Background(), profile.longMemProfile(), config)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = session.Close() })
	return session, readerModel
}

type brokerRemoteConn struct {
	net.Conn
	remote net.Addr
}

func (connection brokerRemoteConn) RemoteAddr() net.Addr { return connection.remote }

func TestConfirmationCaseSnapshotParksFailedEmbeddingInActiveGeneration(t *testing.T) {
	attempts := 0
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		attempts++
		if attempts == 1 {
			http.Error(w, "transient", http.StatusInternalServerError)
			return
		}
		response := platformEmbeddingResponse{Model: hostedEmbeddingModel}
		response.Data = append(response.Data, struct {
			Index     int       `json:"index"`
			Embedding []float64 `json:"embedding"`
		}{Index: 0, Embedding: make([]float64, embeddingDimensions)})
		response.Usage.PromptTokens, response.Usage.TotalTokens = 1, 1
		_ = json.NewEncoder(w).Encode(response)
	}))
	defer upstream.Close()

	broker := newInferenceBroker(1, 1)
	broker.sleep = func(context.Context, time.Duration) error { return nil }
	_, privateKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	id, runID := "confirmation-case-embedding", uuid.NewString()
	session := &brokerSession{
		id: id, boundRunID: runID, expectedSourceIP: "127.0.0.1", expiresAt: time.Now().Add(time.Hour),
		benchVersion: confirmationBenchVersion, legacyGateway: "active", privateKey: privateKey,
		confirmationSession: true,
		confirmationGrants: map[string]brokerConfirmationGrant{
			"embedding": {
				Lane: "embedding", GrantID: uuid.NewString(), Bearer: "capability", ProxyURL: upstream.URL,
				Generation: 1, Model: hostedEmbeddingModel,
			},
		},
		embeddingPhaseActive: true, embeddingConcurrency: 1,
		embeddingCalls: make(map[chan struct{}]context.CancelFunc), cancels: make(map[string]context.CancelFunc),
	}
	broker.sessions[id] = session
	generation, _, err := broker.beginCaseSnapshot(id)
	if err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(http.MethodPost, embeddingAPIPath, bytes.NewBufferString(
		`{"model":"embeddinggemma","input":["query"]}`,
	))
	request.RemoteAddr = "127.0.0.1:4321"
	response := httptest.NewRecorder()
	broker.handleEmbedding(response, request)
	if response.Code != http.StatusBadGateway {
		t.Fatalf("response=%d %s", response.Code, response.Body.String())
	}
	snapshot, err := broker.endCaseSnapshot(id, generation)
	if err != nil {
		t.Fatal(err)
	}
	if snapshot.EmbeddingAttempts != 1 || snapshot.EmbeddingDispatches != 1 || snapshot.EmbeddingDelivered != 0 ||
		snapshot.EmbeddingInFlight != 0 || snapshot.EmbeddingCancellations != 0 {
		t.Fatalf("embedding case snapshot=%+v", snapshot)
	}
	if validEmbeddingOnlyCaseActivityForTest(snapshot) {
		t.Fatalf("failed embedding was incorrectly complete: %+v", snapshot)
	}

	generation, _, err = broker.beginCaseSnapshot(id)
	if err != nil {
		t.Fatal(err)
	}
	request = httptest.NewRequest(http.MethodPost, embeddingAPIPath, bytes.NewBufferString(
		`{"model":"embeddinggemma","input":["next query"]}`,
	))
	request.RemoteAddr = "127.0.0.1:4321"
	response = httptest.NewRecorder()
	broker.handleEmbedding(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("second response=%d %s", response.Code, response.Body.String())
	}
	snapshot, err = broker.endCaseSnapshot(id, generation)
	if err != nil {
		t.Fatal(err)
	}
	if !validEmbeddingOnlyCaseActivityForTest(snapshot) {
		t.Fatalf("single delivered embedding was not complete: %+v", snapshot)
	}
}

func validEmbeddingOnlyCaseActivityForTest(snapshot brokerCaseSnapshot) bool {
	return snapshot.ReaderAttempts == 0 && snapshot.ReaderDispatches == 0 && snapshot.ReaderReceipted == 0 &&
		snapshot.ReaderInFlight == 0 && snapshot.ReaderCancellations == 0 && snapshot.EmbeddingAttempts > 0 &&
		snapshot.EmbeddingAttempts == snapshot.EmbeddingDispatches &&
		snapshot.EmbeddingAttempts == snapshot.EmbeddingDelivered && snapshot.EmbeddingInFlight == 0 &&
		snapshot.EmbeddingCancellations == 0
}

func TestConfirmationCaseSnapshotAttributesReaderCallToActiveGeneration(t *testing.T) {
	broker := newInferenceBroker(1, 1)
	id, runID := "confirmation-case-reader", uuid.NewString()
	model := llm.HarnessModelForVersion(confirmationBenchVersion)
	session := &brokerSession{
		id: id, boundRunID: runID, expectedSourceIP: "127.0.0.1", expiresAt: time.Now().Add(time.Hour),
		benchVersion: confirmationBenchVersion, confirmationSession: true, requestModel: model, model: model,
		trustedChatHandler: http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			_, _ = io.WriteString(w, `{"choices":[{"message":{"content":"OK"}}],"usage":{"prompt_tokens":1,"completion_tokens":1}}`)
		}),
		cancels: make(map[string]context.CancelFunc),
	}
	broker.sessions[id] = session
	generation, _, err := broker.beginCaseSnapshot(id)
	if err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", bytes.NewBufferString(
		`{"model":"ignored","messages":[{"role":"user","content":"query"}]}`,
	))
	request.RemoteAddr = "127.0.0.1:4321"
	response := httptest.NewRecorder()
	broker.handleChat(response, request, brokerSourceLease{
		session: session, sourceIP: session.expectedSourceIP, epoch: session.sourceEpoch,
	}, "")
	if response.Code != http.StatusOK {
		t.Fatalf("response=%d %s", response.Code, response.Body.String())
	}
	snapshot, err := broker.endCaseSnapshot(id, generation)
	if err != nil {
		t.Fatal(err)
	}
	if snapshot.ReaderAttempts != 1 || snapshot.ReaderDispatches != 1 || snapshot.ReaderReceipted != 1 ||
		snapshot.ReaderInFlight != 0 || snapshot.ReaderCancellations != 0 || snapshot.EmbeddingAttempts != 0 {
		t.Fatalf("reader case snapshot=%+v", snapshot)
	}
}

func TestConfirmationCaseSnapshotAttributesAgentReaderRejection(t *testing.T) {
	broker := newInferenceBroker(1, 1)
	id, runID := "confirmation-case-reader-rejection", uuid.NewString()
	providerSession, model := newBrokerTestReaderSession(t, http.StatusInternalServerError)
	session := &brokerSession{
		id: id, boundRunID: runID, expectedSourceIP: "127.0.0.1", expiresAt: time.Now().Add(time.Hour),
		benchVersion: confirmationBenchVersion, confirmationSession: true, requestModel: model, model: model,
		trustedChatHandler: providerSession.ReaderHandler(),
		cancels:            make(map[string]context.CancelFunc),
	}
	broker.sessions[id] = session
	generation, _, err := broker.beginCaseSnapshot(id)
	if err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", bytes.NewBufferString(
		`{"model":"ignored","max_tokens":1001,"messages":[{"role":"user","content":"query"}]}`,
	))
	request.RemoteAddr = "127.0.0.1:4321"
	response := httptest.NewRecorder()
	broker.handleChat(response, request, brokerSourceLease{
		session: session, sourceIP: session.expectedSourceIP, epoch: session.sourceEpoch,
	}, "")
	if response.Code != http.StatusBadRequest {
		t.Fatalf("response=%d %s", response.Code, response.Body.String())
	}
	snapshot, err := broker.endCaseSnapshot(id, generation)
	if err != nil {
		t.Fatal(err)
	}
	if snapshot.ReaderAttempts != 1 || snapshot.ReaderDispatches != 1 || snapshot.ReaderReceipted != 0 ||
		snapshot.ReaderAgentRejections != 1 || snapshot.ReaderInFlight != 0 ||
		snapshot.ReaderCancellations != 0 {
		t.Fatalf("reader rejection snapshot=%+v", snapshot)
	}
}

func TestConfirmationCaseSnapshotRejectsUntrustedReaderFailureStatuses(t *testing.T) {
	statuses := []int{
		http.StatusBadRequest,
		http.StatusRequestEntityTooLarge,
		http.StatusUnauthorized,
		http.StatusNotFound,
		http.StatusRequestTimeout,
		http.StatusConflict,
		http.StatusGone,
		http.StatusUnprocessableEntity,
		http.StatusTooManyRequests,
		http.StatusInternalServerError,
	}
	for _, status := range statuses {
		t.Run(strconv.Itoa(status), func(t *testing.T) {
			broker := newInferenceBroker(1, 1)
			id, runID := "confirmation-case-reader-failure-"+strconv.Itoa(status), uuid.NewString()
			model := llm.HarnessModelForVersion(confirmationBenchVersion)
			session := &brokerSession{
				id: id, boundRunID: runID, expectedSourceIP: "127.0.0.1", expiresAt: time.Now().Add(time.Hour),
				benchVersion: confirmationBenchVersion, confirmationSession: true, requestModel: model, model: model,
				trustedChatHandler: http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
					writeError(w, status, "reader unavailable")
				}),
				cancels: make(map[string]context.CancelFunc),
			}
			broker.sessions[id] = session
			generation, _, err := broker.beginCaseSnapshot(id)
			if err != nil {
				t.Fatal(err)
			}
			request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", bytes.NewBufferString(
				`{"model":"ignored","messages":[{"role":"user","content":"query"}]}`,
			))
			request.RemoteAddr = "127.0.0.1:4321"
			response := httptest.NewRecorder()
			broker.handleChat(response, request, brokerSourceLease{
				session: session, sourceIP: session.expectedSourceIP, epoch: session.sourceEpoch,
			}, "")
			snapshot, err := broker.endCaseSnapshot(id, generation)
			if err != nil {
				t.Fatal(err)
			}
			if snapshot.ReaderAttempts != 1 || snapshot.ReaderDispatches != 1 ||
				snapshot.ReaderReceipted != 0 || snapshot.ReaderAgentRejections != 0 {
				t.Fatalf("status %d was attributed as agent rejection: %+v", status, snapshot)
			}
		})
	}
}

func TestConfirmationCaseSnapshotDoesNotAttributeReceiptedUpstreamReaderFailure(t *testing.T) {
	for _, status := range []int{http.StatusBadRequest, http.StatusRequestEntityTooLarge} {
		t.Run(strconv.Itoa(status), func(t *testing.T) {
			broker := newInferenceBroker(1, 1)
			providerSession, model := newBrokerTestReaderSession(t, status)
			id := "confirmation-case-upstream-reader-" + strconv.Itoa(status)
			session := &brokerSession{
				id: id, boundRunID: uuid.NewString(), expectedSourceIP: "127.0.0.1", expiresAt: time.Now().Add(time.Hour),
				benchVersion: confirmationBenchVersion, confirmationSession: true, requestModel: model, model: model,
				trustedChatHandler: providerSession.ReaderHandler(), cancels: make(map[string]context.CancelFunc),
			}
			broker.sessions[id] = session
			generation, _, err := broker.beginCaseSnapshot(id)
			if err != nil {
				t.Fatal(err)
			}
			request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", bytes.NewBufferString(
				`{"model":"ignored","max_tokens":1,"messages":[{"role":"user","content":"query"}]}`,
			))
			request.RemoteAddr = "127.0.0.1:4321"
			broker.handleChat(httptest.NewRecorder(), request, brokerSourceLease{
				session: session, sourceIP: session.expectedSourceIP, epoch: session.sourceEpoch,
			}, "")
			snapshot, err := broker.endCaseSnapshot(id, generation)
			if err != nil {
				t.Fatal(err)
			}
			if snapshot.ReaderAttempts != 1 || snapshot.ReaderDispatches != 1 ||
				snapshot.ReaderReceipted != 0 || snapshot.ReaderAgentRejections != 0 {
				t.Fatalf("status %d was attributed as agent rejection: %+v", status, snapshot)
			}
			provider, err := providerSession.Snapshot(context.Background())
			if err != nil {
				t.Fatal(err)
			}
			for _, lane := range provider {
				if lane.Lane == longmemeval.ReaderLane &&
					(lane.Requests != 1 || lane.Successes != 0 || lane.ReceiptedRequests != 1) {
					t.Fatalf("status %d reader accounting=%+v", status, lane)
				}
			}
		})
	}
}

func TestConfirmationSourceLeaseRejectsZeroDrainingAndStaleEpochBeforeAttribution(t *testing.T) {
	broker := newInferenceBroker(1, 1)
	id, runID, source := "confirmation-source-epoch", uuid.NewString(), "192.0.2.90"
	var upstreamCalls atomic.Int64
	session := &brokerSession{
		id: id, boundRunID: runID, expectedSourceIP: source, sourceEpoch: 1,
		expiresAt: time.Now().Add(time.Hour), benchVersion: confirmationBenchVersion,
		confirmationSession: true, requestModel: llm.HarnessModelForVersion(confirmationBenchVersion),
		model: llm.HarnessModelForVersion(confirmationBenchVersion),
		trustedChatHandler: http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			upstreamCalls.Add(1)
			w.Header().Set("Content-Type", "application/json")
			_, _ = io.WriteString(w, `{"choices":[{"message":{"content":"ok"}}],"usage":{"prompt_tokens":1,"completion_tokens":1}}`)
		}),
		cancels: make(map[string]context.CancelFunc),
	}
	broker.sessions[id] = session
	chat := func(lease brokerSourceLease) *httptest.ResponseRecorder {
		request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(
			`{"model":"ignored","messages":[{"role":"user","content":"query"}]}`))
		request.RemoteAddr = source + ":4321"
		response := httptest.NewRecorder()
		broker.handleChat(response, request, lease, "")
		return response
	}
	embed := func() *httptest.ResponseRecorder {
		request := httptest.NewRequest(http.MethodPost, embeddingAPIPath, strings.NewReader(
			`{"model":"embeddinggemma","input":["query"]}`))
		request.RemoteAddr = source + ":4321"
		response := httptest.NewRecorder()
		broker.handleEmbedding(response, request)
		return response
	}
	lease := brokerSourceLease{session: session, sourceIP: source, epoch: 1}
	if response := chat(lease); response.Code != http.StatusConflict {
		t.Fatalf("generation-zero response = %d", response.Code)
	}
	if response := embed(); response.Code != http.StatusConflict {
		t.Fatalf("generation-zero embedding response = %d", response.Code)
	}
	generation, _, err := broker.beginCaseSnapshot(id)
	if err != nil {
		t.Fatal(err)
	}
	stale := lease
	if !broker.unbindSource(id, runID, source) || !broker.bindSource(id, runID, source) {
		t.Fatal("same-IP source rotation failed")
	}
	if response := chat(stale); response.Code != http.StatusUnauthorized {
		t.Fatalf("stale-epoch response = %d", response.Code)
	}
	request := httptest.NewRequest(http.MethodPost, embeddingAPIPath, strings.NewReader(
		`{"model":"embeddinggemma","input":["query"]}`))
	request.RemoteAddr = source + ":4321"
	response := httptest.NewRecorder()
	broker.handleEmbeddingWithLease(response, request, stale)
	if response.Code != http.StatusUnauthorized {
		t.Fatalf("stale-epoch embedding response = %d", response.Code)
	}
	session.mu.Lock()
	currentEpoch := session.sourceEpoch
	snapshot := session.caseSnapshots[generation]
	session.mu.Unlock()
	if snapshot.ReaderAttempts != 0 || snapshot.EmbeddingAttempts != 0 || snapshot.ActiveHandlers != 0 || upstreamCalls.Load() != 0 {
		t.Fatalf("stale lease mutated attribution: %+v calls=%d", snapshot, upstreamCalls.Load())
	}
	if err := broker.beginConfirmationCaseDrain(id, generation); err != nil {
		t.Fatal(err)
	}
	current := brokerSourceLease{session: session, sourceIP: source, epoch: currentEpoch}
	if response := chat(current); response.Code != http.StatusConflict {
		t.Fatalf("draining response = %d", response.Code)
	}
	if response := embed(); response.Code != http.StatusConflict {
		t.Fatalf("draining embedding response = %d", response.Code)
	}
	session.mu.Lock()
	snapshot = session.caseSnapshots[generation]
	session.mu.Unlock()
	if snapshot.ReaderAttempts != 0 || snapshot.EmbeddingAttempts != 0 || snapshot.ActiveHandlers != 0 || upstreamCalls.Load() != 0 {
		t.Fatalf("draining request mutated attribution: %+v calls=%d", snapshot, upstreamCalls.Load())
	}
}

func TestConfirmationConnectionAcceptedBeforeRebindKeepsStaleEpoch(t *testing.T) {
	broker := newInferenceBroker(1, 1)
	id, runID, source := "confirmation-accepted-before-rebind", uuid.NewString(), "192.0.2.91"
	var upstreamCalls atomic.Int64
	model := llm.HarnessModelForVersion(confirmationBenchVersion)
	session := &brokerSession{
		id: id, boundRunID: runID, expectedSourceIP: source, sourceEpoch: 1,
		expiresAt: time.Now().Add(time.Hour), benchVersion: confirmationBenchVersion,
		confirmationSession: true, requestModel: model, model: model,
		trustedChatHandler: http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			upstreamCalls.Add(1)
			writeJSON(w, http.StatusOK, map[string]any{"choices": []any{}, "usage": map[string]int{}})
		}),
		cancels: make(map[string]context.CancelFunc),
	}
	broker.sessions[id] = session
	first, _, err := broker.beginCaseSnapshot(id)
	if err != nil {
		t.Fatal(err)
	}
	accepted := broker.connectionContext(context.Background(), brokerRemoteConn{remote: &net.TCPAddr{
		IP: net.ParseIP(source), Port: 4321,
	}})
	if err := broker.beginConfirmationCaseDrain(id, first); err != nil {
		t.Fatal(err)
	}
	if !broker.unbindSource(id, runID, source) {
		t.Fatal("failed to revoke first source epoch")
	}
	if _, err := broker.endCaseSnapshot(id, first); err != nil {
		t.Fatal(err)
	}
	second, _, err := broker.beginCaseSnapshot(id)
	if err != nil {
		t.Fatal(err)
	}
	if !broker.bindSource(id, runID, source) {
		t.Fatal("failed to reuse source IP for second case")
	}

	chat := httptest.NewRequest(http.MethodPost, "/v1/inference/chat/completions", strings.NewReader(
		`{"model":"ignored","messages":[{"role":"user","content":"late"}]}`)).WithContext(accepted)
	chat.RemoteAddr = source + ":4321"
	chat.SetPathValue("rest", "chat/completions")
	chatResponse := httptest.NewRecorder()
	broker.handle(chatResponse, chat)
	if chatResponse.Code != http.StatusUnauthorized {
		t.Fatalf("queued old chat status=%d", chatResponse.Code)
	}

	embed := httptest.NewRequest(http.MethodPost, embeddingAPIPath, strings.NewReader(
		`{"model":"embeddinggemma","input":["late"]}`)).WithContext(accepted)
	embed.RemoteAddr = source + ":4321"
	embedResponse := httptest.NewRecorder()
	broker.handleEmbedding(embedResponse, embed)
	if embedResponse.Code != http.StatusUnauthorized {
		t.Fatalf("queued old embedding status=%d", embedResponse.Code)
	}
	snapshot, err := broker.generationCaseSnapshot(id, second)
	if err != nil {
		t.Fatal(err)
	}
	if snapshot.ReaderAttempts != 0 || snapshot.EmbeddingAttempts != 0 || snapshot.ActiveHandlers != 0 || upstreamCalls.Load() != 0 {
		t.Fatalf("queued old connection contaminated second case: snapshot=%+v upstream=%d", snapshot, upstreamCalls.Load())
	}
}

func TestConfirmationCaseDrainWaitsForEveryActiveHandler(t *testing.T) {
	broker := newInferenceBroker(1, 1)
	id := "confirmation-handler-drain"
	session := &brokerSession{
		confirmationSession: true, boundRunID: uuid.NewString(), expiresAt: time.Now().Add(time.Hour),
		trustedChatHandler: http.NotFoundHandler(), caseSnapshots: make(map[uint64]brokerCaseSnapshot),
	}
	broker.sessions[id] = session
	generation, _, err := broker.beginCaseSnapshot(id)
	if err != nil {
		t.Fatal(err)
	}
	if err := broker.beginConfirmationCaseDrain(id, generation); err != nil {
		t.Fatal(err)
	}
	session.mu.Lock()
	snapshot := session.caseSnapshots[generation]
	snapshot.ActiveHandlers = 1
	session.caseSnapshots[generation] = snapshot
	session.mu.Unlock()
	ctx, cancel := context.WithTimeout(context.Background(), 25*time.Millisecond)
	defer cancel()
	if _, err := broker.waitConfirmationCaseDrained(ctx, id, generation); err == nil {
		t.Fatal("active handler drain unexpectedly succeeded")
	}
	session.mu.Lock()
	snapshot = session.caseSnapshots[generation]
	snapshot.ActiveHandlers = 0
	session.caseSnapshots[generation] = snapshot
	session.mu.Unlock()
	ctx, cancel = context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if _, err := broker.waitConfirmationCaseDrained(ctx, id, generation); err != nil {
		t.Fatal(err)
	}
}

func TestLegacyBrokerSessionsRunConcurrentlyWithIsolatedAccounting(t *testing.T) {
	var inFlight atomic.Int64
	var peak atomic.Int64
	arrived := make(chan struct{}, 2)
	release := make(chan struct{})
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "" {
			t.Error("legacy broker must not send a provider bearer to model-relay")
		}
		current := inFlight.Add(1)
		defer inFlight.Add(-1)
		for {
			previous := peak.Load()
			if current <= previous || peak.CompareAndSwap(previous, current) {
				break
			}
		}
		arrived <- struct{}{}
		<-release
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"usage":{"prompt_tokens":3,"completion_tokens":4},"choices":[{"message":{"content":"OK"}}]}`))
	}))
	defer upstream.Close()

	broker := newInferenceBroker(2)
	ids := make([]string, 2)
	runIDs := []string{uuid.NewString(), uuid.NewString()}
	for i, ip := range []string{"192.0.2.40", "192.0.2.41"} {
		id, err := broker.prepareLegacy(runIDs[i], protocol.BenchVersionV6, upstream.URL, relayHealthSnapshot{
			Provider: "openrouter", ProfileRevision: llm.OpenRouterRelayProfileRevision,
			Model: llm.LockedHarnessModel,
		})
		if err != nil {
			t.Fatal(err)
		}
		ids[i] = id
		if !broker.bindSource(id, runIDs[i], ip) {
			t.Fatalf("failed to bind legacy session %d", i)
		}
	}

	var wg sync.WaitGroup
	recorders := make([]*httptest.ResponseRecorder, 2)
	for i, ip := range []string{"192.0.2.40", "192.0.2.41"} {
		wg.Add(1)
		go func(i int, ip string) {
			defer wg.Done()
			// Match the frozen starter-kit adapter exactly: CHUTES_BASE_URL is
			// the broker base and the client appends /chat/completions.
			request := httptest.NewRequest(http.MethodPost, "/v1/inference/chat/completions", bytes.NewBufferString(`{"model":"qwen/qwen3-32b"}`))
			request.RemoteAddr = ip + ":4321"
			request.SetPathValue("rest", "chat/completions")
			recorders[i] = httptest.NewRecorder()
			broker.handle(recorders[i], request)
		}(i, ip)
	}
	for range 2 {
		select {
		case <-arrived:
		case <-time.After(time.Second):
			close(release)
			t.Fatal("legacy requests did not overlap")
		}
	}
	close(release)
	wg.Wait()

	if peak.Load() != 2 {
		t.Fatalf("peak legacy relay concurrency = %d, want 2", peak.Load())
	}
	for i, id := range ids {
		if recorders[i].Code != http.StatusOK {
			t.Fatalf("legacy response %d = %d: %s", i, recorders[i].Code, recorders[i].Body.String())
		}
		snapshot, err := broker.snapshot(id)
		if err != nil {
			t.Fatal(err)
		}
		if snapshot.Provider != "openrouter" || snapshot.ProfileRevision != llm.OpenRouterRelayProfileRevision ||
			snapshot.Model != llm.LockedHarnessModel || snapshot.Requests != 1 || snapshot.Successes != 1 ||
			snapshot.UsageAvailable != 1 {
			t.Fatalf("legacy session %d accounting = %+v", i, snapshot)
		}
		if snapshot.PromptTokens != 3 || snapshot.CompletionTokens != 4 {
			t.Fatalf("legacy session %d token accounting = %+v", i, snapshot)
		}
	}
}

func TestBrokerSettledCaseSnapshotWaitsForAdmittedRequest(t *testing.T) {
	broker := newInferenceBroker(1)
	id := uuid.NewString()
	broker.mu.Lock()
	broker.sessions[id] = &brokerSession{inFlight: 1, requests: 7, successes: 6}
	broker.mu.Unlock()

	released := make(chan struct{})
	go func() {
		time.Sleep(20 * time.Millisecond)
		broker.mu.RLock()
		session := broker.sessions[id]
		broker.mu.RUnlock()
		session.mu.Lock()
		session.inFlight--
		session.successes++
		session.mu.Unlock()
		close(released)
	}()

	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	snapshot, err := broker.settledCaseSnapshot(ctx, id)
	if err != nil {
		t.Fatal(err)
	}
	<-released
	if snapshot != (brokerCaseSnapshot{Requests: 7, Successes: 7}) {
		t.Fatalf("settled snapshot = %+v", snapshot)
	}
}

func TestBrokerSettledCaseSnapshotFailsClosedWhileRequestRemainsInFlight(t *testing.T) {
	broker := newInferenceBroker(1)
	id := uuid.NewString()
	broker.mu.Lock()
	broker.sessions[id] = &brokerSession{inFlight: 1, requests: 4, successes: 3}
	broker.mu.Unlock()

	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	snapshot, err := broker.settledCaseSnapshot(ctx, id)
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("error = %v, want deadline exceeded", err)
	}
	if snapshot != (brokerCaseSnapshot{}) {
		t.Fatalf("timeout exposed unsettled counters: %+v", snapshot)
	}
	got, err := broker.caseSnapshot(id)
	if err != nil {
		t.Fatal(err)
	}
	if got != (brokerCaseSnapshot{Requests: 4, Successes: 3, InFlight: 1}) {
		t.Fatalf("unsettled source mutated: %+v", got)
	}
}

func TestCaseSnapshotExposesOnlySettledSourceBoundCalls(t *testing.T) {
	arrived := make(chan struct{})
	release := make(chan struct{})
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		close(arrived)
		<-release
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"usage":{"prompt_tokens":3,"completion_tokens":4},"choices":[{"message":{"content":"OK"}}]}`))
	}))
	defer upstream.Close()

	broker := newInferenceBroker(1)
	runID := uuid.NewString()
	id, err := broker.prepareLegacy(runID, protocol.BenchVersionV6, upstream.URL, relayHealthSnapshot{
		Provider: "openrouter", ProfileRevision: llm.OpenRouterRelayProfileRevision,
		Model: llm.LockedHarnessModel,
	})
	if err != nil {
		t.Fatal(err)
	}
	const sourceIP = "192.0.2.99"
	if !broker.bindSource(id, runID, sourceIP) {
		t.Fatal("failed to bind source")
	}

	done := make(chan *httptest.ResponseRecorder, 1)
	go func() {
		request := httptest.NewRequest(http.MethodPost, "/v1/inference/chat/completions", bytes.NewBufferString(`{"model":"qwen/qwen3-32b"}`))
		request.RemoteAddr = sourceIP + ":4321"
		request.SetPathValue("rest", "chat/completions")
		recorder := httptest.NewRecorder()
		broker.handle(recorder, request)
		done <- recorder
	}()
	select {
	case <-arrived:
	case <-time.After(time.Second):
		close(release)
		t.Fatal("broker call did not reach upstream")
	}
	inFlight, err := broker.caseSnapshot(id)
	if err != nil {
		close(release)
		t.Fatal(err)
	}
	if inFlight.Requests != 1 || inFlight.Successes != 0 || inFlight.InFlight != 1 {
		close(release)
		t.Fatalf("in-flight snapshot = %+v", inFlight)
	}
	close(release)
	response := <-done
	if response.Code != http.StatusOK {
		t.Fatalf("response = %d: %s", response.Code, response.Body.String())
	}
	settled, err := broker.caseSnapshot(id)
	if err != nil {
		t.Fatal(err)
	}
	if settled.Requests != 1 || settled.Successes != 1 || settled.InFlight != 0 {
		t.Fatalf("settled snapshot = %+v", settled)
	}
}

func TestCaseGenerationKeepsLateCompletionOutOfNextCase(t *testing.T) {
	arrived := make(chan struct{})
	release := make(chan struct{})
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		close(arrived)
		<-release
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"usage":{"prompt_tokens":3,"completion_tokens":4},"choices":[{"message":{"content":"OK"}}]}`))
	}))
	defer upstream.Close()

	broker := newInferenceBroker(1)
	runID := uuid.NewString()
	id, err := broker.prepareLegacy(runID, protocol.BenchVersionV6, upstream.URL, relayHealthSnapshot{
		Provider: "openrouter", ProfileRevision: llm.OpenRouterRelayProfileRevision,
		Model: llm.LockedHarnessModel,
	})
	if err != nil {
		t.Fatal(err)
	}
	const sourceIP = "192.0.2.100"
	if !broker.bindSource(id, runID, sourceIP) {
		t.Fatal("failed to bind source")
	}
	first, before, err := broker.beginCaseSnapshot(id)
	if err != nil || before != (brokerCaseSnapshot{}) {
		t.Fatalf("begin first generation = %d %+v, %v", first, before, err)
	}

	done := make(chan *httptest.ResponseRecorder, 1)
	go func() {
		request := httptest.NewRequest(http.MethodPost, "/v1/inference/chat/completions", bytes.NewBufferString(`{"model":"ignored"}`))
		request.RemoteAddr = sourceIP + ":4321"
		request.SetPathValue("rest", "chat/completions")
		recorder := httptest.NewRecorder()
		broker.handle(recorder, request)
		done <- recorder
	}()
	select {
	case <-arrived:
	case <-time.After(time.Second):
		close(release)
		t.Fatal("first-generation call did not reach upstream")
	}
	firstInFlight, err := broker.generationCaseSnapshot(id, first)
	if err != nil || firstInFlight != (brokerCaseSnapshot{Requests: 1, InFlight: 1}) {
		close(release)
		t.Fatalf("first generation in flight = %+v, %v", firstInFlight, err)
	}
	firstClosed, err := broker.endCaseSnapshot(id, first)
	if err != nil || firstClosed != firstInFlight {
		close(release)
		t.Fatalf("close first generation = %+v, %v", firstClosed, err)
	}
	second, secondBefore, err := broker.beginCaseSnapshot(id)
	if err != nil || second != first+1 || secondBefore != (brokerCaseSnapshot{}) {
		close(release)
		t.Fatalf("begin second generation = %d %+v, %v", second, secondBefore, err)
	}

	close(release)
	response := <-done
	if response.Code != http.StatusOK {
		t.Fatalf("response = %d: %s", response.Code, response.Body.String())
	}
	firstSettled, err := broker.generationCaseSnapshot(id, first)
	if err != nil || firstSettled != (brokerCaseSnapshot{Requests: 1, Successes: 1}) {
		t.Fatalf("first generation settled = %+v, %v", firstSettled, err)
	}
	secondAfter, err := broker.generationCaseSnapshot(id, second)
	if err != nil || secondAfter != (brokerCaseSnapshot{}) {
		t.Fatalf("late completion bled into second generation: %+v, %v", secondAfter, err)
	}
}

func TestCaseGenerationKeepsAdmittedUncountedTailOutOfNextCase(t *testing.T) {
	broker := newInferenceBroker(1)
	id := "admitted-uncounted-tail"
	session := &brokerSession{}
	broker.mu.Lock()
	broker.sessions[id] = session
	broker.mu.Unlock()

	first, _, err := broker.beginCaseSnapshot(id)
	if err != nil {
		t.Fatal(err)
	}
	// handleChat has admitted and generation-bound the request, but proxy has
	// not yet parsed the body or incremented Requests. This is the exact narrow
	// race seen in production when /run returned while a background call was
	// still entering the trusted broker.
	session.mu.Lock()
	snapshot := session.caseSnapshots[first]
	snapshot.InFlight++
	session.caseSnapshots[first] = snapshot
	session.inFlight++
	session.mu.Unlock()
	firstClosed, err := broker.endCaseSnapshot(id, first)
	if err != nil || firstClosed != (brokerCaseSnapshot{InFlight: 1}) {
		t.Fatalf("close first generation = %+v, %v", firstClosed, err)
	}
	second, secondBefore, err := broker.beginCaseSnapshot(id)
	if err != nil || second != first+1 || secondBefore != (brokerCaseSnapshot{}) {
		t.Fatalf("begin second generation = %d %+v, %v", second, secondBefore, err)
	}

	// The late parser/completion mutates only the closed first generation. The
	// second remains a clean zero-use window and cannot inherit the tail.
	session.mu.Lock()
	firstSnapshot := session.caseSnapshots[first]
	firstSnapshot.Requests++
	firstSnapshot.Successes++
	firstSnapshot.InFlight--
	session.caseSnapshots[first] = firstSnapshot
	session.requests++
	session.successes++
	session.inFlight--
	secondSnapshot := session.caseSnapshots[second]
	session.mu.Unlock()
	if firstSnapshot != (brokerCaseSnapshot{Requests: 1, Successes: 1}) {
		t.Fatalf("first generation = %+v", firstSnapshot)
	}
	if secondSnapshot != (brokerCaseSnapshot{}) {
		t.Fatalf("tail bled into second generation: %+v", secondSnapshot)
	}
}

func TestLegacyBrokerParksProviderFailureWithoutRetry(t *testing.T) {
	requestCount := 0
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		requestCount++
		if requestCount < 3 {
			http.Error(w, "transient", http.StatusServiceUnavailable)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"usage":{"prompt_tokens":3,"completion_tokens":4},"choices":[{"message":{"content":"OK"}}]}`))
	}))
	defer upstream.Close()

	broker := newInferenceBroker(1)
	broker.sleep = func(context.Context, time.Duration) error { return nil }
	runID := uuid.NewString()
	id, err := broker.prepareLegacy(runID, protocol.BenchVersionV6, upstream.URL, relayHealthSnapshot{
		Provider: "openrouter", ProfileRevision: llm.OpenRouterRelayProfileRevision,
		Model: llm.LockedHarnessModel,
	})
	if err != nil {
		t.Fatal(err)
	}
	if !broker.bindSource(id, runID, "192.0.2.42") {
		t.Fatal("failed to bind legacy session")
	}
	request := httptest.NewRequest(http.MethodPost, "/v1/inference/chat/completions", bytes.NewBufferString(`{"model":"qwen/qwen3-32b"}`))
	request.RemoteAddr = "192.0.2.42:4321"
	request.SetPathValue("rest", "chat/completions")
	recorder := httptest.NewRecorder()
	broker.handle(recorder, request)
	if recorder.Code != http.StatusBadGateway || requestCount != 1 {
		t.Fatalf("legacy single-shot status=%d attempts=%d body=%s", recorder.Code, requestCount, recorder.Body.String())
	}
	snapshot, err := broker.snapshot(id)
	if err != nil {
		t.Fatal(err)
	}
	if snapshot.UpstreamAttempts != 1 || snapshot.InfrastructureFailures != 1 || snapshot.Successes != 0 {
		t.Fatalf("legacy single-shot accounting = %+v", snapshot)
	}
}

func TestLegacyBrokerRejectsUnreviewedRelayIdentity(t *testing.T) {
	broker := newInferenceBroker(1)
	if _, err := broker.prepareLegacy(uuid.NewString(), protocol.BenchVersionV6, "http://127.0.0.1:11434", relayHealthSnapshot{
		Provider: "openrouter", ProfileRevision: "mutable-route", Model: llm.LockedHarnessModel,
	}); err == nil {
		t.Fatal("unreviewed legacy relay identity was accepted")
	}
}

func embeddingBrokerSession(t *testing.T, broker *inferenceBroker, source string) (string, string) {
	t.Helper()
	runID := uuid.NewString()
	id, err := broker.prepareLegacy(runID, protocol.BenchVersionV6, "http://127.0.0.1:11435", relayHealthSnapshot{
		Provider: "openrouter", ProfileRevision: llm.OpenRouterRelayProfileRevision,
		Model: llm.LockedHarnessModel,
	})
	if err != nil {
		t.Fatal(err)
	}
	if !broker.bindSource(id, runID, source) {
		t.Fatal("failed to bind embedding broker source")
	}
	return id, runID
}

func TestBrokerReplacesBoundSourceOnceByCompareAndSwap(t *testing.T) {
	broker := newInferenceBroker(1)
	id, runID := embeddingBrokerSession(t, broker, "192.0.2.50")

	if !broker.replaceBoundSource(id, runID, "192.0.2.50", "192.0.2.51") {
		t.Fatal("same-run compatibility source replacement was rejected")
	}
	if broker.replaceBoundSource(id, runID, "192.0.2.50", "192.0.2.52") {
		t.Fatal("stale source replaced the current binding")
	}
	if broker.replaceBoundSource(id, uuid.NewString(), "192.0.2.51", "192.0.2.52") {
		t.Fatal("another run replaced the source binding")
	}
	if broker.replaceBoundSource(id, runID, "192.0.2.51", "not-an-ip") {
		t.Fatal("invalid replacement source was accepted")
	}

	broker.mu.RLock()
	session := broker.sessions[id]
	broker.mu.RUnlock()
	session.mu.Lock()
	got := session.expectedSourceIP
	session.mu.Unlock()
	if got != "192.0.2.51" {
		t.Fatalf("bound source = %q, want replacement", got)
	}
}

func admittedEmbeddingBrokerSession(t *testing.T, broker *inferenceBroker, source string) (string, string) {
	t.Helper()
	id, runID := embeddingBrokerSession(t, broker, source)
	if !broker.beginEmbeddingPhase(id, runID) {
		t.Fatal("failed to admit embedding phase")
	}
	t.Cleanup(func() { broker.endEmbeddingPhase(id, runID) })
	return id, runID
}

func writeTestEmbedding(w http.ResponseWriter, inputs int) {
	vector := make([]float64, embeddingDimensions)
	vectors := make([][]float64, inputs)
	for index := range vectors {
		vectors[index] = vector
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"embeddings":        vectors,
		"prompt_eval_count": inputs,
	})
}

func callEmbedding(broker *inferenceBroker, source, input string) *httptest.ResponseRecorder {
	return callEmbeddingAs(broker, source, embeddingModel, input)
}

// callEmbeddingAs is callEmbedding with the harness's `model` string under the
// test's control. The literal `embeddinggemma` above is the string every
// shipped harness sends; this variant exists to exercise the ones that do not.
func callEmbeddingAs(broker *inferenceBroker, source, model, input string) *httptest.ResponseRecorder {
	request := httptest.NewRequest(http.MethodPost, embeddingAPIPath, bytes.NewBufferString(
		`{"model":`+strconv.Quote(model)+`,"input":[`+strconv.Quote(input)+`]}`,
	))
	request.RemoteAddr = source + ":4321"
	recorder := httptest.NewRecorder()
	broker.handleEmbedding(recorder, request)
	return recorder
}

func TestEmbeddingBrokerCapacityOneForwardsOnlyLockedOperation(t *testing.T) {
	vector := make([]float64, embeddingDimensions)
	for index := range vector {
		vector[index] = float64(index) / embeddingDimensions
	}
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != embeddingAPIPath {
			t.Fatalf("unexpected embedding upstream route %s %s", r.Method, r.URL.Path)
		}
		var request map[string]any
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
			t.Fatal(err)
		}
		if len(request) != 2 || request["model"] != embeddingModel {
			t.Fatalf("unlocked embedding request: %#v", request)
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"model":             embeddingModel,
			"embeddings":        [][]float64{vector},
			"prompt_eval_count": 3,
		})
	}))
	defer upstream.Close()

	broker := newInferenceBroker(1)
	broker.embeddingURL = upstream.URL + embeddingAPIPath
	broker.client.Transport = upstream.Client().Transport
	admittedEmbeddingBrokerSession(t, broker, "192.0.2.60")
	request := httptest.NewRequest(http.MethodPost, embeddingAPIPath, bytes.NewBufferString(
		`{"model":"embeddinggemma","input":["bounded text"]}`,
	))
	request.RemoteAddr = "192.0.2.60:4321"
	recorder := httptest.NewRecorder()
	broker.handleEmbedding(recorder, request)
	if recorder.Code != http.StatusOK {
		t.Fatalf("embedding status=%d body=%s", recorder.Code, recorder.Body.String())
	}
	var response map[string]json.RawMessage
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if len(response) != 2 || response["model"] != nil {
		t.Fatalf("embedding response leaked upstream metadata: %s", recorder.Body.String())
	}
}

func TestV7EmbeddingBrokerUsesSignedLockedPlatformRoute(t *testing.T) {
	vector := make([]float64, embeddingDimensions)
	var calls atomic.Int64
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.Method != http.MethodPost || r.URL.Path != platformEmbeddingAPIPath {
			t.Fatalf("unexpected hosted embedding route %s %s", r.Method, r.URL.Path)
		}
		for _, header := range []string{"Authorization", "X-Ditto-Grant", "X-Ditto-Generation", "X-Ditto-Nonce", "X-Ditto-Requested-At", "X-Ditto-Proof"} {
			if r.Header.Get(header) == "" {
				t.Fatalf("missing signed embedding header %s", header)
			}
		}
		var payload map[string]any
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Fatal(err)
		}
		if len(payload) != 4 || payload["model"] != hostedEmbeddingModel || payload["dimensions"] != float64(embeddingDimensions) || payload["encoding_format"] != "float" {
			t.Fatalf("unlocked hosted embedding payload: %#v", payload)
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"object": "list", "model": hostedEmbeddingModel,
			"data":  []map[string]any{{"object": "embedding", "index": 0, "embedding": vector}},
			"usage": map[string]int{"prompt_tokens": 4, "total_tokens": 4},
		})
	}))
	defer upstream.Close()

	broker := newInferenceBroker(1)
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSessionFor(t, broker, prepared, proxyURL, "openrouter", "openrouter-route-0123456789abcdef-v1", llm.V7HarnessModel)
	runID := claimAndBindBrokerSession(t, broker, prepared["session_id"], "192.0.2.70", protocol.BenchVersionV8)
	if !broker.beginEmbeddingPhase(prepared["session_id"], runID) {
		t.Fatal("failed to admit v7 embedding phase")
	}
	defer broker.endEmbeddingPhase(prepared["session_id"], runID)
	response := callEmbedding(broker, "192.0.2.70", "hosted text")
	if response.Code != http.StatusOK {
		t.Fatalf("hosted embedding status=%d body=%s", response.Code, response.Body.String())
	}
	if calls.Load() != 1 {
		t.Fatalf("hosted embedding calls=%d, want 1", calls.Load())
	}
}

func TestV8EmbeddingBrokerUsesSignedLockedPlatformRoute(t *testing.T) {
	vector := make([]float64, embeddingDimensions)
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != platformEmbeddingAPIPath {
			t.Fatalf("unexpected hosted embedding route %s", r.URL.Path)
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"object": "list", "model": hostedEmbeddingModel,
			"data":  []map[string]any{{"object": "embedding", "index": 0, "embedding": vector}},
			"usage": map[string]int{"prompt_tokens": 4, "total_tokens": 4},
		})
	}))
	defer upstream.Close()

	broker := newInferenceBroker(1)
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSessionFor(t, broker, prepared, proxyURL, "openrouter",
		"openrouter-route-0123456789abcdef-v1", llm.V7HarnessModel)
	runID := claimAndBindBrokerSession(t, broker, prepared["session_id"], "192.0.2.170", protocol.BenchVersionV8)
	if !broker.beginEmbeddingPhase(prepared["session_id"], runID) {
		t.Fatal("failed to admit v8 embedding phase")
	}
	defer broker.endEmbeddingPhase(prepared["session_id"], runID)
	response := callEmbedding(broker, "192.0.2.170", "hosted v8 text")
	if response.Code != http.StatusOK {
		t.Fatalf("hosted v8 embedding status=%d body=%s", response.Code, response.Body.String())
	}
}

func TestV7EmbeddingBrokerCountsPlatformFailureAsInfrastructure(t *testing.T) {
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, "unavailable", http.StatusServiceUnavailable)
	}))
	defer upstream.Close()

	broker := newInferenceBroker(1)
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSessionFor(t, broker, prepared, proxyURL, "openrouter", "openrouter-route-0123456789abcdef-v1", llm.V7HarnessModel)
	runID := claimAndBindBrokerSession(t, broker, prepared["session_id"], "192.0.2.71", protocol.BenchVersionV8)
	if !broker.beginEmbeddingPhase(prepared["session_id"], runID) {
		t.Fatal("failed to admit v7 embedding phase")
	}
	defer broker.endEmbeddingPhase(prepared["session_id"], runID)
	start, err := broker.snapshot(prepared["session_id"])
	if err != nil {
		t.Fatal(err)
	}

	response := callEmbedding(broker, "192.0.2.71", "hosted text")
	if response.Code != http.StatusBadGateway {
		t.Fatalf("hosted embedding status=%d body=%s", response.Code, response.Body.String())
	}
	snapshot, err := broker.snapshot(prepared["session_id"])
	if err != nil {
		t.Fatal(err)
	}
	if snapshot.InfrastructureFailures != 1 {
		t.Fatalf("hosted embedding failure accounting = %+v", snapshot)
	}
	if err := relayDegradedSince(start, snapshot); err == nil {
		t.Fatal("hosted embedding infrastructure failure did not fail scoring closed")
	}
}

func TestEmbeddingBrokerRejectsMalformedPayloadsAndManagementProbes(t *testing.T) {
	var upstreamCalls atomic.Int64
	upstream := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		upstreamCalls.Add(1)
	}))
	defer upstream.Close()
	broker := newInferenceBroker(1)
	broker.embeddingURL = upstream.URL + embeddingAPIPath
	broker.client.Transport = upstream.Client().Transport
	admittedEmbeddingBrokerSession(t, broker, "192.0.2.61")

	tests := []struct {
		method string
		path   string
		body   string
		ip     string
		status int
	}{
		// The model name is no longer an allowlist (see
		// TestEmbeddingBrokerSubstitutesAnyRequestedModel), but every other
		// field is still validated exactly as strictly as before.
		{http.MethodPost, embeddingAPIPath, `{"model":"embeddinggemma","input":["x"],"keep_alive":"24h"}`, "192.0.2.61", http.StatusBadRequest},
		{http.MethodPost, embeddingAPIPath, `{"model":"other","input":["x"],"keep_alive":"24h"}`, "192.0.2.61", http.StatusBadRequest},
		{http.MethodPost, embeddingAPIPath, `{"model":"other","input":[]}`, "192.0.2.61", http.StatusBadRequest},
		{http.MethodPost, embeddingAPIPath, `{"model":"other","input":["x"]} {"model":"other","input":["y"]}`, "192.0.2.61", http.StatusBadRequest},
		{http.MethodPost, embeddingAPIPath, `not json`, "192.0.2.61", http.StatusBadRequest},
		{http.MethodPost, "/api/pull", `{"model":"embeddinggemma"}`, "192.0.2.61", http.StatusNotFound},
		{http.MethodGet, embeddingAPIPath, "", "192.0.2.61", http.StatusNotFound},
		{http.MethodPost, embeddingAPIPath, `{"model":"embeddinggemma","input":["x"]}`, "192.0.2.62", http.StatusUnauthorized},
	}
	for _, test := range tests {
		request := httptest.NewRequest(test.method, test.path, bytes.NewBufferString(test.body))
		request.RemoteAddr = test.ip + ":4321"
		recorder := httptest.NewRecorder()
		broker.handleEmbedding(recorder, request)
		if recorder.Code != test.status {
			t.Fatalf("%s %s status=%d want=%d body=%s", test.method, test.path, recorder.Code, test.status, recorder.Body.String())
		}
	}
	if upstreamCalls.Load() != 0 {
		t.Fatalf("rejected embedding probes reached upstream %d time(s)", upstreamCalls.Load())
	}
}

func TestEmbeddingBrokerRejectsPrePhaseAndLateUse(t *testing.T) {
	var upstreamCalls atomic.Int64
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		upstreamCalls.Add(1)
		writeTestEmbedding(w, 1)
	}))
	defer upstream.Close()
	broker := newInferenceBroker(1)
	broker.embeddingURL = upstream.URL + embeddingAPIPath
	broker.client.Transport = upstream.Client().Transport
	id, runID := embeddingBrokerSession(t, broker, "192.0.2.62")

	if got := callEmbedding(broker, "192.0.2.62", "before").Code; got != http.StatusConflict {
		t.Fatalf("pre-phase embedding status = %d", got)
	}
	if !broker.beginEmbeddingPhase(id, runID) {
		t.Fatal("failed to begin embedding phase")
	}
	if got := callEmbedding(broker, "192.0.2.62", "during").Code; got != http.StatusOK {
		t.Fatalf("admitted embedding status = %d", got)
	}
	broker.endEmbeddingPhase(id, runID)
	if got := callEmbedding(broker, "192.0.2.62", "after").Code; got != http.StatusConflict {
		t.Fatalf("late embedding status = %d", got)
	}
	if broker.beginEmbeddingPhase(id, runID) {
		t.Fatal("ended embedding phase reopened")
	}
	if upstreamCalls.Load() != 1 {
		t.Fatalf("pre/late probes reached upstream %d time(s)", upstreamCalls.Load())
	}
}

func TestEmbeddingBrokerOneRequestPerSessionDoesNotStarveSibling(t *testing.T) {
	firstArrived := make(chan struct{}, 1)
	releaseFirst := make(chan struct{})
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var payload embeddingRequest
		if json.NewDecoder(r.Body).Decode(&payload) != nil || len(payload.Input) != 1 {
			http.Error(w, "bad test request", http.StatusBadRequest)
			return
		}
		if payload.Input[0] == "hold" {
			firstArrived <- struct{}{}
			<-releaseFirst
		}
		writeTestEmbedding(w, 1)
	}))
	defer upstream.Close()
	broker := newInferenceBroker(2, 2)
	broker.embeddingURL = upstream.URL + embeddingAPIPath
	broker.client.Transport = upstream.Client().Transport
	admittedEmbeddingBrokerSession(t, broker, "192.0.2.63")
	admittedEmbeddingBrokerSession(t, broker, "192.0.2.64")

	firstDone := make(chan int, 1)
	go func() {
		firstDone <- callEmbedding(broker, "192.0.2.63", "hold").Code
	}()
	select {
	case <-firstArrived:
	case <-time.After(time.Second):
		close(releaseFirst)
		t.Fatal("first embedding request did not reach upstream")
	}
	secondDone := make(chan int, 1)
	go func() {
		secondDone <- callEmbedding(broker, "192.0.2.63", "same-session").Code
	}()
	select {
	case status := <-secondDone:
		close(releaseFirst)
		t.Fatalf("same-session embedding bypassed its local queue: status=%d", status)
	case <-time.After(25 * time.Millisecond):
	}
	if got := callEmbedding(broker, "192.0.2.64", "sibling").Code; got != http.StatusOK {
		close(releaseFirst)
		t.Fatalf("sibling embedding status = %d", got)
	}
	close(releaseFirst)
	if got := <-firstDone; got != http.StatusOK {
		t.Fatalf("first embedding status = %d", got)
	}
	if got := <-secondDone; got != http.StatusOK {
		t.Fatalf("queued same-session embedding status = %d", got)
	}
}

func TestEndEmbeddingPhaseWakesQueuedCalls(t *testing.T) {
	firstArrived := make(chan struct{})
	releaseFirst := make(chan struct{})
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		close(firstArrived)
		select {
		case <-r.Context().Done():
		case <-releaseFirst:
		}
	}))
	defer upstream.Close()
	defer close(releaseFirst)

	broker := newInferenceBroker(1, 1)
	broker.embeddingURL = upstream.URL + embeddingAPIPath
	broker.client.Transport = upstream.Client().Transport
	id, runID := admittedEmbeddingBrokerSession(t, broker, "192.0.2.74")

	firstDone := make(chan int, 1)
	go func() {
		firstDone <- callEmbedding(broker, "192.0.2.74", "hold").Code
	}()
	select {
	case <-firstArrived:
	case <-time.After(time.Second):
		t.Fatal("first embedding did not reach upstream")
	}

	queuedDone := make(chan int, 1)
	go func() {
		queuedDone <- callEmbedding(broker, "192.0.2.74", "queued").Code
	}()
	select {
	case status := <-queuedDone:
		t.Fatalf("queued embedding returned before phase end: %d", status)
	case <-time.After(25 * time.Millisecond):
	}

	phaseEnded := make(chan struct{})
	go func() {
		broker.endEmbeddingPhase(id, runID)
		close(phaseEnded)
	}()
	select {
	case status := <-queuedDone:
		if status != http.StatusConflict {
			t.Fatalf("queued embedding status after phase end = %d, want 409", status)
		}
	case <-time.After(time.Second):
		t.Fatal("phase end did not wake queued embedding")
	}
	select {
	case status := <-firstDone:
		if status != http.StatusBadGateway {
			t.Fatalf("active embedding status after phase end = %d, want 502", status)
		}
	case <-time.After(time.Second):
		t.Fatal("phase end did not cancel active embedding")
	}
	select {
	case <-phaseEnded:
	case <-time.After(time.Second):
		t.Fatal("embedding phase did not finish draining")
	}
}

func TestEmbeddingBrokerFailsClosedAtEverySessionBudget(t *testing.T) {
	var upstreamCalls atomic.Int64
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		upstreamCalls.Add(1)
		writeTestEmbedding(w, 1)
	}))
	defer upstream.Close()
	broker := newInferenceBroker(1)
	broker.embeddingURL = upstream.URL + embeddingAPIPath
	broker.client.Transport = upstream.Client().Transport
	id, _ := admittedEmbeddingBrokerSession(t, broker, "192.0.2.65")
	broker.mu.RLock()
	session := broker.sessions[id]
	broker.mu.RUnlock()

	tests := []struct {
		name string
		set  func()
	}{
		{"requests", func() { session.embeddingRequests = embeddingSessionRequests }},
		{"inputs", func() { session.embeddingInputs = embeddingSessionInputs }},
		{"input-bytes", func() { session.embeddingInputBytes = embeddingSessionInputBytes }},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			session.mu.Lock()
			session.embeddingRequests = 0
			session.embeddingInputs = 0
			session.embeddingInputBytes = 0
			test.set()
			session.mu.Unlock()
			recorder := callEmbedding(broker, "192.0.2.65", "x")
			if recorder.Code != http.StatusTooManyRequests ||
				!strings.Contains(recorder.Body.String(), "budget exhausted") {
				t.Fatalf("budget status=%d body=%s", recorder.Code, recorder.Body.String())
			}
		})
	}
	if upstreamCalls.Load() != 0 {
		t.Fatalf("budget-exhausted requests reached upstream %d time(s)", upstreamCalls.Load())
	}
}

func TestMemoryAdmissionCapacityOneQueuesThenAdmitsSibling(t *testing.T) {
	broker := newInferenceBroker(2, 1)
	firstID, firstRun := embeddingBrokerSession(t, broker, "192.0.2.66")
	secondID, secondRun := embeddingBrokerSession(t, broker, "192.0.2.67")
	server := &server{memorySlots: make(chan struct{}, 1), broker: broker}
	endFirst, ok := server.beginMemoryPhase(context.Background(), firstID, firstRun, true)
	if !ok {
		t.Fatal("capacity-one first phase was not admitted")
	}

	type admission struct {
		end func()
		ok  bool
	}
	admitted := make(chan admission, 1)
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	go func() {
		end, admittedOK := server.beginMemoryPhase(ctx, secondID, secondRun, true)
		admitted <- admission{end: end, ok: admittedOK}
	}()
	select {
	case <-admitted:
		endFirst()
		t.Fatal("sibling bypassed capacity-one memory admission")
	case <-time.After(25 * time.Millisecond):
	}
	endFirst()
	select {
	case result := <-admitted:
		if !result.ok {
			t.Fatal("sibling was starved after the first phase released capacity")
		}
		result.end()
	case <-time.After(time.Second):
		t.Fatal("sibling did not acquire released memory capacity")
	}
}

func TestHostedMemoryAdmissionDoesNotUseLocalOllamaQueue(t *testing.T) {
	broker := newInferenceBroker(2, 1)
	firstID, firstRun := embeddingBrokerSession(t, broker, "192.0.2.76")
	secondID, secondRun := embeddingBrokerSession(t, broker, "192.0.2.77")
	server := &server{memorySlots: make(chan struct{}, 1), broker: broker}
	endFirst, ok := server.beginMemoryPhase(context.Background(), firstID, firstRun, false)
	if !ok {
		t.Fatal("first hosted phase was not admitted")
	}
	defer endFirst()
	endSecond, ok := server.beginMemoryPhase(context.Background(), secondID, secondRun, false)
	if !ok {
		t.Fatal("second hosted phase queued behind the local Ollama lane")
	}
	endSecond()
	if len(server.memorySlots) != 0 {
		t.Fatal("hosted phases consumed the local Ollama admission slot")
	}
}

func TestMemoryAdmissionDrainsHostileInFlightEmbeddingBeforeSibling(t *testing.T) {
	firstArrived := make(chan struct{})
	firstCanceled := make(chan struct{})
	releaseFirst := make(chan struct{})
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var payload embeddingRequest
		if json.NewDecoder(r.Body).Decode(&payload) != nil || len(payload.Input) != 1 {
			http.Error(w, "bad test request", http.StatusBadRequest)
			return
		}
		if payload.Input[0] == "hostile-background" {
			close(firstArrived)
			select {
			case <-r.Context().Done():
				close(firstCanceled)
			case <-releaseFirst:
			}
			return
		}
		writeTestEmbedding(w, 1)
	}))
	defer upstream.Close()
	defer close(releaseFirst)

	broker := newInferenceBroker(2, 1)
	broker.embeddingURL = upstream.URL + embeddingAPIPath
	broker.client.Transport = upstream.Client().Transport
	firstID, firstRun := embeddingBrokerSession(t, broker, "192.0.2.68")
	secondID, secondRun := embeddingBrokerSession(t, broker, "192.0.2.69")
	server := &server{memorySlots: make(chan struct{}, 1), broker: broker}
	endFirst, ok := server.beginMemoryPhase(context.Background(), firstID, firstRun, true)
	if !ok {
		t.Fatal("capacity-one first phase was not admitted")
	}

	firstDone := make(chan int, 1)
	go func() {
		firstDone <- callEmbedding(broker, "192.0.2.68", "hostile-background").Code
	}()
	select {
	case <-firstArrived:
	case <-time.After(time.Second):
		t.Fatal("hostile embedding request did not reach upstream")
	}

	type admission struct {
		end func()
		ok  bool
	}
	admitted := make(chan admission, 1)
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	go func() {
		end, admittedOK := server.beginMemoryPhase(ctx, secondID, secondRun, true)
		admitted <- admission{end: end, ok: admittedOK}
	}()
	select {
	case <-admitted:
		t.Fatal("sibling bypassed the first memory-phase admission")
	case <-time.After(25 * time.Millisecond):
	}

	endFirst()
	var sibling admission
	select {
	case sibling = <-admitted:
		if !sibling.ok {
			t.Fatal("sibling was not admitted after predecessor drain")
		}
	case <-time.After(time.Second):
		t.Fatal("sibling was starved after predecessor drain")
	}
	defer sibling.end()
	if got := len(broker.embeddingSlots); got != 0 {
		t.Fatalf("memory admission released with %d predecessor embedding slot(s) held", got)
	}
	select {
	case status := <-firstDone:
		if status != http.StatusBadGateway {
			t.Fatalf("canceled predecessor embedding status = %d", status)
		}
	case <-time.After(time.Second):
		t.Fatal("canceled predecessor embedding request did not drain")
	}
	select {
	case <-firstCanceled:
	case <-time.After(time.Second):
		t.Fatal("predecessor embedding cancellation did not reach upstream")
	}
	if got := callEmbedding(broker, "192.0.2.69", "sibling").Code; got != http.StatusOK {
		t.Fatalf("sibling embedding inherited predecessor capacity: status=%d", got)
	}
}

func prepareBrokerSession(t *testing.T, broker *inferenceBroker) map[string]string {
	t.Helper()
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/v1/inference/session", nil)
	request.RemoteAddr = "127.0.0.1:4321"
	broker.prepare(recorder, request)
	if recorder.Code != http.StatusCreated {
		t.Fatalf("prepare status = %d: %s", recorder.Code, recorder.Body.String())
	}
	var prepared map[string]string
	if err := json.Unmarshal(recorder.Body.Bytes(), &prepared); err != nil {
		t.Fatal(err)
	}
	return prepared
}

func activateBrokerSession(t *testing.T, broker *inferenceBroker, prepared map[string]string, proxyURL string) {
	t.Helper()
	activateBrokerSessionFor(t, broker, prepared, proxyURL, "", "", "")
}

func activateBrokerSessionFor(t *testing.T, broker *inferenceBroker, prepared map[string]string, proxyURL, provider, profile, model string, budgetEvidence ...brokerActivation) {
	t.Helper()
	ticketDeadline := time.Now().Add(2 * time.Minute)
	activation := brokerActivation{
		ActivationSecret: prepared["activation_secret"],
		GrantID:          "00000000-0000-0000-0000-000000000001",
		AgentID:          testBrokerAgentID,
		SlotID:           "slot-0",
		TicketDeadline:   ticketDeadline,
		Bearer:           "platform-bearer-never-given-to-harness",
		ProxyURL:         proxyURL,
		Generation:       1,
		ExpiresAt:        time.Now().Add(time.Minute),
		Provider:         provider,
		ProfileRevision:  profile,
		Model:            model,
	}
	if len(budgetEvidence) > 0 {
		activation.RequestBudget = budgetEvidence[0].RequestBudget
		activation.TokenBudget = budgetEvidence[0].TokenBudget
		activation.EmbeddingRequestBudget = budgetEvidence[0].EmbeddingRequestBudget
		activation.EmbeddingTokenBudget = budgetEvidence[0].EmbeddingTokenBudget
		activation.MaxOutputTokens = budgetEvidence[0].MaxOutputTokens
	}
	body, _ := json.Marshal(activation)
	request := httptest.NewRequest(http.MethodPost, "/v1/inference/session/id/activate", bytes.NewReader(body))
	request.RemoteAddr = "127.0.0.1:4321"
	request.SetPathValue("id", prepared["session_id"])
	recorder := httptest.NewRecorder()
	broker.activate(recorder, request)
	if recorder.Code != http.StatusOK {
		t.Fatalf("activate status = %d: %s", recorder.Code, recorder.Body.String())
	}
}

func claimAndBindBrokerSession(t *testing.T, broker *inferenceBroker, sessionID, sourceIP string, benchVersion int) string {
	t.Helper()
	broker.mu.RLock()
	session := broker.sessions[sessionID]
	broker.mu.RUnlock()
	if session == nil {
		t.Fatal("prepared broker session disappeared")
	}
	session.mu.Lock()
	identity := brokerTicketIdentity{
		GrantID: session.grantID, AgentID: session.ticketAgentID,
		SlotID: session.ticketSlotID, TicketDeadline: session.ticketDeadline,
	}
	session.mu.Unlock()
	runID := uuid.NewString()
	if !broker.claimRun(sessionID, runID, identity, benchVersion) {
		t.Fatal("failed to claim prepared broker session")
	}
	if !broker.bindSource(sessionID, runID, sourceIP) {
		t.Fatal("failed to bind claimed broker session")
	}
	return runID
}

func configureBrokerUpstream(broker *inferenceBroker, upstream *httptest.Server) string {
	proxyURL := upstream.URL + platformInferenceAPIPath
	broker.platformProxyURL = proxyURL
	broker.platformTransportURL = proxyURL
	broker.client.Transport = upstream.Client().Transport
	return proxyURL
}

func activationResponse(
	broker *inferenceBroker,
	prepared map[string]string,
	proxyURL string,
	expiresAt time.Time,
) *httptest.ResponseRecorder {
	body, _ := json.Marshal(brokerActivation{
		ActivationSecret: prepared["activation_secret"],
		GrantID:          "00000000-0000-0000-0000-000000000001",
		Bearer:           "platform-bearer-never-given-to-harness",
		ProxyURL:         proxyURL,
		Generation:       1,
		ExpiresAt:        expiresAt,
	})
	request := httptest.NewRequest(
		http.MethodPost,
		"/v1/inference/session/id/activate",
		bytes.NewReader(body),
	)
	request.RemoteAddr = "127.0.0.1:4321"
	request.SetPathValue("id", prepared["session_id"])
	recorder := httptest.NewRecorder()
	broker.activate(recorder, request)
	return recorder
}

func TestInferenceControlPlaneRequiresLoopbackOrBearer(t *testing.T) {
	broker := newInferenceBroker(1)
	broker.controlToken = "control-secret"

	external := httptest.NewRequest(http.MethodPost, "/v1/inference/session", nil)
	external.RemoteAddr = "192.0.2.5:4321"
	recorder := httptest.NewRecorder()
	broker.prepare(recorder, external)
	if recorder.Code != http.StatusUnauthorized || len(broker.sessions) != 0 {
		t.Fatalf("unauthorized prepare status=%d sessions=%d", recorder.Code, len(broker.sessions))
	}

	external = httptest.NewRequest(http.MethodPost, "/v1/inference/session", nil)
	external.RemoteAddr = "192.0.2.5:4321"
	external.Header.Set("Authorization", "Bearer control-secret")
	recorder = httptest.NewRecorder()
	broker.prepare(recorder, external)
	if recorder.Code != http.StatusCreated || len(broker.sessions) != 1 {
		t.Fatalf("authorized prepare status=%d sessions=%d", recorder.Code, len(broker.sessions))
	}
}

func TestInferenceActivationRequiresConfiguredExactProxyAndBoundedExpiry(t *testing.T) {
	broker := newInferenceBroker(1)
	broker.platformProxyURL = "https://platform.example" + platformInferenceAPIPath
	broker.platformTransportURL = "https://platform-origin.example" + platformInferenceAPIPath
	prepared := prepareBrokerSession(t, broker)

	recorder := activationResponse(
		broker,
		prepared,
		"https://attacker.example"+platformInferenceAPIPath,
		time.Now().Add(time.Minute),
	)
	if recorder.Code != http.StatusUnauthorized {
		t.Fatalf("unconfigured proxy status = %d", recorder.Code)
	}
	recorder = activationResponse(
		broker,
		prepared,
		broker.platformProxyURL,
		time.Now().Add(brokerMaximumSessionTTL+time.Minute),
	)
	if recorder.Code != http.StatusUnauthorized {
		t.Fatalf("overlong expiry status = %d", recorder.Code)
	}
	recorder = activationResponse(
		broker,
		prepared,
		broker.platformProxyURL,
		time.Now().Add(time.Minute),
	)
	if recorder.Code != http.StatusOK {
		t.Fatalf("valid activation status = %d: %s", recorder.Code, recorder.Body.String())
	}
	broker.mu.RLock()
	session := broker.sessions[prepared["session_id"]]
	broker.mu.RUnlock()
	session.mu.Lock()
	transportURL := session.proxyURL
	session.mu.Unlock()
	if transportURL != broker.platformTransportURL {
		t.Fatalf("session transport URL = %q, want %q", transportURL, broker.platformTransportURL)
	}
}

func TestInferenceActivationAcceptsPlatformTicketTTL(t *testing.T) {
	broker := newInferenceBroker(1)
	broker.platformProxyURL = "https://platform.example" + platformInferenceAPIPath
	broker.platformTransportURL = broker.platformProxyURL
	prepared := prepareBrokerSession(t, broker)
	recorder := activationResponse(
		broker,
		prepared,
		broker.platformProxyURL,
		time.Now().Add(430*time.Minute),
	)
	if recorder.Code != http.StatusOK {
		t.Fatalf("430-minute platform grant status = %d: %s", recorder.Code, recorder.Body.String())
	}
}

func TestInferenceActivationRequiresTicketIdentityForV7Route(t *testing.T) {
	broker := newInferenceBroker(1)
	broker.platformProxyURL = "https://platform.example" + platformInferenceAPIPath
	broker.platformTransportURL = broker.platformProxyURL
	prepared := prepareBrokerSession(t, broker)
	body, _ := json.Marshal(brokerActivation{
		ActivationSecret: prepared["activation_secret"],
		GrantID:          "00000000-0000-0000-0000-000000000001",
		Bearer:           "platform-bearer-never-given-to-harness",
		ProxyURL:         broker.platformProxyURL,
		Generation:       1,
		ExpiresAt:        time.Now().Add(time.Minute),
		Provider:         "amazon-bedrock",
		ProfileRevision:  "openrouter-route-0123456789abcdef-v1",
		Model:            llm.V7HarnessModel,
	})
	request := httptest.NewRequest(http.MethodPost, "/v1/inference/session/id/activate", bytes.NewReader(body))
	request.RemoteAddr = "127.0.0.1:4321"
	request.SetPathValue("id", prepared["session_id"])
	recorder := httptest.NewRecorder()
	broker.activate(recorder, request)
	if recorder.Code != http.StatusUnauthorized {
		t.Fatalf("identity-free v7 activation status = %d", recorder.Code)
	}
}

func TestConfiguredPlatformProxyURLFailsClosed(t *testing.T) {
	valid := "https://platform.example" + platformInferenceAPIPath
	if got := configuredPlatformProxyURL("  " + valid + "  "); got != valid {
		t.Fatalf("configured proxy = %q", got)
	}
	for _, invalid := range []string{
		"http://platform.example" + platformInferenceAPIPath,
		"https://platform.example/other",
		valid + "?next=https://attacker.example",
		"https://user@platform.example" + platformInferenceAPIPath,
	} {
		if got := configuredPlatformProxyURL(invalid); got != "" {
			t.Errorf("unsafe proxy %q accepted as %q", invalid, got)
		}
	}
}

func TestConfiguredPlatformTransportURLDefaultsToCanonicalAndFailsClosed(t *testing.T) {
	canonical := "https://platform.example" + platformInferenceAPIPath
	direct := "https://platform-origin.example" + platformInferenceAPIPath
	if got := configuredPlatformTransportURL("", canonical); got != canonical {
		t.Fatalf("empty transport = %q, want canonical %q", got, canonical)
	}
	if got := configuredPlatformTransportURL("  "+direct+"  ", canonical); got != direct {
		t.Fatalf("direct transport = %q, want %q", got, direct)
	}
	if got := configuredPlatformTransportURL("http://platform-origin.example"+platformInferenceAPIPath, canonical); got != "" {
		t.Fatalf("unsafe transport accepted as %q", got)
	}
}

func TestInferenceBrokerDoesNotFollowPlatformRedirects(t *testing.T) {
	redirectFollowed := false
	destination := httptest.NewTLSServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		redirectFollowed = true
	}))
	defer destination.Close()
	redirector := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, destination.URL+platformInferenceAPIPath, http.StatusTemporaryRedirect)
	}))
	defer redirector.Close()

	broker := newInferenceBroker(1)
	proxyURL := configureBrokerUpstream(broker, redirector)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSession(t, broker, prepared, proxyURL)
	claimAndBindBrokerSession(t, broker, prepared["session_id"], "192.0.2.50", protocol.BenchVersionV6)
	request := httptest.NewRequest(
		http.MethodPost,
		"/v1/inference/v1/chat/completions",
		bytes.NewBufferString(`{"model":"qwen/qwen3-32b"}`),
	)
	request.RemoteAddr = "192.0.2.50:4321"
	request.SetPathValue("rest", "v1/chat/completions")
	recorder := httptest.NewRecorder()
	broker.handle(recorder, request)
	if recorder.Code != http.StatusBadGateway || redirectFollowed {
		t.Fatalf("redirect status=%d followed=%t", recorder.Code, redirectFollowed)
	}
}

func TestInferenceBrokerRejectsSiblingSource(t *testing.T) {
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		t.Fatal("wrong-source request reached upstream")
	}))
	defer upstream.Close()
	broker := newInferenceBroker(1)
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSession(t, broker, prepared, proxyURL)
	claimAndBindBrokerSession(t, broker, prepared["session_id"], "10.0.0.10", protocol.BenchVersionV6)

	request := httptest.NewRequest(http.MethodPost, "/v1/inference/id/v1/chat/completions", bytes.NewBufferString(`{"model":"qwen/qwen3-32b"}`))
	request.RemoteAddr = "10.0.0.11:4321"
	request.SetPathValue("rest", "v1/chat/completions")
	recorder := httptest.NewRecorder()
	broker.handle(recorder, request)
	if recorder.Code != http.StatusUnauthorized {
		t.Fatalf("wrong source status = %d", recorder.Code)
	}
}

func TestInferenceBrokerAddsProofWithoutExposingBearerToHarness(t *testing.T) {
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		for _, header := range []string{"Authorization", "X-Ditto-Grant", "X-Ditto-Generation", "X-Ditto-Nonce", "X-Ditto-Requested-At", "X-Ditto-Proof"} {
			if r.Header.Get(header) == "" {
				t.Errorf("missing trusted broker header %s", header)
			}
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"usage":{"prompt_tokens":3,"completion_tokens":4},"choices":[]}`))
	}))
	defer upstream.Close()
	broker := newInferenceBroker(1)
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSession(t, broker, prepared, proxyURL)
	claimAndBindBrokerSession(t, broker, prepared["session_id"], "192.0.2.10", protocol.BenchVersionV6)

	request := httptest.NewRequest(http.MethodPost, "/v1/inference/id/v1/chat/completions", bytes.NewBufferString(`{"model":"qwen/qwen3-32b","max_tokens":32}`))
	request.RemoteAddr = "192.0.2.10:4321"
	request.SetPathValue("rest", "v1/chat/completions")
	recorder := httptest.NewRecorder()
	broker.handle(recorder, request)
	if recorder.Code != http.StatusOK {
		t.Fatalf("proxy status = %d: %s", recorder.Code, recorder.Body.String())
	}
	if bytes.Contains(recorder.Body.Bytes(), []byte("platform-bearer")) {
		t.Fatal("platform bearer leaked to harness response")
	}
}

func TestInferenceBrokerTrustedProbeUsesControlPlaneSession(t *testing.T) {
	const profile = "openrouter-route-0123456789abcdef-v1"
	embeddingCalls := 0
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if r.URL.Path == platformEmbeddingAPIPath {
			embeddingCalls++
			vector := make([]float64, embeddingDimensions)
			writeJSON(w, http.StatusOK, map[string]any{
				"object": "list", "model": hostedEmbeddingModel,
				"data":  []map[string]any{{"object": "embedding", "index": 0, "embedding": vector}},
				"usage": map[string]int{"prompt_tokens": 3, "total_tokens": 3},
			})
			return
		}
		_, _ = w.Write([]byte(`{"usage":{"prompt_tokens":2,"completion_tokens":1},"choices":[{"message":{"content":"OK"}}]}`))
	}))
	defer upstream.Close()
	broker := newInferenceBroker(1)
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSessionFor(t, broker, prepared, proxyURL, "amazon-bedrock", profile, llm.V7HarnessModel)
	claimAndBindBrokerSession(t, broker, prepared["session_id"], "192.0.2.30", protocol.BenchVersionV8)

	if err := broker.trustedProbe(context.Background(), prepared["session_id"]); err != nil {
		t.Fatalf("trusted probe: %v", err)
	}
	snapshot, err := broker.snapshot(prepared["session_id"])
	if err != nil {
		t.Fatal(err)
	}
	if snapshot.Requests != 1 || snapshot.Successes != 1 || snapshot.UsageAvailable != 1 {
		t.Fatalf("unexpected trusted probe accounting: %+v", snapshot)
	}
	if embeddingCalls != 1 {
		t.Fatalf("trusted v7 probe made %d embedding calls, want 1", embeddingCalls)
	}
	if snapshot.Model != llm.V7HarnessModel || snapshot.ProfileRevision != profile || snapshot.Provider != "amazon-bedrock" {
		t.Fatalf("v7 relay identity = %+v", snapshot)
	}
}

// The trusted probe is cost-bearing work, so provider backpressure parks the
// lease after one signed delivery. An operator may retry the enclosing ticket.
func TestInferenceBrokerTrustedProbeParksPlatformEmbeddingBackpressure(t *testing.T) {
	var chatCalls atomic.Int64
	var embeddingCalls atomic.Int64
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if r.URL.Path == platformEmbeddingAPIPath {
			if embeddingCalls.Add(1) == 1 {
				w.Header().Set("Retry-After", "1")
				http.Error(w, "embedding provider rolling window is full", http.StatusServiceUnavailable)
				return
			}
			vector := make([]float64, embeddingDimensions)
			writeJSON(w, http.StatusOK, map[string]any{
				"object": "list", "model": hostedEmbeddingModel,
				"data":  []map[string]any{{"object": "embedding", "index": 0, "embedding": vector}},
				"usage": map[string]int{"prompt_tokens": 3, "total_tokens": 3},
			})
			return
		}
		chatCalls.Add(1)
		_, _ = w.Write([]byte(`{"usage":{"prompt_tokens":2,"completion_tokens":1},"choices":[{"message":{"content":"OK"}}]}`))
	}))
	defer upstream.Close()

	broker := newInferenceBroker(1)
	broker.sleep = func(context.Context, time.Duration) error { return nil }
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSessionFor(t, broker, prepared, proxyURL, "openrouter", llm.V9AggregateProfileRevision, llm.HarnessModelForVersion(protocol.BenchVersionV9))
	claimAndBindBrokerSession(t, broker, prepared["session_id"], "192.0.2.31", protocol.BenchVersionV9)

	if err := broker.trustedProbe(context.Background(), prepared["session_id"]); err == nil {
		t.Fatal("trusted probe accepted provider backpressure without parking")
	}
	if got := chatCalls.Load(); got != 1 {
		t.Fatalf("chat probe deliveries=%d, want 1", got)
	}
	if got := embeddingCalls.Load(); got != 1 {
		t.Fatalf("embedding probe deliveries=%d, want one", got)
	}
	snapshot, err := broker.snapshot(prepared["session_id"])
	if err != nil {
		t.Fatal(err)
	}
	if snapshot.InfrastructureFailures != 0 || snapshot.GrantDenials != 0 || snapshot.EmbeddingRetries != 0 {
		t.Fatalf("provider backpressure was booked as a probe fault: %+v", snapshot)
	}
}

// TestV7InferenceBrokerParksTransientProviderFaults proves that a transient
// provider response receives one signed delivery and no automatic recovery
// phase. The failed evaluation remains available for an operator retry.
func TestV7InferenceBrokerParksTransientProviderFaults(t *testing.T) {
	const profile = "openrouter-route-0123456789abcdef-v1"
	requestCount := 0
	var nonces []string
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requestCount++
		nonces = append(nonces, r.Header.Get("X-Ditto-Nonce"))
		http.Error(w, "transient", http.StatusServiceUnavailable)
	}))
	defer upstream.Close()

	broker := newInferenceBroker(1)
	broker.sleep = func(context.Context, time.Duration) error { return nil }
	var waitEvents []bool
	broker.relayWait = func(_ string, waiting bool) { waitEvents = append(waitEvents, waiting) }
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSessionFor(t, broker, prepared, proxyURL, "groq", profile, llm.V7HarnessModel)
	claimAndBindBrokerSession(t, broker, prepared["session_id"], "192.0.2.31", protocol.BenchVersionV8)
	start, err := broker.snapshot(prepared["session_id"])
	if err != nil {
		t.Fatal(err)
	}

	request := httptest.NewRequest(http.MethodPost, "/v1/inference/id/v1/chat/completions", bytes.NewBufferString(`{"model":"openai/gpt-oss-20b"}`))
	request.RemoteAddr = "192.0.2.31:4321"
	request.SetPathValue("rest", "v1/chat/completions")
	recorder := httptest.NewRecorder()
	broker.handle(recorder, request)
	if recorder.Code != http.StatusBadGateway {
		t.Fatalf("request status = %d, want 502: %s", recorder.Code, recorder.Body.String())
	}
	if requestCount != 1 {
		t.Fatalf("platform deliveries=%d, want 1", requestCount)
	}
	if len(waitEvents) != 0 {
		t.Fatalf("relay wait events = %v, want none", waitEvents)
	}
	seen := map[string]bool{}
	for _, nonce := range nonces {
		if nonce == "" || seen[nonce] {
			t.Fatalf("platform deliveries were not independently signed: nonces=%v", nonces)
		}
		seen[nonce] = true
	}

	end, err := broker.snapshot(prepared["session_id"])
	if err != nil {
		t.Fatal(err)
	}
	usage, err := relayUsageSince(start, end)
	if err != nil {
		t.Fatal(err)
	}
	if usage.Requests != 1 || usage.Successes != 0 || usage.UsageAvailable != 0 {
		t.Fatalf("provider failure accounting = %+v", usage)
	}
	execution, err := relayExecutionSince(start, end)
	if err != nil {
		t.Fatal(err)
	}
	if execution.UpstreamAttempts != 1 || execution.Retries != 0 || execution.InfrastructureFailures != 1 ||
		execution.RecoveryWaits != 0 || execution.RecoveryExhaustions != 0 {
		t.Fatalf("provider execution accounting = %+v", execution)
	}
	// One logical request, one delivery, one authoritative terminal outcome.
	if execution.Requests != 1 || execution.GrantDenials != 0 {
		t.Fatalf("single-shot failure inflated requests or denials: %+v", execution)
	}
	if err := requireCompleteV7Usage(protocol.BenchVersionV8, usage, relayExecutionSummary{}); err == nil {
		t.Fatal("v7 accepted a run with a provider infrastructure failure")
	}
}

func TestV7InferenceBrokerLeavesStructuredOutputRepairToMiner(t *testing.T) {
	const profile = "openrouter-route-0123456789abcdef-v1"
	var calls atomic.Int64
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		if calls.Add(1) == 1 {
			w.Header().Set(minerRecoverableFailureHeader, minerRecoverableGeneration)
			http.Error(w, "inference provider unavailable", http.StatusBadGateway)
			return
		}
		_, _ = w.Write([]byte(`{"usage":{"prompt_tokens":2,"completion_tokens":1},"choices":[{"message":{"content":"OK"}}]}`))
	}))
	defer upstream.Close()

	broker := newInferenceBroker(1)
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSessionFor(t, broker, prepared, proxyURL, "groq", profile, llm.V7HarnessModel)
	claimAndBindBrokerSession(t, broker, prepared["session_id"], "192.0.2.31", protocol.BenchVersionV8)
	start, err := broker.snapshot(prepared["session_id"])
	if err != nil {
		t.Fatal(err)
	}

	call := func() *httptest.ResponseRecorder {
		request := httptest.NewRequest(http.MethodPost, "/v1/inference/id/v1/chat/completions", bytes.NewBufferString(`{"model":"openai/gpt-oss-20b"}`))
		request.RemoteAddr = "192.0.2.31:4321"
		request.SetPathValue("rest", "v1/chat/completions")
		recorder := httptest.NewRecorder()
		broker.handle(recorder, request)
		return recorder
	}
	if first := call(); first.Code != http.StatusBadGateway {
		t.Fatalf("first status=%d want 502: %s", first.Code, first.Body.String())
	}
	if second := call(); second.Code != http.StatusOK {
		t.Fatalf("second status=%d want 200: %s", second.Code, second.Body.String())
	}
	if calls.Load() != 2 {
		t.Fatalf("platform deliveries=%d want exactly two miner-issued calls", calls.Load())
	}

	end, err := broker.snapshot(prepared["session_id"])
	if err != nil {
		t.Fatal(err)
	}
	if err := relayDegradedSince(start, end); err != nil {
		t.Fatalf("miner-recoverable response degraded the relay: %v", err)
	}
	usage, err := relayUsageSince(start, end)
	if err != nil {
		t.Fatal(err)
	}
	execution, err := relayExecutionSince(start, end)
	if err != nil {
		t.Fatal(err)
	}
	if usage.Status != "complete" || usage.Requests != 2 || usage.Successes != 1 || usage.UsageAvailable != 1 {
		t.Fatalf("usage=%+v", usage)
	}
	if execution.Requests != 2 || execution.Successes != 1 || execution.UpstreamAttempts != 2 ||
		execution.Retries != 0 || execution.InfrastructureFailures != 0 || execution.MinerRecoverableFailures != 1 {
		t.Fatalf("execution=%+v", execution)
	}
	if err := requireCompleteV7Usage(protocol.BenchVersionV8, usage, execution); err != nil {
		t.Fatalf("successful miner repair was not scoreable: %v", err)
	}
}

func TestMinerRecoverableFailureClassRequiresAuthenticatedPlatformRoute(t *testing.T) {
	if !minerRecoverablePlatformFailure("", nil, http.StatusBadGateway, minerRecoverableGeneration) {
		t.Fatal("authenticated Platform classification was rejected")
	}
	if minerRecoverablePlatformFailure("https://legacy.invalid", nil, http.StatusBadGateway, minerRecoverableGeneration) {
		t.Fatal("legacy gateway spoofed miner-recoverable classification")
	}
	if minerRecoverablePlatformFailure("", http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}), http.StatusBadGateway, minerRecoverableGeneration) {
		t.Fatal("in-process reader spoofed miner-recoverable classification")
	}
	if minerRecoverablePlatformFailure("", nil, http.StatusServiceUnavailable, minerRecoverableGeneration) {
		t.Fatal("non-502 provider failure was marked miner-recoverable")
	}
}

func TestV7InferenceBrokerDoesNotEnterRecoveryWait(t *testing.T) {
	var attempts atomic.Int64
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		if attempts.Add(1) == 1 {
			http.Error(w, "transient", http.StatusServiceUnavailable)
			return
		}
		_, _ = w.Write([]byte(`{"usage":{"prompt_tokens":2,"completion_tokens":1},"choices":[{"message":{"content":"OK"}}]}`))
	}))
	defer upstream.Close()

	broker := newInferenceBroker(1)
	broker.sleep = func(context.Context, time.Duration) error { return nil }
	var waitEvents []bool
	broker.relayWait = func(_ string, waiting bool) { waitEvents = append(waitEvents, waiting) }
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSessionFor(t, broker, prepared, proxyURL, "openrouter", "openrouter-route-0123456789abcdef-v1", llm.V7HarnessModel)
	claimAndBindBrokerSession(t, broker, prepared["session_id"], "192.0.2.33", protocol.BenchVersionV8)
	start, err := broker.snapshot(prepared["session_id"])
	if err != nil {
		t.Fatal(err)
	}

	request := httptest.NewRequest(http.MethodPost, "/v1/inference/id/v1/chat/completions", bytes.NewBufferString(`{"model":"openai/gpt-oss-20b"}`))
	request.RemoteAddr = "192.0.2.33:4321"
	request.SetPathValue("rest", "v1/chat/completions")
	recorder := httptest.NewRecorder()
	broker.handle(recorder, request)
	if recorder.Code != http.StatusBadGateway {
		t.Fatalf("request status = %d, want 502: %s", recorder.Code, recorder.Body.String())
	}
	if attempts.Load() != 1 || len(waitEvents) != 0 {
		t.Fatalf("attempts=%d relay wait events=%v, want one attempt and no recovery wait", attempts.Load(), waitEvents)
	}
	end, err := broker.snapshot(prepared["session_id"])
	if err != nil {
		t.Fatal(err)
	}
	if err := relayDegradedSince(start, end); err == nil {
		t.Fatal("terminal provider fault did not fail the run closed")
	}
	execution, err := relayExecutionSince(start, end)
	if err != nil {
		t.Fatal(err)
	}
	if execution.UpstreamAttempts != 1 || execution.RecoveryWaits != 0 || execution.RecoveryExhaustions != 0 ||
		execution.Successes != 0 || execution.InfrastructureFailures != 1 {
		t.Fatalf("single-shot execution = %+v", execution)
	}
}

func TestInferenceBrokerRejectsExhaustedTransientFailures(t *testing.T) {
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, "unavailable", http.StatusServiceUnavailable)
	}))
	defer upstream.Close()

	broker := newInferenceBroker(1)
	broker.sleep = func(context.Context, time.Duration) error { return nil }
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSessionFor(t, broker, prepared, proxyURL, "groq", "openrouter-route-0123456789abcdef-v1", llm.V7HarnessModel)
	claimAndBindBrokerSession(t, broker, prepared["session_id"], "192.0.2.32", protocol.BenchVersionV8)

	request := httptest.NewRequest(http.MethodPost, "/v1/inference/id/v1/chat/completions", bytes.NewBufferString(`{"model":"openai/gpt-oss-20b"}`))
	request.RemoteAddr = "192.0.2.32:4321"
	request.SetPathValue("rest", "v1/chat/completions")
	recorder := httptest.NewRecorder()
	broker.handle(recorder, request)
	if recorder.Code != http.StatusBadGateway {
		t.Fatalf("request status = %d, want 502: %s", recorder.Code, recorder.Body.String())
	}
	snapshot, err := broker.snapshot(prepared["session_id"])
	if err != nil {
		t.Fatal(err)
	}
	if snapshot.Requests != 1 || snapshot.Successes != 0 || snapshot.UpstreamAttempts != 1 ||
		snapshot.InfrastructureFailures != 1 || snapshot.RecoveryExhaustions != 0 {
		t.Fatalf("exhausted provider accounting = %+v", snapshot)
	}
}

// TestInferenceBrokerServesTheTicketModelNotTheRequestedOne pins version
// isolation under the substitution rule. The caller no longer selects a model
// at all: whatever it sends, the upstream receives the session's own model. A
// pre-v7 harness carrying the stale qwen/qwen3-32b default is therefore scored
// on the v7 model rather than failing closed with an error it cannot act on,
// and a caller still cannot reach a model its ticket does not pin.
func TestInferenceBrokerServesTheTicketModelNotTheRequestedOne(t *testing.T) {
	var delivered []string
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		var seen struct {
			Model string `json:"model"`
		}
		_ = json.Unmarshal(body, &seen)
		delivered = append(delivered, seen.Model)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"usage":{"prompt_tokens":3,"completion_tokens":4},"choices":[{"message":{"content":"OK"}}]}`))
	}))
	defer upstream.Close()
	broker := newInferenceBroker(1)
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSessionFor(t, broker, prepared, proxyURL, "amazon-bedrock", "openrouter-route-0123456789abcdef-v1", llm.V7HarnessModel)
	claimAndBindBrokerSession(t, broker, prepared["session_id"], "192.0.2.40", protocol.BenchVersionV8)
	request := httptest.NewRequest(http.MethodPost, "/v1/inference/id/v1/chat/completions",
		bytes.NewBufferString(`{"model":"qwen/qwen3-32b","temperature":0}`))
	request.RemoteAddr = "192.0.2.40:4321"
	request.SetPathValue("rest", "v1/chat/completions")
	recorder := httptest.NewRecorder()
	broker.handle(recorder, request)
	if recorder.Code != http.StatusOK {
		t.Fatalf("substituted request status = %d: %s", recorder.Code, recorder.Body.String())
	}
	if len(delivered) != 1 || delivered[0] != llm.V7HarnessModel {
		t.Fatalf("upstream received %v, want only the ticket model %q", delivered, llm.V7HarnessModel)
	}
	for _, model := range delivered {
		if model == "qwen/qwen3-32b" {
			t.Fatal("the caller's model reached the platform proxy")
		}
	}
}

// TestInferenceBrokerRejectsUnparseableRequestBody keeps the substitution path
// fail-closed on input it cannot safely rewrite.
func TestInferenceBrokerRejectsUnparseableRequestBody(t *testing.T) {
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		t.Fatal("unparseable request reached upstream")
	}))
	defer upstream.Close()
	broker := newInferenceBroker(1)
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSessionFor(t, broker, prepared, proxyURL, "groq", "openrouter-route-0123456789abcdef-v1", llm.V7HarnessModel)
	claimAndBindBrokerSession(t, broker, prepared["session_id"], "192.0.2.41", protocol.BenchVersionV8)
	request := httptest.NewRequest(http.MethodPost, "/v1/inference/id/v1/chat/completions",
		bytes.NewBufferString(`["not","an","object"]`))
	request.RemoteAddr = "192.0.2.41:4321"
	request.SetPathValue("rest", "v1/chat/completions")
	recorder := httptest.NewRecorder()
	broker.handle(recorder, request)
	if recorder.Code != http.StatusBadRequest {
		t.Fatalf("unparseable body status = %d, want 400", recorder.Code)
	}
}

func TestNormalizeV9ReasoningStrategy(t *testing.T) {
	tests := []struct {
		name       string
		body       string
		wantEffort string
	}{
		{name: "omitted defaults medium", body: `{}`, wantEffort: "medium"},
		{name: "flat low", body: `{"reasoning_effort":"low"}`, wantEffort: "low"},
		{name: "flat medium", body: `{"reasoning_effort":"medium"}`, wantEffort: "medium"},
		{name: "flat high", body: `{"reasoning_effort":"high"}`, wantEffort: "high"},
		{name: "nested low", body: `{"reasoning":{"effort":"low"}}`, wantEffort: "low"},
		{name: "nested medium", body: `{"reasoning":{"effort":"medium"}}`, wantEffort: "medium"},
		{name: "nested high", body: `{"reasoning":{"effort":"high"}}`, wantEffort: "high"},
		{name: "equal aliases", body: `{"reasoning":{"effort":"high"},"reasoning_effort":"high"}`, wantEffort: "high"},
		{name: "conflicting aliases prefer nested", body: `{"reasoning":{"effort":"high"},"reasoning_effort":"low"}`, wantEffort: "high"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			got, err := normalizeChatRequest([]byte(test.body), llm.V7HarnessModel, protocol.BenchVersionV9)
			if err != nil {
				t.Fatal(err)
			}
			var decoded map[string]any
			if err := json.Unmarshal(got, &decoded); err != nil {
				t.Fatal(err)
			}
			if decoded["model"] != llm.V7HarnessModel {
				t.Fatalf("model = %v, want ticket model", decoded["model"])
			}
			if _, present := decoded["reasoning_effort"]; present {
				t.Fatal("flat reasoning alias survived normalization")
			}
			reasoning, ok := decoded["reasoning"].(map[string]any)
			if !ok || len(reasoning) != 2 || reasoning["effort"] != test.wantEffort || reasoning["exclude"] != true {
				t.Fatalf("reasoning = %#v, want effort=%q exclude=true", reasoning, test.wantEffort)
			}
		})
	}
}

func TestNormalizeV9ReasoningRejectsInvalidAndConflictingInputs(t *testing.T) {
	tests := []struct {
		name string
		body string
		want string
	}{
		{name: "null flat", body: `{"reasoning_effort":null}`, want: "invalid reasoning_effort"},
		{name: "boolean flat", body: `{"reasoning_effort":true}`, want: "invalid reasoning_effort"},
		{name: "unknown flat", body: `{"reasoning_effort":"minimal"}`, want: "invalid reasoning_effort"},
		{name: "case drift flat", body: `{"reasoning_effort":"LOW"}`, want: "invalid reasoning_effort"},
		{name: "spaced flat", body: `{"reasoning_effort":" medium"}`, want: "invalid reasoning_effort"},
		{name: "null nested", body: `{"reasoning":null}`, want: "invalid reasoning"},
		{name: "string nested", body: `{"reasoning":"low"}`, want: "invalid reasoning"},
		{name: "empty nested", body: `{"reasoning":{}}`, want: "invalid reasoning"},
		{name: "unknown nested", body: `{"reasoning":{"effort":"minimal"}}`, want: "invalid reasoning effort"},
		{name: "boolean nested", body: `{"reasoning":{"effort":true}}`, want: "invalid reasoning effort"},
		{name: "caller exclude", body: `{"reasoning":{"effort":"medium","exclude":true}}`, want: "invalid reasoning"},
		{name: "caller enabled", body: `{"reasoning":{"effort":"medium","enabled":true}}`, want: "invalid reasoning"},
		{name: "caller budget", body: `{"reasoning":{"effort":"medium","max_tokens":1}}`, want: "invalid reasoning"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if _, err := normalizeChatRequest([]byte(test.body), llm.V7HarnessModel, protocol.BenchVersionV9); err == nil || err.Error() != test.want {
				t.Fatalf("error = %v, want %q", err, test.want)
			}
		})
	}
}

func TestNormalizeV8ReasoningRemainsCallerOpaque(t *testing.T) {
	body := []byte(`{"model":"stale","reasoning":{"effort":"high","exclude":false},"reasoning_effort":"low"}`)
	got, err := normalizeChatRequest(body, llm.V7HarnessModel, protocol.BenchVersionV8)
	if err != nil {
		t.Fatal(err)
	}
	var decoded map[string]any
	if err := json.Unmarshal(got, &decoded); err != nil {
		t.Fatal(err)
	}
	if decoded["model"] != llm.V7HarnessModel || decoded["reasoning_effort"] != "low" {
		t.Fatalf("v8 compatibility fields drifted: %#v", decoded)
	}
	reasoning := decoded["reasoning"].(map[string]any)
	if reasoning["effort"] != "high" || reasoning["exclude"] != false {
		t.Fatalf("v8 reasoning was unexpectedly normalized: %#v", reasoning)
	}
}

func TestNormalizeV10InheritsV9ReasoningContract(t *testing.T) {
	got, err := normalizeChatRequest(
		[]byte(`{"reasoning_effort":"high"}`),
		llm.V7HarnessModel,
		protocol.BenchVersionV10,
	)
	if err != nil {
		t.Fatal(err)
	}
	var decoded map[string]any
	if err := json.Unmarshal(got, &decoded); err != nil {
		t.Fatal(err)
	}
	if _, present := decoded["reasoning_effort"]; present {
		t.Fatal("v10 retained the flat reasoning alias")
	}
	reasoning, ok := decoded["reasoning"].(map[string]any)
	if !ok || reasoning["effort"] != "high" || reasoning["exclude"] != true {
		t.Fatalf("v10 reasoning = %#v, want v9 contract", reasoning)
	}
	if _, err := normalizeChatRequest(
		[]byte(`{"reasoning_effort":"minimal"}`),
		llm.V7HarnessModel,
		protocol.BenchVersionV10,
	); err == nil || err.Error() != "invalid reasoning_effort" {
		t.Fatalf("v10 invalid reasoning error = %v", err)
	}
}

func TestV9BrokerNormalizesReasoningBeforePlatformAndAccountsRejections(t *testing.T) {
	var delivered []map[string]any
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Fatal(err)
		}
		delivered = append(delivered, body)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"usage":{"prompt_tokens":3,"completion_tokens":4},"choices":[{"message":{"content":"OK"}}]}`))
	}))
	defer upstream.Close()

	broker := newInferenceBroker(1)
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSessionFor(
		t, broker, prepared, proxyURL, "openrouter",
		llm.V9AggregateProfileRevision, llm.V7HarnessModel,
	)
	claimAndBindBrokerSession(t, broker, prepared["session_id"], "192.0.2.45", protocol.BenchVersionV9)

	call := func(body string) *httptest.ResponseRecorder {
		request := httptest.NewRequest(
			http.MethodPost, "/v1/inference/id/v1/chat/completions", bytes.NewBufferString(body),
		)
		request.RemoteAddr = "192.0.2.45:4321"
		request.SetPathValue("rest", "v1/chat/completions")
		recorder := httptest.NewRecorder()
		broker.handle(recorder, request)
		return recorder
	}

	if response := call(`{"model":"stale","reasoning_effort":"low"}`); response.Code != http.StatusOK {
		t.Fatalf("flat low status=%d body=%s", response.Code, response.Body.String())
	}
	if response := call(`{"model":"openai/gpt-oss-20b"}`); response.Code != http.StatusOK {
		t.Fatalf("default status=%d body=%s", response.Code, response.Body.String())
	}
	if response := call(`{"model":"openai/gpt-oss-20b","reasoning":{"effort":"high","exclude":false}}`); response.Code != http.StatusBadRequest {
		t.Fatalf("provider-control status=%d body=%s", response.Code, response.Body.String())
	}
	if len(delivered) != 2 {
		t.Fatalf("provider received %d requests, want 2", len(delivered))
	}
	for index, effort := range []string{"low", "medium"} {
		if _, present := delivered[index]["reasoning_effort"]; present {
			t.Fatalf("request %d retained flat alias", index)
		}
		reasoning := delivered[index]["reasoning"].(map[string]any)
		if reasoning["effort"] != effort || reasoning["exclude"] != true {
			t.Fatalf("request %d reasoning=%#v", index, reasoning)
		}
	}
	snapshot, err := broker.snapshot(prepared["session_id"])
	if err != nil {
		t.Fatal(err)
	}
	if snapshot.Requests != 2 || snapshot.Successes != 2 || snapshot.UsageAvailable != 2 ||
		snapshot.PromptTokens != 6 || snapshot.CompletionTokens != 8 ||
		snapshot.AgentRequestRejections != 1 || snapshot.UpstreamAttempts != 2 {
		t.Fatalf("v9 reasoning accounting = %+v", snapshot)
	}
}

func TestInferenceBrokerClaimsTicketIdentityOnceAndRejectsSiblingRemoval(t *testing.T) {
	const profile = "openrouter-route-0123456789abcdef-v1"
	broker := newInferenceBroker(1)
	broker.platformProxyURL = "https://platform.example" + platformInferenceAPIPath
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSessionFor(
		t, broker, prepared, broker.platformProxyURL,
		"amazon-bedrock", profile, llm.V7HarnessModel,
	)

	broker.mu.RLock()
	session := broker.sessions[prepared["session_id"]]
	broker.mu.RUnlock()
	session.mu.Lock()
	identity := brokerTicketIdentity{
		GrantID: session.grantID, AgentID: session.ticketAgentID,
		SlotID: session.ticketSlotID, TicketDeadline: session.ticketDeadline,
	}
	session.mu.Unlock()

	wrongIdentity := identity
	wrongIdentity.AgentID = "00000000-0000-0000-0000-000000000003"
	if broker.claimRun(prepared["session_id"], uuid.NewString(), wrongIdentity, protocol.BenchVersionV8) {
		t.Fatal("session accepted a run for a sibling ticket identity")
	}
	ownerRunID := uuid.NewString()
	if !broker.claimRun(prepared["session_id"], ownerRunID, identity, protocol.BenchVersionV8) {
		t.Fatal("session rejected its ticket identity")
	}
	if broker.claimRun(prepared["session_id"], uuid.NewString(), identity, protocol.BenchVersionV8) {
		t.Fatal("session accepted a second run claim")
	}
	if broker.bindSource(prepared["session_id"], uuid.NewString(), "192.0.2.60") {
		t.Fatal("sibling run bound a source")
	}
	if !broker.bindSource(prepared["session_id"], ownerRunID, "192.0.2.60") {
		t.Fatal("owner run could not bind its source")
	}
	if broker.bindSource(prepared["session_id"], ownerRunID, "192.0.2.61") {
		t.Fatal("owner run rebound an already-bound source")
	}
	if broker.removeRun(prepared["session_id"], uuid.NewString()) {
		t.Fatal("sibling run removed the session")
	}
	broker.mu.RLock()
	stillPresent := broker.sessions[prepared["session_id"]] != nil
	broker.mu.RUnlock()
	if !stillPresent {
		t.Fatal("sibling removal deleted the owner session")
	}
	if !broker.removeRun(prepared["session_id"], ownerRunID) {
		t.Fatal("owner run could not remove its session")
	}
}

type readTrackingBody struct {
	reads int
}

func (b *readTrackingBody) Read([]byte) (int, error) {
	b.reads++
	return 0, io.EOF
}

func TestInferenceBrokerRejectsOverCapacityBeforeReadingBody(t *testing.T) {
	broker := newInferenceBroker(1)
	broker.platformProxyURL = "https://platform.example" + platformInferenceAPIPath
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSession(t, broker, prepared, broker.platformProxyURL)
	claimAndBindBrokerSession(t, broker, prepared["session_id"], "192.0.2.70", protocol.BenchVersionV6)

	broker.mu.RLock()
	session := broker.sessions[prepared["session_id"]]
	broker.mu.RUnlock()
	session.mu.Lock()
	session.inFlight = brokerPerSourceConcurrency
	session.mu.Unlock()
	body := &readTrackingBody{}
	request := httptest.NewRequest(http.MethodPost, "/v1/inference/v1/chat/completions", body)
	request.RemoteAddr = "192.0.2.70:4321"
	request.SetPathValue("rest", "v1/chat/completions")
	recorder := httptest.NewRecorder()
	broker.handle(recorder, request)
	if recorder.Code != http.StatusTooManyRequests {
		t.Fatalf("over-capacity status = %d", recorder.Code)
	}
	if body.reads != 0 {
		t.Fatalf("request body read %d time(s) before capacity rejection", body.reads)
	}
}

func TestInferenceBrokerHTTPServerSetsUntrustedListenerTimeouts(t *testing.T) {
	server := newInferenceBrokerHTTPServer(":11436", http.NewServeMux(), newInferenceBroker(1))
	if server.ReadHeaderTimeout != brokerReadHeaderTimeout ||
		server.ReadTimeout != brokerReadTimeout || server.WriteTimeout != brokerWriteTimeout ||
		server.IdleTimeout != brokerIdleTimeout || server.MaxHeaderBytes != brokerMaximumHeaderBytes {
		t.Fatalf("unexpected broker server limits: %+v", server)
	}
	if server.ConnContext == nil {
		t.Fatal("broker server did not snapshot source leases at connection acceptance")
	}
}

func TestInferenceBrokerListenerUsesDockerHostGatewayAddressFamily(t *testing.T) {
	listener, err := listenInferenceBrokerTCP4("127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	address, ok := listener.Addr().(*net.TCPAddr)
	if !ok || address.IP.To4() == nil {
		t.Fatalf("broker listener address = %v, want IPv4", listener.Addr())
	}
}

func TestInferenceBrokerPrunesUnactivatedSessionsBeforeCapacityCheck(t *testing.T) {
	broker := newInferenceBroker(1) // bounded to two prepared/active sessions
	first := prepareBrokerSession(t, broker)
	_ = prepareBrokerSession(t, broker)

	broker.mu.RLock()
	stale := broker.sessions[first["session_id"]]
	broker.mu.RUnlock()
	stale.mu.Lock()
	stale.preparedAt = time.Now().Add(-3 * time.Minute)
	stale.mu.Unlock()

	third := prepareBrokerSession(t, broker)
	if third["session_id"] == "" {
		t.Fatal("stale prepared session did not release broker capacity")
	}
}

func toolRouteRequest(
	t *testing.T,
	route registeredToolRoute,
	method, remoteAddr, capabilityCaseID, capabilityUserID, bodyCaseID, bodyUserID string,
) *http.Request {
	t.Helper()
	base := "http://broker.test/v1/tools/" + route.id + "/tool"
	endpoint := route.endpoint(base, capabilityCaseID, capabilityUserID)
	var body io.Reader
	if method == http.MethodPost {
		raw, err := json.Marshal(protocol.ToolExecRequest{
			CaseID: bodyCaseID, UserID: bodyUserID, Name: "search_web",
		})
		if err != nil {
			t.Fatal(err)
		}
		body = bytes.NewReader(raw)
	}
	request := httptest.NewRequest(method, endpoint, body)
	request.SetPathValue("id", route.id)
	request.RemoteAddr = remoteAddr
	return request
}

func TestLegacyToolRoutePreservesFrozenV8WireAndSourceBinding(t *testing.T) {
	broker := newInferenceBroker(1)
	called := 0
	var forwardedBody string
	route, stop, err := broker.registerTool(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		called++
		body, readErr := io.ReadAll(r.Body)
		if readErr != nil {
			t.Errorf("read forwarded body: %v", readErr)
		}
		forwardedBody = string(body)
		if r.URL.Path != "/tool" || r.URL.RawQuery != "" {
			t.Errorf("forwarded target path=%q query=%q", r.URL.Path, r.URL.RawQuery)
		}
		w.WriteHeader(http.StatusNoContent)
	}), "192.0.2.20", true, false)
	if err != nil {
		t.Fatal(err)
	}
	defer stop()

	const base = "http://broker.test/v1/tools/legacy-v8/tool"
	if got := route.endpoint(base, "v9-case", "v9-user"); got != base {
		t.Fatalf("legacy endpoint=%q want bare %q", got, base)
	}
	if got := route.healthEndpoint(base); got != base {
		t.Fatalf("legacy health endpoint=%q want bare %q", got, base)
	}

	health := httptest.NewRequest(http.MethodGet, base, nil)
	health.SetPathValue("id", route.id)
	health.RemoteAddr = "127.0.0.1:1234"
	healthRecorder := httptest.NewRecorder()
	broker.handleTool(healthRecorder, health)
	if healthRecorder.Code != http.StatusNoContent || called != 0 {
		t.Fatalf("legacy health status=%d called=%d", healthRecorder.Code, called)
	}

	// The v8 broker never parsed or coupled the request body to new query
	// identity fields. Preserve that byte-transparent behavior for old harnesses.
	const legacyBody = `{"case_id":"v8-case","name":"search_web"}`
	request := httptest.NewRequest(http.MethodPost, base, strings.NewReader(legacyBody))
	request.SetPathValue("id", route.id)
	request.RemoteAddr = "192.0.2.20:1234"
	recorder := httptest.NewRecorder()
	broker.handleTool(recorder, request)
	if recorder.Code != http.StatusNoContent || called != 1 || forwardedBody != legacyBody {
		t.Fatalf("legacy source status=%d called=%d body=%q", recorder.Code, called, forwardedBody)
	}

	// Passing allowNATFallback cannot weaken a legacy route because v8 has no
	// per-case capability to authenticate a translated source safely.
	request = httptest.NewRequest(http.MethodPost, base, strings.NewReader(legacyBody))
	request.SetPathValue("id", route.id)
	request.RemoteAddr = "192.168.65.1:1234"
	recorder = httptest.NewRecorder()
	broker.handleTool(recorder, request)
	if recorder.Code != http.StatusUnauthorized || called != 1 {
		t.Fatalf("legacy sibling source status=%d called=%d", recorder.Code, called)
	}
}

func TestToolRouteRequiresSourceAndCaseCapabilityInProduction(t *testing.T) {
	broker := newInferenceBroker(1)
	called := 0
	route, stop, err := broker.registerTool(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		called++
		if r.URL.Path != "/tool" {
			t.Errorf("forwarded path = %q", r.URL.Path)
		}
		if r.URL.RawQuery != "" {
			t.Errorf("capability query forwarded to tool handler: %q", r.URL.RawQuery)
		}
		w.WriteHeader(http.StatusNoContent)
	}), "192.0.2.20", false, true)
	if err != nil {
		t.Fatal(err)
	}

	request := toolRouteRequest(t, route, http.MethodPost, "192.0.2.21:1234", "case-a", "user-a", "case-a", "user-a")
	recorder := httptest.NewRecorder()
	broker.handleTool(recorder, request)
	if recorder.Code != http.StatusUnauthorized || called != 0 {
		t.Fatalf("sibling source status=%d called=%d", recorder.Code, called)
	}

	request = toolRouteRequest(t, route, http.MethodPost, "192.0.2.20:1234", "case-a", "user-a", "case-a", "user-a")
	recorder = httptest.NewRecorder()
	broker.handleTool(recorder, request)
	if recorder.Code != http.StatusNoContent || called != 1 {
		t.Fatalf("bound source status=%d called=%d", recorder.Code, called)
	}

	stop()
	recorder = httptest.NewRecorder()
	broker.handleTool(recorder, request)
	if recorder.Code != http.StatusNotFound {
		t.Fatalf("removed route status=%d", recorder.Code)
	}
}

func TestToolRouteHealthCheckRequiresDedicatedCapability(t *testing.T) {
	broker := newInferenceBroker(1)
	route, stop, err := broker.registerTool(http.NotFoundHandler(), "192.0.2.20", true, true)
	if err != nil {
		t.Fatal(err)
	}
	defer stop()

	request := httptest.NewRequest(http.MethodGet, "http://broker.test/v1/tools/"+route.id+"/tool", nil)
	request.SetPathValue("id", route.id)
	request.RemoteAddr = "127.0.0.1:1234"
	recorder := httptest.NewRecorder()
	broker.handleTool(recorder, request)
	if recorder.Code != http.StatusUnauthorized {
		t.Fatalf("unauthenticated health status=%d", recorder.Code)
	}

	request = httptest.NewRequest(http.MethodGet, route.healthEndpoint("http://broker.test/v1/tools/"+route.id+"/tool"), nil)
	request.SetPathValue("id", route.id)
	request.RemoteAddr = "127.0.0.1:1234"
	recorder = httptest.NewRecorder()
	broker.handleTool(recorder, request)
	if recorder.Code != http.StatusNoContent {
		t.Fatalf("authenticated health status=%d", recorder.Code)
	}
}

func TestToolRouteAllowsDockerDesktopNATOnlyWithCaseCapability(t *testing.T) {
	broker := newInferenceBroker(1)
	called := 0
	route, stop, err := broker.registerTool(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		called++
		w.WriteHeader(http.StatusNoContent)
	}), "172.30.0.2", true, true)
	if err != nil {
		t.Fatal(err)
	}
	defer stop()

	request := toolRouteRequest(t, route, http.MethodPost, "192.168.65.1:1234", "case-a", "user-a", "case-a", "user-a")
	recorder := httptest.NewRecorder()
	broker.handleTool(recorder, request)
	if recorder.Code != http.StatusNoContent || called != 1 {
		t.Fatalf("NAT capability status=%d called=%d", recorder.Code, called)
	}

	request = toolRouteRequest(t, route, http.MethodPost, "192.168.65.1:1234", "case-a", "user-a", "case-a", "user-a")
	query := request.URL.Query()
	query.Set("cap", "not-the-capability")
	request.URL.RawQuery = query.Encode()
	recorder = httptest.NewRecorder()
	broker.handleTool(recorder, request)
	if recorder.Code != http.StatusUnauthorized || called != 1 {
		t.Fatalf("NAT spoof status=%d called=%d", recorder.Code, called)
	}
}

func TestToolRouteCapabilityIsBoundToRunCaseAndUser(t *testing.T) {
	broker := newInferenceBroker(1)
	called := 0
	handler := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		called++
		w.WriteHeader(http.StatusNoContent)
	})
	runA, stopA, err := broker.registerTool(handler, "172.30.0.2", true, true)
	if err != nil {
		t.Fatal(err)
	}
	defer stopA()
	runB, stopB, err := broker.registerTool(handler, "172.30.0.3", true, true)
	if err != nil {
		t.Fatal(err)
	}
	defer stopB()

	tests := []struct {
		name               string
		capCase, capUser   string
		bodyCase, bodyUser string
		mutate             func(*http.Request)
	}{
		{name: "wrong case", capCase: "case-a", capUser: "user-a", bodyCase: "case-b", bodyUser: "user-a"},
		{name: "wrong user", capCase: "case-a", capUser: "user-a", bodyCase: "case-a", bodyUser: "user-b"},
		{name: "wrong run", capCase: "case-a", capUser: "user-a", bodyCase: "case-a", bodyUser: "user-a", mutate: func(r *http.Request) {
			r.SetPathValue("id", runB.id)
		}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			request := toolRouteRequest(t, runA, http.MethodPost, "192.168.65.1:1234", test.capCase, test.capUser, test.bodyCase, test.bodyUser)
			if test.mutate != nil {
				test.mutate(request)
			}
			recorder := httptest.NewRecorder()
			broker.handleTool(recorder, request)
			if recorder.Code != http.StatusUnauthorized || called != 0 {
				t.Fatalf("status=%d called=%d", recorder.Code, called)
			}
		})
	}
}

func TestToolRouteRejectsMalformedAndOversizedAuthenticatedBodies(t *testing.T) {
	broker := newInferenceBroker(1)
	route, stop, err := broker.registerTool(http.NotFoundHandler(), "172.30.0.2", true, true)
	if err != nil {
		t.Fatal(err)
	}
	defer stop()
	base := route.endpoint("http://broker.test/v1/tools/"+route.id+"/tool", "case-a", "user-a")

	for _, test := range []struct {
		name string
		body io.Reader
		want int
	}{
		{name: "malformed", body: strings.NewReader(`{"case_id":`), want: http.StatusBadRequest},
		{name: "oversized", body: strings.NewReader(strings.Repeat("x", toolRouteBodyLimit+1)), want: http.StatusRequestEntityTooLarge},
	} {
		t.Run(test.name, func(t *testing.T) {
			request := httptest.NewRequest(http.MethodPost, base, test.body)
			request.SetPathValue("id", route.id)
			request.RemoteAddr = "192.168.65.1:1234"
			recorder := httptest.NewRecorder()
			broker.handleTool(recorder, request)
			if recorder.Code != test.want {
				t.Fatalf("status=%d want=%d", recorder.Code, test.want)
			}
		})
	}
}

func TestToolRouteRejectsOverCapacityBeforeReadingBody(t *testing.T) {
	broker := newInferenceBroker(1)
	called := false
	registration, stop, err := broker.registerTool(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		called = true
	}), "192.0.2.80", false, true)
	if err != nil {
		t.Fatal(err)
	}
	defer stop()
	broker.mu.RLock()
	route := broker.tools[registration.id]
	broker.mu.RUnlock()
	for range brokerPerSourceConcurrency {
		route.slots <- struct{}{}
	}
	body := &readTrackingBody{}
	endpoint := registration.endpoint("http://broker.test/v1/tools/"+registration.id+"/tool", "case-a", "user-a")
	request := httptest.NewRequest(http.MethodPost, endpoint, body)
	request.SetPathValue("id", registration.id)
	request.RemoteAddr = "192.0.2.80:1234"
	recorder := httptest.NewRecorder()
	broker.handleTool(recorder, request)
	if recorder.Code != http.StatusTooManyRequests || called || body.reads != 0 {
		t.Fatalf("over-capacity tool status=%d called=%t body_reads=%d", recorder.Code, called, body.reads)
	}
}

// TestV7BrokerServesBYOKShapedRequests exercises the exact request a harness
// built against the pre-v7 BYOK contract emits once OPENAI_BASE_URL /
// OPENROUTER_BASE_URL are aliased to the broker gateway: base URL
// ".../v1/inference" plus the client's own "/chat/completions" suffix, carrying
// whatever key the client found in its environment.
//
// Two invariants are pinned:
//   - the aliased path shape is served (no miner change required), and
//   - the caller's Authorization header is ignored and replaced by the
//     ticket-bound platform bearer, so the placeholder key handed to the sandbox
//     grants nothing and is worthless if exfiltrated.
func TestV7BrokerServesBYOKShapedRequests(t *testing.T) {
	const profile = "openrouter-route-0123456789abcdef-v1"
	var seenAuthorization []string
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seenAuthorization = append(seenAuthorization, r.Header.Get("Authorization"))
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"usage":{"prompt_tokens":3,"completion_tokens":4},"choices":[{"message":{"content":"OK"}}]}`))
	}))
	defer upstream.Close()

	broker := newInferenceBroker(1)
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSessionFor(t, broker, prepared, proxyURL, "groq", profile, llm.V7HarnessModel)
	claimAndBindBrokerSession(t, broker, prepared["session_id"], "192.0.2.71", protocol.BenchVersionV8)

	// The BYOK suffix: an OpenAI/OpenRouter client appends /chat/completions to
	// its configured base URL, which under the alias is ".../v1/inference".
	request := httptest.NewRequest(http.MethodPost, "/v1/inference/chat/completions",
		bytes.NewBufferString(`{"model":"`+llm.V7HarnessModel+`"}`))
	request.RemoteAddr = "192.0.2.71:4321"
	request.SetPathValue("rest", "chat/completions")
	// A harness may send the injected placeholder, a stale real OpenRouter key,
	// or nothing at all. None of it may reach or influence the upstream.
	request.Header.Set("Authorization", "Bearer sk-or-v1-attacker-supplied")
	recorder := httptest.NewRecorder()
	broker.handle(recorder, request)

	if recorder.Code != http.StatusOK {
		t.Fatalf("BYOK-shaped request status = %d, want 200: %s", recorder.Code, recorder.Body.String())
	}
	if len(seenAuthorization) != 1 {
		t.Fatalf("upstream deliveries = %d, want 1", len(seenAuthorization))
	}
	if seenAuthorization[0] != "Bearer platform-bearer-never-given-to-harness" {
		t.Fatalf("upstream Authorization = %q; the broker must substitute the ticket bearer", seenAuthorization[0])
	}
	if strings.Contains(seenAuthorization[0], "attacker-supplied") {
		t.Fatal("the caller's key reached the platform proxy")
	}
}

// --- transient and integrity failures both fail closed --------------------
//
// Both transient provider faults and integrity faults park the run. The
// accounting must distinguish them without issuing another paid request.

func TestV7ChatParksOneTransientProviderFault(t *testing.T) {
	var attempts atomic.Int64
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		if attempts.Add(1) == 1 {
			http.Error(w, "transient", http.StatusServiceUnavailable)
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"choices": []map[string]any{{"message": map[string]string{"content": "ok"}}},
			"usage":   map[string]int{"prompt_tokens": 11, "completion_tokens": 7},
		})
	}))
	defer upstream.Close()

	broker := newInferenceBroker(1)
	broker.sleep = func(context.Context, time.Duration) error { return nil }
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSessionFor(t, broker, prepared, proxyURL, "openrouter", "openrouter-route-0123456789abcdef-v1", llm.V7HarnessModel)
	claimAndBindBrokerSession(t, broker, prepared["session_id"], "192.0.2.90", protocol.BenchVersionV8)
	start, err := broker.snapshot(prepared["session_id"])
	if err != nil {
		t.Fatal(err)
	}

	request := httptest.NewRequest(http.MethodPost, "/v1/inference/id/v1/chat/completions", bytes.NewBufferString(`{"model":"openai/gpt-oss-20b"}`))
	request.RemoteAddr = "192.0.2.90:4321"
	request.SetPathValue("rest", "v1/chat/completions")
	recorder := httptest.NewRecorder()
	broker.handle(recorder, request)
	if recorder.Code != http.StatusBadGateway {
		t.Fatalf("transient fault was not parked: status=%d body=%s", recorder.Code, recorder.Body.String())
	}
	if attempts.Load() != 1 {
		t.Fatalf("platform deliveries=%d, want 1", attempts.Load())
	}

	end, err := broker.snapshot(prepared["session_id"])
	if err != nil {
		t.Fatal(err)
	}
	if err := relayDegradedSince(start, end); err == nil {
		t.Fatal("transient provider fault did not fail the run closed")
	}
	usage, err := relayUsageSince(start, end)
	if err != nil {
		t.Fatal(err)
	}
	if usage.Successes != 0 {
		t.Fatalf("terminal fault usage was not preserved: %+v", usage)
	}
	if usage.PromptTokens != 0 || usage.CompletionTokens != 0 || usage.TotalTokens != 0 {
		t.Fatalf("failed attempt leaked into observed usage: %+v", usage)
	}
	execution, err := relayExecutionSince(start, end)
	if err != nil {
		t.Fatal(err)
	}
	if execution.Requests != 1 || execution.UpstreamAttempts != 1 || execution.Retries != 0 ||
		execution.InfrastructureFailures != 1 || execution.GrantDenials != 0 {
		t.Fatalf("single-shot ledger = %+v", execution)
	}
}

// TestRetriedRunReportsIdenticalObservedUsageToACleanRun answers the accounting
// question directly: a miner must never be charged for a provider fault. The
// retried run and the clean run see the same provider usage, so every accounted
// field of their scored TokenUsage must be identical; only the attempt ledger
// may differ.
func TestSingleShotRunReportsCleanObservedUsage(t *testing.T) {
	const retries = 0
	run := func(t *testing.T, faults int, sourceIP string) (protocol.TokenUsage, relayExecutionSummary) {
		t.Helper()
		var attempts atomic.Int64
		upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			if attempts.Add(1) <= int64(faults) {
				http.Error(w, "transient", http.StatusBadGateway)
				return
			}
			writeJSON(w, http.StatusOK, map[string]any{
				"choices": []map[string]any{{"message": map[string]string{"content": "ok"}}},
				"usage":   map[string]int{"prompt_tokens": 23, "completion_tokens": 5},
			})
		}))
		defer upstream.Close()

		broker := newInferenceBroker(1)
		broker.sleep = func(context.Context, time.Duration) error { return nil }
		proxyURL := configureBrokerUpstream(broker, upstream)
		prepared := prepareBrokerSession(t, broker)
		activateBrokerSessionFor(t, broker, prepared, proxyURL, "openrouter", "openrouter-route-0123456789abcdef-v1", llm.V7HarnessModel)
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
		if recorder.Code != http.StatusOK {
			t.Fatalf("run with %d fault(s) did not complete: %d", faults, recorder.Code)
		}
		end, err := broker.snapshot(prepared["session_id"])
		if err != nil {
			t.Fatal(err)
		}
		usage, err := relayUsageSince(start, end)
		if err != nil {
			t.Fatal(err)
		}
		execution, err := relayExecutionSince(start, end)
		if err != nil {
			t.Fatal(err)
		}
		return usage, execution
	}

	cleanUsage, cleanExecution := run(t, 0, "192.0.2.91")
	retriedUsage, retriedExecution := run(t, retries, "192.0.2.92")

	// The figures that feed efficiency: identical. Retried attempts are not
	// charged to the miner, so they book zero usage — token counts, the
	// request and success counts, and usage availability all match the clean
	// run exactly.
	//
	// ProviderLatencyMs is deliberately excluded from the comparison: it is
	// measured wall clock against the httptest upstream, so it drifts by a
	// millisecond or two between two otherwise identical runs (routinely so
	// under -race). It is no part of the accounting invariant this test
	// protects. Every other field stays exactly as strict as before.
	cleanAccounted, retriedAccounted := cleanUsage, retriedUsage
	cleanAccounted.ProviderLatencyMs = 0
	retriedAccounted.ProviderLatencyMs = 0
	if cleanAccounted != retriedAccounted {
		t.Fatalf("retries changed observed usage:\n clean   = %+v\n retried = %+v", cleanAccounted, retriedAccounted)
	}
	// The audit ledger: different, and by exactly the number of retries.
	if cleanExecution.Retries != 0 || cleanExecution.UpstreamAttempts != 1 {
		t.Fatalf("clean run ledger = %+v", cleanExecution)
	}
	if retriedExecution.Retries != retries || retriedExecution.UpstreamAttempts != retries+1 {
		t.Fatalf("retried run ledger = %+v, want %d retries", retriedExecution, retries)
	}
	if retriedExecution.Requests != cleanExecution.Requests {
		t.Fatal("retries inflated the logical request count")
	}
}

// TestV7EmbeddingParksOneTransientPlatformFault covers the lane that carries
// roughly two thirds of a v7 run's inference requests.
func TestV7EmbeddingParksOneTransientPlatformFault(t *testing.T) {
	vector := make([]float64, embeddingDimensions)
	var attempts atomic.Int64
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		if attempts.Add(1) == 1 {
			http.Error(w, "transient", http.StatusServiceUnavailable)
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"object": "list", "model": hostedEmbeddingModel,
			"data":  []map[string]any{{"object": "embedding", "index": 0, "embedding": vector}},
			"usage": map[string]int{"prompt_tokens": 4, "total_tokens": 4},
		})
	}))
	defer upstream.Close()

	broker := newInferenceBroker(1)
	broker.sleep = func(context.Context, time.Duration) error { return nil }
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSessionFor(t, broker, prepared, proxyURL, "openrouter", "openrouter-route-0123456789abcdef-v1", llm.V7HarnessModel)
	runID := claimAndBindBrokerSession(t, broker, prepared["session_id"], "192.0.2.93", protocol.BenchVersionV8)
	if !broker.beginEmbeddingPhase(prepared["session_id"], runID) {
		t.Fatal("failed to admit v7 embedding phase")
	}
	defer broker.endEmbeddingPhase(prepared["session_id"], runID)
	start, err := broker.snapshot(prepared["session_id"])
	if err != nil {
		t.Fatal(err)
	}

	response := callEmbedding(broker, "192.0.2.93", "hosted text")
	if response.Code != http.StatusBadGateway {
		t.Fatalf("transient embedding fault was not parked: status=%d body=%s", response.Code, response.Body.String())
	}
	if attempts.Load() != 1 {
		t.Fatalf("embedding deliveries=%d, want 1", attempts.Load())
	}
	end, err := broker.snapshot(prepared["session_id"])
	if err != nil {
		t.Fatal(err)
	}
	if err := relayDegradedSince(start, end); err == nil {
		t.Fatal("terminal embedding fault did not fail the run closed")
	}
	execution, err := relayExecutionSince(start, end)
	if err != nil {
		t.Fatal(err)
	}
	if execution.EmbeddingRetries != 0 || execution.InfrastructureFailures != 1 {
		t.Fatalf("embedding single-shot ledger = %+v", execution)
	}
}

// TestV7EmbeddingIntegrityFaultIsNeverRetriedAndFailsClosed is the other
// direction. A provider that answers 200 with the WRONG model is an integrity
// violation. Repeating it could not help,
// so it must be delivered once and must still discard the run.
func TestV7EmbeddingIntegrityFaultIsNeverRetriedAndFailsClosed(t *testing.T) {
	vector := make([]float64, embeddingDimensions)
	var attempts atomic.Int64
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		attempts.Add(1)
		writeJSON(w, http.StatusOK, map[string]any{
			"object": "list", "model": "some-other/embedding-model",
			"data":  []map[string]any{{"object": "embedding", "index": 0, "embedding": vector}},
			"usage": map[string]int{"prompt_tokens": 4, "total_tokens": 4},
		})
	}))
	defer upstream.Close()

	broker := newInferenceBroker(1)
	broker.sleep = func(context.Context, time.Duration) error { return nil }
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSessionFor(t, broker, prepared, proxyURL, "openrouter", "openrouter-route-0123456789abcdef-v1", llm.V7HarnessModel)
	runID := claimAndBindBrokerSession(t, broker, prepared["session_id"], "192.0.2.94", protocol.BenchVersionV8)
	if !broker.beginEmbeddingPhase(prepared["session_id"], runID) {
		t.Fatal("failed to admit v7 embedding phase")
	}
	defer broker.endEmbeddingPhase(prepared["session_id"], runID)
	start, err := broker.snapshot(prepared["session_id"])
	if err != nil {
		t.Fatal(err)
	}

	if response := callEmbedding(broker, "192.0.2.94", "hosted text"); response.Code != http.StatusBadGateway {
		t.Fatalf("integrity fault status=%d, want 502", response.Code)
	}
	if attempts.Load() != 1 {
		t.Fatalf("integrity fault was retried: deliveries=%d, want 1", attempts.Load())
	}
	end, err := broker.snapshot(prepared["session_id"])
	if err != nil {
		t.Fatal(err)
	}
	if end.InfrastructureFailures != 1 || end.EmbeddingRetries != 0 {
		t.Fatalf("integrity fault accounting = %+v", end)
	}
	if err := relayDegradedSince(start, end); err == nil {
		t.Fatal("an integrity fault no longer fails the run closed")
	}
}

// --- lost lease is not a provider fault -----------------------------------

// TestPlatformGrantDenialIsNotCountedAsAnUpstreamProviderFault pins the
// misdiagnosis that started this. The platform answers 429 only when it
// declines to reserve capacity for the grant -- a revoked lease, a rewritten or
// passed ticket deadline, an exhausted budget. It converts every real provider
// rejection into 502 instead, so a 429 here is never a provider rate limit.
// It must be counted as a grant denial, reported by name, and never retried.
func TestPlatformGrantDenialIsNotCountedAsAnUpstreamProviderFault(t *testing.T) {
	for _, lane := range []string{"chat", "embedding"} {
		t.Run(lane, func(t *testing.T) {
			var attempts atomic.Int64
			upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				attempts.Add(1)
				http.Error(w, "inference grant unavailable", http.StatusTooManyRequests)
			}))
			defer upstream.Close()

			broker := newInferenceBroker(1)
			broker.sleep = func(context.Context, time.Duration) error { return nil }
			proxyURL := configureBrokerUpstream(broker, upstream)
			prepared := prepareBrokerSession(t, broker)
			activateBrokerSessionFor(t, broker, prepared, proxyURL, "openrouter", "openrouter-route-0123456789abcdef-v1", llm.V7HarnessModel)
			sourceIP := "192.0.2.95"
			if lane == "embedding" {
				sourceIP = "192.0.2.96"
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
				// The harness-visible response is unchanged from before this
				// change: the run is discarded either way, and its remaining
				// requests must not observe a different gateway contract.
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

			// Never retried: the grant is gone, so another delivery cannot
			// succeed and would burn a fresh reservation against a dead lease.
			if attempts.Load() != 1 {
				t.Fatalf("a denied grant was retried: deliveries=%d, want 1", attempts.Load())
			}
			end, err := broker.snapshot(prepared["session_id"])
			if err != nil {
				t.Fatal(err)
			}
			if end.GrantDenials != 1 {
				t.Fatalf("grant denial was not recorded: %+v", end)
			}
			if end.InfrastructureFailures != 0 {
				t.Fatalf("a lost lease was miscounted as an upstream provider fault: %+v", end)
			}
			// Only the diagnosis changes; the run still fails closed.
			degraded := relayDegradedSince(start, end)
			if degraded == nil {
				t.Fatal("a denied grant no longer fails the run closed")
			}
			if strings.Contains(degraded.Error(), "upstream failure") {
				t.Fatalf("a lost lease is still reported as a provider fault: %v", degraded)
			}
			if !strings.Contains(degraded.Error(), "declined this run's inference grant") {
				t.Fatalf("grant denial was not reported by name: %v", degraded)
			}
		})
	}
}

// TestLegacyRelayStillTreatsA429AsAProviderFault guards the exception. The
// frozen model-relay forwards a real provider 429 verbatim, so on that path a
// 429 IS an upstream fault and its historical accounting must not move.
func TestLegacyRelayStillTreatsA429AsAProviderFault(t *testing.T) {
	if platformDeniesGrant("http://host.docker.internal:11434", http.StatusTooManyRequests) {
		t.Fatal("a legacy relay 429 was reclassified as a platform grant denial")
	}
	if !platformDeniesGrant("", http.StatusTooManyRequests) {
		t.Fatal("a ticket-path 429 was not classified as a platform grant denial")
	}
	if platformDeniesGrant("", http.StatusBadGateway) {
		t.Fatal("a platform 502 must stay an upstream provider fault")
	}
}

// TestPlatformDeclineCodeIsAdvisoryAndFailsSoft pins the parser's contract. It
// may only ever ADD information: the fleet runs pinned builds against a
// platform that may be older or newer, so a body this cannot understand must
// leave every status-only decision exactly as it was.
func TestPlatformDeclineCodeIsAdvisoryAndFailsSoft(t *testing.T) {
	for name, testCase := range map[string]struct {
		body []byte
		want int
	}{
		"revoked":        {[]byte(`{"error_code":4101,"message":"x"}`), platformDeclineGrantRevoked},
		"exhausted":      {[]byte(`{"error_code":4102,"message":"x"}`), platformDeclineBudgetExhausted},
		"at capacity":    {[]byte(`{"error_code":4103}`), platformDeclineAtCapacity},
		"unspecified":    {[]byte(`{"error_code":4100}`), platformDeclineUnspecified},
		"older platform": {[]byte(`{"detail":"inference grant unavailable"}`), 0},
		"unknown code":   {[]byte(`{"error_code":9999}`), 0},
		"not json":       {[]byte(`inference grant unavailable`), 0},
		"empty":          {nil, 0},
	} {
		t.Run(name, func(t *testing.T) {
			if got := platformDeclineCode(testCase.body); got != testCase.want {
				t.Fatalf("platformDeclineCode=%d, want %d", got, testCase.want)
			}
		})
	}
}

// A full provider lane is an infrastructure failure, but it is still terminal
// for this attempt. The enclosing ticket is the manual retry boundary.
func TestChatCapacityDeclineParksTheRun(t *testing.T) {
	var attempts atomic.Int64
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		if attempts.Add(1) <= 3 {
			w.Header().Set("Retry-After", "1")
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusServiceUnavailable)
			_, _ = w.Write([]byte(`{"error_code":4103,"message":"inference lane is at capacity"}`))
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"id":"c","object":"chat.completion","created":1,"model":"openai/gpt-oss-20b","choices":[{"index":0,"finish_reason":"stop","message":{"role":"assistant","content":"ok"}}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}`))
	}))
	defer upstream.Close()

	broker := newInferenceBroker(1)
	broker.sleep = func(context.Context, time.Duration) error { return nil }
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSessionFor(t, broker, prepared, proxyURL, "openrouter", "openrouter-route-0123456789abcdef-v1", llm.V7HarnessModel)
	sourceIP := "192.0.2.97"
	claimAndBindBrokerSession(t, broker, prepared["session_id"], sourceIP, protocol.BenchVersionV8)

	request := httptest.NewRequest(http.MethodPost, "/v1/inference/id/v1/chat/completions", bytes.NewBufferString(`{"model":"openai/gpt-oss-20b"}`))
	request.RemoteAddr = sourceIP + ":4321"
	request.SetPathValue("rest", "v1/chat/completions")
	recorder := httptest.NewRecorder()
	broker.handle(recorder, request)

	if recorder.Code != http.StatusBadGateway {
		t.Fatalf("capacity decline did not park the request: status=%d", recorder.Code)
	}
	if attempts.Load() != 1 {
		t.Fatalf("capacity failure was redispatched: deliveries=%d", attempts.Load())
	}
	end, err := broker.snapshot(prepared["session_id"])
	if err != nil {
		t.Fatal(err)
	}
	if end.GrantDenials != 0 {
		t.Fatalf("backpressure was miscounted as a lost lease: %+v", end)
	}
	if end.InfrastructureFailures != 1 || end.CapacityExhaustions != 1 {
		t.Fatalf("capacity failure was not recorded as terminal infrastructure: %+v", end)
	}
}

// TestChatCapacityWaitingIsBounded stops the previous test's tolerance from
// becoming a way to hold a check open forever. A platform pinned at zero
// headroom must surface as a bounded failure, not an unbounded stall.
func TestChatCapacityWaitingIsBounded(t *testing.T) {
	var attempts atomic.Int64
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		attempts.Add(1)
		w.Header().Set("Retry-After", "1")
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = w.Write([]byte(`{"error_code":4103}`))
	}))
	defer upstream.Close()

	broker := newInferenceBroker(1)
	broker.sleep = func(context.Context, time.Duration) error { return nil }
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSessionFor(t, broker, prepared, proxyURL, "openrouter", "openrouter-route-0123456789abcdef-v1", llm.V7HarnessModel)
	sourceIP := "192.0.2.98"
	claimAndBindBrokerSession(t, broker, prepared["session_id"], sourceIP, protocol.BenchVersionV8)

	request := httptest.NewRequest(http.MethodPost, "/v1/inference/id/v1/chat/completions", bytes.NewBufferString(`{"model":"openai/gpt-oss-20b"}`))
	request.RemoteAddr = sourceIP + ":4321"
	request.SetPathValue("rest", "v1/chat/completions")
	recorder := httptest.NewRecorder()
	broker.handle(recorder, request)

	if recorder.Code != http.StatusBadGateway {
		t.Fatalf("an endlessly-full lane did not fail closed: status=%d", recorder.Code)
	}
	if got := attempts.Load(); got != 1 {
		t.Fatalf("capacity failure was redispatched: deliveries=%d", got)
	}
	end, err := broker.snapshot(prepared["session_id"])
	if err != nil {
		t.Fatal(err)
	}
	if end.GrantDenials != 0 {
		t.Fatalf("backpressure was miscounted as a lost lease: %+v", end)
	}
}

// A capacity refusal parks immediately; a later success needs a manual ticket.
func TestChatCapacityFailureDoesNotWaitOutMinuteRateWindow(t *testing.T) {
	var attempts atomic.Int64
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		if attempts.Add(1) <= 60 {
			w.Header().Set("Retry-After", "1")
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusServiceUnavailable)
			_, _ = w.Write([]byte(`{"error_code":4103,"message":"inference lane is at capacity"}`))
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"id":"c","object":"chat.completion","created":1,"model":"openai/gpt-oss-20b","choices":[{"index":0,"finish_reason":"stop","message":{"role":"assistant","content":"ok"}}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}`))
	}))
	defer upstream.Close()

	broker := newInferenceBroker(1)
	broker.sleep = func(context.Context, time.Duration) error { return nil }
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSessionFor(t, broker, prepared, proxyURL, "openrouter", "openrouter-route-0123456789abcdef-v1", llm.V7HarnessModel)
	sourceIP := "192.0.2.99"
	claimAndBindBrokerSession(t, broker, prepared["session_id"], sourceIP, protocol.BenchVersionV8)

	request := httptest.NewRequest(http.MethodPost, "/v1/inference/id/v1/chat/completions", bytes.NewBufferString(`{"model":"openai/gpt-oss-20b"}`))
	request.RemoteAddr = sourceIP + ":4321"
	request.SetPathValue("rest", "v1/chat/completions")
	recorder := httptest.NewRecorder()
	broker.handle(recorder, request)

	if recorder.Code != http.StatusBadGateway {
		t.Fatalf("capacity failure did not park the request: status=%d", recorder.Code)
	}
	if attempts.Load() != 1 {
		t.Fatalf("deliveries=%d, want one", attempts.Load())
	}
	end, err := broker.snapshot(prepared["session_id"])
	if err != nil {
		t.Fatal(err)
	}
	if end.CapacityExhaustions != 1 {
		t.Fatalf("capacity failure was not classified as saturation: %+v", end)
	}
}

// Session-scoped v10+ tool provenance: with no case window open, overlapping
// tool_endpoint requests racing for the SAME model emission must consume it
// exactly once. The consumed flag is the double-spend guard; every loser is
// rejected before the mock tool runs and booked as a duplicate on its own case.
func TestToolRouteSessionProvenanceConsumesOneEmissionUnderConcurrentRun(t *testing.T) {
	broker := newInferenceBroker(1)
	const sessionID = "v10-session-race"
	session := &brokerSession{benchVersion: protocol.BenchVersionV10}
	broker.mu.Lock()
	broker.sessions[sessionID] = session
	broker.mu.Unlock()
	session.mu.Lock()
	recordModelToolCallsLocked(session, 0, []byte(`{"choices":[{"message":{"tool_calls":[{
		"id":"call-1","type":"function","function":{"name":"search_web","arguments":"{\"query\":\"veltrix\"}"}
	}]}}]}`))
	session.mu.Unlock()

	var forwarded atomic.Int32
	const racers = 16
	route, stop, err := broker.registerToolRoute(
		http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			forwarded.Add(1)
			w.WriteHeader(http.StatusNoContent)
		}),
		"192.0.2.20", false, true, sessionID, racers,
	)
	if err != nil {
		t.Fatal(err)
	}
	defer stop()

	var wg sync.WaitGroup
	codes := make([]int, racers)
	for i := 0; i < racers; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			caseID := "case-" + strconv.Itoa(i)
			raw, _ := json.Marshal(protocol.ToolExecRequest{
				CaseID: caseID, UserID: "user-a", Name: "search_web",
				Args: json.RawMessage(`{"query":"veltrix"}`),
			})
			endpoint := route.endpoint("http://broker.test/v1/tools/"+route.id+"/tool", caseID, "user-a")
			request := httptest.NewRequest(http.MethodPost, endpoint, bytes.NewReader(raw))
			request.SetPathValue("id", route.id)
			request.RemoteAddr = "192.0.2.20:1234"
			recorder := httptest.NewRecorder()
			broker.handleTool(recorder, request)
			codes[i] = recorder.Code
		}(i)
	}
	wg.Wait()
	accepted, rejected := 0, 0
	for _, code := range codes {
		switch code {
		case http.StatusNoContent:
			accepted++
		case http.StatusConflict:
			rejected++
		default:
			t.Fatalf("unexpected status %d in %v", code, codes)
		}
	}
	if accepted != 1 || rejected != racers-1 || forwarded.Load() != 1 {
		t.Fatalf("accepted=%d rejected=%d forwarded=%d codes=%v", accepted, rejected, forwarded.Load(), codes)
	}
	totals, ok := broker.sessionToolProvenanceTotals(sessionID)
	if !ok || totals != (sessionToolProvenanceTotals{ModelEmitted: 1, Consumed: 1}) {
		t.Fatalf("totals=%+v ok=%t", totals, ok)
	}
	matchedCases, duplicateCases := 0, 0
	for i := 0; i < racers; i++ {
		evidence := broker.sessionToolProvenance(sessionID, "case-"+strconv.Itoa(i))
		if evidence == nil || evidence.EndpointAttempts != 1 || !evidence.Complete {
			t.Fatalf("case-%d evidence=%+v", i, evidence)
		}
		switch {
		case evidence.Matched == 1 && evidence.Unmatched == 0:
			matchedCases++
		case evidence.Matched == 0 && evidence.Unmatched == 1:
			duplicateCases++
		default:
			t.Fatalf("case-%d evidence=%+v", i, evidence)
		}
	}
	if matchedCases != 1 || duplicateCases != racers-1 {
		t.Fatalf("matched cases=%d duplicate cases=%d", matchedCases, duplicateCases)
	}
}

// A pre-v10 route registration never binds provenance, so the frozen v9 tool
// wire forwards without touching the session ledger even when a v10-capable
// session id is supplied by mistake: the legacy handler must stay byte-for-byte.
func TestToolRouteWithoutProvenanceLeavesSessionLedgerUntouched(t *testing.T) {
	broker := newInferenceBroker(1)
	const sessionID = "v10-session-unbound"
	session := &brokerSession{benchVersion: protocol.BenchVersionV10}
	broker.mu.Lock()
	broker.sessions[sessionID] = session
	broker.mu.Unlock()
	route, stop, err := broker.registerToolWithProvenance(
		http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusNoContent) }),
		"192.0.2.20", false, true, "",
	)
	if err != nil {
		t.Fatal(err)
	}
	defer stop()
	raw, _ := json.Marshal(protocol.ToolExecRequest{
		CaseID: "case-a", UserID: "user-a", Name: "search_web", Args: json.RawMessage(`{}`),
	})
	endpoint := route.endpoint("http://broker.test/v1/tools/"+route.id+"/tool", "case-a", "user-a")
	request := httptest.NewRequest(http.MethodPost, endpoint, bytes.NewReader(raw))
	request.SetPathValue("id", route.id)
	request.RemoteAddr = "192.0.2.20:1234"
	recorder := httptest.NewRecorder()
	broker.handleTool(recorder, request)
	if recorder.Code != http.StatusNoContent {
		t.Fatalf("unbound route status=%d", recorder.Code)
	}
	evidence := broker.sessionToolProvenance(sessionID, "case-a")
	if evidence == nil || evidence.EndpointAttempts != 0 || !evidence.Complete {
		t.Fatalf("unbound route must not book attempts: %+v", evidence)
	}
}

func TestInferenceBrokerStampsTraceContextForRelayCapture(t *testing.T) {
	var seen []traceContext
	var mu sync.Mutex
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw := r.Header.Get(traceContextHeader)
		if raw == "" {
			t.Errorf("missing %s", traceContextHeader)
		}
		var tc traceContext
		if err := json.Unmarshal([]byte(raw), &tc); err != nil {
			t.Errorf("trace context is not JSON: %v", err)
		}
		mu.Lock()
		seen = append(seen, tc)
		mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"usage":{"prompt_tokens":3,"completion_tokens":4},"choices":[]}`))
	}))
	defer upstream.Close()
	broker := newInferenceBroker(1)
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSession(t, broker, prepared, proxyURL)
	sessionID := prepared["session_id"]
	runID := claimAndBindBrokerSession(t, broker, sessionID, "192.0.2.10", protocol.BenchVersionV6)

	call := func(claim string) {
		t.Helper()
		request := httptest.NewRequest(http.MethodPost, "/v1/inference/id/v1/chat/completions", bytes.NewBufferString(`{"model":"qwen/qwen3-32b","max_tokens":32}`))
		request.RemoteAddr = "192.0.2.10:4321"
		request.SetPathValue("rest", "v1/chat/completions")
		if claim != "" {
			request.Header.Set(harnessCaseHeader, claim)
		}
		recorder := httptest.NewRecorder()
		broker.handle(recorder, request)
		if recorder.Code != http.StatusOK {
			t.Fatalf("proxy status = %d: %s", recorder.Code, recorder.Body.String())
		}
	}

	// 1. One case in flight: attributed exactly without any harness claim.
	if _, started := broker.beginRunCase(sessionID, "web_search-0001"); !started {
		t.Fatal("beginRunCase refused a live session")
	}
	call("")
	// 2. Two in flight: only a candidate set, unless the harness claims one.
	broker.beginRunCase(sessionID, "web_search-0002")
	call("")
	call("web_search-0002")
	call("not-in-flight")
	broker.endRunCase(sessionID, "web_search-0001")
	broker.endRunCase(sessionID, "web_search-0002")
	// 3. Nothing in flight: run/agent context only.
	call("")

	mu.Lock()
	defer mu.Unlock()
	if len(seen) != 5 {
		t.Fatalf("want 5 upstream calls, got %d", len(seen))
	}
	for i, tc := range seen {
		if tc.Version != 1 || tc.RunID != runID || tc.SessionID != sessionID || tc.BenchVersion != protocol.BenchVersionV6 {
			t.Fatalf("call %d envelope: %+v", i, tc)
		}
	}
	if seen[0].CaseID != "web_search-0001" || seen[0].CaseSource != "in_flight" || !seen[0].CaseVerified {
		t.Fatalf("single in-flight case must attribute exactly: %+v", seen[0])
	}
	if seen[1].CaseID != "" || len(seen[1].CasesInFlight) != 2 || seen[1].CaseVerified {
		t.Fatalf("two in flight without a claim is a candidate set: %+v", seen[1])
	}
	if seen[2].CaseID != "web_search-0002" || seen[2].CaseSource != "claim" || !seen[2].CaseVerified {
		t.Fatalf("a claim naming an in-flight case is verified: %+v", seen[2])
	}
	if seen[3].CaseID != "not-in-flight" || seen[3].CaseSource != "claim" || seen[3].CaseVerified {
		t.Fatalf("a claim naming an unknown case is recorded but unverified: %+v", seen[3])
	}
	if seen[4].CaseID != "" || len(seen[4].CasesInFlight) != 0 || seen[4].ClaimedCaseID != "" {
		t.Fatalf("nothing in flight: %+v", seen[4])
	}
}
