package gen

import (
	"context"
	"reflect"
	"sort"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

// Structural fixture only; never semantic or private qualification evidence.
type artifactFactRenderer struct{}

func (artifactFactRenderer) PlanDocument(_ context.Context, r universe.V13FactDocumentRequest) (universe.V13FactDocumentPlan, error) {
	p := universe.V13FactDocumentPlan{}
	for _, record := range r.Records {
		unique := map[string]bool{}
		for _, a := range record.Assertions {
			for _, token := range a.Arguments {
				unique[token] = true
			}
		}
		tokens := make([]string, 0, len(unique))
		for token := range unique {
			tokens = append(tokens, token)
		}
		sort.Strings(tokens)
		text := strings.Join(tokens, " ")
		if record.InteriorFacts {
			text = strings.Repeat(" Neutral texture.", 60) + text + strings.Repeat(" Neutral texture.", 60)
		} else if record.MinBytes > 1 {
			text += strings.Repeat(" Neutral texture.", 15)
		}
		p.Records = append(p.Records, text)
	}
	return p, nil
}
func (artifactFactRenderer) CheckDocument(context.Context, universe.V13FactDocumentRequest, universe.V13FactDocumentPlan) error {
	return nil
}

func (artifactFactRenderer) Plan(_ context.Context, r universe.V13FactRenderRequest) (universe.V13FactRenderPlan, error) {
	var p universe.V13FactRenderPlan
	for i := range p.Records {
		p.Records[i] = strings.Join(r.Required[i], " ")
	}
	p.Question = "Question about " + r.Subject
	if r.QuestionTemplate != "" {
		p.Question = r.QuestionTemplate
	}
	for _, f := range r.Facts {
		if f.Mode == "history" && f.Sequence == 0 {
			p.Records[f.Record] = "Initial state: " + p.Records[f.Record]
		}
		if f.Mode == "history" && f.Sequence == 1 {
			p.Records[f.Record] = "Final update: " + p.Records[f.Record]
		}
	}
	return p, nil
}
func (artifactFactRenderer) Check(context.Context, universe.V13FactRenderRequest, universe.V13FactRenderPlan) error {
	return nil
}

func TestV13FactArtifactRoundTrip(t *testing.T) {
	for _, size := range []string{"small", "medium", "full"} {
		t.Run(size, func(t *testing.T) {
			want, err := GenerateV13FactDataset(context.Background(), 42, 731, 92713, size, artifactFactRenderer{})
			if err != nil {
				t.Fatal(err)
			}
			pin, raw, err := want.SHA256Hex()
			if err != nil {
				t.Fatal(err)
			}
			got, err := DecodePrivateArtifact(raw, pin, 42, size)
			if err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(want, got) {
				t.Fatal("roundtrip lost grading authority")
			}
			if got.ExecutionWorldSeed() != 731 || got.Seed != 42 || got.SurfaceSalt != 0 {
				t.Fatal("world/lease identity or surface contract drift")
			}
			profile, _ := ProfileForVersion(size, 13)
			scale, _ := v8WorldProfile(profile.Mem)
			world := universe.GenerateForVersion(731, scale, 13)
			if err := world.RenderV13FactStories(context.Background(), artifactFactRenderer{}); err != nil {
				t.Fatal(err)
			}
			if err := world.RenderV13FactOrdinaryWorld(context.Background(), artifactFactRenderer{}); err != nil {
				t.Fatal(err)
			}
			if err := world.RenderV13FactBusinessImport(context.Background(), artifactFactRenderer{}); err != nil {
				t.Fatal(err)
			}
			shared := map[string]protocol.MemoryPair{}
			for _, p := range world.Pairs {
				p.Prompt = v13RotateInjectionMarkers(v13SurfaceSeed(731, 0), p.Prompt)
				shared[p.PairID] = p
			}
			matched := 0
			seenWorld := map[string]bool{}
			for _, tc := range got.ToolCases {
				for _, p := range tc.PrerequisitePairs {
					if mem, ok := shared[p.PairID]; ok {
						matched++
						seenWorld[p.PairID] = true
						if mem.Prompt != p.Prompt || mem.SessionID != p.SessionID || mem.Timestamp != p.Timestamp {
							t.Fatal("tool and memory phases disagree on rendered world")
						}
					}
				}
			}
			if matched < len(world.InitialPairs(world.StagedCorrectionMembership(world.V13Allocation(0)))) {
				t.Fatal("initial world did not reach tool prerequisites")
			}
			for _, p := range world.InitialPairs(world.StagedCorrectionMembership(world.V13Allocation(0))) {
				if !seenWorld[p.PairID] {
					t.Fatal("specific initial world record omitted from tool seed")
				}
			}
		})
	}
}

func TestFactPrerequisiteReconciliationBoundaries(t *testing.T) {
	p := protocol.MemoryPair{PairID: "shared", SessionID: "session", Timestamp: "time", Prompt: "old", Response: "old response"}
	toolOnly := protocol.MemoryPair{PairID: "tool-only", Prompt: "scoped decision"}
	checked := p
	checked.Prompt = "checked"
	checked.Response = "checked response"
	future := protocol.MemoryPair{PairID: "future", Prompt: "later correction"}
	tools := []protocol.ToolCase{{ID: "tool", PrerequisitePairs: []protocol.MemoryPair{p, toolOnly}}}
	waves := []protocol.SeedRequest{{UserID: PrimaryUser, Pairs: []protocol.MemoryPair{checked, future}}, {UserID: "other-user", Pairs: []protocol.MemoryPair{{PairID: "shared", Prompt: "other user's value"}}}}
	out, err := reconcileFactPrerequisites(tools, waves)
	if err != nil {
		t.Fatal(err)
	}
	if len(out[0].PrerequisitePairs) != 2 || out[0].PrerequisitePairs[0] != checked || out[0].PrerequisitePairs[1] != toolOnly || tools[0].PrerequisitePairs[0] != p {
		t.Fatal("reconciliation changes scope, stage or source")
	}
	waves[0].Pairs[0].SessionID = "different"
	if _, err := reconcileFactPrerequisites(tools, waves); err == nil {
		t.Fatal("session identity mismatch accepted")
	}
	waves[0].Pairs[0] = checked
	conflict := checked
	conflict.Prompt = "conflicting"
	waves = append(waves, protocol.SeedRequest{UserID: PrimaryUser, Pairs: []protocol.MemoryPair{conflict}})
	if _, err := reconcileFactPrerequisites(tools, waves); err == nil {
		t.Fatal("inconsistent shared identity accepted")
	}
}

func TestV13FactArtifactRejectsTampering(t *testing.T) {
	for _, mode := range []string{"answer", "surface", "world", "transcript", "revision", "legacy-salt"} {
		t.Run(mode, func(t *testing.T) {
			a, err := GenerateV13FactDataset(context.Background(), 42, 731, 92713, "small", artifactFactRenderer{})
			if err != nil {
				t.Fatal(err)
			}
			switch mode {
			case "answer":
				a.MemoryCases[0].ExpectedAnswer = "forged"
			case "surface":
				a.MemoryCases[0].Question = "forged"
			case "world":
				a.FactGeneration.WorldSeed++
			case "transcript":
				a.FactGeneration.Events = a.FactGeneration.Events[:1]
			case "revision":
				a.FactGeneration.Revision = "unknown"
			case "legacy-salt":
				a.SurfaceSalt = 3
			}
			pin, raw, err := a.SHA256Hex()
			if err != nil {
				t.Fatal(err)
			}
			if _, err := DecodePrivateArtifact(raw, pin, 42, "small"); err == nil {
				t.Fatal("tampering accepted despite new object digest")
			}
		})
	}
}
