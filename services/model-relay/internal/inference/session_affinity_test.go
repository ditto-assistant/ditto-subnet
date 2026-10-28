package inference

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestOpenRouterSessionAffinityBoundaries(t *testing.T) {
	ctx := context.Background()
	header := func(enabled bool, grant string, generation int64, model string) string {
		h := map[string]string{}
		attachOpenRouterSessionHeader(withOpenRouterSession(ctx, enabled, grant, generation, model), h)
		return h["X-Session-Id"]
	}
	base := header(true, "grant-a", 1, "model-a")
	if base == "" || len(base) > 256 || strings.Contains(base, "grant-a") {
		t.Fatalf("session is not opaque and bounded: %q", base)
	}
	if base != header(true, "grant-a", 1, "model-a") {
		t.Fatal("one lease generation lost affinity")
	}
	for _, other := range []string{
		header(false, "grant-a", 1, "model-a"),
		header(true, "grant-b", 1, "model-a"),
		header(true, "grant-a", 2, "model-a"),
		header(true, "grant-a", 1, "model-b"),
	} {
		if other == base {
			t.Fatal("affinity crossed an opt-out, grant, generation or model boundary")
		}
	}
	if got := header(false, "grant-a", 1, "model-a"); got != "" {
		t.Fatalf("disabled affinity changed headers: %q", got)
	}
	// Cancellation detachment must retain trusted routing values.
	h := map[string]string{"Authorization": "test-token"}
	attachOpenRouterSessionHeader(context.WithoutCancel(withOpenRouterSession(ctx, true, "grant-a", 1, "model-a")), h)
	if h["X-Session-Id"] != base || h["Authorization"] != "test-token" {
		t.Fatal("affinity changed credentials or disappeared on context detachment")
	}
}

// Provider KV reuse must never become completion reuse or prompt rewriting.
func TestOpenRouterSessionOnlyChangesRoutingHeader(t *testing.T) {
	var bodies [][]byte
	var sessions []string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(r.Body)
		if err != nil {
			t.Error(err)
			return
		}
		bodies = append(bodies, body)
		sessions = append(sessions, r.Header.Get("X-Session-Id"))
		_, _ = fmt.Fprint(w, strings.Replace(validAggregateChatBody, "gen-1", fmt.Sprintf("gen-%d", len(bodies)), 1))
	}))
	defer upstream.Close()
	payload := map[string]any{"model": "openai/gpt-oss-20b", "messages": []any{map[string]any{"role": "user", "content": "fixture"}}, "seed": 42, "temperature": 0}
	for i, enabled := range []bool{false, true, true} {
		ctx := withOpenRouterSession(context.Background(), enabled, "grant-a", 1, "openai/gpt-oss-20b")
		completed, exhausted := completeChatWithRecovery(ctx, upstream.Client(), aggregateChatConfig(upstream.URL), payload, "openai/gpt-oss-20b", "openrouter", "", nil, nil, func(context.Context, time.Duration) { t.Error("unexpected retry") })
		if exhausted != nil || completed == nil {
			t.Fatalf("request %d failed: %v", i, exhausted)
		}
		if !bytes.Contains(completed.raw, []byte(fmt.Sprintf("gen-%d", i+1))) {
			t.Fatal("a prior completion was reused")
		}
	}
	if len(bodies) != 3 || sessions[0] != "" || sessions[1] == "" || sessions[1] != sessions[2] {
		t.Fatalf("upstream calls/sessions: %d / %v", len(bodies), sessions)
	}
	if !bytes.Equal(bodies[0], bodies[1]) || !bytes.Equal(bodies[1], bodies[2]) {
		t.Fatal("affinity changed model payload")
	}
	if _, ok := payload["provider"]; ok {
		t.Fatal("caller payload mutated")
	}
}
