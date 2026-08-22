// Real-Postgres tests for the inference trace capture hooks: a settled chat
// call, a settled embedding call and an admission decline each produce one
// record carrying the bodies, the provider exchange, usage and grant context.
package inference

import (
	"bufio"
	"context"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/ditto-assistant/model-relay/internal/traces"
)

func newTraceSpool(t *testing.T) (*traces.Spooler, string) {
	t.Helper()
	dir := t.TempDir()
	spool, err := traces.NewSpooler(traces.SpoolOptions{Dir: dir, RotateBytes: 1 << 30, RotateInterval: time.Hour, Instance: "test-relay:8010", Commit: "c0ffee"})
	if err != nil {
		t.Fatal(err)
	}
	return spool, dir
}

// drainTraces closes the spool and returns every record in ready/, oldest first.
func drainTraces(t *testing.T, spool *traces.Spooler, dir string) []traces.Record {
	t.Helper()
	if err := spool.Close(context.Background()); err != nil {
		t.Fatal(err)
	}
	entries, err := os.ReadDir(filepath.Join(dir, "ready"))
	if err != nil {
		t.Fatal(err)
	}
	var out []traces.Record
	for _, e := range entries {
		if !strings.HasSuffix(e.Name(), ".jsonl") {
			continue
		}
		f, err := os.Open(filepath.Join(dir, "ready", e.Name()))
		if err != nil {
			t.Fatal(err)
		}
		sc := bufio.NewScanner(f)
		sc.Buffer(make([]byte, 1<<20), 16<<20)
		for sc.Scan() {
			var r traces.Record
			if err := json.Unmarshal(sc.Bytes(), &r); err != nil {
				t.Fatalf("record line: %v", err)
			}
			out = append(out, r)
		}
		_ = f.Close()
	}
	return out
}

func TestChatSettlementIsTracedWithBodiesAndProviderExchange(t *testing.T) {
	var captured map[string]any
	upstream := fakeChatUpstream(t, &captured)
	defer upstream.Close()
	f := newPGFixture(t, chatTestConfig(t, upstream.URL))
	f.seedRoute(t)
	spool, dir := newTraceSpool(t)
	f.deps.Traces = spool

	nonce := uuid.New()
	body := []byte(chatBody)
	headers := f.signedProxyHeaders(1, nonce, body)
	headers["X-Ditto-Trace-Context"] = `{"v":1,"run_id":"run-42","agent_id":"` + f.agentID.String() + `","case_id":"web_search-0007","case_source":"in_flight","case_verified":true,"cases_in_flight":["web_search-0007"]}`
	w := serve(f.deps, proxyRequest("/api/v1/inference/chat/completions", string(body), headers))
	if w.Code != 200 {
		t.Fatalf("want 200, got %d: %s", w.Code, w.Body.String())
	}
	recs := drainTraces(t, spool, dir)
	if len(recs) != 1 {
		t.Fatalf("want 1 trace record, got %d", len(recs))
	}
	r := recs[0]
	if r.Schema != traces.SchemaVersion || r.Event != traces.EventSettled || r.Relay.Instance != "test-relay:8010" || r.Relay.Commit != "c0ffee" {
		t.Fatalf("envelope: %+v", r)
	}
	if r.Request.RunID != "run-42" || r.Request.CaseID != "web_search-0007" || !strings.Contains(string(r.Request.Context), `"case_verified":true`) {
		t.Fatalf("broker trace context must be recorded verbatim and lifted: run=%q case=%q ctx=%s", r.Request.RunID, r.Request.CaseID, r.Request.Context)
	}
	if r.Request.Lane != "inference" || r.Request.Kind != "chat" || r.Request.GrantID != f.grantID.String() || r.Request.Nonce != nonce.String() || r.Request.Generation != 1 {
		t.Fatalf("request: %+v", r.Request)
	}
	if string(r.Request.Body) != chatBody || r.Request.BodySHA256 != traces.SHA256Hex(body) || r.Request.RequestID == "" {
		t.Fatalf("request body/id: %s %s %q", r.Request.Body, r.Request.BodySHA256, r.Request.RequestID)
	}
	if r.Grant == nil || r.Grant.AgentID != f.agentID.String() || r.Grant.BenchVersion != 9 || r.Grant.ValidatorHotkey != pgTestHotkey || r.Grant.Model != pgTestModel || r.Grant.RouteProvider != "openrouter" {
		t.Fatalf("grant: %+v", r.Grant)
	}
	if r.Upstream == nil || r.Upstream.Provider != "deepinfra" || r.Upstream.Attempts != 1 || len(r.Upstream.Phases) != 1 {
		t.Fatalf("upstream: %+v", r.Upstream)
	}
	phase := r.Upstream.Phases[0]
	if phase.Status != 200 || phase.Route != "openrouter" || !strings.Contains(string(phase.Body), `"provider":"deepinfra"`) {
		t.Fatalf("phase must carry the RAW provider body: %+v", phase)
	}
	var sent map[string]any
	if err := json.Unmarshal(phase.Payload, &sent); err != nil || sent["model"] != pgTestModel || sent["provider"] == nil {
		t.Fatalf("phase payload must be the locked upstream payload incl. provider prefs: %s", phase.Payload)
	}
	if _, leaked := phase.Headers["authorization"]; leaked {
		t.Fatalf("credential header captured")
	}
	if r.Response == nil || r.Response.HTTPStatus != 200 || !r.Response.Deliverable || string(r.Response.Body) != w.Body.String() {
		t.Fatalf("response: %+v", r.Response)
	}
	if r.Usage == nil || r.Usage.PromptTokens != 12 || r.Usage.CompletionTokens != 5 || r.Usage.CostMicrousd != 2100 || !r.Usage.UsageAvailable {
		t.Fatalf("usage: %+v", r.Usage)
	}
	if r.Outcome == nil || r.Outcome.Status != "completed" || r.Admission == nil || r.Admission.ReservedTokens <= 0 {
		t.Fatalf("outcome/admission: %+v %+v", r.Outcome, r.Admission)
	}
}

func TestDeclineIsTracedWithReason(t *testing.T) {
	upstream := fakeChatUpstream(t, nil)
	defer upstream.Close()
	f := newPGFixture(t, chatTestConfig(t, upstream.URL))
	f.seedRoute(t)
	spool, dir := newTraceSpool(t)
	f.deps.Traces = spool

	nonce := uuid.New()
	body := []byte(chatBody)
	headers := f.signedProxyHeaders(1, nonce, body)
	// An oversized or non-object context is ignored, never a 4xx.
	headers["X-Ditto-Trace-Context"] = `[1,2,3]`
	if w := serve(f.deps, proxyRequest("/api/v1/inference/chat/completions", string(body), headers)); w.Code != 200 {
		t.Fatalf("first call: %d %s", w.Code, w.Body.String())
	}
	// Same nonce again: the replay decline.
	if w := serve(f.deps, proxyRequest("/api/v1/inference/chat/completions", string(body), headers)); w.Code != 429 {
		t.Fatalf("replay should be declined: %d %s", w.Code, w.Body.String())
	}
	recs := drainTraces(t, spool, dir)
	if len(recs) != 2 {
		t.Fatalf("want settled + declined, got %d", len(recs))
	}
	declined := recs[1]
	if declined.Event != traces.EventDeclined || declined.Admission == nil || declined.Admission.Decline != "nonce_replayed" {
		t.Fatalf("declined record: %+v %+v", declined.Event, declined.Admission)
	}
	if len(recs[0].Request.Context) != 0 || recs[0].Request.CaseID != "" {
		t.Fatalf("non-object context must be dropped: %s", recs[0].Request.Context)
	}
	if declined.Grant == nil || declined.Grant.AgentID != f.agentID.String() || string(declined.Request.Body) != chatBody {
		t.Fatalf("declined record must keep grant + body: %+v", declined)
	}
}

func TestEmbeddingSettlementIsTracedAndVectorsCanBeStripped(t *testing.T) {
	vector := make([]byte, 768)
	for i := range vector {
		vector[i] = byte(i % 256)
	}
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"model":"pplx-embed-v1-0.6b","data":[{"index":0,"embedding":"` +
			base64.StdEncoding.EncodeToString(vector) + `"}],"usage":{"prompt_tokens":5}}`))
	}))
	defer upstream.Close()
	cfg := testConfig(t, map[string]string{
		"DITTO_INFERENCE_TIMEOUT_SECONDS":   "5",
		"PERPLEXITY_API_KEY":                "test-pplx-key",
		"INFERENCE_TRACE_EMBEDDING_VECTORS": "false",
	})
	cfg.Inference.EmbeddingFallbackURL = upstream.URL
	f := newPGFixture(t, cfg)
	spool, dir := newTraceSpool(t)
	f.deps.Traces = spool

	nonce := uuid.New()
	body := []byte(embeddingBody())
	w := serve(f.deps, proxyRequest("/api/v1/inference/embeddings", string(body), f.signedProxyHeaders(1, nonce, body)))
	if w.Code != 200 {
		t.Fatalf("want 200, got %d: %s", w.Code, w.Body.String())
	}
	recs := drainTraces(t, spool, dir)
	if len(recs) != 1 {
		t.Fatalf("want 1 record, got %d", len(recs))
	}
	r := recs[0]
	if r.Request.Kind != "embedding" || string(r.Request.Body) != embeddingBody() || r.Usage == nil || r.Usage.PromptTokens != 5 {
		t.Fatalf("embedding record: %+v", r)
	}
	if r.Upstream == nil || len(r.Upstream.Phases) != 1 || r.Upstream.Phases[0].Route != "direct" || r.Upstream.Phases[0].Status != 200 {
		t.Fatalf("embedding upstream: %+v", r.Upstream)
	}
	if !strings.Contains(string(r.Response.Body), `"<stripped>"`) || len(r.Response.Body) > 400 {
		t.Fatalf("vectors should be stripped when INFERENCE_TRACE_EMBEDDING_VECTORS=false: %s", truncateStr(string(r.Response.Body), 200))
	}
	if !strings.Contains(string(r.Upstream.Phases[0].Body), `"<stripped>"`) {
		t.Fatalf("raw provider body should also be stripped: %s", truncateStr(string(r.Upstream.Phases[0].Body), 200))
	}
	if !strings.Contains(string(r.Upstream.Payload), `"hello world"`) {
		t.Fatalf("embedding inputs must be captured: %s", r.Upstream.Payload)
	}
}

func truncateStr(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "..."
}
