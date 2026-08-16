package gen

import (
	"reflect"
	"strconv"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

func TestV11GenerationIsExplicitAndNotActivated(t *testing.T) {
	if protocol.CurrentBenchVersion != protocol.BenchVersionV8 {
		t.Fatalf("v11 scaffold changed active version to %d", protocol.CurrentBenchVersion)
	}
	if !protocol.SupportedBenchVersion(protocol.BenchVersionV11) {
		t.Fatal("v11 deterministic generation is not supported")
	}
	for _, runSize := range []string{"small", "medium", "full"} {
		v10, _ := ProfileForVersion(runSize, protocol.BenchVersionV10)
		v11, ok := ProfileForVersion(runSize, protocol.BenchVersionV11)
		if !ok || v10 != v11 {
			t.Errorf("v11 %s profile=(%+v,%v), want the v10 envelope %+v", runSize, v11, ok, v10)
		}
	}
}

func TestV11ProgramsSampleShapesAndRegenerateExactly(t *testing.T) {
	a, err := universe.GenerateV11Programs(41, 40)
	if err != nil {
		t.Fatal(err)
	}
	b, err := universe.GenerateV11Programs(41, 40)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(a, b) {
		t.Fatal("same v11 seed did not regenerate identical programs")
	}
	ops := map[string]bool{}
	for _, generated := range a {
		collectOps(generated.Provenance.Program, ops)
		if generated.Provenance.Revision != universe.V11ProvenanceRevision {
			t.Fatalf("case %s carries revision %q", generated.Plan.Case.ID, generated.Provenance.Revision)
		}
	}
	if !ops["subtract"] || (!ops["add"] && !ops["max"]) {
		t.Fatalf("v11 groups did not sample varied program shapes: %v", ops)
	}
}

func TestV11AnswersStayPositiveAcrossSeeds(t *testing.T) {
	for seed := int64(1); seed <= 25; seed++ {
		generated, err := universe.GenerateV11Programs(seed, 40)
		if err != nil {
			t.Fatalf("seed %d: %v", seed, err)
		}
		for _, g := range generated {
			answer, err := strconv.Atoi(g.Plan.Case.ExpectedAnswer)
			if err != nil || answer <= 0 {
				t.Fatalf("seed %d case %s expected answer %q", seed, g.Plan.Case.ID, g.Plan.Case.ExpectedAnswer)
			}
		}
	}
}

func TestV11SurfaceDropsV10LiteralAnchors(t *testing.T) {
	generated, err := universe.GenerateV11Programs(123456789, 40)
	if err != nil {
		t.Fatal(err)
	}
	questionSurfaces := map[string]bool{}
	descriptiveBinding := false
	for _, g := range generated {
		q := g.Plan.Case.Question
		// The four fixed v10 question templates all named the alias directly
		// and opened with one of four exact strings; v11 must not reuse them.
		for _, v10Literal := range []string{
			"Using this run's glossary, reconcile",
			"interpret the local schema rather than guessing field names",
			"Resolve the", "Read the per-run field meanings for",
		} {
			if strings.Contains(q, v10Literal) {
				t.Fatalf("v11 question reuses v10 template surface %q: %s", v10Literal, q)
			}
		}
		questionSurfaces[q[:24]] = true
		if strings.Contains(q, "the workstream whose") {
			descriptiveBinding = true
		}
		for _, pair := range g.Pairs {
			if strings.Contains(pair.Prompt, "I am going to describe one line from a custom workspace schema") {
				t.Fatalf("v11 record reuses the fixed v10 conversation lead: %s", pair.Prompt)
			}
			if strings.HasPrefix(pair.Response, "Understood — I will interpret those labels") {
				t.Fatalf("v11 ack reuses the fixed v10 acknowledgement: %s", pair.Response)
			}
		}
	}
	if len(questionSurfaces) < 4 {
		t.Fatalf("v11 question openers collapsed to %d distinct surfaces", len(questionSurfaces))
	}
	if !descriptiveBinding {
		t.Fatal("no v11 group used descriptive entity binding")
	}
}

func TestV11DatasetAppliesSurfaceNoiseAndKeepsV10Frozen(t *testing.T) {
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV11)
	v11, err := GenerateDataset(123456789, prof, protocol.BenchVersionV11)
	if err != nil {
		t.Fatal(err)
	}
	v10a, err := GenerateDataset(123456789, prof, protocol.BenchVersionV10)
	if err != nil {
		t.Fatal(err)
	}
	v10b, err := GenerateDataset(123456789, prof, protocol.BenchVersionV10)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(v10a, v10b) {
		t.Fatal("v10 generation stopped being deterministic")
	}

	// v11 memory cases still bind provenance and complete metamorphic groups.
	groups := map[string]int{}
	v11Cases := 0
	for _, c := range v11.MemoryCases {
		if c.V10Provenance == nil {
			continue
		}
		v11Cases++
		groups[c.V10Provenance.MetamorphicGroup]++
		if c.V10Provenance.Revision != universe.V11ProvenanceRevision {
			t.Fatalf("case %s revision %q", c.ID, c.V10Provenance.Revision)
		}
	}
	if v11Cases != 40 || len(groups) != 10 {
		t.Fatalf("v11 program coverage cases/groups=%d/%d, want 40/10", v11Cases, len(groups))
	}

	// The surface pass rewrote the fixed stored-directive markers.
	for _, wave := range v11.MemoryWaves {
		for _, pair := range wave.Pairs {
			if strings.Contains(pair.Prompt, "[SYNC]") {
				t.Fatalf("v11 kept the fixed [SYNC] injection marker: %s", pair.Prompt)
			}
		}
	}

	// The typo projector edited framing across many records shared with the
	// v10 world (same PairID, different surface). Values must survive: every
	// v11 expected answer still appears verbatim where its v10 twin's did.
	v10ByPair := map[string]string{}
	for _, wave := range v10a.MemoryWaves {
		for _, pair := range wave.Pairs {
			v10ByPair[pair.PairID] = pair.Prompt + "\x00" + pair.Response
		}
	}
	shared, changed := 0, 0
	for _, wave := range v11.MemoryWaves {
		for _, pair := range wave.Pairs {
			original, ok := v10ByPair[pair.PairID]
			if !ok {
				continue
			}
			shared++
			if original != pair.Prompt+"\x00"+pair.Response {
				changed++
			}
		}
	}
	if shared == 0 {
		t.Fatal("v11 shares no world pairs with v10; framing comparison impossible")
	}
	if changed*10 < shared {
		t.Fatalf("v11 surface pass edited only %d of %d shared records", changed, shared)
	}
}

func collectOps(node universe.V10QueryNode, out map[string]bool) {
	out[node.Op] = true
	for _, child := range node.Children {
		collectOps(child, out)
	}
}
