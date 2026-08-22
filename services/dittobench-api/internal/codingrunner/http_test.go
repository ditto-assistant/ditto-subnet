package codingrunner

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func serveTool(handler http.Handler, method, path string, body []byte) *httptest.ResponseRecorder {
	request := httptest.NewRequest(method, path, bytes.NewReader(body))
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	return response
}

func requestBody(t *testing.T, request ToolRequest) []byte {
	t.Helper()
	body, err := json.Marshal(request)
	if err != nil {
		t.Fatal(err)
	}
	return body
}

func TestHTTPHandlerServesOnlyBoundedToolRoute(t *testing.T) {
	session, _ := newFixtureSession(t, nil)
	handler := session.Handler()
	body := requestBody(t, toolRequest("http-001", "repo.read_file", map[string]any{"path": "src/parser.py"}))
	response := serveTool(handler, http.MethodPost, "/tool", body)
	if response.Code != http.StatusOK || response.Header().Get("Cache-Control") != "no-store" ||
		!strings.HasSuffix(response.Body.String(), "\n") {
		t.Fatalf("unexpected HTTP response: code=%d headers=%v body=%s", response.Code, response.Header(), response.Body.String())
	}
	var toolResponse ToolResponse
	if err := json.Unmarshal(response.Body.Bytes(), &toolResponse); err != nil {
		t.Fatal(err)
	}
	if !toolResponse.OK || toolResponse.Sequence != 1 || !isLowerSHA256(toolResponse.EventSHA256) {
		t.Fatalf("unexpected tool response: %#v", toolResponse)
	}

	if got := serveTool(handler, http.MethodGet, "/tool", nil); got.Code != http.StatusMethodNotAllowed {
		t.Fatalf("GET /tool status=%d", got.Code)
	}
	if got := serveTool(handler, http.MethodPost, "/other", body); got.Code != http.StatusNotFound {
		t.Fatalf("POST /other status=%d", got.Code)
	}
	oversized := bytes.Repeat([]byte{'x'}, int(MaxToolRequestBytes)+1)
	if got := serveTool(handler, http.MethodPost, "/tool", oversized); got.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("oversized status=%d", got.Code)
	}

	wrong := toolRequest("http-002", "repo.read_file", map[string]any{"path": "src/parser.py"})
	wrong.ProfileCapabilityID = "other-profile"
	if got := serveTool(handler, http.MethodPost, "/tool", requestBody(t, wrong)); got.Code != http.StatusForbidden {
		t.Fatalf("identity mismatch status=%d body=%s", got.Code, got.Body.String())
	}
	frozen := session.Freeze()
	if frozen.Failure == nil || frozen.Failure.Code != "capability_identity" {
		t.Fatalf("capability mismatch did not latch integrity: %#v", frozen)
	}
	if got := serveTool(handler, http.MethodPost, "/tool", requestBody(t, toolRequest("http-003", "git.status", map[string]any{}))); got.Code != http.StatusGone {
		t.Fatalf("revoked status=%d body=%s", got.Code, got.Body.String())
	}
}

func TestHTTPParserRejectsDuplicateMissingAndDeepFields(t *testing.T) {
	session, _ := newFixtureSession(t, nil)
	handler := session.Handler()
	tests := [][]byte{
		[]byte(`{"coding_contract_version":1,"coding_contract_version":1,"case_id":"case-001","profile_capability_id":"profile-001","call_id":"x","name":"git.status","arguments":{}}`),
		[]byte(`{"coding_contract_version":1,"case_id":"case-001","profile_capability_id":"profile-001","call_id":"x","name":"git.status"}`),
		[]byte(`{"coding_contract_version":1,"case_id":"case-001","profile_capability_id":"profile-001","call_id":"x","name":"git.status","arguments":{"x":1,"x":2}}`),
	}
	deep := `"leaf"`
	for range 17 {
		deep = "[" + deep + "]"
	}
	tests = append(tests, []byte(`{"coding_contract_version":1,"case_id":"case-001","profile_capability_id":"profile-001","call_id":"deep","name":"git.status","arguments":{},"future":`+deep+`}`))
	for _, body := range tests {
		response := serveTool(handler, http.MethodPost, "/tool", body)
		if response.Code != http.StatusBadRequest {
			t.Fatalf("invalid body status=%d body=%s", response.Code, response.Body.String())
		}
	}
	loneSurrogate := []byte(`{"coding_contract_version":1,"case_id":"case-001","profile_capability_id":"profile-001","call_id":"unicode","name":"git.status","arguments":{},"future":"\ud800"}`)
	if response := serveTool(handler, http.MethodPost, "/tool", loneSurrogate); response.Code != http.StatusBadRequest {
		t.Fatalf("lone surrogate status=%d body=%s", response.Code, response.Body.String())
	}
	invalidUTF8 := append([]byte(nil), loneSurrogate...)
	invalidUTF8 = bytes.Replace(invalidUTF8, []byte(`\ud800`), []byte{0xff}, 1)
	if response := serveTool(handler, http.MethodPost, "/tool", invalidUTF8); response.Code != http.StatusBadRequest {
		t.Fatalf("invalid UTF-8 status=%d body=%s", response.Code, response.Body.String())
	}
	pairedSurrogate := bytes.Replace(loneSurrogate, []byte(`\ud800`), []byte(`\ud83d\ude00`), 1)
	if response := serveTool(handler, http.MethodPost, "/tool", pairedSurrogate); response.Code != http.StatusOK {
		t.Fatalf("paired surrogate status=%d body=%s", response.Code, response.Body.String())
	}

	request := toolRequest("future-field", "git.status", map[string]any{})
	body := requestBody(t, request)
	body = bytes.Replace(body, []byte(`"name"`), []byte(`"future_hint":{"ignored":true},"name"`), 1)
	if response := serveTool(handler, http.MethodPost, "/tool", body); response.Code != http.StatusOK {
		t.Fatalf("unknown top-level field broke compatibility: %d %s", response.Code, response.Body.String())
	}
}

func TestInvalidToolArgumentsAreRecordedButEnvelopeFailuresAreNot(t *testing.T) {
	session, _ := newFixtureSession(t, nil)
	unknownArgument := toolRequest("bad-args", "repo.read_file", map[string]any{"path": "src/parser.py", "extra": true})
	response, err := session.Invoke(t.Context(), unknownArgument)
	if err != nil || response.OK || response.Error == nil || response.Error.Code != "invalid_tool_request" || response.Sequence != 1 {
		t.Fatalf("invalid arguments response=%#v err=%v", response, err)
	}
	wrongIdentity := toolRequest("wrong-id", "git.status", map[string]any{})
	wrongIdentity.CaseID = "other-case"
	if _, err := session.Invoke(t.Context(), wrongIdentity); err == nil {
		t.Fatal("wrong identity was accepted")
	}
	if session.sequence != 1 {
		t.Fatalf("envelope failure consumed event sequence %d", session.sequence)
	}
}
