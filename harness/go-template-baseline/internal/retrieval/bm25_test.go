package retrieval

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/heyditto/ditto-subnet/go/bittensor"
)

func TestBM25Handler_RanksExpectedPair(t *testing.T) {
	dir := t.TempDir()

	pairsPath := filepath.Join(dir, "alice", "pairs.jsonl")
	if err := os.MkdirAll(filepath.Dir(pairsPath), 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	pairs := []map[string]string{
		{"pair_id": "p1", "content": "I am driving a Kubernetes migration project this quarter"},
		{"pair_id": "p2", "content": "Bought groceries yesterday including bread and avocados"},
		{"pair_id": "p3", "content": "Prefer dark mode in VS Code and the iOS apps stay dark"},
	}
	f, _ := os.Create(pairsPath)
	enc := json.NewEncoder(f)
	for _, p := range pairs {
		_ = enc.Encode(p)
	}
	f.Close()

	manifest := map[string]any{
		"users": map[string]any{
			"alice": map[string]string{"pairs": "alice/pairs.jsonl"},
		},
	}
	mb, _ := json.Marshal(manifest)
	_ = os.WriteFile(filepath.Join(dir, "manifest.json"), mb, 0o644)
	t.Setenv("DITTO_FIXTURES_PATH", dir)

	h := NewBM25Handler()
	resp, err := h.Handle(context.Background(), bittensor.ChallengeRequest{
		Mechanism:     bittensor.MechanismRetrieval,
		Query:         "what project am I working on?",
		UserFixtureID: "alice",
		K:             3,
	})
	if err != nil {
		t.Fatalf("Handle: %v", err)
	}
	if len(resp.EvidenceIDs) == 0 || resp.EvidenceIDs[0] != "p1" {
		t.Fatalf("expected p1 ranked first, got %v", resp.EvidenceIDs)
	}
}

func TestBM25Handler_RefusesWhenManifestMissing(t *testing.T) {
	t.Setenv("DITTO_FIXTURES_PATH", t.TempDir())
	h := NewBM25Handler()
	resp, err := h.Handle(context.Background(), bittensor.ChallengeRequest{
		Mechanism:     bittensor.MechanismRetrieval,
		Query:         "anything",
		UserFixtureID: "alice",
		K:             5,
	})
	if err != nil {
		t.Fatalf("Handle: %v", err)
	}
	if resp.Refusal == "" {
		t.Fatalf("expected refusal when manifest missing, got %+v", resp)
	}
}

func TestBM25Handler_RefusesWhenUserUnknown(t *testing.T) {
	dir := t.TempDir()
	// Empty manifest -> known users map but no entries.
	_ = os.WriteFile(filepath.Join(dir, "manifest.json"), []byte(`{"users":{}}`), 0o644)
	t.Setenv("DITTO_FIXTURES_PATH", dir)
	h := NewBM25Handler()
	resp, _ := h.Handle(context.Background(), bittensor.ChallengeRequest{
		Mechanism:     bittensor.MechanismRetrieval,
		Query:         "anything",
		UserFixtureID: "bob",
		K:             5,
	})
	if resp.Refusal != "unknown_user_fixture" {
		t.Fatalf("expected unknown_user_fixture refusal, got %+v", resp)
	}
}
