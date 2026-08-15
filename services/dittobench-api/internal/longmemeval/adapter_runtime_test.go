package longmemeval

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/google/uuid"
)

func TestProjectionIsDeterministicForSameInputAndKey(t *testing.T) {
	_, _, dataset := runtimeFixture(t)
	first, err := ProjectSelectedCases(dataset, fixtureProjectionKey, 1)
	if err != nil {
		t.Fatal(err)
	}
	second, err := ProjectSelectedCases(dataset, append([]byte(nil), fixtureProjectionKey...), 1)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(wireJSON(t, first), wireJSON(t, second)) {
		t.Fatal("same dataset/key produced different wire payloads")
	}
	if len(first) != 12 {
		t.Fatalf("projected %d cases", len(first))
	}
}

func TestProjectionDifferentKeysAreUnlinkable(t *testing.T) {
	_, _, dataset := runtimeFixture(t)
	first, err := ProjectSelectedCases(dataset, fixtureProjectionKey, 64)
	if err != nil {
		t.Fatal(err)
	}
	otherKey := []byte("fedcba9876543210fedcba9876543210")
	second, err := ProjectSelectedCases(dataset, otherKey, 64)
	if err != nil {
		t.Fatal(err)
	}
	firstIDs := visibleIDs(first)
	secondIDs := visibleIDs(second)
	for value := range firstIDs {
		if _, linked := secondIDs[value]; linked {
			t.Fatalf("identifier %q linked projections made with different keys", value)
		}
	}
	if bytes.Equal(wireJSON(t, first), wireJSON(t, second)) {
		t.Fatal("different keys produced identical wire payloads")
	}
}

func TestProjectionUsesOneOpaqueNamespacePerCase(t *testing.T) {
	_, _, dataset := runtimeFixture(t)
	projected, err := ProjectSelectedCases(dataset, fixtureProjectionKey, 64)
	if err != nil {
		t.Fatal(err)
	}
	users := make(map[string]struct{}, len(projected))
	cases := make(map[string]struct{}, len(projected))
	for _, item := range projected {
		if _, err := uuid.Parse(item.RunRequest.UserID); err != nil {
			t.Fatalf("user id is not UUID-shaped: %q", item.RunRequest.UserID)
		}
		if _, err := uuid.Parse(item.RunRequest.CaseID); err != nil {
			t.Fatalf("case id is not UUID-shaped: %q", item.RunRequest.CaseID)
		}
		if _, duplicate := users[item.RunRequest.UserID]; duplicate {
			t.Fatalf("reused user namespace %q", item.RunRequest.UserID)
		}
		if _, duplicate := cases[item.RunRequest.CaseID]; duplicate {
			t.Fatalf("reused case id %q", item.RunRequest.CaseID)
		}
		users[item.RunRequest.UserID] = struct{}{}
		cases[item.RunRequest.CaseID] = struct{}{}
		for _, seed := range item.SeedRequests {
			if seed.UserID != item.RunRequest.UserID {
				t.Fatal("seed and run user namespaces differ")
			}
			for _, pair := range seed.Pairs {
				if _, err := uuid.Parse(pair.PairID); err != nil {
					t.Fatalf("pair id is not UUID-shaped: %q", pair.PairID)
				}
				if _, err := uuid.Parse(pair.SessionID); err != nil {
					t.Fatalf("session id is not UUID-shaped: %q", pair.SessionID)
				}
			}
		}
	}
}

func TestProjectionNeverSerializesPrivateLabelsOrQuestionIDs(t *testing.T) {
	_, _, dataset := runtimeFixture(t)
	projected, err := ProjectSelectedCases(dataset, fixtureProjectionKey, 64)
	if err != nil {
		t.Fatal(err)
	}
	raw := wireJSON(t, projected)
	for _, forbidden := range []string{
		"question_id", "question_type", "answer_session_ids", "has_answer",
		"origin", "provenance", "answer-proof", "single-session-user",
		"multi-session", "temporal-reasoning", "knowledge-update",
		"single-session-preference", "memory_lookup-lme",
	} {
		if bytes.Contains(raw, []byte(forbidden)) {
			t.Fatalf("wire payload contains private label %q", forbidden)
		}
	}
	for questionID := range dataset.selected {
		if bytes.Contains(raw, []byte(questionID)) {
			t.Fatalf("wire payload contains raw question id %q", questionID)
		}
	}
}

func TestPairProjectionPreservesIrregularChronology(t *testing.T) {
	entry := DatasetCase{
		QuestionID:         "private-irregular",
		QuestionType:       "multi-session",
		Question:           "What did I say?",
		QuestionDate:       "2026/01/02 (Fri) 12:00",
		HaystackSessionIDs: []string{"origin-role-label"},
		HaystackDates:      []string{"2025/02/02 (Sun) 11:00"},
		HaystackSessions: [][]DatasetTurn{{
			{Role: "assistant", Content: "assistant first"},
			{Role: "user", Content: "first user"},
			{Role: "user", Content: "second user"},
			{Role: "assistant", Content: "paired answer"},
			{Role: "assistant", Content: "assistant adjacent"},
			{Role: "user", Content: "last user"},
		}},
	}
	pairs, err := projectPairs(entry, fixtureProjectionKey, strings.Repeat("a", 64))
	if err != nil {
		t.Fatal(err)
	}
	want := [][2]string{
		{"", "assistant first"},
		{"first user", ""},
		{"second user", "paired answer"},
		{"", "assistant adjacent"},
		{"last user", ""},
	}
	if len(pairs) != len(want) {
		t.Fatalf("pairs=%d want=%d", len(pairs), len(want))
	}
	for index, pair := range pairs {
		if got := [2]string{pair.Prompt, pair.Response}; got != want[index] {
			t.Fatalf("pair %d=%q want=%q", index, got, want[index])
		}
		if pair.Timestamp != "2025-02-02T11:00:00Z" {
			t.Fatalf("timestamp=%q", pair.Timestamp)
		}
	}
}

func TestPairProjectionDropsContentlessAndUsesLastWriteWins(t *testing.T) {
	entry := DatasetCase{
		QuestionID:         "private-repeat",
		QuestionType:       "multi-session",
		Question:           "What survived?",
		QuestionDate:       "2026/01/02 (Fri) 12:00",
		HaystackSessionIDs: []string{"same", "same", "empty"},
		HaystackDates:      []string{"2025/01/01 (Wed) 10:00", "2025/01/01 (Wed) 10:00", "2025/01/01 (Wed) 10:00"},
		HaystackSessions: [][]DatasetTurn{
			{{Role: "user", Content: "first"}, {Role: "assistant", Content: "old"}},
			{{Role: "user", Content: "second"}, {Role: "assistant", Content: "new"}},
			{{Role: "user", Content: ""}, {Role: "assistant", Content: ""}},
		},
	}
	pairs, err := projectPairs(entry, fixtureProjectionKey, strings.Repeat("a", 64))
	if err != nil {
		t.Fatal(err)
	}
	if len(pairs) != 1 || pairs[0].Prompt != "second" || pairs[0].Response != "new" {
		t.Fatalf("last-write-wins pairs=%#v", pairs)
	}
}

func TestPairProjectionRejectsBadTimestampAndRole(t *testing.T) {
	base := runtimeDatasetCases()[0]
	badTimestamp := cloneDatasetCase(base)
	badTimestamp.HaystackDates[0] = "2025-01-01"
	if _, err := projectPairs(badTimestamp, fixtureProjectionKey, strings.Repeat("a", 64)); err == nil {
		t.Fatal("bad timestamp accepted")
	}
	badRole := cloneDatasetCase(base)
	badRole.HaystackSessions[0][0].Role = "system"
	if _, err := projectPairs(badRole, fixtureProjectionKey, strings.Repeat("a", 64)); err == nil {
		t.Fatal("bad role accepted")
	}
}

func TestProjectionRejectsWeakKeysBadBatchesAndTamperedDataset(t *testing.T) {
	_, _, dataset := runtimeFixture(t)
	if _, err := ProjectSelectedCases(dataset, []byte("weak"), 64); err == nil {
		t.Fatal("weak projection key accepted")
	}
	if _, err := ProjectSelectedCases(dataset, fixtureProjectionKey, 0); err == nil {
		t.Fatal("zero seed batch accepted")
	}
	tampered := dataset
	tampered.SHA256 = strings.Repeat("f", 64)
	if _, err := ProjectSelectedCases(tampered, fixtureProjectionKey, 64); err == nil {
		t.Fatal("tampered loaded dataset accepted")
	}
	missing := dataset
	missing.selected = make(map[string]DatasetCase)
	if _, err := ProjectSelectedCases(missing, fixtureProjectionKey, 64); err == nil {
		t.Fatal("missing selected rows accepted")
	}
}

func TestNativeMemoryToolCatalogMatchesImportedAdapter(t *testing.T) {
	tools := NativeMemoryTools()
	if names := []string{tools[0].Name, tools[1].Name, tools[2].Name, tools[3].Name}; !reflect.DeepEqual(names, []string{"search_memories", "search_subjects", "fetch_memories", "search_memories_in_subjects"}) {
		t.Fatalf("tool names=%v", names)
	}
	var search, fetch, scoped map[string]any
	if err := json.Unmarshal(tools[0].Parameters, &search); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(tools[2].Parameters, &fetch); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(tools[3].Parameters, &scoped); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(search["required"], []any{"queries"}) ||
		!reflect.DeepEqual(fetch["required"], []any{"pairIds"}) ||
		!reflect.DeepEqual(scoped["required"], []any{"subject_id", "queries"}) {
		t.Fatal("tool required fields drifted from imported adapter")
	}
	copy := NativeMemoryTools()
	copy[0].Name = "mutated"
	if NativeMemoryTools()[0].Name != "search_memories" {
		t.Fatal("caller mutated tool catalog")
	}
}

func TestHTTPHarnessHostileRecorderSeesOnlyAllowlistedWireShape(t *testing.T) {
	_, _, dataset := runtimeFixture(t)
	projected, err := ProjectSelectedCases(dataset, fixtureProjectionKey, 64)
	if err != nil {
		t.Fatal(err)
	}
	item := projected[0]
	type recorded struct {
		path   string
		body   []byte
		header http.Header
	}
	var mu sync.Mutex
	var requests []recorded
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		raw, _ := io.ReadAll(request.Body)
		mu.Lock()
		requests = append(requests, recorded{request.URL.Path, raw, request.Header.Clone()})
		mu.Unlock()
		writer.Header().Set("Content-Type", "application/json")
		if request.URL.Path == "/seed" {
			var seed struct {
				Pairs []protocol.MemoryPair `json:"pairs"`
			}
			_ = json.Unmarshal(raw, &seed)
			_ = json.NewEncoder(writer).Encode(protocol.SeedResponse{Pairs: len(seed.Pairs)})
			return
		}
		_, _ = writer.Write([]byte(`{"final_text":"answer","tool_calls":[]}`))
	}))
	defer server.Close()
	harness, err := NewHTTPHarness(server.URL, server.Client())
	if err != nil {
		t.Fatal(err)
	}
	for _, seed := range item.SeedRequests {
		if _, err := harness.Seed(context.Background(), seed); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := harness.Run(context.Background(), item.RunRequest); err != nil {
		t.Fatal(err)
	}

	mu.Lock()
	defer mu.Unlock()
	if len(requests) != len(item.SeedRequests)+1 {
		t.Fatalf("requests=%d", len(requests))
	}
	for _, request := range requests {
		if request.header.Get("Authorization") != "" || request.header.Get("Cookie") != "" {
			t.Fatal("secret-bearing header crossed harness boundary")
		}
		if request.header.Get("Content-Type") != "application/json" || request.header.Get("Accept") != "application/json" {
			t.Fatalf("unexpected content negotiation headers: %v", request.header)
		}
		for _, forbidden := range []string{"question_id", "question_type", "answer_session", "has_answer", "origin", "provenance"} {
			if bytes.Contains(request.body, []byte(forbidden)) {
				t.Fatalf("%s body contains %q: %s", request.path, forbidden, request.body)
			}
		}
	}
}

func TestHTTPHarnessRejectsInvalidConstructionAndResponses(t *testing.T) {
	for _, baseURL := range []string{"", "ftp://example.com", "http://user:pass@example.com", "http://example.com?q=1"} {
		if _, err := NewHTTPHarness(baseURL, http.DefaultClient); err == nil {
			t.Fatalf("invalid URL %q accepted", baseURL)
		}
	}
	if _, err := NewHTTPHarness("http://example.com", nil); err == nil {
		t.Fatal("nil HTTP client accepted")
	}

	tests := map[string]struct {
		handler http.HandlerFunc
		kind    string
		status  int
	}{
		"status": {
			handler: func(writer http.ResponseWriter, _ *http.Request) {
				writer.WriteHeader(http.StatusBadGateway)
				_, _ = writer.Write([]byte(`{"error":"private submitted detail"}`))
			},
			kind: "http_status", status: http.StatusBadGateway,
		},
		"bad json": {
			handler: func(writer http.ResponseWriter, _ *http.Request) { _, _ = writer.Write([]byte(`{`)) },
			kind:    "malformed_json", status: http.StatusOK,
		},
		"missing final": {
			handler: func(writer http.ResponseWriter, _ *http.Request) { _, _ = writer.Write([]byte(`{"tool_calls":[]}`)) },
			kind:    "missing_final_text",
		},
	}
	for name, testCase := range tests {
		t.Run(name, func(t *testing.T) {
			server := httptest.NewServer(testCase.handler)
			defer server.Close()
			harness, err := NewHTTPHarness(server.URL, server.Client())
			if err != nil {
				t.Fatal(err)
			}
			harness.seedRetryDelay = [seedMaxAttempts - 1]time.Duration{}
			_, err = harness.Run(context.Background(), protocol.RunRequest{Tools: []protocol.ToolDefinition{}})
			var failure *HarnessCaseFailure
			if !errors.As(err, &failure) {
				t.Fatal("invalid run response accepted")
			}
			if failure.Kind != testCase.kind || failure.StatusCode != testCase.status {
				t.Fatalf("failure=%#v", failure)
			}
			if strings.Contains(err.Error(), "private submitted detail") {
				t.Fatalf("private response body leaked: %v", err)
			}
		})
	}
}

func TestHTTPHarnessSeedReceivedFailuresRemainFatal(t *testing.T) {
	tests := map[string]http.HandlerFunc{
		"status": func(writer http.ResponseWriter, _ *http.Request) {
			writer.WriteHeader(http.StatusBadGateway)
			_, _ = writer.Write([]byte(`{"error":"private submitted detail"}`))
		},
		"malformed json": func(writer http.ResponseWriter, _ *http.Request) {
			_, _ = writer.Write([]byte(`{`))
		},
	}
	for name, handler := range tests {
		t.Run(name, func(t *testing.T) {
			server := httptest.NewServer(handler)
			defer server.Close()
			harness, err := NewHTTPHarness(server.URL, server.Client())
			if err != nil {
				t.Fatal(err)
			}
			_, err = harness.Seed(context.Background(), protocol.SeedRequest{
				Pairs: []protocol.MemoryPair{}, Subjects: []protocol.Subject{}, Links: []protocol.SubjectLink{},
			})
			var failure *HarnessCaseFailure
			if err == nil || errors.As(err, &failure) {
				t.Fatalf("seed response failure was attributed to a scored case: %v", err)
			}
			if strings.Contains(err.Error(), "private submitted detail") {
				t.Fatalf("private response body leaked: %v", err)
			}
		})
	}
}

func TestHTTPHarnessSeedRetriesTransientFailureWithIdenticalIdempotentBody(t *testing.T) {
	var requests [][]byte
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		raw, _ := io.ReadAll(request.Body)
		requests = append(requests, raw)
		if len(requests) < seedMaxAttempts {
			writer.WriteHeader(http.StatusServiceUnavailable)
			_, _ = writer.Write([]byte(`{"error":"private transient detail"}`))
			return
		}
		_, _ = writer.Write([]byte(`{"pairs":1,"subjects":0,"links":0}`))
	}))
	defer server.Close()
	harness, err := NewHTTPHarness(server.URL, server.Client())
	if err != nil {
		t.Fatal(err)
	}
	harness.seedRetryDelay = [seedMaxAttempts - 1]time.Duration{}
	seed := protocol.SeedRequest{
		UserID: uuid.NewString(), Pairs: []protocol.MemoryPair{{
			PairID: uuid.NewString(), SessionID: uuid.NewString(), Prompt: "opaque prompt", Response: "opaque response",
		}}, Subjects: []protocol.Subject{}, Links: []protocol.SubjectLink{},
	}
	response, err := harness.Seed(context.Background(), seed)
	if err != nil || response.Pairs != 1 {
		t.Fatalf("response=%+v err=%v", response, err)
	}
	if len(requests) != seedMaxAttempts {
		t.Fatalf("requests=%d", len(requests))
	}
	for index := 1; index < len(requests); index++ {
		if !bytes.Equal(requests[0], requests[index]) {
			t.Fatalf("retry %d changed the idempotent seed body", index+1)
		}
	}
}

func TestHTTPHarnessSeedRetriesTransportAndMalformedResponseWithFreshDecode(t *testing.T) {
	var requests [][]byte
	client := &http.Client{Transport: roundTripFunc(func(request *http.Request) (*http.Response, error) {
		raw, _ := io.ReadAll(request.Body)
		requests = append(requests, raw)
		switch len(requests) {
		case 1:
			return nil, errors.New("transient local transport failure")
		case 2:
			return &http.Response{
				StatusCode: http.StatusOK,
				Body:       io.NopCloser(strings.NewReader(`{"pairs":99,`)),
				Header:     make(http.Header),
				Request:    request,
			}, nil
		default:
			return &http.Response{
				StatusCode: http.StatusOK,
				Body:       io.NopCloser(strings.NewReader(`{"pairs":1,"subjects":0,"links":0}`)),
				Header:     make(http.Header),
				Request:    request,
			}, nil
		}
	})}
	harness, err := NewHTTPHarness("http://harness.invalid", client)
	if err != nil {
		t.Fatal(err)
	}
	harness.seedRetryDelay = [seedMaxAttempts - 1]time.Duration{}
	seed := protocol.SeedRequest{
		Pairs:    []protocol.MemoryPair{{PairID: uuid.NewString(), SessionID: uuid.NewString()}},
		Subjects: []protocol.Subject{}, Links: []protocol.SubjectLink{},
	}
	response, err := harness.Seed(context.Background(), seed)
	if err != nil || response.Pairs != 1 {
		t.Fatalf("response=%+v err=%v", response, err)
	}
	if len(requests) != seedMaxAttempts {
		t.Fatalf("requests=%d", len(requests))
	}
	for index := 1; index < len(requests); index++ {
		if !bytes.Equal(requests[0], requests[index]) {
			t.Fatalf("retry %d changed the idempotent seed body", index+1)
		}
	}
}

func TestHTTPHarnessSeedRetryIsBoundedAndFailClosed(t *testing.T) {
	tests := map[string]struct {
		status       int
		wantRequests int
	}{
		"request timeout":            {status: http.StatusRequestTimeout, wantRequests: seedMaxAttempts},
		"too many requests":          {status: http.StatusTooManyRequests, wantRequests: seedMaxAttempts},
		"internal server error":      {status: http.StatusInternalServerError, wantRequests: seedMaxAttempts},
		"bad gateway":                {status: http.StatusBadGateway, wantRequests: seedMaxAttempts},
		"service unavailable":        {status: http.StatusServiceUnavailable, wantRequests: seedMaxAttempts},
		"gateway timeout":            {status: http.StatusGatewayTimeout, wantRequests: seedMaxAttempts},
		"submitted contract failure": {status: http.StatusBadRequest, wantRequests: 1},
	}
	for name, testCase := range tests {
		t.Run(name, func(t *testing.T) {
			requests := 0
			server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
				requests++
				writer.WriteHeader(testCase.status)
			}))
			defer server.Close()
			harness, err := NewHTTPHarness(server.URL, server.Client())
			if err != nil {
				t.Fatal(err)
			}
			harness.seedRetryDelay = [seedMaxAttempts - 1]time.Duration{}
			_, err = harness.Seed(context.Background(), protocol.SeedRequest{
				Pairs: []protocol.MemoryPair{}, Subjects: []protocol.Subject{}, Links: []protocol.SubjectLink{},
			})
			if err == nil || requests != testCase.wantRequests {
				t.Fatalf("requests=%d err=%v", requests, err)
			}
			var failure *HarnessCaseFailure
			if errors.As(err, &failure) {
				t.Fatalf("seed failure was attributed to a scored case: %v", err)
			}
		})
	}
}

func TestHTTPHarnessSeedRetriesResponseBodyReadFailure(t *testing.T) {
	requests := 0
	client := &http.Client{Transport: roundTripFunc(func(request *http.Request) (*http.Response, error) {
		requests++
		body := io.ReadCloser(io.NopCloser(strings.NewReader(`{"pairs":0,"subjects":0,"links":0}`)))
		if requests == 1 {
			body = io.NopCloser(errorReader{})
		}
		return &http.Response{
			StatusCode: http.StatusOK,
			Body:       body,
			Header:     make(http.Header),
			Request:    request,
		}, nil
	})}
	harness, err := NewHTTPHarness("http://harness.invalid", client)
	if err != nil {
		t.Fatal(err)
	}
	harness.seedRetryDelay = [seedMaxAttempts - 1]time.Duration{}
	response, err := harness.Seed(context.Background(), protocol.SeedRequest{
		Pairs: []protocol.MemoryPair{}, Subjects: []protocol.Subject{}, Links: []protocol.SubjectLink{},
	})
	if err != nil || requests != 2 || response.Pairs != 0 {
		t.Fatalf("requests=%d response=%+v err=%v", requests, response, err)
	}
}

func TestHTTPHarnessSeedDoesNotRetryTerminalStatusBodyReadFailure(t *testing.T) {
	requests := 0
	client := &http.Client{Transport: roundTripFunc(func(request *http.Request) (*http.Response, error) {
		requests++
		return &http.Response{
			StatusCode: http.StatusBadRequest,
			Body:       io.NopCloser(errorReader{}),
			Header:     make(http.Header),
			Request:    request,
		}, nil
	})}
	harness, err := NewHTTPHarness("http://harness.invalid", client)
	if err != nil {
		t.Fatal(err)
	}
	harness.seedRetryDelay = [seedMaxAttempts - 1]time.Duration{}
	_, err = harness.Seed(context.Background(), protocol.SeedRequest{
		Pairs: []protocol.MemoryPair{}, Subjects: []protocol.Subject{}, Links: []protocol.SubjectLink{},
	})
	if err == nil || requests != 1 {
		t.Fatalf("terminal status body-read failure was retried: requests=%d err=%v", requests, err)
	}
}

func TestHTTPHarnessSeedDoesNotRetryContextSentinelFromTransport(t *testing.T) {
	for _, sentinel := range []error{context.Canceled, context.DeadlineExceeded} {
		t.Run(sentinel.Error(), func(t *testing.T) {
			requests := 0
			client := &http.Client{Transport: roundTripFunc(func(*http.Request) (*http.Response, error) {
				requests++
				return nil, sentinel
			})}
			harness, err := NewHTTPHarness("http://harness.invalid", client)
			if err != nil {
				t.Fatal(err)
			}
			harness.seedRetryDelay = [seedMaxAttempts - 1]time.Duration{}
			_, err = harness.Seed(context.Background(), protocol.SeedRequest{
				Pairs: []protocol.MemoryPair{}, Subjects: []protocol.Subject{}, Links: []protocol.SubjectLink{},
			})
			if err == nil || requests != 1 {
				t.Fatalf("context sentinel was retried: requests=%d err=%v", requests, err)
			}
		})
	}
}

func TestHTTPHarnessSeedRetryRespectsCancellation(t *testing.T) {
	requests := 0
	first := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		requests++
		if requests == 1 {
			close(first)
		}
		writer.WriteHeader(http.StatusServiceUnavailable)
	}))
	defer server.Close()
	harness, err := NewHTTPHarness(server.URL, server.Client())
	if err != nil {
		t.Fatal(err)
	}
	harness.seedRetryDelay = [seedMaxAttempts - 1]time.Duration{time.Hour, time.Hour}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		_, operationErr := harness.Seed(ctx, protocol.SeedRequest{
			Pairs: []protocol.MemoryPair{}, Subjects: []protocol.Subject{}, Links: []protocol.SubjectLink{},
		})
		done <- operationErr
	}()
	<-first
	cancel()
	if err := <-done; !errors.Is(err, context.Canceled) || requests != 1 {
		t.Fatalf("requests=%d err=%v", requests, err)
	}
}

func TestHTTPHarnessTransportFailureIsNotAttributedToSubmittedCase(t *testing.T) {
	requests := 0
	client := &http.Client{Transport: roundTripFunc(func(*http.Request) (*http.Response, error) {
		requests++
		return nil, errors.New("network path failed")
	})}
	harness, err := NewHTTPHarness("http://harness.invalid", client)
	if err != nil {
		t.Fatal(err)
	}
	_, err = harness.Run(context.Background(), protocol.RunRequest{Tools: []protocol.ToolDefinition{}})
	var failure *HarnessCaseFailure
	if err == nil || errors.As(err, &failure) || requests != 1 {
		t.Fatalf("run transport failure was retried or attributed to submitted response: requests=%d err=%v", requests, err)
	}
}

func TestHTTPHarnessMalformedResponseCannotRetainPartialFinalText(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		_, _ = writer.Write([]byte(`{"final_text":"must-not-survive",`))
	}))
	defer server.Close()
	harness, err := NewHTTPHarness(server.URL, server.Client())
	if err != nil {
		t.Fatal(err)
	}
	response, err := harness.Run(context.Background(), protocol.RunRequest{Tools: []protocol.ToolDefinition{}})
	var failure *HarnessCaseFailure
	if !errors.As(err, &failure) || failure.Kind != "malformed_json" {
		t.Fatalf("failure=%v", err)
	}
	if response.FinalText != "" {
		t.Fatalf("partial final_text escaped malformed response: %#v", response)
	}
}

func TestHTTPHarnessOversizeAndBodyReadFailuresRemainInfrastructureFatal(t *testing.T) {
	t.Run("oversize", func(t *testing.T) {
		server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
			_, _ = writer.Write([]byte(strings.Repeat("x", maxHarnessResponseBytes+1)))
		}))
		defer server.Close()
		harness, err := NewHTTPHarness(server.URL, server.Client())
		if err != nil {
			t.Fatal(err)
		}
		_, err = harness.Run(context.Background(), protocol.RunRequest{Tools: []protocol.ToolDefinition{}})
		var failure *HarnessCaseFailure
		if err == nil || errors.As(err, &failure) || !strings.Contains(err.Error(), "size bound") {
			t.Fatalf("oversize response was not fatal: %v", err)
		}
	})

	t.Run("body read", func(t *testing.T) {
		client := &http.Client{Transport: roundTripFunc(func(request *http.Request) (*http.Response, error) {
			return &http.Response{
				StatusCode: http.StatusOK,
				Body:       io.NopCloser(errorReader{}),
				Header:     make(http.Header),
				Request:    request,
			}, nil
		})}
		harness, err := NewHTTPHarness("http://harness.invalid", client)
		if err != nil {
			t.Fatal(err)
		}
		_, err = harness.Run(context.Background(), protocol.RunRequest{Tools: []protocol.ToolDefinition{}})
		var failure *HarnessCaseFailure
		if err == nil || errors.As(err, &failure) || !strings.Contains(err.Error(), "read LongMemEval") {
			t.Fatalf("body read failure was not fatal: %v", err)
		}
	})
}

type errorReader struct{}

func (errorReader) Read([]byte) (int, error) { return 0, errors.New("read failed") }

type roundTripFunc func(*http.Request) (*http.Response, error)

func (fn roundTripFunc) RoundTrip(request *http.Request) (*http.Response, error) {
	return fn(request)
}

func TestHTTPHarnessRejectsIncompleteSeedAcknowledgement(t *testing.T) {
	requests := 0
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		requests++
		_, _ = writer.Write([]byte(`{"pairs":0,"subjects":0,"links":0}`))
	}))
	defer server.Close()
	harness, err := NewHTTPHarness(server.URL, server.Client())
	if err != nil {
		t.Fatal(err)
	}
	_, err = harness.Seed(context.Background(), protocol.SeedRequest{
		UserID:   uuid.NewString(),
		Pairs:    []protocol.MemoryPair{{PairID: uuid.NewString(), SessionID: uuid.NewString(), Prompt: "x"}},
		Subjects: []protocol.Subject{}, Links: []protocol.SubjectLink{},
	})
	if err == nil || requests != 1 {
		t.Fatalf("incomplete seed acknowledgement accepted or retried: requests=%d err=%v", requests, err)
	}
	diagnostic, ok := FailureDiagnostic(err)
	if !ok || diagnostic != (HarnessFailureDiagnostic{Operation: "seed", Kind: "incomplete_ack"}) {
		t.Fatalf("diagnostic = %#v, %v", diagnostic, ok)
	}
}

func TestHTTPHarnessSeedFailureDiagnosticsAreBoundedAndBodyFree(t *testing.T) {
	testCases := []struct {
		name       string
		statusCode int
		body       string
		wantKind   string
		wantStatus int
	}{
		{
			name:       "received status",
			statusCode: http.StatusUnprocessableEntity,
			body:       `{"error":"private submitted detail and credential-material"}`,
			wantKind:   "http_status",
			wantStatus: http.StatusUnprocessableEntity,
		},
		{
			name:       "malformed acknowledgement",
			statusCode: http.StatusOK,
			body:       `{"pairs":1,"private":"credential-material"`,
			wantKind:   "malformed_json",
			wantStatus: http.StatusOK,
		},
	}
	for _, testCase := range testCases {
		t.Run(testCase.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
				writer.WriteHeader(testCase.statusCode)
				_, _ = writer.Write([]byte(testCase.body))
			}))
			defer server.Close()
			harness, err := NewHTTPHarness(server.URL, server.Client())
			if err != nil {
				t.Fatal(err)
			}
			_, err = harness.Seed(context.Background(), protocol.SeedRequest{
				UserID: uuid.NewString(), Pairs: []protocol.MemoryPair{},
				Subjects: []protocol.Subject{}, Links: []protocol.SubjectLink{},
			})
			diagnostic, ok := FailureDiagnostic(err)
			want := HarnessFailureDiagnostic{
				Operation: "seed", Kind: testCase.wantKind, StatusCode: testCase.wantStatus,
			}
			if !ok || diagnostic != want {
				t.Fatalf("diagnostic = %#v, %v; want %#v, true", diagnostic, ok, want)
			}
			for _, forbidden := range []string{"private", "credential-material"} {
				if strings.Contains(err.Error(), forbidden) {
					t.Fatalf("seed failure leaked %q: %v", forbidden, err)
				}
			}
		})
	}
}

func TestFailureDiagnosticRejectsNonAllowlistedFields(t *testing.T) {
	testCases := []error{
		&HarnessCaseFailure{Kind: "private submitted detail"},
		&HarnessCaseFailure{Kind: "http_status", StatusCode: 999},
	}
	for _, operationErr := range testCases {
		if diagnostic, ok := FailureDiagnostic(operationErr); ok {
			t.Fatalf("non-allowlisted diagnostic accepted: %#v", diagnostic)
		}
	}
}

func TestBindTrustedCaseInferenceActivityOnlyBindsReceivedCaseFailure(t *testing.T) {
	private := errors.New("private transport detail")
	if got := BindTrustedCaseInferenceActivity(private, TrustedCaseInferenceActivity{EmbeddingAttempts: 1}); got != private {
		t.Fatalf("non-case error changed: %v", got)
	}
	unsealed := &HarnessCaseFailure{Kind: "http_status", StatusCode: http.StatusInternalServerError}
	if got := BindTrustedCaseInferenceActivity(unsealed, TrustedCaseInferenceActivity{EmbeddingAttempts: 1}); got != unsealed {
		t.Fatalf("unsealed case error changed: %v", got)
	}

	original := &HarnessCaseFailure{Kind: "http_status", StatusCode: http.StatusInternalServerError, received: true}
	bound := BindTrustedCaseInferenceActivity(original, TrustedCaseInferenceActivity{
		EmbeddingAttempts: 1, EmbeddingDispatches: 1, EmbeddingDelivered: 1,
	})
	var failure *HarnessCaseFailure
	if !errors.As(bound, &failure) || failure == original || failure.activity == nil || failure.activity.EmbeddingDelivered != 1 {
		t.Fatalf("trusted activity was not privately bound: %#v", failure)
	}
	diagnostic, ok := FailureDiagnostic(bound)
	if !ok || diagnostic != (HarnessFailureDiagnostic{
		Operation: "run", Kind: "http_status", StatusCode: http.StatusInternalServerError,
	}) {
		t.Fatalf("safe diagnostic changed: %#v, %v", diagnostic, ok)
	}
	encoded, err := json.Marshal(failure)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(encoded), "Embedding") || strings.Contains(string(encoded), "activity") {
		t.Fatalf("private trusted activity serialized: %s", encoded)
	}
}

func TestWirePayloadGolden(t *testing.T) {
	_, _, dataset := runtimeFixture(t)
	projected, err := ProjectSelectedCases(dataset, fixtureProjectionKey, 1)
	if err != nil {
		t.Fatal(err)
	}
	got := wireJSON(t, projected[:1])
	want, err := os.ReadFile("testdata/wire-payload-v1.json")
	if err != nil {
		t.Fatalf("read golden: %v\nGOT:\n%s", err, got)
	}
	if !bytes.Equal(got, want) {
		t.Fatalf("wire payload golden changed\nGOT:\n%s\nWANT:\n%s", got, want)
	}
}

func visibleIDs(values []ProjectedCase) map[string]struct{} {
	result := make(map[string]struct{})
	for _, item := range values {
		result[item.RunRequest.CaseID] = struct{}{}
		result[item.RunRequest.UserID] = struct{}{}
		for _, seed := range item.SeedRequests {
			for _, pair := range seed.Pairs {
				result[pair.PairID] = struct{}{}
				result[pair.SessionID] = struct{}{}
			}
		}
	}
	return result
}
