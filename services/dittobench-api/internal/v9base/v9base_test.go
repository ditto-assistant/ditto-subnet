package v9base

import (
	"encoding/json"
	"errors"
	"math"
	"os"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/scoregates"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

const (
	testArtifact   = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	testDataset    = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
	testTranscript = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
)

func attributedGateEvidence(t *testing.T) scoregates.Evidence {
	t.Helper()
	gates, err := scoregates.Build(
		scoregates.ModelUseInput{
			AdministeredCases: 4, EligibleCases: 3, SuccessfulInferenceCases: 2,
			ObservedRequests: 5, SuccessfulRequests: 4, PromptTokens: 1200, CompletionTokens: 240,
			Excluded:          scoregates.ExclusionCounts{Undelivered: 1},
			TelemetryComplete: true, CaseAttributionComplete: true,
		},
		scoregates.AuthoritativeToolInput{
			ExpectedExecutions: 3, MatchedExecutions: 2, UnexpectedExecutions: 1, TelemetryComplete: true,
		},
		ContractThresholds(), scoregates.RolloutShadow,
	)
	if err != nil {
		t.Fatal(err)
	}
	return gates
}

func validInput(t *testing.T) Inputs {
	return Inputs{
		RunID: "run-v9-vector", ArtifactSHA256: testArtifact,
		DatasetSHA256: testDataset, TranscriptSHA256: testTranscript,
		Ordinary: scoregates.Score{Composite: 0.812345, CompositeStderr: 0.023456},
		Gates:    attributedGateEvidence(t),
	}
}

func mustBuild(t *testing.T) (protocol.V9BaseDetails, string) {
	t.Helper()
	details, digest, _, err := Build(validInput(t))
	if err != nil {
		t.Fatal(err)
	}
	return details, digest
}

func TestCompiledShadowContractChecksumAndReadiness(t *testing.T) {
	if err := VerifyCompiledContract(); err != nil {
		t.Fatal(err)
	}
	if ProductionReady() {
		t.Fatal("uncalibrated shadow collection contract must not activate v9")
	}
	thresholds := ContractThresholds()
	if thresholds.Profile.ID != ContractRevision || thresholds.Profile.ManifestSHA256 != ContractManifestSHA256 {
		t.Fatalf("compiled identity drift: %+v", thresholds.Profile)
	}
	if thresholds.ModelUseCoverageBPS != 1 || thresholds.AuthoritativeToolCoverageBPS != 1 {
		t.Fatalf("shadow diagnostic thresholds drift: %+v", thresholds)
	}
}

func TestBuildCanonicalRootGolden(t *testing.T) {
	details, digest := mustBuild(t)
	body, err := CanonicalBytes(details)
	if err != nil {
		t.Fatal(err)
	}
	const want = "ditto-v9-base-v1\n" +
		"schema_version=1\n" +
		"bench_version=9\n" +
		"score_contract.revision=v9-base-shadow-calibration-v1\n" +
		"score_contract.manifest_sha256=5adfbae18c2af63f39d5d087414ae4f1484db0b192ea6da205e2cb9166507bd1\n" +
		"run_id=run-v9-vector\n" +
		"artifact_sha256=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n" +
		"dataset_sha256=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n" +
		"transcript_sha256=cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc\n" +
		"ordinary_composite_micros=812345\n" +
		"ordinary_stderr_micros=23456\n" +
		"score_gates_sha256=5b8a065952838eb072a38808a338e07f9eb3080ecd0cc620812e19c5ab29cf9e\n" +
		"semantic_gate_factor_bps=10000\n" +
		"applied_gate_factor_bps=10000\n" +
		"effective_composite_micros=812345\n" +
		"effective_stderr_micros=23456\n"
	if string(body) != want {
		t.Fatalf("canonical root mismatch:\n%s\ndigest=%s gate=%s", body, digest, details.ScoreGatesSHA256)
	}
	if digest != "bb3e471f477ffc47f329293ec086771cf4ee738e740a3df6d5a568e4728146da" {
		t.Fatalf("root digest = %s", digest)
	}
}

func TestSharedCrossLanguageVectorMatchesGoCanonicalization(t *testing.T) {
	body, err := os.ReadFile("../../testdata/v9_base_contract_vectors.json")
	if err != nil {
		t.Fatal(err)
	}
	var fixture struct {
		Vectors []struct {
			Details             protocol.V9BaseDetails `json:"details"`
			ScoreGatesCanonical string                 `json:"score_gates_canonical"`
			ScoreGatesSHA256    string                 `json:"score_gates_sha256"`
			BaseCanonical       string                 `json:"base_canonical"`
			BaseEvidenceSHA256  string                 `json:"base_evidence_sha256"`
		} `json:"vectors"`
	}
	if err := json.Unmarshal(body, &fixture); err != nil {
		t.Fatal(err)
	}
	if len(fixture.Vectors) != 1 {
		t.Fatalf("vectors = %d, want 1", len(fixture.Vectors))
	}
	vector := fixture.Vectors[0]
	gates, err := fromWireEvidence(vector.Details.ScoreGates)
	if err != nil {
		t.Fatal(err)
	}
	gateBody, err := gates.CanonicalBytes()
	if err != nil {
		t.Fatal(err)
	}
	gateDigest, err := gates.DigestHex()
	if err != nil {
		t.Fatal(err)
	}
	rootBody, err := CanonicalBytes(vector.Details)
	if err != nil {
		t.Fatal(err)
	}
	rootDigest, err := DigestHex(vector.Details)
	if err != nil {
		t.Fatal(err)
	}
	if string(gateBody) != vector.ScoreGatesCanonical || gateDigest != vector.ScoreGatesSHA256 ||
		string(rootBody) != vector.BaseCanonical || rootDigest != vector.BaseEvidenceSHA256 {
		t.Fatalf("shared vector drift:\ngate=%s/%s\nroot=%s/%s", gateDigest, vector.ScoreGatesSHA256, rootDigest, vector.BaseEvidenceSHA256)
	}
}

func TestBuildProducesTypedValidShadowEvidence(t *testing.T) {
	details, digest := mustBuild(t)
	if err := Validate(details); err != nil {
		t.Fatal(err)
	}
	if len(digest) != 64 || details.SchemaVersion != 1 || details.BenchVersion != 9 {
		t.Fatalf("invalid root identity: %+v digest=%s", details, digest)
	}
	if details.SemanticGateFactorBPS != 10_000 || details.AppliedGateFactorBPS != 10_000 {
		t.Fatalf("unexpected factors: semantic=%d applied=%d", details.SemanticGateFactorBPS, details.AppliedGateFactorBPS)
	}
	if details.EffectiveCompositeMicros != 812345 || details.EffectiveStderrMicros != 23456 {
		t.Fatalf("micros changed: %+v", details)
	}
}

func TestShadowInsufficientAttributionPublishesZeroSemanticButPreservesScore(t *testing.T) {
	perCase := []protocol.CaseScore{
		{CaseID: "a", Expected: []string{"search"}, Called: []string{"search"}},
		{CaseID: "b"}, {CaseID: "c"},
	}
	gates, err := BuildGateEvidence(perCase, AggregateModelTelemetry{
		ObservedRequests: 100, SuccessfulRequests: 100, PromptTokens: 20_000,
		TelemetryComplete: true,
	}, true)
	if err != nil {
		t.Fatal(err)
	}
	input := validInput(t)
	input.Gates = gates
	details, _, effective, err := Build(input)
	if err != nil {
		t.Fatal(err)
	}
	if details.ScoreGates.ModelUse.Result != string(scoregates.ResultInsufficientEvidence) ||
		details.ScoreGates.ModelUse.RequestCoverageBPS != 10_000 || details.ScoreGates.ModelUse.CoverageBPS != 0 {
		t.Fatalf("unattributed request evidence passed: %+v", details.ScoreGates.ModelUse)
	}
	if details.SemanticGateFactorBPS != 0 || details.AppliedGateFactorBPS != 10_000 || effective != input.Ordinary {
		t.Fatalf("shadow semantic/application split failed: %+v effective=%+v", details, effective)
	}
}

func TestZeroInferenceIsValidSignedEvidence(t *testing.T) {
	perCase := []protocol.CaseScore{{CaseID: "a"}, {CaseID: "b"}}
	gates, err := BuildGateEvidence(perCase, AggregateModelTelemetry{TelemetryComplete: true}, true)
	if err != nil {
		t.Fatal(err)
	}
	input := validInput(t)
	input.Gates = gates
	details, digest, effective, err := Build(input)
	if err != nil {
		t.Fatal(err)
	}
	if details.ScoreGates.ModelUse.Result != string(scoregates.ResultZeroInference) || details.SemanticGateFactorBPS != 0 {
		t.Fatalf("zero inference not retained: %+v", details.ScoreGates.ModelUse)
	}
	if len(digest) != 64 || effective != input.Ordinary {
		t.Fatalf("zero-inference shadow evidence not signable: digest=%s effective=%+v", digest, effective)
	}
}

func TestBuildRejectsTamperedIdentityAndContract(t *testing.T) {
	tests := []struct {
		name   string
		mutate func(*Inputs)
	}{
		{"empty run", func(v *Inputs) { v.RunID = "" }},
		{"artifact", func(v *Inputs) { v.ArtifactSHA256 = "A" + v.ArtifactSHA256[1:] }},
		{"dataset", func(v *Inputs) { v.DatasetSHA256 = "short" }},
		{"transcript", func(v *Inputs) { v.TranscriptSHA256 = strings.Repeat("g", 64) }},
		{"gate profile", func(v *Inputs) { v.Gates.ThresholdProfile.ID = "other" }},
		{"gate threshold", func(v *Inputs) { v.Gates.ModelUse.ThresholdBPS++ }},
		{"gate mode", func(v *Inputs) { v.Gates.RolloutMode = scoregates.RolloutEnforce }},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			input := validInput(t)
			tt.mutate(&input)
			_, _, _, err := Build(input)
			if err == nil {
				t.Fatal("Build unexpectedly succeeded")
			}
		})
	}
}

func TestValidateRejectsEveryRootFieldFamilyTamper(t *testing.T) {
	base, _ := mustBuild(t)
	tests := []struct {
		name   string
		mutate func(*protocol.V9BaseDetails)
	}{
		{"schema", func(v *protocol.V9BaseDetails) { v.SchemaVersion++ }},
		{"bench", func(v *protocol.V9BaseDetails) { v.BenchVersion-- }},
		{"contract revision", func(v *protocol.V9BaseDetails) { v.ScoreContract.Revision += "x" }},
		{"contract digest", func(v *protocol.V9BaseDetails) { v.ScoreContract.ManifestSHA256 = testArtifact }},
		{"ordinary", func(v *protocol.V9BaseDetails) { v.OrdinaryCompositeMicros++ }},
		{"stderr", func(v *protocol.V9BaseDetails) { v.OrdinaryStderrMicros++ }},
		{"nested evidence", func(v *protocol.V9BaseDetails) { v.ScoreGates.ModelUse.PromptTokens++ }},
		{"nested digest", func(v *protocol.V9BaseDetails) { v.ScoreGatesSHA256 = testArtifact }},
		{"semantic", func(v *protocol.V9BaseDetails) { v.SemanticGateFactorBPS = 0 }},
		{"applied", func(v *protocol.V9BaseDetails) { v.AppliedGateFactorBPS = 0 }},
		{"effective", func(v *protocol.V9BaseDetails) { v.EffectiveCompositeMicros++ }},
		{"effective stderr", func(v *protocol.V9BaseDetails) { v.EffectiveStderrMicros++ }},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			value := base
			tt.mutate(&value)
			if err := Validate(value); err == nil {
				t.Fatal("Validate unexpectedly accepted tamper")
			}
		})
	}
}

func TestRootIdentityTamperChangesDigest(t *testing.T) {
	base, baseDigest := mustBuild(t)
	tests := []struct {
		name   string
		mutate func(*protocol.V9BaseDetails)
	}{
		{"run", func(v *protocol.V9BaseDetails) { v.RunID += "x" }},
		{"artifact", func(v *protocol.V9BaseDetails) { v.ArtifactSHA256 = testDataset }},
		{"dataset", func(v *protocol.V9BaseDetails) { v.DatasetSHA256 = testArtifact }},
		{"transcript", func(v *protocol.V9BaseDetails) { v.TranscriptSHA256 = testArtifact }},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			value := base
			tt.mutate(&value)
			digest, err := DigestHex(value)
			if err != nil {
				t.Fatal(err)
			}
			if digest == baseDigest {
				t.Fatal("identity tamper did not change root digest")
			}
		})
	}
}

func TestPreV9ScoreReportJSONOmitsAllV9Fields(t *testing.T) {
	report := protocol.ScoreReport{
		RunID: "v8", Seed: 8, GeneratedAt: "epoch", Composite: math.Float64frombits(0x3fe0123456789abc),
		ToolMean: 0.5, MemoryMean: 0.25, N: 1,
		Details: &protocol.RunDetails{BenchVersion: protocol.BenchVersionV8, ToolMean: 0.5, MemoryMean: 0.25},
	}
	body, err := json.Marshal(report)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(body), "v9_base") || strings.Contains(string(body), "base_evidence_sha256") {
		t.Fatalf("v8 report leaked v9 fields: %s", body)
	}
}

func TestBuildGateEvidenceFailsClosedOnMissingTelemetry(t *testing.T) {
	_, err := BuildGateEvidence([]protocol.CaseScore{{CaseID: "a"}}, AggregateModelTelemetry{}, true)
	if !errors.Is(err, scoregates.ErrTelemetryUnavailable) {
		t.Fatalf("error = %v, want telemetry unavailable", err)
	}
	_, err = BuildGateEvidence([]protocol.CaseScore{{CaseID: "a"}}, AggregateModelTelemetry{TelemetryComplete: true}, false)
	if !errors.Is(err, scoregates.ErrTelemetryUnavailable) {
		t.Fatalf("tool error = %v, want telemetry unavailable", err)
	}
}

func TestPopulationExclusionsAndAuthoritativeMultisetCounts(t *testing.T) {
	perCase := []protocol.CaseScore{
		{CaseID: "ordinary-a", Observed: true, Expected: []string{"a", "a", "b"}, Called: []string{"a", "b", "extra"}},
		{CaseID: "ordinary-b", Observed: true, Expected: []string{"c"}, Called: []string{"c", "optional"}},
		{CaseID: "undelivered", Undelivered: true, Expected: []string{"ignored"}, Called: []string{"ignored"}},
		{CaseID: "preflight:probe", Expected: []string{"ignored"}, Called: []string{"ignored"}},
		{CaseID: "ablation:inference", Expected: []string{"ignored"}, Called: []string{"ignored"}},
		{CaseID: "validator-fault", ValidatorFault: true, Expected: []string{"ignored"}, Called: []string{"ignored"}},
	}
	gates, err := BuildGateEvidence(perCase, AggregateModelTelemetry{TelemetryComplete: true}, true)
	if err != nil {
		t.Fatal(err)
	}
	if gates.ModelUse.AdministeredCases != 6 || gates.ModelUse.EligibleCases != 2 ||
		gates.ModelUse.Excluded.Undelivered != 1 || gates.ModelUse.Excluded.Preflight != 1 || gates.ModelUse.Excluded.Ablation != 1 || gates.ModelUse.Excluded.ValidatorFault != 1 {
		t.Fatalf("population partition wrong: %+v", gates.ModelUse)
	}
	tool := gates.AuthoritativeTool
	if tool.ExpectedExecutions != 4 || tool.MatchedExecutions != 3 || tool.MissingExecutions != 1 || tool.UnexpectedExecutions != 2 {
		t.Fatalf("tool multiset counts wrong: %+v", tool)
	}
	if tool.FactorBPS != scoregates.BasisPointScale {
		t.Fatalf("unexpected calls affected semantic factor: %+v", tool)
	}
}

func TestSelfReportedExpectedToolCallGetsZeroAuthoritativeCredit(t *testing.T) {
	perCase := []protocol.CaseScore{{
		CaseID: "self-report-only", Observed: false,
		Expected: []string{"search_web"}, Called: []string{"search_web"},
	}}
	gates, err := BuildGateEvidence(perCase, AggregateModelTelemetry{TelemetryComplete: true}, true)
	if err != nil {
		t.Fatal(err)
	}
	tool := gates.AuthoritativeTool
	if tool.ExpectedExecutions != 1 || tool.MatchedExecutions != 0 || tool.MissingExecutions != 1 || tool.ObservedExecutions != 0 || tool.UnexpectedExecutions != 0 {
		t.Fatalf("self-report created authoritative credit: %+v", tool)
	}
	if tool.FactorBPS != 0 {
		t.Fatalf("self-report passed authoritative gate: %+v", tool)
	}
}
