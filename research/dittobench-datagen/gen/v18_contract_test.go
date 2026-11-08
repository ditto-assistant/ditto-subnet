package gen

import (
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

// v18KnownVectorWant is the v18 accuracy contract's datagen-half vector (seed
// 123456789, full profile, public surface salt 0). docs/bench-versions.md and
// the README publish the same string.
const v18KnownVectorWant = "9029ef85b86ac44832ae5f3d7dadd0475cd14fb4271c00db2a6f78c8c5dc46d3"

// V18 is supported (promoted from staging by the cross-layer PR, with the epoch
// and seed domain it was staged with, so this vector did not move). Every lever is gated on bench_version >= 18; the v2..v17 vectors
// (TestV17KnownVector and the earlier ones) prove nothing below moved.
func TestV18KnownVector(t *testing.T) {
	prof, ok := ProfileForVersion("full", protocol.BenchVersionV18)
	if !ok {
		t.Fatal("v18 full profile missing")
	}
	a, err := GenerateDataset(123456789, prof, protocol.BenchVersionV18)
	if err != nil {
		t.Fatal(err)
	}
	digest, _, err := a.SHA256Hex()
	if err != nil {
		t.Fatal(err)
	}
	if digest != v18KnownVectorWant {
		t.Fatalf("v18 known vector: got %s, want %s", digest, v18KnownVectorWant)
	}
	if a.BenchVersion != 18 || a.GeneratedAt != "2027-10-01T00:00:00Z" {
		t.Fatalf("v18 envelope: version=%d epoch=%s", a.BenchVersion, a.GeneratedAt)
	}
	if !protocol.SupportedBenchVersion(protocol.BenchVersionV18) || protocol.CurrentBenchVersion != protocol.BenchVersionV8 {
		t.Fatal("v18 is supported, and supporting it does not move the preactivation default")
	}
	if len(a.ToolCases) != 50 || len(a.MemoryCases) != 125 {
		t.Fatalf("v18 datagen half: %d tool + %d memory cases, want 50 + 125", len(a.ToolCases), len(a.MemoryCases))
	}
	for _, doc := range []string{"../docs/bench-versions.md", "../README.md"} {
		if !strings.Contains(readFile(t, doc), v18KnownVectorWant) {
			t.Fatalf("%s does not publish the pinned v18 vector", doc)
		}
	}
}

// The v18 table is the dueling 125-case half with slots moved between
// families; only the full table changes.
func TestV18EnvelopeIsTheDuelingHalf(t *testing.T) {
	full, ok := MemoryEnvelopeForRunSize("full", protocol.BenchVersionV18)
	if !ok || full != V18FullEnvelope || full.Total() != 125 {
		t.Fatalf("full v18 envelope %+v (ok %v), total %d", full, ok, full.Total())
	}
	if full.Story != full.StoryArcs*full.StoryPerArc {
		t.Fatalf("story slot %d is not %d arcs x %d oracles", full.Story, full.StoryArcs, full.StoryPerArc)
	}
	if legacy := full.BusinessPrograms - full.EnterpriseCount(); legacy != 12 || full.EnterpriseCount() != 4 {
		t.Fatalf("business split: %d family + %d enterprise, want 12 + 4", legacy, full.EnterpriseCount())
	}
	for _, runSize := range []string{"small", "medium"} {
		v17, _ := MemoryEnvelopeForRunSize(runSize, protocol.BenchVersionV17)
		v18, _ := MemoryEnvelopeForRunSize(runSize, protocol.BenchVersionV18)
		if v17 != v18 {
			t.Fatalf("%s: v18 table %+v differs from v17 %+v", runSize, v18, v17)
		}
	}
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV18)
	if prof.Tools != 50 || prof.Mem != 125 || prof.IsoCases != full.Isolation {
		t.Fatalf("v18 full profile %+v", prof)
	}
	quota := v18AbstentionQuotas[full.Abstention]
	total := 0
	for _, family := range universe.V13AbsenceFamilies {
		if quota[family] < 1 {
			t.Fatalf("absence family %s dropped from the v18 slot", family)
		}
		total += quota[family]
	}
	if total != full.Abstention {
		t.Fatalf("abstention quota sums to %d, want %d", total, full.Abstention)
	}
}

// Every swept seed fills the v18 table exactly, asks three distinct business
// families (never client-vs-vendor), keeps the absence quota, and drops the
// saturated lesson-claims oracle from every asked arc.
func TestV18MemoryMixAcrossSeeds(t *testing.T) {
	seeds := 24
	if testing.Short() {
		seeds = 6
	}
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV18)
	familiesSeen := map[universe.V13Family]bool{}
	for seed := int64(1); seed <= int64(seeds); seed++ {
		a, err := GenerateDataset(seed, prof, protocol.BenchVersionV18)
		if err != nil {
			t.Fatalf("seed %d: %v", seed, err)
		}
		counts := map[string]int{}
		for _, c := range a.MemoryCases {
			counts[c.QuestionType]++
		}
		if counts["world-story-lesson-claims"] != 0 {
			t.Fatalf("seed %d asks the saturated lesson-claims oracle %d times", seed, counts["world-story-lesson-claims"])
		}
		if counts[universe.V13ProgramQuestionType] != 12 || counts[universe.V13PersonalQuestionType] != 8 || counts["world-isolation-contact-current"] != 5 {
			t.Fatalf("seed %d slot counts: business %d personal %d isolation %d", seed,
				counts[universe.V13ProgramQuestionType], counts[universe.V13PersonalQuestionType], counts["world-isolation-contact-current"])
		}
		for family, want := range v18AbstentionQuotas[12] {
			if got := counts[QTAbsence+family]; got != want {
				t.Fatalf("seed %d: %d %s probes, want %d", seed, got, family, want)
			}
		}
		if f := universe.V13FamilyOfVersion(seed, 0, protocol.BenchVersionV18); f != universe.V14FamilyHolderVsActor {
			t.Fatalf("seed %d business group 0 is %s, want holder-vs-actor", seed, f)
		}
		groups := map[universe.V13Family]bool{}
		for g := 0; g < 3; g++ {
			f := universe.V13FamilyOfVersion(seed, 4*g, protocol.BenchVersionV18)
			if f == universe.V13FamilyClientVsVendor {
				t.Fatalf("seed %d group %d draws client-vs-vendor", seed, g)
			}
			groups[f] = true
			familiesSeen[f] = true
		}
		if len(groups) != 3 {
			t.Fatalf("seed %d asks %d distinct business families, want 3", seed, len(groups))
		}
	}
	if !testing.Short() && len(familiesSeen) != len(universe.V18BusinessFamilies()) {
		t.Fatalf("%d seeds reached %d of %d v18 business families", seeds, len(familiesSeen), len(universe.V18BusinessFamilies()))
	}
}

func TestV18MixGate(t *testing.T) {
	if testing.Short() {
		t.Skip("mix gate sweep")
	}
	gate := MixGateForVersion(protocol.BenchVersionV18)
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV18)
	for seed := int64(1); seed <= 16; seed++ {
		a, err := GenerateDataset(seed, prof, protocol.BenchVersionV18)
		if err != nil {
			t.Fatal(err)
		}
		audit, err := AuditMix(a)
		if err != nil {
			t.Fatal(err)
		}
		if v := audit.Summarize(gate).Violations; len(v) != 0 {
			t.Fatalf("seed %d violates the v18 mix gate: %v", seed, v)
		}
	}
}

// A pool that under-supplies one absence family still fills the 12-pair slot:
// the quota pass runs first, then the remaining pairs top it up in pool order.
func TestV18AbstentionSubsetFillsTheSlot(t *testing.T) {
	pair := func(family string, n int) [][2]StagedCase {
		var out [][2]StagedCase
		for i := 0; i < n; i++ {
			c := StagedCase{Case: protocol.MemoryCase{QuestionType: QTAbsence + family}}
			out = append(out, [2]StagedCase{c, c})
		}
		return out
	}
	var pool [][2]StagedCase
	pool = append(pool, pair(universe.V13FamilyPureAbsence, 4)...)
	pool = append(pool, pair(universe.V13FamilyNearMiss, 3)...)
	pool = append(pool, pair(universe.V13FamilyStaleRemoved, 3)...)
	pool = append(pool, pair(universe.V13FamilyCrossUser, 1)...) // quota 2
	pool = append(pool, pair(universe.V13FamilyFalsePremise, 3)...)
	pool = append(pool, pair(universe.V13FamilyInsufficient, 2)...)
	if got := len(v18AbstentionSubset(pool, 12)); got != 12 {
		t.Fatalf("slot holds %d pairs, want 12", got)
	}
}
