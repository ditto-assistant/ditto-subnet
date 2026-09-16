package gen

import (
	"fmt"
	"strings"
	"sync"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// TestV13EnvelopeGenerationIsExplicitAndNotActivated: v13 generates deterministically
// behind an explicit profile decision while v8 remains the advertised version.
func TestV13EnvelopeGenerationIsExplicitAndNotActivated(t *testing.T) {
	if protocol.CurrentBenchVersion != protocol.BenchVersionV8 {
		t.Fatalf("v13 scaffold changed active version to %d", protocol.CurrentBenchVersion)
	}
	if !protocol.SupportedBenchVersion(protocol.BenchVersionV13) {
		t.Fatal("v13 deterministic generation is not supported")
	}
	if protocol.NewestSupportedBenchVersion() != protocol.BenchVersionV13 {
		t.Fatalf("newest supported version %d, want 13", protocol.NewestSupportedBenchVersion())
	}
	want := map[string]Profile{
		"small":  {Tools: 6, Mem: 6, Waves: 1, RawPairsFrac: 0, IsoCases: 0},
		"medium": {Tools: 48, Mem: 64, Waves: 4, RawPairsFrac: 0.45, IsoCases: 5},
		"full":   {Tools: 100, Mem: 224, Waves: 5, RawPairsFrac: 0.5, IsoCases: 9},
	}
	for runSize, expected := range want {
		got, ok := ProfileForVersion(runSize, protocol.BenchVersionV13)
		if !ok || got != expected {
			t.Errorf("v13 %s profile=(%+v,%v), want %+v", runSize, got, ok, expected)
		}
	}
}

// TestV13EnvelopeTablesArePublished pins the slot tables. The full table is
// the one issue #1848 publishes; moving any count is a new contract.
func TestV13EnvelopeTablesArePublished(t *testing.T) {
	want := V13MemoryEnvelope{
		Story: 78, OrdinaryWorld: 32, BusinessPrograms: 28, PersonalPrograms: 24, Abstention: 25,
		RecordQuantity: 16, Divergence: 12, PointInTime: 12, Integrity: 14, Isolation: 9,
	}
	if V13FullEnvelope != want || V13FullEnvelope.Total() != 250 {
		t.Fatalf("full envelope %+v (total %d), want %+v (250)", V13FullEnvelope, V13FullEnvelope.Total(), want)
	}
	for runSize, wantTotal := range map[string]int{"full": 250, "medium": 95, "small": 27} {
		envelope, ok := V13EnvelopeForRunSize(runSize)
		if !ok {
			t.Fatalf("%s: no envelope", runSize)
		}
		if envelope.Total() != wantTotal {
			t.Errorf("%s envelope totals %d, want %d", runSize, envelope.Total(), wantTotal)
		}
		prof, _ := ProfileForVersion(runSize, protocol.BenchVersionV13)
		if envelope.Isolation != prof.IsoCases {
			t.Errorf("%s envelope isolation %d != profile IsoCases %d", runSize, envelope.Isolation, prof.IsoCases)
		}
		if envelope.BusinessPrograms%4 != 0 {
			t.Errorf("%s business programs %d is not a whole metamorphic group", runSize, envelope.BusinessPrograms)
		}
		if envelope.Divergence%4 != 0 {
			t.Errorf("%s divergence %d is not a whole round of four", runSize, envelope.Divergence)
		}
	}
	if _, ok := v13EnvelopeFor(225); ok {
		t.Fatal("the v12 memory size 225 must not inherit a v13 envelope")
	}
}

// TestV13ReferenceRunHasFixedCaseEnvelope: every public run size has a fixed
// tool + memory case count at v13, like the v8 envelope test.
func TestV13ReferenceRunHasFixedCaseEnvelope(t *testing.T) {
	for _, seed := range []int64{1, 2, 3, 7, 11, 42, 123456789, 3058240546919425205} {
		for runSize, want := range map[string]int{"small": 33, "medium": 143, "full": 350} {
			prof, _ := ProfileForVersion(runSize, protocol.BenchVersionV13)
			artifact, err := GenerateDataset(seed, prof, protocol.BenchVersionV13)
			if err != nil {
				t.Fatalf("v13 seed %d %s: %v", seed, runSize, err)
			}
			got := len(artifact.ToolCases) + len(artifact.MemoryCases)
			if got != want {
				t.Fatalf("v13 seed %d %s run has %d cases, want fixed envelope %d", seed, runSize, got, want)
			}
			envelope, _ := V13EnvelopeForRunSize(runSize)
			if len(artifact.MemoryCases) != envelope.Total() {
				t.Fatalf("v13 seed %d %s has %d memory cases, envelope publishes %d", seed, runSize, len(artifact.MemoryCases), envelope.Total())
			}
		}
	}
}

// v13FortySeeds generates the pinned 40 full-profile qualification seeds once
// per test binary; every v13 40-seed assertion reads this cache, so adding an
// assertion costs no generation time.
var (
	v13FortySeedsOnce sync.Once
	v13FortySeedsOut  []DatasetArtifact
	v13FortySeedsErr  error
)

func v13FortySeeds(t *testing.T) []DatasetArtifact {
	t.Helper()
	v13FortySeedsOnce.Do(func() {
		prof, _ := ProfileForVersion("full", protocol.BenchVersionV13)
		for seed := int64(1); seed <= 40; seed++ {
			artifact, err := GenerateDataset(seed, prof, protocol.BenchVersionV13)
			if err != nil {
				v13FortySeedsErr = fmt.Errorf("seed %d: %w", seed, err)
				return
			}
			v13FortySeedsOut = append(v13FortySeedsOut, artifact)
		}
	})
	if v13FortySeedsErr != nil {
		t.Fatal(v13FortySeedsErr)
	}
	return v13FortySeedsOut
}

// TestV13SuiteTelemetryReportsPublishedSlots: the suite's own slot telemetry
// matches the published table (a few seeds; the artifact-level check below
// covers all forty).
func TestV13SuiteTelemetryReportsPublishedSlots(t *testing.T) {
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV13)
	envelope := V13FullEnvelope
	for _, seed := range []int64{1, 42, 123456789} {
		rng, err := NewRNGForVersion(seed, protocol.BenchVersionV13)
		if err != nil {
			t.Fatal(err)
		}
		GenerateToolsForVersion(rng, seed, prof.Tools, protocol.BenchVersionV13)
		suite, err := GenerateMemorySuiteForVersion(rng, seed, prof.Mem, prof.Waves, prof.RawPairsFrac, protocol.BenchVersionV13)
		if err != nil {
			t.Fatalf("seed %d: %v", seed, err)
		}
		wantSlots := map[string]int{
			V13SlotStory: envelope.Story, V13SlotOrdinaryWorld: envelope.OrdinaryWorld,
			V13SlotBusinessPrograms: envelope.BusinessPrograms, V13SlotPersonalPrograms: envelope.PersonalPrograms,
			V13SlotAbstention: envelope.Abstention, V13SlotRecordQuantity: envelope.RecordQuantity,
			V13SlotDivergence: envelope.Divergence, V13SlotPointInTime: envelope.PointInTime,
			V13SlotIntegrity: envelope.Integrity, V13SlotIsolation: envelope.Isolation,
		}
		for slot, want := range wantSlots {
			if suite.V13Slots[slot] != want {
				t.Errorf("seed %d slot %s=%d, want %d", seed, slot, suite.V13Slots[slot], want)
			}
		}
		if len(suite.Cases)+envelope.Isolation != envelope.Total() {
			t.Fatalf("seed %d suite has %d cases + %d isolation, want %d", seed, len(suite.Cases), envelope.Isolation, envelope.Total())
		}
	}
}

// TestV13EnvelopeSlotsAcrossFortySeeds checks every pinned full seed fills the
// published slots exactly: the artifact family counts, the integrity
// composition, the project-outstanding cap, and the case version.
func TestV13EnvelopeSlotsAcrossFortySeeds(t *testing.T) {
	envelope := V13FullEnvelope
	wantFamilies := v13MemoryFamilies()
	union := map[string]bool{}
	histograms := map[string]bool{}
	for _, artifact := range v13FortySeeds(t) {
		seed := artifact.Seed
		hist := map[string]int{}
		questions := map[string]bool{}
		for _, c := range artifact.MemoryCases {
			hist[c.QuestionType]++
			union[c.QuestionType] = true
			if c.BenchVersion != protocol.BenchVersionV13 {
				t.Errorf("seed %d case %s carries bench version %d", seed, c.ID, c.BenchVersion)
			}
			// The parser-divergence family deliberately repeats its short question
			// surface across rounds (the trap is in the record, not the question);
			// every shared-world question is unique.
			if strings.HasPrefix(c.QuestionType, "world-") {
				if questions[c.Question] {
					t.Errorf("seed %d duplicate question %q", seed, c.Question)
				}
				questions[c.Question] = true
			}
			if !wantFamilies[c.QuestionType] {
				t.Errorf("seed %d emitted unexpected memory family %q", seed, c.QuestionType)
			}
		}
		story, world, programs, records, divergence := 0, 0, 0, 0, 0
		for family, count := range hist {
			switch {
			case strings.HasPrefix(family, "world-story-"):
				story += count
			case strings.HasPrefix(family, "world-contact-"), strings.HasPrefix(family, "world-project-"), strings.HasPrefix(family, "world-trip-"):
				world += count
			case strings.HasSuffix(family, "-open-program"):
				programs += count
			case strings.HasPrefix(family, "record-balance-"):
				records += count
			case strings.HasPrefix(family, "parser-divergence-"):
				divergence += count
			}
		}
		if story != envelope.Story || world != envelope.OrdinaryWorld+envelope.Interim() || programs != envelope.BusinessPrograms ||
			records != envelope.RecordQuantity || divergence != envelope.Divergence {
			t.Errorf("seed %d story/world/programs/records/divergence = %d/%d/%d/%d/%d, want %d/%d/%d/%d/%d",
				seed, story, world, programs, records, divergence,
				envelope.Story, envelope.OrdinaryWorld+envelope.Interim(), envelope.BusinessPrograms, envelope.RecordQuantity, envelope.Divergence)
		}
		if hist["world-project-outstanding"] > V13OrdinaryProjectOutstandingCap {
			t.Errorf("seed %d has %d project-outstanding cases, cap %d", seed, hist["world-project-outstanding"], V13OrdinaryProjectOutstandingCap)
		}
		// Every story arc keeps six oracles: the two non-monetary oracles and the
		// two list oracles on every arc, one pure-money oracle dropped per arc.
		for _, family := range []string{"world-story-contact-current", "world-story-lesson", "world-story-later-net-change", "world-story-outcome-summary"} {
			if hist[family] != 13 {
				t.Errorf("seed %d %s=%d, want 13 (one per arc)", seed, family, hist[family])
			}
		}
		if pure := hist["world-story-balance-current"] + hist["world-story-budget-delta"] + hist["world-story-post-approval-balance"]; pure != 26 {
			t.Errorf("seed %d pure-money story oracles=%d, want 26 (two of three per arc)", seed, pure)
		}
		// Integrity tail: 3 chitchat, 3 declarative ack, 3 declarative behaviour,
		// 1 canary, 4 injection.
		if hist[QTChitchat] != 3 || hist[QTDeclarativeAck] != 3 || hist[QTDeclarativeBehavior] != 3 || hist["world-canary"] != 1 || hist["world-injection-resistance"] != 4 {
			t.Errorf("seed %d integrity tail chitchat/ack/behaviour/canary/injection = %d/%d/%d/%d/%d, want 3/3/3/1/4",
				seed, hist[QTChitchat], hist[QTDeclarativeAck], hist[QTDeclarativeBehavior], hist["world-canary"], hist["world-injection-resistance"])
		}
		if hist["world-isolation-contact-current"] != envelope.Isolation {
			t.Errorf("seed %d isolation=%d, want %d", seed, hist["world-isolation-contact-current"], envelope.Isolation)
		}
		for family := range wantFamilies {
			// The capped monetary oracle may legitimately be absent from a seed's
			// ordinary slot; every other family has a positive per-seed floor.
			if family != "world-project-outstanding" && hist[family] < 1 {
				t.Errorf("seed %d omitted memory family %q", seed, family)
			}
		}
		histograms[v9HistogramKey(hist)] = true
	}
	for family := range wantFamilies {
		if !union[family] {
			t.Errorf("40-seed union omitted memory family %q", family)
		}
	}
	if len(histograms) < 35 {
		t.Errorf("only %d distinct v13 memory histograms across 40 seeds", len(histograms))
	}
}

// TestV13ComposedMemoryFloor mirrors the v8 composed/indirect floor at v13.
func TestV13ComposedMemoryFloor(t *testing.T) {
	for _, artifact := range v13FortySeeds(t) {
		seed := artifact.Seed
		composed := 0
		for _, mc := range artifact.MemoryCases {
			if v8ComposedMemoryType(mc.QuestionType) || strings.HasSuffix(mc.QuestionType, "-open-program") ||
				strings.HasPrefix(mc.QuestionType, "record-balance-") || strings.HasPrefix(mc.QuestionType, "parser-divergence-") {
				composed++
			}
		}
		if 100*composed < 65*len(artifact.MemoryCases) {
			t.Fatalf("seed %d composed/indirect share %d/%d is below 65%%", seed, composed, len(artifact.MemoryCases))
		}
	}
}

// TestV13EveryDeclaredEvidencePairIsSeeded: like the v8/v9 answerability tests,
// every memory case's declared evidence is present in the seeded world or wave.
func TestV13EveryDeclaredEvidencePairIsSeeded(t *testing.T) {
	for _, runSize := range []string{"small", "medium", "full"} {
		prof, _ := ProfileForVersion(runSize, protocol.BenchVersionV13)
		for _, seed := range []int64{1, 42, 123456789} {
			artifact, err := GenerateDataset(seed, prof, protocol.BenchVersionV13)
			if err != nil {
				t.Fatalf("%s seed %d: %v", runSize, seed, err)
			}
			available := map[string]map[string]bool{}
			add := func(user string, pairs []protocol.MemoryPair) {
				if user == "" {
					user = PrimaryUser
				}
				if available[user] == nil {
					available[user] = map[string]bool{}
				}
				for _, pair := range pairs {
					available[user][pair.PairID] = true
				}
			}
			for _, tc := range artifact.ToolCases {
				add(PrimaryUser, tc.PrerequisitePairs)
			}
			for _, wave := range artifact.MemoryWaves {
				add(wave.UserID, wave.Pairs)
			}
			declared := 0
			for _, c := range artifact.MemoryCases {
				user := c.UserID
				if user == "" {
					user = PrimaryUser
				}
				for _, pairID := range c.V10EvidencePairIDs {
					declared++
					if !available[user][pairID] {
						t.Fatalf("%s seed %d case %s requires unseeded pair %s for user %s", runSize, seed, c.ID, pairID, user)
					}
				}
			}
			if declared == 0 {
				t.Fatalf("%s seed %d declared no evidence", runSize, seed)
			}
		}
	}
}

// TestV13DoesNotMoveEarlierContracts: the v13 dispatch is a floor. v12 keeps
// its 251-case envelope and 13-case integrity tail on the same seeds.
func TestV13DoesNotMoveEarlierContracts(t *testing.T) {
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV12)
	artifact, err := GenerateDataset(123456789, prof, protocol.BenchVersionV12)
	if err != nil {
		t.Fatal(err)
	}
	if len(artifact.MemoryCases) != 251 {
		t.Fatalf("v12 memory cases=%d, want 251", len(artifact.MemoryCases))
	}
	injection := 0
	for _, c := range artifact.MemoryCases {
		if c.QuestionType == "world-injection-resistance" {
			injection++
		}
	}
	if injection != v8WorldInjectionCaseCount {
		t.Fatalf("v12 injection cases=%d, want %d", injection, v8WorldInjectionCaseCount)
	}
	if worldIntegrityCaseCount(protocol.BenchVersionV12, 18) != v8WorldIntegrityCaseCount || worldIntegrityCaseCount(protocol.BenchVersionV13, 18) != 14 || worldIntegrityCaseCount(protocol.BenchVersionV13, 3) != 13 {
		t.Fatal("integrity tail sizing is not a version floor bounded by the world's projects")
	}
}

// TestV13ReviewAnnotatesEveryWorldCase: the local review path regenerates the
// v13 plan selection and annotates every shared-world, program-free case.
func TestV13ReviewAnnotatesEveryWorldCase(t *testing.T) {
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV13)
	review, err := GenerateDatasetReview(5, prof, protocol.BenchVersionV13)
	if err != nil {
		t.Fatal(err)
	}
	annotated := map[string]bool{}
	for _, annotation := range review.MemoryAnnotations {
		annotated[annotation.CaseID] = true
	}
	for _, c := range review.Artifact.MemoryCases {
		if !strings.HasPrefix(c.QuestionType, "world-") {
			continue
		}
		if c.QuestionType == QTChitchat || c.QuestionType == QTDeclarativeAck || c.QuestionType == QTDeclarativeBehavior {
			continue
		}
		if !annotated[c.ID] {
			t.Errorf("v13 world case %s (%s) has no review annotation", c.ID, c.QuestionType)
		}
	}
}

// v13MemoryFamilies is the final v13 memory family set while the interim slots
// are filled by ordinary world questions. Dedicated generators add their own
// families here as they land.
func v13MemoryFamilies() map[string]bool {
	families := v9MemoryFamilies()
	for _, family := range []string{
		"v12-open-program",
		QTRecordBalancePlain, QTRecordBalanceAdjusted, QTRecordBalanceSuperseded, QTRecordBalanceCapped,
		QTRecordBalanceForgiven, QTRecordBalanceReferred, QTRecordBalanceCFBase, QTRecordBalanceCFVariant,
		QTParserDivergenceNegation, QTParserDivergenceRetraction, QTParserDivergenceHypothetical, QTParserDivergenceReported,
	} {
		families[family] = true
	}
	return families
}
