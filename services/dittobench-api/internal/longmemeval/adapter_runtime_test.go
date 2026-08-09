package longmemeval

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"reflect"
	"strings"
	"sync"
	"testing"

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

	tests := map[string]http.HandlerFunc{
		"status":        func(writer http.ResponseWriter, _ *http.Request) { writer.WriteHeader(http.StatusBadGateway) },
		"bad json":      func(writer http.ResponseWriter, _ *http.Request) { _, _ = writer.Write([]byte(`{`)) },
		"missing final": func(writer http.ResponseWriter, _ *http.Request) { _, _ = writer.Write([]byte(`{"tool_calls":[]}`)) },
	}
	for name, handler := range tests {
		t.Run(name, func(t *testing.T) {
			server := httptest.NewServer(handler)
			defer server.Close()
			harness, err := NewHTTPHarness(server.URL, server.Client())
			if err != nil {
				t.Fatal(err)
			}
			if _, err := harness.Run(context.Background(), protocol.RunRequest{Tools: []protocol.ToolDefinition{}}); err == nil {
				t.Fatal("invalid run response accepted")
			}
		})
	}
}

func TestHTTPHarnessRejectsIncompleteSeedAcknowledgement(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
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
	if err == nil {
		t.Fatal("incomplete seed acknowledgement accepted")
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
