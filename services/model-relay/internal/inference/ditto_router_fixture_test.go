package inference

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// This fixture is the actual competition producer's JSON, not a hand-written
// gateway request. The product backend consumes the same bytes to check its
// user-billed admission/settlement contract. Update deliberately with
// DITTO_ROUTER_UPDATE_FIXTURES=1 and review both producer and consumer changes.
func TestDittoRouterCompetitionPayloadFixture(t *testing.T) {
	for _, tc := range []struct{ name, body string }{
		{"ditto_router_competition_request.json", chatBody},
		{"ditto_router_competition_ceiling_request.json", strings.Replace(chatBody, `"max_tokens":64`, `"max_tokens":32768`, 1)},
	} {
		t.Run(tc.name, func(t *testing.T) { checkDittoRouterCompetitionFixture(t, tc.name, tc.body) })
	}
}

func checkDittoRouterCompetitionFixture(t *testing.T, name, body string) {
	t.Helper()
	var input map[string]any
	decoder := json.NewDecoder(strings.NewReader(body))
	decoder.UseNumber()
	if err := decoder.Decode(&input); err != nil {
		t.Fatal(err)
	}
	if herr := validateRequestSchema(input); herr != nil {
		t.Fatal(herr)
	}
	maxTokens, herr := outputTokenLimit(input, 32768)
	if herr != nil {
		t.Fatal(herr)
	}
	payload, herr := lockedUpstreamPayload(input, v7Model, maxTokens, 13)
	if herr != nil {
		t.Fatal(herr)
	}
	attachPlatformMiddleOut(payload, len(body))
	raw, err := json.Marshal(payload)
	if err != nil {
		t.Fatal(err)
	}
	fixture := filepath.Join("testdata", name)
	if os.Getenv("DITTO_ROUTER_UPDATE_FIXTURES") == "1" {
		if err := os.MkdirAll(filepath.Dir(fixture), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(fixture, append(raw, '\n'), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	want, err := os.ReadFile(fixture)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(raw, bytes.TrimSpace(want)) {
		t.Fatalf("producer payload drifted; update and qualify the backend consumer:\n got %s\nwant %s", raw, want)
	}
	t.Logf("DITTO_COMPETITION_PAYLOAD %s=%s", name, raw)
}
