package efficiency

import (
	"encoding/json"
	"fmt"
	"math"
	"os"
	"reflect"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/llm"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func testReport(composite float64) protocol.ScoreReport {
	return protocol.ScoreReport{Composite: composite, ToolMean: 0.8, MemoryMean: 0.8}
}

// Under the quality-only contract the embedded v7 manifest (scoring_enabled=
// false, permanent) is a live, valid readiness with the exact aggregate route.
func TestV7CalibrationReadinessReflectsEmbeddedQualityOnlyManifest(t *testing.T) {
	got := V7CalibrationReadiness()
	if !ValidV7CalibrationReadiness(got) || got.ManifestSHA256 == "" || len(got.SupportedRoutes) != 1 {
		t.Fatalf("embedded quality-only v7 readiness must be live and valid = %+v", got)
	}
	if got.SupportedRoutes[0] != (CalibrationRouteIdentity{
		Provider: v7AggregateProvider, ProfileRevision: v7AggregateProfile, Model: llm.V7HarnessModel,
	}) {
		t.Fatalf("unexpected aggregate route: %+v", got.SupportedRoutes)
	}
	if got.Provenance != ProvenanceReviewedMeasured {
		t.Fatalf("embedded manifest provenance = %s", got.Provenance)
	}
}

func TestV7CalibrationReadinessRejectsProviderSpecificManifest(t *testing.T) {
	manifest := Manifest{
		BenchVersion: protocol.BenchVersionV7, ScoringEnabled: true,
		StarterKitRevision: "60aab4e5e2839ddb0fe8c80492bd7b76ba2668fd",
		Calibration:        validV7Datasets(),
		Embedding: &EmbeddingContract{
			Provider: v7EmbeddingProvider, Model: v7EmbeddingModel, Profile: v7EmbeddingProfile,
			Dimensions: v7EmbeddingDimensions, CatalogSHA256: v7EmbeddingCatalogSHA256,
		},
	}
	for _, provider := range []struct{ name, profile string }{
		{"groq", "openrouter-route-bbbbbbbbbbbbbbbb-v1"},
		{"amazon", "openrouter-route-aaaaaaaaaaaaaaaa-v1"},
	} {
		for _, runSize := range []string{"small", "medium", "full"} {
			manifest.Baselines = append(manifest.Baselines, Baseline{
				ID:           "reviewed-" + provider.name + "-" + runSize,
				BenchVersion: protocol.BenchVersionV7, RunSize: runSize,
				Provider: provider.name, ProfileRevision: provider.profile, Model: llm.V7HarnessModel,
				PromptTokens: 900, CompletionTokens: 100, TotalTokens: 1_000,
				Samples: 20, Aggregation: "nearest_rank_p90", StarterKitRevision: manifest.StarterKitRevision,
			})
		}
	}
	got := v7CalibrationReadiness(manifest, []byte(`{"reviewed":"manifest"}`))
	if got.ManifestSHA256 != "" || len(got.SupportedRoutes) != 0 {
		t.Fatalf("provider-specific readiness escaped aggregate gate: %+v", got)
	}
}

func TestAggregateV7ManifestRequiresExactTwentyByThreeContract(t *testing.T) {
	const profile = "openrouter-route-a471cd87ae7df5b9-v1"
	manifest := Manifest{
		BenchVersion: protocol.BenchVersionV7, ScoringEnabled: true,
		StarterKitRevision: v7StarterKitRevision,
		DatasetKnownVector: v7DatasetKnownVector,
		Calibration:        append([]CalibrationDataset(nil), productionV7Manifest.Calibration...),
		Embedding: &EmbeddingContract{
			Provider: v7EmbeddingProvider, Model: v7EmbeddingModel, Profile: v7EmbeddingProfile,
			Dimensions: v7EmbeddingDimensions, CatalogSHA256: v7EmbeddingCatalogSHA256,
		},
	}
	for _, runSize := range []string{"small", "medium", "full"} {
		baseline := Baseline{
			BenchVersion: protocol.BenchVersionV7,
			RunSize:      runSize, Provider: "openrouter", ProfileRevision: profile,
			Model:                    llm.V7HarnessModel,
			RawReferencePromptTokens: 900, RawReferenceCompletionTokens: 100,
			RawReferenceTotalTokens: 1_000, AllowanceMultiplierBPS: 7500,
			AllowancePromptTokens: 675, AllowanceCompletionTokens: 75,
			AllowanceTotalTokens: 750, AllowancePolicy: v7AllowancePolicy,
			Samples: 20, Aggregation: "nearest_rank_p90",
			StarterKitRevision: manifest.StarterKitRevision,
		}
		baseline.ID = baselineID(protocol.BenchVersionV7, baseline)
		manifest.Baselines = append(manifest.Baselines, baseline)
	}
	if !ReadyForV7Production(manifest) {
		t.Fatal("exact aggregate 20x3 manifest must be production-ready")
	}
	// The production capability readiness flows through the quality-only
	// contract (scoring_enabled=false); the same exact 20x3 identity must
	// resolve its aggregate route there.
	qualityOnly := manifest
	qualityOnly.ScoringEnabled = false
	if !ReadyForV7QualityOnly(qualityOnly) {
		t.Fatal("exact aggregate 20x3 manifest must satisfy the quality-only contract")
	}
	readiness := v7CalibrationReadiness(qualityOnly, []byte(`{"reviewed":true}`))
	if len(readiness.SupportedRoutes) != 1 || readiness.SupportedRoutes[0] != (CalibrationRouteIdentity{
		Provider: "openrouter", ProfileRevision: profile, Model: llm.V7HarnessModel,
	}) {
		t.Fatalf("aggregate route identity = %+v", readiness.SupportedRoutes)
	}

	manifest.Baselines[0].Samples = 21
	if ReadyForV7Production(manifest) {
		t.Fatal("v7 accepted a baseline not built from exactly 20 runs")
	}
	manifest.Baselines[0].Samples = 20
	manifest.Baselines[0].ID = "v7-starter-p90-0000000000000000"
	if ReadyForV7Production(manifest) {
		t.Fatal("v7 accepted a baseline whose content-derived id was forged")
	}
	manifest.Baselines[0].ID = baselineID(protocol.BenchVersionV7, manifest.Baselines[0])
	for _, mutate := range []func(*Baseline){
		func(b *Baseline) { b.RawReferenceTotalTokens++ },
		func(b *Baseline) { b.AllowanceMultiplierBPS = 7499 },
		func(b *Baseline) { b.AllowanceTotalTokens++ },
		func(b *Baseline) { b.AllowanceCompletionTokens++ },
		func(b *Baseline) { b.AllowancePolicy = "unreviewed" },
	} {
		baseline := manifest.Baselines[0]
		mutate(&baseline)
		baseline.ID = baselineID(protocol.BenchVersionV7, baseline)
		candidate := manifest
		candidate.Baselines = append([]Baseline(nil), manifest.Baselines...)
		candidate.Baselines[0] = baseline
		if ReadyForV7Production(candidate) {
			t.Fatal("v7 accepted malformed raw-reference allowance derivation")
		}
	}
	manifest.Calibration[20].RunSize = manifest.Calibration[0].RunSize
	manifest.Calibration[20].Seed = manifest.Calibration[0].Seed
	if ReadyForV7Production(manifest) {
		t.Fatal("v7 accepted a duplicate run-size/seed contract with a different digest")
	}
}

func TestV7ManifestPinsReviewedGeneratorAndStarterIdentity(t *testing.T) {
	for _, mutate := range []func(*Manifest){
		func(m *Manifest) { m.StarterKitRevision = "60aab4e5e2839ddb0fe8c80492bd7b76ba2668fd" },
		func(m *Manifest) { m.DatasetKnownVector = fmt.Sprintf("%064x", 1) },
		func(m *Manifest) { m.Calibration[0], m.Calibration[1] = m.Calibration[1], m.Calibration[0] },
		func(m *Manifest) { m.Embedding.Profile = "unreviewed" },
	} {
		manifest := V7ManifestSnapshot()
		manifest.ScoringEnabled = true
		mutate(&manifest)
		if ReadyForV7Production(manifest) {
			t.Fatal("v7 accepted drift from the reviewed starter/generator contract")
		}
	}
}

func TestEmbeddedV7ManifestMatchesFailClosedCandidateBeforeCampaign(t *testing.T) {
	body, err := os.ReadFile("../../docs/baselines-v7-candidate.json")
	if err != nil {
		t.Fatal(err)
	}
	var candidate Manifest
	if err := json.Unmarshal(body, &candidate); err != nil {
		t.Fatal(err)
	}
	if candidate.ScoringEnabled {
		t.Fatal("candidate calibration artifact must remain fail-closed")
	}
	if !reflect.DeepEqual(candidate, V7ManifestSnapshot()) {
		t.Fatal("embedded fail-closed manifest drifted from the hosted calibration candidate")
	}
	reviewed := candidate
	reviewed.ScoringEnabled = true
	if !ReadyForV7Production(reviewed) {
		t.Fatal("completed disabled candidate cannot pass the exact readiness contract when explicitly enabled")
	}
}

func validV7Datasets() []CalibrationDataset {
	datasets := make([]CalibrationDataset, 0, 60)
	for _, runSize := range []string{"small", "medium", "full"} {
		for seed := int64(1); seed <= 20; seed++ {
			datasets = append(datasets, CalibrationDataset{
				RunSize: runSize, Seed: seed,
				DatasetSHA256: fmt.Sprintf("%064x", len(datasets)+1),
			})
		}
	}
	return datasets
}

func completeUsage(prompt, completion uint64) protocol.TokenUsage {
	return protocol.TokenUsage{
		Status: "complete", Successes: 10, UsageAvailable: 10,
		PromptTokens: prompt, CompletionTokens: completion,
		TotalTokens: prompt + completion,
	}
}

func testBaseline() Baseline {
	return Baseline{
		ID: "v5-test", BenchVersion: 5, RunSize: "full", Provider: "p",
		ProfileRevision: "r", Model: "m", PromptTokens: 900,
		CompletionTokens: 100, TotalTokens: 1_000, Samples: 20,
		Aggregation: "nearest_rank_p90", StarterKitRevision: "sha",
	}
}

func TestWastePenaltyIsOneSidedAndSaturating(t *testing.T) {
	baseline := testBaseline()
	for _, tc := range []struct {
		name      string
		prompt    uint64
		complete  uint64
		wantMult  float64
		wantScore float64
		applied   bool
	}{
		{name: "far below budget is neutral", prompt: 50, complete: 50, wantMult: 1, wantScore: 0.9},
		{name: "budget is neutral", prompt: 900, complete: 100, wantMult: 1, wantScore: 0.9},
		{name: "twenty five percent over", prompt: 1_150, complete: 100, wantMult: 0.98, wantScore: 0.882, applied: true},
		{name: "double budget", prompt: 1_900, complete: 100, wantMult: 0.95, wantScore: 0.855, applied: true},
		{name: "four times budget", prompt: 3_900, complete: 100, wantMult: 0.925, wantScore: 0.8325, applied: true},
		{name: "gross waste approaches floor", prompt: 99_900, complete: 100, wantMult: 0.901, wantScore: 0.8109, applied: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got := Apply(testReport(0.9), completeUsage(tc.prompt, tc.complete), &baseline)
			if math.Abs(got.Multiplier-tc.wantMult) > 1e-9 || got.AdjustedComposite != tc.wantScore || got.PenaltyApplied != tc.applied {
				t.Fatalf("result = %#v", got)
			}
			if got.Multiplier > 1 || got.Multiplier < MinMultiplier {
				t.Fatalf("waste-only multiplier outside [%v,1]: %#v", MinMultiplier, got)
			}
		})
	}
}

func TestPromptAndCompletionTokensBothCountTowardWaste(t *testing.T) {
	baseline := testBaseline()
	promptGrowth := Apply(testReport(0.9), completeUsage(1_000, 100), &baseline)
	completionGrowth := Apply(testReport(0.9), completeUsage(900, 200), &baseline)
	if promptGrowth.Multiplier != completionGrowth.Multiplier {
		t.Fatalf("equal provider-token totals must have equal waste cost: prompt=%#v completion=%#v", promptGrowth, completionGrowth)
	}
}

func TestV7RawStarterUsageUsesDerivedSeventyFivePercentAllowance(t *testing.T) {
	baseline := Baseline{
		ID: "v7-test", BenchVersion: protocol.BenchVersionV7, RunSize: "full",
		Provider: "openrouter", ProfileRevision: v7AggregateProfile, Model: llm.V7HarnessModel,
		RawReferencePromptTokens: 900, RawReferenceCompletionTokens: 100, RawReferenceTotalTokens: 1_000,
		AllowanceMultiplierBPS: 7500, AllowancePromptTokens: 675,
		AllowanceCompletionTokens: 75, AllowanceTotalTokens: 750,
		AllowancePolicy: v7AllowancePolicy, Samples: 20, Aggregation: "nearest_rank_p90",
		StarterKitRevision: v7StarterKitRevision,
	}
	got := Apply(testReport(0.8), completeUsage(900, 100), &baseline)
	if math.Abs(got.Multiplier-0.975) > 1e-9 || got.AdjustedComposite != 0.78 ||
		got.BaselineTotalTokens != 750 || !got.PenaltyApplied {
		t.Fatalf("raw starter usage did not receive the audited 2.5%% penalty: %#v", got)
	}
	neutral := Apply(testReport(0.8), completeUsage(675, 75), &baseline)
	if neutral.Multiplier != 1 || neutral.AdjustedComposite != 0.8 || neutral.PenaltyApplied {
		t.Fatalf("75%% allowance was not neutral: %#v", neutral)
	}
}

func TestCheapAnswersNeverReceiveAReward(t *testing.T) {
	baseline := testBaseline()
	for _, composite := range []float64{0, 0.1, 0.9, 1} {
		got := Apply(testReport(composite), completeUsage(1, 0), &baseline)
		if got.Multiplier != 1 || got.AdjustedComposite != composite || got.PenaltyApplied {
			t.Fatalf("composite %v received a minimization reward: %#v", composite, got)
		}
	}
}

func TestMissingTelemetryAndBaselineFailNeutral(t *testing.T) {
	baseline := testBaseline()
	for _, tc := range []struct {
		name   string
		usage  protocol.TokenUsage
		base   *Baseline
		reason string
	}{
		{name: "zero usage", usage: completeUsage(0, 0), base: &baseline, reason: "token_telemetry_unavailable"},
		{name: "missing provider usage", usage: protocol.TokenUsage{Status: "unavailable", Successes: 10}, base: &baseline, reason: "token_telemetry_unavailable"},
		{name: "missing baseline", usage: completeUsage(500, 10), reason: "baseline_unavailable"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got := Apply(testReport(0.9), tc.usage, tc.base)
			if got.Multiplier != 1 || got.AdjustedComposite != 0.9 || got.DecisionReason != tc.reason {
				t.Fatalf("result = %#v", got)
			}
		})
	}
}

func TestEmbeddedManifestIsMeasuredAndPhaseBApproved(t *testing.T) {
	if !ProductionReady() {
		t.Fatal("reviewed dual-provider v5 manifest must be production ready")
	}
	manifest := ManifestSnapshot()
	if !manifest.ScoringEnabled || manifest.StarterKitRevision == "" || len(manifest.Calibration) != 60 || len(manifest.Baselines) != 6 {
		t.Fatalf("manifest = %#v", manifest)
	}
}

func TestEmbeddedV7ManifestIsQualityOnlyProductionReady(t *testing.T) {
	if !ProductionReadyForVersion(protocol.BenchVersionV7) {
		t.Fatal("embedded quality-only v7 manifest must be production-ready")
	}
	manifest := V7ManifestSnapshot()
	// scoring_enabled stays false permanently under the v7 quality-only contract.
	if manifest.ScoringEnabled || manifest.StarterKitRevision == "" || len(manifest.Calibration) != 60 || len(manifest.Baselines) != 3 {
		t.Fatalf("v7 manifest = %#v", manifest)
	}
	// The reviewed aggregate route resolves its reference baseline now that the
	// contract is production-ready, but under quality-only that baseline never
	// moves the composite (ApplyForVersion is neutral).
	usage := protocol.TokenUsage{
		Provider: v7AggregateProvider, ProfileRevision: v7AggregateProfile,
		Model: llm.V7HarnessModel,
	}
	if _, ok := LookupForVersion(protocol.BenchVersionV7, "full", usage); !ok {
		t.Fatal("reviewed aggregate route must resolve its reference baseline once production-ready")
	}
	neutral := ApplyForVersion(protocol.BenchVersionV7, testReport(0.9), completeUsage(50_000, 5_000), nil)
	if neutral.Multiplier != 1 || neutral.AdjustedComposite != 0.9 {
		t.Fatalf("quality-only v7 must stay neutral regardless of usage/baseline: %#v", neutral)
	}
}

func TestV9TechnicalReadinessDoesNotActivateCurrentVersion(t *testing.T) {
	if !ProductionReadyForVersion(protocol.BenchVersionV9) {
		t.Fatal("v9 ordinary execution should inherit the reviewed quality-only authority")
	}
	if protocol.CurrentBenchVersion != protocol.BenchVersionV8 {
		t.Fatalf("technical readiness changed active version to %d", protocol.CurrentBenchVersion)
	}
}

func TestEmbeddingV7ManifestDoesNotChangeV6QwenLookup(t *testing.T) {
	v5 := ManifestSnapshot()
	baseline := v5.Baselines[0]
	usage := protocol.TokenUsage{
		Provider: baseline.Provider, ProfileRevision: baseline.ProfileRevision,
		Model: baseline.Model,
	}
	fromV5, okV5 := LookupForVersion(protocol.BenchVersionV5, baseline.RunSize, usage)
	fromV6, okV6 := LookupForVersion(protocol.BenchVersionV6, baseline.RunSize, usage)
	if !okV5 || !okV6 || fromV5 != fromV6 {
		t.Fatalf("v6 lookup changed: v5=%#v/%v v6=%#v/%v", fromV5, okV5, fromV6, okV6)
	}
}

func TestReadyForProductionRequiresExplicitDualProviderPhaseBManifest(t *testing.T) {
	manifest := ManifestSnapshot()
	manifest.ScoringEnabled = true
	for _, provider := range []struct{ name, revision, model string }{
		{"chutes", llm.ChutesRelayProfileRevision, llm.LockedUpstreamModel},
		{"openrouter", llm.OpenRouterRelayProfileRevision, llm.LockedHarnessModel},
	} {
		for _, runSize := range []string{"small", "medium", "full"} {
			manifest.Baselines = append(manifest.Baselines, Baseline{
				ID: "reviewed-" + provider.name + "-" + runSize, BenchVersion: 5,
				RunSize: runSize, Provider: provider.name, ProfileRevision: provider.revision,
				Model: provider.model, PromptTokens: 900, CompletionTokens: 100,
				TotalTokens: 1_000, Samples: 20, Aggregation: "nearest_rank_p90",
				StarterKitRevision: manifest.StarterKitRevision,
			})
		}
	}
	if !ReadyForProduction(manifest) {
		t.Fatal("complete explicitly enabled dual-provider manifest must be ready")
	}
	manifest.Baselines = manifest.Baselines[:5]
	if ReadyForProduction(manifest) {
		t.Fatal("missing provider/run-size group must fail closed")
	}
}
