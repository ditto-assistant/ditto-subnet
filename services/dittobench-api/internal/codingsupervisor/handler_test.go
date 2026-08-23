package codingsupervisor

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
)

const fixtureToken = "coding-supervisor-control-token-000000000000"

type fixtureVector struct {
	Requests          map[string]json.RawMessage `json:"requests"`
	Responses         map[string]json.RawMessage `json:"responses"`
	AuthoringEvidence json.RawMessage
	TaskEvidence      json.RawMessage
}

type backendFunc func(context.Context, Request) (Response, error)

func (function backendFunc) Execute(ctx context.Context, request Request) (Response, error) {
	return function(ctx, request)
}

func loadFixtureVector(t *testing.T) fixtureVector {
	t.Helper()
	body, err := os.ReadFile(filepath.Join(
		"..", "..", "..", "..", "packages", "dittobench-coding-contract", "testdata",
		"coding_attempt_supervisor_v1.json",
	))
	if err != nil {
		t.Fatal(err)
	}
	var vector fixtureVector
	if err := json.Unmarshal(body, &vector); err != nil {
		t.Fatal(err)
	}
	freezeBody, err := os.ReadFile(filepath.Join(
		"..", "..", "..", "..", "packages", "dittobench-coding-contract", "testdata",
		"coding_authoring_freeze_v1.json",
	))
	if err != nil {
		t.Fatal(err)
	}
	var freeze struct {
		Request struct {
			Evidence json.RawMessage `json:"evidence"`
		} `json:"request"`
	}
	if err := json.Unmarshal(freezeBody, &freeze); err != nil {
		t.Fatal(err)
	}
	contractBody, err := os.ReadFile(filepath.Join(
		"..", "..", "..", "..", "packages", "dittobench-coding-contract", "testdata",
		"coding_contract_v1.json",
	))
	if err != nil {
		t.Fatal(err)
	}
	var contract struct {
		TaskEvidence json.RawMessage `json:"task_evidence"`
	}
	if err := json.Unmarshal(contractBody, &contract); err != nil {
		t.Fatal(err)
	}
	vector.AuthoringEvidence = append(json.RawMessage(nil), freeze.Request.Evidence...)
	var taskEvidence codingcontract.TaskEvidence
	if err := json.Unmarshal(contract.TaskEvidence, &taskEvidence); err != nil {
		t.Fatal(err)
	}
	taskEvidence.CodingRunID = "coding-run-supervisor-001"
	taskEvidence.ValidatorTicketID = "22222222-2222-4222-8222-222222222222"
	vector.TaskEvidence, err = json.Marshal(taskEvidence)
	if err != nil {
		t.Fatal(err)
	}
	return vector
}

func vectorResponse(t *testing.T, vector fixtureVector, operation string) Response {
	t.Helper()
	var response Response
	if err := json.Unmarshal(vector.Responses[operation], &response); err != nil {
		t.Fatal(err)
	}
	if response.Authoring != nil {
		response.Authoring.Evidence = append(json.RawMessage(nil), vector.AuthoringEvidence...)
		var evidence codingcontract.AuthoringEvidence
		if err := json.Unmarshal(vector.AuthoringEvidence, &evidence); err != nil {
			t.Fatal(err)
		}
		response.Authoring.AuthoringTranscriptObjectKey = "sha256/" + evidence.AuthoringTranscriptSHA256
		response.Authoring.FrozenSubmissionObjectKey = "sha256/" + evidence.FrozenPatchSHA256
	}
	if response.Grading != nil {
		response.Grading.TaskEvidence = []json.RawMessage{
			append(json.RawMessage(nil), vector.TaskEvidence...),
		}
	}
	return response
}

func fixtureService(t *testing.T, backend Backend) *Service {
	t.Helper()
	service, err := New(Config{
		ControlToken: fixtureToken, Backend: backend, OperationTimeout: time.Minute,
		Now: func() time.Time { return time.Date(2026, 8, 23, 6, 0, 0, 0, time.UTC) },
	})
	if err != nil {
		t.Fatal(err)
	}
	return service
}

func invoke(t *testing.T, handler http.Handler, path string, body []byte, token string) *httptest.ResponseRecorder {
	t.Helper()
	request := httptest.NewRequest(http.MethodPost, "http://supervisor.invalid"+path, bytes.NewReader(body))
	request.Header.Set("Content-Type", "application/json")
	if token != "" {
		request.Header.Set("Authorization", "Bearer "+token)
	}
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	return response
}

func TestSupervisorVectorRoundTripsEveryOperation(t *testing.T) {
	vector := loadFixtureVector(t)
	paths := map[string]string{
		"prepare":         "/v1/coding/supervisor/prepare",
		"author":          "/v1/coding/supervisor/author",
		"grade":           "/v1/coding/supervisor/grade",
		"abort_authoring": "/v1/coding/supervisor/abort-authoring",
		"abort_grading":   "/v1/coding/supervisor/abort-grading",
		"recover":         "/v1/coding/supervisor/recover",
	}
	service := fixtureService(t, backendFunc(func(_ context.Context, request Request) (Response, error) {
		return vectorResponse(t, vector, string(request.Operation)), nil
	}))
	for operation, path := range paths {
		t.Run(operation, func(t *testing.T) {
			response := invoke(t, service.Handler(), path, vector.Requests[operation], fixtureToken)
			if response.Code != http.StatusOK {
				t.Fatalf("status=%d body=%s", response.Code, response.Body.String())
			}
			var got any
			if err := json.Unmarshal(response.Body.Bytes(), &got); err != nil {
				t.Fatal(err)
			}
			expected := vectorResponse(t, vector, operation)
			expectedBody, err := json.Marshal(expected)
			if err != nil {
				t.Fatal(err)
			}
			var want any
			if err := json.Unmarshal(expectedBody, &want); err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(got, want) {
				t.Fatalf("response=%#v want=%#v", got, want)
			}
		})
	}
	if err := service.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestSupervisorAuthenticationAndWireFailClosed(t *testing.T) {
	vector := loadFixtureVector(t)
	calls := 0
	service := fixtureService(t, backendFunc(func(context.Context, Request) (Response, error) {
		calls++
		return Response{}, nil
	}))
	handler := service.Handler()
	for name, response := range map[string]*httptest.ResponseRecorder{
		"missing token":   invoke(t, handler, "/v1/coding/supervisor/author", vector.Requests["author"], ""),
		"wrong token":     invoke(t, handler, "/v1/coding/supervisor/author", vector.Requests["author"], strings.Repeat("x", 40)),
		"wrong path":      invoke(t, handler, "/v1/coding/supervisor/unknown", vector.Requests["author"], fixtureToken),
		"query":           invoke(t, handler, "/v1/coding/supervisor/author?x=1", vector.Requests["author"], fixtureToken),
		"operation drift": invoke(t, handler, "/v1/coding/supervisor/grade", vector.Requests["author"], fixtureToken),
		"duplicate":       invoke(t, handler, "/v1/coding/supervisor/author", []byte(`{"schema":"one","schema":"two"}`), fixtureToken),
	} {
		if response.Code < 400 {
			t.Fatalf("%s status=%d", name, response.Code)
		}
		if response.Header().Get("Cache-Control") != "no-store" || strings.Contains(response.Body.String(), fixtureToken) {
			t.Fatalf("%s unsafe response=%s", name, response.Body.String())
		}
	}
	if calls != 0 {
		t.Fatalf("backend calls=%d", calls)
	}
}

func TestSupervisorRejectsConcurrentSameAttemptAndAllowsRetry(t *testing.T) {
	vector := loadFixtureVector(t)
	entered := make(chan struct{})
	release := make(chan struct{})
	var once sync.Once
	backend := backendFunc(func(ctx context.Context, request Request) (Response, error) {
		once.Do(func() { close(entered) })
		select {
		case <-release:
		case <-ctx.Done():
			return Response{}, ctx.Err()
		}
		return vectorResponse(t, vector, string(request.Operation)), nil
	})
	service := fixtureService(t, backend)
	firstDone := make(chan *httptest.ResponseRecorder, 1)
	go func() {
		firstDone <- invoke(t, service.Handler(), "/v1/coding/supervisor/author", vector.Requests["author"], fixtureToken)
	}()
	<-entered
	second := invoke(t, service.Handler(), "/v1/coding/supervisor/author", vector.Requests["author"], fixtureToken)
	if second.Code != http.StatusConflict {
		t.Fatalf("concurrent status=%d body=%s", second.Code, second.Body.String())
	}
	if err := service.Close(); !errors.Is(err, ErrConcurrent) {
		t.Fatalf("close while active err=%v", err)
	}
	close(release)
	if first := <-firstDone; first.Code != http.StatusOK {
		t.Fatalf("first status=%d body=%s", first.Code, first.Body.String())
	}
	retry := invoke(t, service.Handler(), "/v1/coding/supervisor/author", vector.Requests["author"], fixtureToken)
	if retry.Code != http.StatusOK {
		t.Fatalf("retry status=%d body=%s", retry.Code, retry.Body.String())
	}
	if err := service.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestSupervisorBackendErrorsAndDiagnosticsAreSafe(t *testing.T) {
	vector := loadFixtureVector(t)
	service := fixtureService(t, backendFunc(func(context.Context, Request) (Response, error) {
		return Response{}, errors.New("private task provider secret")
	}))
	response := invoke(t, service.Handler(), "/v1/coding/supervisor/author", vector.Requests["author"], fixtureToken)
	if response.Code != http.StatusServiceUnavailable || strings.Contains(response.Body.String(), "private") ||
		strings.Contains(response.Body.String(), "secret") {
		t.Fatalf("unsafe backend error status=%d body=%s", response.Code, response.Body.String())
	}
	config := Config{ControlToken: fixtureToken, Backend: backendFunc(func(context.Context, Request) (Response, error) {
		return Response{}, nil
	})}
	if body, err := json.Marshal(config); !errors.Is(err, ErrPrivate) || body != nil {
		t.Fatalf("config marshal body=%q err=%v", body, err)
	}
	if body, err := json.Marshal(service); !errors.Is(err, ErrPrivate) || body != nil {
		t.Fatalf("service marshal body=%q err=%v", body, err)
	}
	if strings.Contains(service.String(), fixtureToken) {
		t.Fatal("service diagnostics exposed token")
	}
	if err := service.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestSupervisorRejectsExpiredAndInvalidBackendOutcome(t *testing.T) {
	vector := loadFixtureVector(t)
	var request map[string]any
	if err := json.Unmarshal(vector.Requests["author"], &request); err != nil {
		t.Fatal(err)
	}
	request["deadline"] = "2026-08-23T05:59:59Z"
	expired, _ := json.Marshal(request)
	service := fixtureService(t, backendFunc(func(context.Context, Request) (Response, error) {
		response := vectorResponse(t, vector, "author")
		response.Authoring.CapabilitiesRevoked = false
		return response, nil
	}))
	response := invoke(t, service.Handler(), "/v1/coding/supervisor/author", expired, fixtureToken)
	if response.Code != http.StatusGatewayTimeout {
		t.Fatalf("expired status=%d body=%s", response.Code, response.Body.String())
	}
	response = invoke(t, service.Handler(), "/v1/coding/supervisor/author", vector.Requests["author"], fixtureToken)
	if response.Code != http.StatusBadGateway {
		t.Fatalf("invalid backend status=%d body=%s", response.Code, response.Body.String())
	}
	if err := service.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestSupervisorRejectsGrantAndPreparationAuthorityDrift(t *testing.T) {
	vector := loadFixtureVector(t)
	calls := 0
	service := fixtureService(t, backendFunc(func(_ context.Context, request Request) (Response, error) {
		calls++
		response := vectorResponse(t, vector, string(request.Operation))
		if request.Operation == OperationPrepare {
			response.Preparation.BrokerPublicKey = "invalid"
		}
		return response, nil
	}))
	var author map[string]any
	if err := json.Unmarshal(vector.Requests["author"], &author); err != nil {
		t.Fatal(err)
	}
	grant := author["grant"].(map[string]any)
	grant["ticket_id"] = "99999999-9999-4999-8999-999999999999"
	body, _ := json.Marshal(author)
	response := invoke(t, service.Handler(), "/v1/coding/supervisor/author", body, fixtureToken)
	if response.Code != http.StatusBadRequest || calls != 0 {
		t.Fatalf("grant drift status=%d calls=%d body=%s", response.Code, calls, response.Body.String())
	}
	var harnessDrift map[string]any
	if err := json.Unmarshal(vector.Requests["author"], &harnessDrift); err != nil {
		t.Fatal(err)
	}
	harnessDrift["harness"].(map[string]any)["screened_image_sha256"] = "bad"
	body, _ = json.Marshal(harnessDrift)
	response = invoke(t, service.Handler(), "/v1/coding/supervisor/author", body, fixtureToken)
	if response.Code != http.StatusBadRequest || calls != 0 {
		t.Fatalf("harness drift status=%d calls=%d body=%s", response.Code, calls, response.Body.String())
	}
	response = invoke(t, service.Handler(), "/v1/coding/supervisor/prepare", vector.Requests["prepare"], fixtureToken)
	if response.Code != http.StatusBadGateway || calls != 1 {
		t.Fatalf("preparation drift status=%d calls=%d body=%s", response.Code, calls, response.Body.String())
	}
	if err := service.Close(); err != nil {
		t.Fatal(err)
	}
}
