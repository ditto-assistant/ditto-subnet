package universe

import (
	"context"
	"encoding/json"
	"reflect"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

type storyDocumentFixture struct {
	documentRendererFixture
	rejectAt int
}

func (f *storyDocumentFixture) PlanDocument(ctx context.Context, r V13FactDocumentRequest) (V13FactDocumentPlan, error) {
	p, err := f.documentRendererFixture.PlanDocument(ctx, r)
	for i := range p.Records {
		p.Records[i] = strings.Repeat(" Neutral texture.", 60) + p.Records[i] + strings.Repeat(" Neutral texture.", 60)
	}
	return p, err
}
func (f *storyDocumentFixture) CheckDocument(ctx context.Context, r V13FactDocumentRequest, p V13FactDocumentPlan) error {
	f.reject = f.rejectAt > 0 && f.checks+1 == f.rejectAt
	return f.documentRendererFixture.CheckDocument(ctx, r, p)
}

func TestV13StoryDocumentAtomicApplication(t *testing.T) {
	for _, rejectAt := range []int{0, 2} {
		w := v13World(1)
		before := append([]protocol.MemoryPair(nil), w.Pairs...)
		f := &storyDocumentFixture{rejectAt: rejectAt}
		err := w.RenderV13FactStories(context.Background(), f)
		if rejectAt > 0 {
			if err == nil || !reflect.DeepEqual(before, w.Pairs) {
				t.Fatal("failed render modified world")
			}
			continue
		}
		if err != nil {
			t.Fatal(err)
		}
		storyIDs := map[string]bool{}
		for _, s := range w.Stories {
			storyIDs[s.PairID] = true
		}
		changed := 0
		for i, pair := range w.Pairs {
			original := before[i]
			if storyIDs[pair.PairID] {
				if pair.Prompt == original.Prompt {
					t.Fatal("story not rendered")
				}
				changed++
				pair.Prompt = original.Prompt
			}
			if pair != original {
				t.Fatal("non-prose authority changed")
			}
		}
		if changed != len(w.Stories) || f.checks != len(w.StoryArcs) {
			t.Fatal("incomplete story coverage")
		}
	}
}

func TestV13StoryDocumentTypedAuthority(t *testing.T) {
	effects := map[string]bool{}
	for seed := int64(1); seed <= 12; seed++ {
		w := v13World(seed)
		for index, arc := range w.StoryArcs {
			r, ids, err := w.V13StoryDocument(index)
			if err != nil {
				t.Fatalf("seed %d arc %d: %v", seed, index, err)
			}
			if len(ids) != len(arc.V2.PairIDs)+1 || ids[len(ids)-1] != arc.V2.DecoyPairID {
				t.Fatal("record identity lost")
			}
			for record, rec := range r.Records {
				for _, a := range rec.Assertions {
					for _, token := range a.Arguments {
						value := r.Bindings[token]
						if record > 1 && record < len(ids)-1 && (value == arc.V2.SubjectAlias || value == arc.V2.JoinKey1) {
							t.Fatal("later record leaked anchor or first join")
						}
					}
				}
			}
			q := arc.V2.Quantity
			matchedQuantity := q == nil
			for _, e := range arc.V2.Events {
				if e.Slots["qtyphrase"] != "" || e.Slots["qtybase"] != "" || e.Kind == EventApprovalCapped {
					if e.QuantityEffect == nil {
						t.Fatal("quantity only exists as prose")
					}
				}
				if effect := e.QuantityEffect; effect != nil {
					effects[effect.Op] = true
					if q != nil && e.Memory == q.Memory && effect.Kind == q.Kind && effect.Op == q.Op && effect.Operand == q.Operand && effect.Operand2 == q.Operand2 {
						matchedQuantity = true
					}
				}
			}
			if !matchedQuantity {
				t.Fatal("graded quantity has no source operands")
			}
			// Prose cannot become source authority through an accidental fallback.
			before, _ := json.Marshal(r)
			for i := range arc.V2.Events {
				arc.V2.Events[i].Slots["qtyphrase"] = "poisoned prose"
				arc.V2.Events[i].Slots["qtybase"] = "poisoned prose"
			}
			// Only effect-bearing events have quantity prose in production; retain
			// that invariant while poisoning its actual contents.
			for i := range arc.V2.Events {
				if arc.V2.Events[i].QuantityEffect == nil {
					delete(arc.V2.Events[i].Slots, "qtyphrase")
					delete(arc.V2.Events[i].Slots, "qtybase")
				}
			}
			for i := range w.Stories {
				if w.Stories[i].ArcIndex != index {
					continue
				}
				w.Stories[i].Beginning = StorySection{}
				w.Stories[i].Middle = StorySection{}
				w.Stories[i].End = StorySection{}
				w.Stories[i].Facts = nil
			}
			after, afterIDs, err := w.V13StoryDocument(index)
			encoded, _ := json.Marshal(after)
			if err != nil || string(before) != string(encoded) || !reflect.DeepEqual(ids, afterIDs) {
				t.Fatal("document depends on rendered prose")
			}
		}
	}
	for _, op := range []string{"initial", "delay", "replace", "add", "subtract"} {
		if !effects[op] {
			t.Fatalf("unexercised effect %s", op)
		}
	}
}

func TestV13StoryDocumentRejectsIncompleteSource(t *testing.T) {
	for _, mode := range []string{"decoy", "quantity", "event"} {
		t.Run(mode, func(t *testing.T) {
			w := v13World(1)
			switch mode {
			case "decoy":
				for i := range w.Stories {
					if w.Stories[i].PairID == w.StoryArcs[0].V2.DecoyPairID {
						w.Stories[i].DecoySource = nil
					}
				}
			case "quantity":
				for i := range w.StoryArcs[0].V2.Events {
					e := &w.StoryArcs[0].V2.Events[i]
					if e.QuantityEffect != nil {
						e.QuantityEffect = nil
						e.Slots["qtyphrase"] = "legacy prose"
						break
					}
				}
			case "event":
				w.StoryArcs[0].V2.Events[0].Kind = "unsupported"
			}
			if _, _, err := w.V13StoryDocument(0); err == nil {
				t.Fatal("incomplete source accepted")
			}
		})
	}
}

func TestV13StoryPrivateSourceNotSerialized(t *testing.T) {
	w := v13World(1)
	data, err := json.Marshal(w.Stories)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(data), "DecoySource") {
		t.Fatal("private decoy source serialized")
	}
	data, err = json.Marshal(w.StoryArcs)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(data), "QuantityEffect") {
		t.Fatal("private quantity source serialized")
	}
}
