package scorer

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Captured from the pre-v14 source 861814b58a38c0582e9fc73848566d1b6fe8c325.
// This file can be run unchanged against that source. Do not refresh this hash
// when implementing a new contract: changes to any v2-v13 result are regressions.
func TestV14HistoricalReplayParity(t *testing.T) {
	type vector struct {
		Version  int
		Category string
		Called   []string
		Observed bool
		Correct  bool
		Gate     float64
	}
	var vectors []vector
	for version := 2; version <= 13; version++ {
		for _, category := range []string{gen.QTDeclarativeAck, gen.QTLifecycleWrite, gen.QTChitchat, "single-session-recall"} {
			for _, called := range [][]string{nil, {"search_memories"}, {"save_memory"}, {"update_memory"}, {"delete_memory"}, {"set_theme"}, {"gmail_send"}, {"save_memory", "set_theme", "gmail_send"}} {
				for _, observed := range []bool{false, true} {
					for _, correct := range []bool{false, true} {
						input := []protocol.CaseScore{
							{Kind: protocol.KindMemory, Category: category, Observed: observed, Called: called, Correct: correct},
							{Kind: protocol.KindMemory, Category: "single-session-recall", Observed: true, Called: []string{"set_accent_color"}, Correct: true},
						}
						vectors = append(vectors, vector{version, category, called, observed, correct, CompositeGateForVersion(input, version)})
					}
				}
			}
		}
	}
	encoded, err := json.Marshal(vectors)
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(encoded)
	got := hex.EncodeToString(sum[:])
	const want = "165918b4e74c227baddb1a45cd634df40a619c3f3c1eff1c38667f57b2220a79"
	if got != want {
		t.Fatalf("%d historical replay vectors changed: got %s, want %s", len(vectors), got, want)
	}
}
