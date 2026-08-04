package gen

import (
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/persona"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// evidenceCarriesValue is the reproducibility invariant (plan level): for every
// fact, the pair the evidence map points at contains the fact's canonical value
// verbatim. If this holds, the answer token a memory case is graded on is always
// seeded. Generation is non-LLM, so the value comes straight from the beat's
// deterministic template surface.
func evidenceCarriesValue(t *testing.T, plan *persona.Plan, pairs []protocol.MemoryPair, evidence map[string]string) {
	t.Helper()
	byID := map[string]protocol.MemoryPair{}
	for _, p := range pairs {
		byID[p.PairID] = p
	}
	recurGrounded := map[string]bool{} // attr -> label seen in at least one mention
	for _, f := range plan.Facts {
		pid, ok := evidence[f.ID]
		if !ok {
			t.Fatalf("fact %s has no evidence pair", f.ID)
		}
		pair, ok := byID[pid]
		if !ok {
			t.Fatalf("evidence pair %s for fact %s missing from haystack", pid, f.ID)
		}
		joined := strings.ToLower(pair.Prompt + " " + pair.Response)
		if f.Kind == persona.KindRecurring {
			// Coreference (anti-gaming V4): only the anchor mention names the topic
			// in full; follow-ups refer to it obliquely, so the label appears once,
			// not K times. Grounding (the label present in >=1 mention) is checked
			// after the loop; per-mention containment does not hold by design.
			if strings.Contains(joined, strings.ToLower(f.Value)) {
				recurGrounded[f.Attribute] = true
			}
			continue
		}
		if !strings.Contains(joined, strings.ToLower(f.Value)) {
			t.Fatalf("fact %s value %q not present in its evidence pair %q", f.ID, f.Value, joined)
		}
	}
	// Every recurring topic must be grounded by its full label in at least one
	// mention, so the coreference chain has an anchor a reader can resolve.
	for _, f := range plan.Facts {
		if f.Kind == persona.KindRecurring && !recurGrounded[f.Attribute] {
			t.Fatalf("recurring topic %q never grounded by its full label in any mention", f.Attribute)
		}
	}
}

func TestRenderHaystackTemplateDeterministic(t *testing.T) {
	plan := persona.BuildPlan(42, persona.DefaultOpts())
	a, evA := RenderHaystack(plan)
	b, evB := RenderHaystack(plan)
	if len(a) != len(b) {
		t.Fatalf("pair count differs: %d vs %d", len(a), len(b))
	}
	ja, _ := json.Marshal(a)
	jb, _ := json.Marshal(b)
	if string(ja) != string(jb) {
		t.Fatal("template-only render is not deterministic")
	}
	if len(evA) != len(evB) {
		t.Fatal("evidence maps differ")
	}
	evidenceCarriesValue(t, plan, a, evA)

	// Timestamps: strictly non-decreasing across the flat pair list and all ≤ the
	// pinned epoch (the haystack is the past).
	var prev time.Time
	for i, p := range a {
		ts, err := time.Parse(time.RFC3339, p.Timestamp)
		if err != nil {
			t.Fatalf("pair %s bad timestamp %q: %v", p.PairID, p.Timestamp, err)
		}
		if ts.After(protocol.DatasetEpoch) {
			t.Fatalf("pair %s timestamp %s after epoch", p.PairID, p.Timestamp)
		}
		if i > 0 && ts.Before(prev) {
			t.Fatalf("pair %s timestamp goes backward", p.PairID)
		}
		prev = ts
	}
}
