package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestValidatorMintedCaseURLsAttributeConcurrentRuns(t *testing.T) {
	var seen []traceContext
	var mu sync.Mutex
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var tc traceContext
		if err := json.Unmarshal([]byte(r.Header.Get(traceContextHeader)), &tc); err != nil {
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
	claimAndBindBrokerSession(t, broker, sessionID, "192.0.2.10", protocol.BenchVersionV6)

	const base = "http://host.docker.internal:11436/v1/inference"
	broker.setHarnessBase(sessionID, base+"/")

	urlA, started := broker.beginRunCase(sessionID, "case-A")
	if !started || !strings.HasPrefix(urlA, base+"/cases/") {
		t.Fatalf("case-A url = %q, started = %v", urlA, started)
	}
	urlB, _ := broker.beginRunCase(sessionID, "case-B")
	if urlB == urlA {
		t.Fatal("concurrent cases must get distinct URLs")
	}
	tokenA := strings.TrimPrefix(urlA, base+"/cases/")
	tokenB := strings.TrimPrefix(urlB, base+"/cases/")

	post := func(rest, claim string) int {
		t.Helper()
		request := httptest.NewRequest(http.MethodPost, "/v1/inference/id/"+rest, bytes.NewBufferString(`{"model":"qwen/qwen3-32b","max_tokens":32}`))
		request.RemoteAddr = "192.0.2.10:4321"
		request.SetPathValue("rest", rest)
		if claim != "" {
			request.Header.Set(harnessCaseHeader, claim)
		}
		recorder := httptest.NewRecorder()
		broker.handle(recorder, request)
		return recorder.Code
	}
	mustOK := func(rest, claim string) {
		t.Helper()
		if code := post(rest, claim); code != http.StatusOK {
			t.Fatalf("proxy status = %d for %s", code, rest)
		}
	}

	mustOK("cases/"+tokenA+"/v1/chat/completions", "")
	mustOK("cases/"+tokenB+"/chat/completions", "")
	mustOK("cases/"+tokenA+"/v1/chat/completions", "case-B")
	mustOK("v1/chat/completions", "")
	if code := post("cases/deadbeef/v1/chat/completions", ""); code != http.StatusUnauthorized {
		t.Fatalf("unknown token status = %d, want 401", code)
	}

	broker.endRunCase(sessionID, "case-A")
	broker.endRunCase(sessionID, "case-B")
	if code := post("cases/"+tokenA+"/v1/chat/completions", ""); code != http.StatusUnauthorized {
		t.Fatalf("stale token status = %d, want 401", code)
	}

	mu.Lock()
	defer mu.Unlock()
	if len(seen) != 4 {
		t.Fatalf("want 4 upstream calls, got %d", len(seen))
	}
	type want struct {
		caseID, source string
		verified       bool
	}
	wants := []want{
		{"case-A", "url", true},
		{"case-B", "url", true},
		{"case-A", "url", true},
		{"", "", false},
	}
	for i, w := range wants {
		tc := seen[i]
		if tc.CaseID != w.caseID || tc.CaseSource != w.source || tc.CaseVerified != w.verified {
			t.Fatalf("call %d: got (%q,%q,%v) want (%q,%q,%v)",
				i, tc.CaseID, tc.CaseSource, tc.CaseVerified, w.caseID, w.source, w.verified)
		}
		if len(tc.CasesInFlight) != 2 && i < 3 {
			t.Fatalf("call %d: candidate set should still list both cases, got %v", i, tc.CasesInFlight)
		}
	}
}

func TestCaseURLLifecycleFollowsRunCaseRefcounts(t *testing.T) {
	broker := newInferenceBroker(1)
	prepared := prepareBrokerSession(t, broker)
	sessionID := prepared["session_id"]

	if url, started := broker.beginRunCase(sessionID, "case-early"); !started || url != "" {
		t.Fatalf("without a recorded base: url=%q started=%v, want empty url + started", url, started)
	}
	broker.endRunCase(sessionID, "case-early")

	broker.setHarnessBase(sessionID, "http://host.docker.internal:11436/v1/inference")
	first, _ := broker.beginRunCase(sessionID, "case-R")
	second, _ := broker.beginRunCase(sessionID, "case-R")
	if first == "" || first != second {
		t.Fatalf("re-entered case must reuse its URL: %q vs %q", first, second)
	}
	token := first[strings.LastIndex(first, "/")+1:]
	session := broker.sessions[sessionID]
	session.mu.Lock()
	live := session.urlCases[token]
	session.mu.Unlock()
	if live != "case-R" {
		t.Fatalf("token resolves to %q, want case-R", live)
	}
	broker.endRunCase(sessionID, "case-R")
	session.mu.Lock()
	stillLive := session.urlCases[token]
	session.mu.Unlock()
	if stillLive != "case-R" {
		t.Fatal("token must survive while one re-entry is still in flight")
	}
	broker.endRunCase(sessionID, "case-R")
	session.mu.Lock()
	gone := session.urlCases[token]
	session.mu.Unlock()
	if gone != "" {
		t.Fatal("last end must invalidate the token")
	}
}
