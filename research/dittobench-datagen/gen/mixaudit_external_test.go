package gen_test

import (
	"math"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/internal/mixaudit"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// mixauditPinnedSeeds is the 40-seed qualification sweep every mix envelope is
// asserted on (per seed, never on the average).
const mixauditPinnedSeeds = 40

func auditSeed(t *testing.T, seed int64, version int) mixaudit.SeedReport {
	t.Helper()
	prof, ok := gen.ProfileForVersion("full", version)
	if !ok {
		t.Fatalf("no full profile for v%d", version)
	}
	artifact, err := gen.GenerateDataset(seed, prof, version)
	if err != nil {
		t.Fatalf("generate v%d seed %d: %v", version, seed, err)
	}
	report, err := mixaudit.Audit(artifact, "full", false)
	if err != nil {
		t.Fatalf("audit v%d seed %d: %v", version, seed, err)
	}
	return report
}

// TestMixauditReproducesV12PublicSeedFigures pins the classifier against the
// figures issue #1529 measured by hand on the public vector seed: 117 direct
// money cases, 143 money-bearing cases, and 127.83/251 = 50.9% of memory
// weight monetary once list items are weighted by their fraction of credit.
func TestMixauditReproducesV12PublicSeedFigures(t *testing.T) {
	report := auditSeed(t, 123456789, protocol.BenchVersionV12)
	if report.MemoryCases != 251 || report.Weight != 251 {
		t.Fatalf("memory cases/weight = %d/%.0f, want 251/251", report.MemoryCases, report.Weight)
	}
	if report.DirectMoneyCases != 117 {
		t.Errorf("direct money cases = %d, want 117", report.DirectMoneyCases)
	}
	if report.MoneyBearingCases != 143 {
		t.Errorf("money-bearing cases = %d, want 143", report.MoneyBearingCases)
	}
	if math.Abs(report.MoneyWeight-127.8333) > 0.001 {
		t.Errorf("money weight = %.4f, want 127.8333", report.MoneyWeight)
	}
	if math.Abs(report.MoneyShare-0.5093) > 0.0005 {
		t.Errorf("money share = %.4f, want 0.5093", report.MoneyShare)
	}
	if report.OpenPrograms != 40 || report.MonetaryOpenPrograms != 40 {
		t.Errorf("open programs = %d monetary %d, want 40/40", report.OpenPrograms, report.MonetaryOpenPrograms)
	}
	if report.AbstentionCases != 0 {
		t.Errorf("abstention cases = %d, want 0 on v12 (no v8+ family emits decline)", report.AbstentionCases)
	}
}

// TestMixauditClassifiesEveryPinnedSeed proves the classifier fails closed on
// nothing the v12 generator emits: every family, answer kind, and list-item kind
// across the 40 pinned seeds is classified, and the report is deterministic.
func TestMixauditClassifiesEveryPinnedSeed(t *testing.T) {
	var reports []mixaudit.SeedReport
	for seed := int64(1); seed <= mixauditPinnedSeeds; seed++ {
		reports = append(reports, auditSeed(t, seed, protocol.BenchVersionV12))
	}
	again := auditSeed(t, 1, protocol.BenchVersionV12)
	if again.MoneyWeight != reports[0].MoneyWeight || again.GateExposedWeight != reports[0].GateExposedWeight || len(again.Families) != len(reports[0].Families) {
		t.Fatal("audit of seed 1 is not deterministic")
	}
	summary := mixaudit.Summarize(reports, mixaudit.V13Envelope)
	if summary.MemoryCases.Min != 251 || summary.MemoryCases.Max != 251 {
		t.Errorf("memory envelope = %v, want a fixed 251", summary.MemoryCases)
	}
	// Every seed classifies its whole weight into a domain.
	for _, r := range reports {
		total := 0.0
		for _, w := range r.Domains {
			total += w
		}
		if math.Abs(total-r.Weight) > 1e-6 {
			t.Errorf("seed %d: domain weight %.4f != case weight %.0f", r.Seed, total, r.Weight)
		}
	}
	// The v13 envelope is DEFINED here and asserted green only by the envelope
	// PR; on v12 it must bite, or the gate is vacuous.
	if summary.SeedsViolating != mixauditPinnedSeeds {
		t.Errorf("v13 envelope flagged %d/%d v12 seeds; the money cap alone must flag every one", summary.SeedsViolating, mixauditPinnedSeeds)
	}
	for _, r := range reports[:1] {
		for _, v := range mixaudit.V13Envelope.Check(r) {
			t.Logf("v12 seed %d violates v13 envelope: %s", r.Seed, v)
		}
	}
	t.Logf("v12 40-seed money share min/mean/max = %.4f/%.4f/%.4f", summary.MoneyShare.Min, summary.MoneyShare.Mean, summary.MoneyShare.Max)
}

// TestMixauditEnvelopeConstantsMatchPlan pins the caps and floors issue #1830
// enumerates so a later PR cannot loosen them silently.
func TestMixauditEnvelopeConstantsMatchPlan(t *testing.T) {
	env := mixaudit.V13Envelope
	want := mixaudit.Envelope{
		MemoryCasesTarget:  250,
		MoneyWeightHardCap: 0.15, MoneyWeightTarget: 0.12, MoneyBearingCasesCap: 22, MonetaryOpenPrograms: 0,
		ArithmeticShareCap: 0.20, PersonalWeightFloor: 0.30, BusinessWeightFloor: 0.40, SubDomainWeightCap: 0.20,
		KindOperationWeightCap: 0.15, AbstentionShareTarget: 0.10, AbstentionShareBand: 0.01, TwinCoverageFloor: 0.40,
		GateExposedShareCap: 0.40, SingleErrorCascadeCap: 0.04,
	}
	if env != want {
		t.Fatalf("V13Envelope = %+v, want %+v", env, want)
	}
}

// TestMixauditFailsClosedOnUnknownKind proves an unclassified answer kind or
// family is an error rather than an "other" bucket.
func TestMixauditFailsClosedOnUnknownKind(t *testing.T) {
	prof, _ := gen.ProfileForVersion("small", protocol.BenchVersionV12)
	artifact, err := gen.GenerateDataset(7, prof, protocol.BenchVersionV12)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := mixaudit.Audit(artifact, "small", false); err != nil {
		t.Fatalf("small v12 audit: %v", err)
	}
	broken := artifact
	broken.MemoryCases = append([]gen.ArtifactCase(nil), artifact.MemoryCases...)
	broken.MemoryCases[0].AnswerKind = "telepathy"
	if _, err := mixaudit.Audit(broken, "small", false); err == nil {
		t.Fatal("unknown answer kind was classified silently")
	}
	broken.MemoryCases[0].AnswerKind = protocol.AnswerValue
	broken.MemoryCases[0].QuestionType = "world-unicorn"
	if _, err := mixaudit.Audit(broken, "small", false); err == nil {
		t.Fatal("unknown family was classified silently")
	}
	broken.MemoryCases[0].QuestionType = artifact.MemoryCases[0].QuestionType
	broken.MemoryCases[0].AnswerKind = protocol.AnswerList
	broken.MemoryCases[0].AnswerItems = []string{"a", "b"}
	broken.MemoryCases[0].AnswerItemKinds = []string{protocol.AnswerValue, "telepathy"}
	if _, err := mixaudit.Audit(broken, "small", false); err == nil {
		t.Fatal("unknown list-item kind was classified silently")
	}
}
