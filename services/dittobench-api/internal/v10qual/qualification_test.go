package v10qual

import (
	"fmt"
	"strings"
	"testing"
)

func syntheticManifest() Manifest {
	manifest := Manifest{
		SchemaVersion: SchemaVersion,
		Revision:      "private-v10-qualification-test-v1",
		BenchVersion:  10,
		Thresholds: Thresholds{
			MaxCompilerV10BPS: 3000, MinCompilerAdvantageLossBPS: 4000,
			MinSemanticV10BPS: 5000, MinRendererInvarianceBPS: 8000,
			MinCausalSensitivityBPS: 8000, MinModelDependenceBPS: 6000,
			MinToolSelectionBPS: 7000, MinEndpointExecutionBPS: 9000,
			MinResultUseBPS: 7000, MaxPostModelInsertionBPS: 1000,
		},
	}
	for index, class := range requiredClasses {
		fixture := Fixture{
			ID: fmt.Sprintf("fixture-%02d", index), SourceSafeLabel: fmt.Sprintf("source-safe class %02d", index),
			ArtifactSHA256: fmt.Sprintf("%064x", index+1), PrivateEvidenceSHA256: fmt.Sprintf("%064x", index+101),
			MutationSetSHA256: fmt.Sprintf("%064x", index+201), LineageID: fmt.Sprintf("lineage-%02d", index),
			LineageRevision: 1, BehaviorClass: class, ToolAxis: true, MemoryAxis: true,
		}
		switch class {
		case TransportControl, InconclusiveControl:
			fixture.Cohort, fixture.ExpectedOutcome = CohortControl, UncertaintyOnly
		case SemanticRetrieval, SemanticReasoning:
			fixture.Cohort, fixture.ExpectedOutcome = CohortSemantic, SemanticRetainsSkill
		case GenericStateInduction:
			fixture.Cohort, fixture.ExpectedOutcome = CohortReference, SemanticRetainsSkill
		default:
			fixture.Cohort, fixture.ExpectedOutcome = CohortCompiler, CompilerLosesAdvantage
		}
		manifest.Fixtures = append(manifest.Fixtures, fixture)
	}
	revision := manifest.Fixtures[0]
	revision.ID = "fixture-lineage-revision-2"
	revision.ArtifactSHA256 = strings.Repeat("e", 64)
	revision.PrivateEvidenceSHA256 = strings.Repeat("d", 64)
	revision.MutationSetSHA256 = strings.Repeat("c", 64)
	revision.LineageRevision = 2
	manifest.Fixtures = append(manifest.Fixtures, revision)
	return manifest
}

func passingResults(t *testing.T, manifest Manifest) Results {
	t.Helper()
	digest, err := ManifestSHA256(manifest)
	if err != nil {
		t.Fatal(err)
	}
	results := Results{SchemaVersion: SchemaVersion, ManifestSHA256: digest}
	for _, fixture := range manifest.Fixtures {
		measurement := Measurement{
			FixtureID: fixture.ID, ArtifactSHA256: fixture.ArtifactSHA256, MutationSetSHA256: fixture.MutationSetSHA256,
			Status: ResultComplete, VariantCount: 8, V8CompositeBPS: 9000, V10CompositeBPS: 7000,
			RawToolBPS: 7000, RawMemoryBPS: 7000, RendererInvarianceBPS: 9000,
			CausalSensitivityBPS: 9000, ModelDependenceBPS: 9000, ModelToolSelectionBPS: 9000,
			EndpointExecutionBPS: 10_000, ResultUseBPS: 9000, PostModelInsertionBPS: 0,
		}
		if fixture.ExpectedOutcome == CompilerLosesAdvantage {
			measurement.V10CompositeBPS = 2000
		}
		if fixture.BehaviorClass == TransportControl {
			measurement.Status, measurement.VariantCount = ResultTransportFailed, 0
		}
		if fixture.BehaviorClass == InconclusiveControl {
			measurement.Status, measurement.VariantCount = ResultInconclusive, 0
		}
		results.Measurements = append(results.Measurements, measurement)
	}
	return results
}

func TestSourceAwareQualificationPassesWithUncertaintyControls(t *testing.T) {
	manifest := syntheticManifest()
	results := passingResults(t, manifest)
	report, err := Evaluate(manifest, results, results.ManifestSHA256)
	if err != nil {
		t.Fatal(err)
	}
	if !report.Ready {
		t.Fatal("complete compiler and semantic cohorts did not qualify")
	}
	uncertain := 0
	for _, verdict := range report.Verdicts {
		if verdict.Outcome == "uncertain" {
			uncertain++
		}
	}
	if uncertain != 2 {
		t.Fatalf("uncertainty controls=%d, want 2", uncertain)
	}
	for _, cohort := range []Cohort{CohortCompiler, CohortReference, CohortSemantic, CohortControl} {
		if report.CohortCounts[cohort] == 0 {
			t.Errorf("report omitted cohort %q", cohort)
		}
	}
}

func TestNonControlTransportFailureIsUncertainAndBlocks(t *testing.T) {
	manifest := syntheticManifest()
	results := passingResults(t, manifest)
	results.Measurements[0].Status = ResultTransportFailed
	report, err := Evaluate(manifest, results, results.ManifestSHA256)
	if err != nil {
		t.Fatal(err)
	}
	if report.Ready {
		t.Fatal("transport failure became release evidence")
	}
}

func TestModelPresenceAndToolExecutionDoNotSubstituteForDependenceAndSelection(t *testing.T) {
	manifest := syntheticManifest()
	results := passingResults(t, manifest)
	for index, fixture := range manifest.Fixtures {
		if fixture.ExpectedOutcome == SemanticRetainsSkill {
			results.Measurements[index].ModelDependenceBPS = 0
			results.Measurements[index].ModelToolSelectionBPS = 0
			results.Measurements[index].EndpointExecutionBPS = 10_000
			break
		}
	}
	report, err := Evaluate(manifest, results, results.ManifestSHA256)
	if err != nil {
		t.Fatal(err)
	}
	if report.Ready {
		t.Fatal("model presence or endpoint execution substituted for causal dependence and model selection")
	}
}

func TestManifestIsStrictAndContentAddressed(t *testing.T) {
	manifest := syntheticManifest()
	digest, err := ManifestSHA256(manifest)
	if err != nil {
		t.Fatal(err)
	}
	results := passingResults(t, manifest)
	if _, err := Evaluate(manifest, results, strings.Repeat("f", 64)); err == nil {
		t.Fatal("unapproved manifest digest was accepted")
	}
	if _, err := DecodeManifest(strings.NewReader(`{"schema_version":1,"source_excerpt":"forbidden"}`)); err == nil {
		t.Fatal("unknown source-bearing field was accepted")
	}
	if digest != results.ManifestSHA256 {
		t.Fatal("synthetic result lost manifest binding")
	}
}
