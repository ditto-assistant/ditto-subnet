package gen

import (
	"math"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// TestMixAuditReproducesV12MoneyExposure pins the classifier to the figures
// issue #1529 measured on the public seed: 117 direct AnswerMoney cases, 143
// cases carrying any monetary claim, and 127.83 of 251 memory score weight
// (50.9%) once typed list items are weighted by their share of case credit.
// Reproducing them exactly is what makes the v13 caps comparable to the v12
// diagnosis rather than to a different measurement.
func TestMixAuditReproducesV12MoneyExposure(t *testing.T) {
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV12)
	artifact, err := GenerateDataset(123456789, prof, protocol.BenchVersionV12)
	if err != nil {
		t.Fatal(err)
	}
	audit, err := AuditMix(artifact)
	if err != nil {
		t.Fatal(err)
	}
	if audit.Cases != 251 || audit.MoneyOnlyCases != 117 || audit.MoneyCases != 143 {
		t.Fatalf("v12 public seed cases/money-only/money-bearing = %d/%d/%d, want 251/117/143", audit.Cases, audit.MoneyOnlyCases, audit.MoneyCases)
	}
	if math.Abs(audit.MoneyWeight-127.8333) > 0.001 {
		t.Fatalf("v12 public seed money weight %.4f, want 127.8333", audit.MoneyWeight)
	}
	if share := audit.MoneyShare(); math.Abs(share-0.5093) > 0.0005 {
		t.Fatalf("v12 public seed money share %.4f, want 0.5093", share)
	}
	// 157 of 245 evidence-bound answers are computed and 143 of those are money
	// (understand-datagen §2.1): "computed difficulty" in v12 is money arithmetic.
	if audit.EvidenceBoundCases != 245 || audit.ComputedCases != 157 || audit.ComputedMoneyCases != 143 {
		t.Fatalf("v12 public seed evidence-bound/computed/computed-money = %d/%d/%d, want 245/157/143", audit.EvidenceBoundCases, audit.ComputedCases, audit.ComputedMoneyCases)
	}
	if audit.MonetaryOpenPrograms != 40 || audit.OpenPrograms != 40 {
		t.Fatalf("v12 public seed open programs monetary/total = %d/%d, want 40/40", audit.MonetaryOpenPrograms, audit.OpenPrograms)
	}
	if len(MixGateV13.Violations(audit)) == 0 {
		t.Fatal("the v13 gate accepted the v12 mix it was written to reject")
	}
}

// TestMixAuditFailsOnUnclassifiedFamilies is the "fail on any unclassified
// kind" acceptance criterion: a new family or answer kind cannot bypass the
// histogram by being unknown to it.
func TestMixAuditFailsOnUnclassifiedFamilies(t *testing.T) {
	prof, _ := ProfileForVersion("small", protocol.BenchVersionV13)
	artifact, err := GenerateDataset(3, prof, protocol.BenchVersionV13)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := AuditMix(artifact); err != nil {
		t.Fatalf("baseline small v13 artifact did not classify: %v", err)
	}
	unknownType := artifact
	unknownType.MemoryCases = append([]ArtifactCase(nil), artifact.MemoryCases...)
	unknownType.MemoryCases[0].QuestionType = "brand-new-family"
	if _, err := AuditMix(unknownType); err == nil || !strings.Contains(err.Error(), "unclassified question type") {
		t.Fatalf("unknown question type was not rejected: %v", err)
	}
	unknownKind := artifact
	unknownKind.MemoryCases = append([]ArtifactCase(nil), artifact.MemoryCases...)
	unknownKind.MemoryCases[0].AnswerKind = "claims"
	if _, err := AuditMix(unknownKind); err == nil || !strings.Contains(err.Error(), "unclassified answer kind") {
		t.Fatalf("unknown answer kind was not rejected: %v", err)
	}
}

// Interim exposure bounds, pinned to the measured 40-seed maxima (seeds 1-40,
// docs/v13-family-mix-study.md: money 36.4% mean, 106 money-bearing cases,
// arithmetic ~42%). They are NOT the #1529 caps; they exist so the interim
// exposure cannot creep upward unnoticed while the monetary generators are
// still in place. A generator swap may lower them, never widen them.
const (
	v13InterimMoneyShareMax      = 0.37
	v13InterimMoneyCasesMax      = 110
	v13InterimArithmeticShareMax = 0.45
)

// TestV13MixAuditStructuralAcrossFortySeeds asserts the bounds every v13 run
// must already satisfy while the interim slots and generators are in place:
// the monetary project-outstanding oracle is capped inside the ordinary slot,
// no gate can zero more than 40% of memory weight, no single honest error can
// cascade into more than 4% of memory weight through a dependency cluster, and
// the interim monetary exposure stays at or below its measured ceiling.
func TestV13MixAuditStructuralAcrossFortySeeds(t *testing.T) {
	for _, artifact := range v13FortySeeds(t) {
		seed := artifact.Seed
		audit, err := AuditMix(artifact)
		if err != nil {
			t.Fatalf("seed %d: %v", seed, err)
		}
		if audit.Cases != V13FullEnvelope.Total() {
			t.Fatalf("seed %d audited %d cases, want %d", seed, audit.Cases, V13FullEnvelope.Total())
		}
		if violations := MixGateV13.StructuralViolations(audit); len(violations) > 0 {
			t.Errorf("seed %d structural violations: %v", seed, violations)
		}
		// The rebalance already cuts the v12 monetary exposure before any
		// dedicated v13 generator lands: story arcs keep six oracles, the ordinary
		// slot caps project-outstanding, and the interim fill is non-monetary.
		if share := audit.MoneyShare(); share > v13InterimMoneyShareMax {
			t.Errorf("seed %d interim money share %.4f exceeds the %.2f interim ceiling (v12 was 0.509)", seed, share, v13InterimMoneyShareMax)
		}
		if audit.MoneyCases > v13InterimMoneyCasesMax {
			t.Errorf("seed %d interim money-bearing cases %d exceed the %d interim ceiling (v12 was 143)", seed, audit.MoneyCases, v13InterimMoneyCasesMax)
		}
		if share := audit.ArithmeticShare(); share > v13InterimArithmeticShareMax {
			t.Errorf("seed %d interim arithmetic share %.4f exceeds the %.2f interim ceiling", seed, share, v13InterimArithmeticShareMax)
		}
		if audit.MonetaryOpenPrograms != 0 {
			t.Errorf("seed %d monetary open programs %d, want zero", seed, audit.MonetaryOpenPrograms)
		}
	}
}

// TestV13MixAuditGateAcrossFortySeeds is the #1529 / #1848 gate: money <= 15%
// of memory weight (12% target), <= 22 money-bearing cases, zero monetary open
// programs, arithmetic <= 20%, money <= 25% of computed answers, personal >=
// 30%, business >= 40%, no sub-domain > 20%, no answer-kind x operation > 15%,
// abstention 10% +/- 1, twin coverage >= 40%, gate-exposed <= 40%, and the
// dependency cascade cap, on every one of the pinned 40 qualification seeds.
//
// The gate arms itself: while V13InterimPending() holds -- an interim slot
// (#1838 personal programs, #1530 abstention, #1844 point-in-time) or an
// interim monetary generator (#1520 business programs, #1837 record quantity,
// #1841 story oracles) has not landed -- the caps are known to be
// unsatisfiable, and the test reports the exact violations on the first
// failing seed and SKIPS. This PR therefore does not satisfy #1529; removing
// the last entry from both interim lists turns every assertion on with no
// further edit.
func TestV13MixAuditGateAcrossFortySeeds(t *testing.T) {
	var report []string
	var firstFailing int64
	failing := 0
	for _, artifact := range v13FortySeeds(t) {
		seed := artifact.Seed
		audit, err := AuditMix(artifact)
		if err != nil {
			t.Fatalf("seed %d: %v", seed, err)
		}
		violations := MixGateV13.Violations(audit)
		if len(violations) == 0 {
			continue
		}
		if failing == 0 {
			firstFailing, report = seed, violations
		}
		failing++
	}
	if failing == 0 {
		return
	}
	if V13InterimPending() {
		t.Skipf("v13 mix gate blocked on interim slots %v and interim generators %v: %d/40 seeds violate; first failing seed %d: %s", V13InterimSlots(), V13InterimGenerators(), failing, firstFailing, strings.Join(report, "; "))
	}
	t.Fatalf("v13 mix gate: %d/40 seeds violate; first failing seed %d: %s", failing, firstFailing, strings.Join(report, "; "))
}

// TestV13InterimSlotsArePinned keeps both interim lists honest: a generator
// swap must remove its slot here in the same change, which is what arms the
// gate.
func TestV13InterimSlotsArePinned(t *testing.T) {
	want := []string{}
	got := V13InterimSlots()
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatalf("interim slots %v, want %v (update this pin and the gate when a generator lands)", got, want)
	}
	if V13FullEnvelope.Interim() != 0 {
		t.Fatalf("interim fill %d does not equal the interim slots", V13FullEnvelope.Interim())
	}
	wantGenerators := []string{}
	if gotGenerators := V13InterimGenerators(); strings.Join(gotGenerators, ",") != strings.Join(wantGenerators, ",") {
		t.Fatalf("interim generators %v, want %v (update this pin when a monetary generator is swapped)", gotGenerators, wantGenerators)
	}
	if V13InterimPending() {
		t.Fatal("all generators must be wired; the mix gate may not skip")
	}
}
