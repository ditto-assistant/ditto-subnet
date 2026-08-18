// Package v9base builds and verifies the signature-bound ordinary-score
// evidence introduced by DittoBench v9.
package v9base

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"math"
	"strings"

	"github.com/ditto-assistant/dittobench-api/internal/scoregates"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

const (
	SchemaVersion = 1
	// ContractRevision identifies the immutable production v9 semantic gate.
	// V9 no longer waits on a sampled calibration artifact: the published
	// one-basis-point contract asks only whether trusted evidence proves any
	// model and authoritative tool use, while efficiency remains a separately
	// versioned ranking signal.
	ContractRevision       = "v9-base-enforce-efficiency-v1"
	ContractManifest       = `{"schema_version":1,"revision":"v9-base-enforce-efficiency-v1","bench_version":9,"rollout_mode":"enforce","model_use_coverage_bps":1,"authoritative_tool_coverage_bps":1}`
	ContractManifestSHA256 = "861d161cd031d5c40a4c50f0ae0c3d4a4f99a8513ff7fc87239f22104ebe3bb8"
	MicrosScale            = int64(1_000_000)

	// ModelDependenceCoverageBPS is the v12 causal model-dependence threshold.
	// It follows the v9 base "prove ANY" discipline (model-use and
	// authoritative-tool are both pinned at 1 bp): a run must prove at least one
	// scored answer in the counterfactual slice is causally downstream of the
	// model. It is bound into the signed root through ScoreGatesSHA256, not the
	// compiled contract manifest, so the v9..v11 manifest/checksum are untouched.
	// A stricter fraction is a future threshold-profile calibration decision.
	ModelDependenceCoverageBPS = 1
)

var ErrInvalidEvidence = errors.New("invalid v9 base evidence")

// Inputs are all trusted identities and observations needed to build one root.
type Inputs struct {
	RunID            string
	BenchVersion     int
	ArtifactSHA256   string
	DatasetSHA256    string
	TranscriptSHA256 string
	Ordinary         scoregates.Score
	Gates            scoregates.Evidence
}

// ContractThresholds returns the compiled production gate contract.
func ContractThresholds() scoregates.Thresholds {
	return scoregates.Thresholds{
		Profile: scoregates.ThresholdProfile{
			ID:             ContractRevision,
			ManifestSHA256: ContractManifestSHA256,
		},
		ModelUseCoverageBPS:          1,
		AuthoritativeToolCoverageBPS: 1,
	}
}

// VerifyCompiledContract catches accidental edits to the manifest without a
// corresponding explicit checksum/revision update.
func VerifyCompiledContract() error {
	sum := sha256.Sum256([]byte(ContractManifest))
	if hex.EncodeToString(sum[:]) != ContractManifestSHA256 {
		return fmt.Errorf("%w: compiled score contract checksum mismatch", ErrInvalidEvidence)
	}
	return nil
}

func ProductionReady() bool { return true }

// Build validates the nested score-gate evidence, applies its versioned mode,
// and returns the typed wire details plus their canonical root digest.
func Build(input Inputs) (protocol.V9BaseDetails, string, scoregates.Score, error) {
	if err := VerifyCompiledContract(); err != nil {
		return protocol.V9BaseDetails{}, "", scoregates.Score{}, err
	}
	if strings.TrimSpace(input.RunID) == "" || len(input.RunID) > 256 {
		return protocol.V9BaseDetails{}, "", scoregates.Score{}, invalid("run_id is required and bounded")
	}
	if !scoregates.SupportedBenchVersion(input.BenchVersion) {
		return protocol.V9BaseDetails{}, "", scoregates.Score{}, invalid("unsupported bench version %d", input.BenchVersion)
	}
	for name, value := range map[string]string{
		"artifact_sha256": input.ArtifactSHA256, "dataset_sha256": input.DatasetSHA256,
		"transcript_sha256": input.TranscriptSHA256,
	} {
		if !validSHA256(value) {
			return protocol.V9BaseDetails{}, "", scoregates.Score{}, invalid("%s is invalid", name)
		}
	}
	if input.Gates.RolloutMode != scoregates.RolloutEnforce || input.Gates.ThresholdProfile != ContractThresholds().Profile {
		return protocol.V9BaseDetails{}, "", scoregates.Score{}, invalid("score gates do not use the compiled contract")
	}
	if input.Gates.ModelUse.ThresholdBPS != ContractThresholds().ModelUseCoverageBPS ||
		input.Gates.AuthoritativeTool.ThresholdBPS != ContractThresholds().AuthoritativeToolCoverageBPS {
		return protocol.V9BaseDetails{}, "", scoregates.Score{}, invalid("score gate thresholds do not use the compiled contract")
	}
	if input.BenchVersion >= scoregates.BenchVersionV12 &&
		input.Gates.ModelDependence.ThresholdBPS != ModelDependenceCoverageBPS {
		return protocol.V9BaseDetails{}, "", scoregates.Score{}, invalid("model-dependence gate threshold does not use the compiled contract")
	}
	semantic, err := input.Gates.CombinedFactorBPS()
	if err != nil {
		return protocol.V9BaseDetails{}, "", scoregates.Score{}, err
	}
	effective, err := scoregates.ApplyForVersion(input.BenchVersion, input.Ordinary, &input.Gates)
	if err != nil {
		return protocol.V9BaseDetails{}, "", scoregates.Score{}, err
	}
	gateDigest, err := input.Gates.DigestHex()
	if err != nil {
		return protocol.V9BaseDetails{}, "", scoregates.Score{}, err
	}
	applied := semantic
	if input.Gates.RolloutMode == scoregates.RolloutShadow {
		applied = scoregates.BasisPointScale
	}
	details := protocol.V9BaseDetails{
		SchemaVersion: SchemaVersion, BenchVersion: input.BenchVersion,
		ScoreContract: protocol.V9ScoreContractIdentity{Revision: ContractRevision, ManifestSHA256: ContractManifestSHA256},
		RunID:         input.RunID, ArtifactSHA256: input.ArtifactSHA256,
		DatasetSHA256: input.DatasetSHA256, TranscriptSHA256: input.TranscriptSHA256,
		OrdinaryCompositeMicros:  scoreMicros(input.Ordinary.Composite),
		OrdinaryStderrMicros:     scoreMicros(input.Ordinary.CompositeStderr),
		ScoreGates:               toWireEvidence(input.Gates),
		ScoreGatesSHA256:         gateDigest,
		SemanticGateFactorBPS:    semantic,
		AppliedGateFactorBPS:     applied,
		EffectiveCompositeMicros: scoreMicros(effective.Composite),
		EffectiveStderrMicros:    scoreMicros(effective.CompositeStderr),
	}
	digest, err := DigestHex(details)
	if err != nil {
		return protocol.V9BaseDetails{}, "", scoregates.Score{}, err
	}
	return details, digest, effective, nil
}

// Validate reconstructs every derived value and rejects identity or evidence
// tampering before a consumer trusts a root digest.
func Validate(details protocol.V9BaseDetails) error {
	if details.SchemaVersion != SchemaVersion || !scoregates.SupportedBenchVersion(details.BenchVersion) {
		return invalid("unsupported schema or bench version")
	}
	if details.ScoreContract != (protocol.V9ScoreContractIdentity{Revision: ContractRevision, ManifestSHA256: ContractManifestSHA256}) {
		return invalid("score contract identity mismatch")
	}
	gates, err := fromWireEvidence(details.ScoreGates)
	if err != nil {
		return err
	}
	ordinary := scoregates.Score{
		Composite:       float64(details.OrdinaryCompositeMicros) / float64(MicrosScale),
		CompositeStderr: float64(details.OrdinaryStderrMicros) / float64(MicrosScale),
	}
	rebuilt, digest, _, err := Build(Inputs{
		RunID: details.RunID, BenchVersion: details.BenchVersion, ArtifactSHA256: details.ArtifactSHA256,
		DatasetSHA256: details.DatasetSHA256, TranscriptSHA256: details.TranscriptSHA256,
		Ordinary: ordinary, Gates: gates,
	})
	if err != nil {
		return err
	}
	if digest == "" || rebuilt != details {
		return invalid("derived v9 base evidence does not match")
	}
	return nil
}

// CanonicalBytes is the language-neutral root bound into the v9 signature.
// The full nested gate evidence is bound through ScoreGatesSHA256.
func CanonicalBytes(details protocol.V9BaseDetails) ([]byte, error) {
	if err := ValidateWithoutDigestRecursion(details); err != nil {
		return nil, err
	}
	var b bytes.Buffer
	fmt.Fprintf(&b, "ditto-v9-base-v1\nschema_version=%d\nbench_version=%d\n", details.SchemaVersion, details.BenchVersion)
	fmt.Fprintf(&b, "score_contract.revision=%s\nscore_contract.manifest_sha256=%s\n", details.ScoreContract.Revision, details.ScoreContract.ManifestSHA256)
	fmt.Fprintf(&b, "run_id=%s\nartifact_sha256=%s\ndataset_sha256=%s\ntranscript_sha256=%s\n", details.RunID, details.ArtifactSHA256, details.DatasetSHA256, details.TranscriptSHA256)
	fmt.Fprintf(&b, "ordinary_composite_micros=%d\nordinary_stderr_micros=%d\nscore_gates_sha256=%s\n", details.OrdinaryCompositeMicros, details.OrdinaryStderrMicros, details.ScoreGatesSHA256)
	fmt.Fprintf(&b, "semantic_gate_factor_bps=%d\napplied_gate_factor_bps=%d\neffective_composite_micros=%d\neffective_stderr_micros=%d\n", details.SemanticGateFactorBPS, details.AppliedGateFactorBPS, details.EffectiveCompositeMicros, details.EffectiveStderrMicros)
	return b.Bytes(), nil
}

// ValidateWithoutDigestRecursion checks every field used by canonicalization.
func ValidateWithoutDigestRecursion(details protocol.V9BaseDetails) error {
	if details.SchemaVersion != SchemaVersion || !scoregates.SupportedBenchVersion(details.BenchVersion) {
		return invalid("unsupported schema or bench version")
	}
	if details.ScoreContract != (protocol.V9ScoreContractIdentity{Revision: ContractRevision, ManifestSHA256: ContractManifestSHA256}) {
		return invalid("score contract identity mismatch")
	}
	if strings.TrimSpace(details.RunID) == "" || len(details.RunID) > 256 {
		return invalid("run_id is required and bounded")
	}
	for name, value := range map[string]string{
		"artifact_sha256": details.ArtifactSHA256, "dataset_sha256": details.DatasetSHA256,
		"transcript_sha256": details.TranscriptSHA256, "score_gates_sha256": details.ScoreGatesSHA256,
	} {
		if !validSHA256(value) {
			return invalid("%s is invalid", name)
		}
	}
	for name, value := range map[string]int64{
		"ordinary_composite_micros":  details.OrdinaryCompositeMicros,
		"ordinary_stderr_micros":     details.OrdinaryStderrMicros,
		"effective_composite_micros": details.EffectiveCompositeMicros,
		"effective_stderr_micros":    details.EffectiveStderrMicros,
	} {
		if value < 0 || value > MicrosScale {
			return invalid("%s is outside [0,%d]", name, MicrosScale)
		}
	}
	gates, err := fromWireEvidence(details.ScoreGates)
	if err != nil {
		return err
	}
	gateDigest, err := gates.DigestHex()
	if err != nil || gateDigest != details.ScoreGatesSHA256 {
		return invalid("score_gates_sha256 mismatch")
	}
	semantic, err := gates.CombinedFactorBPS()
	if err != nil || details.SemanticGateFactorBPS != semantic {
		return invalid("semantic gate factor mismatch")
	}
	applied := semantic
	if gates.RolloutMode == scoregates.RolloutShadow {
		applied = scoregates.BasisPointScale
	}
	if details.AppliedGateFactorBPS != applied {
		return invalid("applied gate factor mismatch")
	}
	if details.EffectiveCompositeMicros != applyBPS(details.OrdinaryCompositeMicros, applied) ||
		details.EffectiveStderrMicros != applyBPS(details.OrdinaryStderrMicros, applied) {
		return invalid("effective score does not match ordinary score and applied factor")
	}
	return nil
}

func DigestHex(details protocol.V9BaseDetails) (string, error) {
	body, err := CanonicalBytes(details)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(body)
	return hex.EncodeToString(sum[:]), nil
}

func scoreMicros(value float64) int64 {
	return int64(math.Round(value * float64(MicrosScale)))
}

func applyBPS(value int64, factor int) int64 {
	return (value*int64(factor) + scoregates.BasisPointScale/2) / scoregates.BasisPointScale
}

func validSHA256(value string) bool {
	if len(value) != sha256.Size*2 || value != strings.ToLower(value) {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}

func invalid(format string, args ...any) error {
	return fmt.Errorf("%w: %s", ErrInvalidEvidence, fmt.Sprintf(format, args...))
}

func toWireEvidence(e scoregates.Evidence) protocol.V9ScoreGateEvidence {
	return protocol.V9ScoreGateEvidence{
		SchemaVersion: e.SchemaVersion, BenchVersion: e.BenchVersion, RolloutMode: string(e.RolloutMode),
		ThresholdProfile: protocol.V9ScoreGateThresholdProfile{ID: e.ThresholdProfile.ID, ManifestSHA256: e.ThresholdProfile.ManifestSHA256},
		ModelUse: protocol.V9ModelUseGateEvidence{
			AdministeredCases: e.ModelUse.AdministeredCases, EligibleCases: e.ModelUse.EligibleCases,
			SuccessfulInferenceCases: e.ModelUse.SuccessfulInferenceCases, MissingInferenceCases: e.ModelUse.MissingInferenceCases,
			ObservedRequests: e.ModelUse.ObservedRequests, SuccessfulRequests: e.ModelUse.SuccessfulRequests,
			PromptTokens: e.ModelUse.PromptTokens, CompletionTokens: e.ModelUse.CompletionTokens,
			Excluded: protocol.V9ScoreGateExclusionCounts{
				Preflight: e.ModelUse.Excluded.Preflight, Ablation: e.ModelUse.Excluded.Ablation,
				Undelivered: e.ModelUse.Excluded.Undelivered, ValidatorFault: e.ModelUse.Excluded.ValidatorFault,
			},
			CaseAttributionComplete: e.ModelUse.CaseAttributionComplete,
			RequestCoverageBPS:      e.ModelUse.RequestCoverageBPS,
			CoverageBPS:             e.ModelUse.CoverageBPS, ThresholdBPS: e.ModelUse.ThresholdBPS,
			Result: string(e.ModelUse.Result), FactorBPS: e.ModelUse.FactorBPS,
		},
		AuthoritativeTool: protocol.V9AuthoritativeToolGateEvidence{
			ExpectedExecutions: e.AuthoritativeTool.ExpectedExecutions, MatchedExecutions: e.AuthoritativeTool.MatchedExecutions,
			MissingExecutions: e.AuthoritativeTool.MissingExecutions, UnexpectedExecutions: e.AuthoritativeTool.UnexpectedExecutions,
			ObservedExecutions: e.AuthoritativeTool.ObservedExecutions, CoverageBPS: e.AuthoritativeTool.CoverageBPS,
			ThresholdBPS: e.AuthoritativeTool.ThresholdBPS, Result: string(e.AuthoritativeTool.Result), FactorBPS: e.AuthoritativeTool.FactorBPS,
		},
		ModelDependence:  toWireModelDependence(e.BenchVersion, e.ModelDependence),
		InferenceLatency: toWireInferenceLatency(e.BenchVersion, e.InferenceLatency),
		AnswerStuffing:   toWireAnswerStuffing(e.BenchVersion, e.AnswerStuffing),
	}
}

// toWireAnswerStuffing emits the v12 answer-stuffing gate only for
// bench_version>=12 AND only when administered, so v9..v11 wire evidence carries
// the zero value (omitted) and stays byte-identical.
func toWireAnswerStuffing(benchVersion int, a scoregates.AnswerStuffingEvidence) protocol.V9AnswerStuffingGateEvidence {
	if benchVersion < scoregates.BenchVersionV12 || a == (scoregates.AnswerStuffingEvidence{}) {
		return protocol.V9AnswerStuffingGateEvidence{}
	}
	return protocol.V9AnswerStuffingGateEvidence{
		AdministeredCases: a.AdministeredCases, EligibleCases: a.EligibleCases,
		StuffedCases: a.StuffedCases, CleanCases: a.CleanCases,
		AttributionComplete: a.AttributionComplete, ReviewRequired: a.ReviewRequired,
		Posture: string(a.Posture), StuffedBPS: a.StuffedBPS, ThresholdBPS: a.ThresholdBPS,
		MinCases:                a.MinCases,
		LooseEligibleCases:      a.LooseEligibleCases,
		LooseStuffedCases:       a.LooseStuffedCases,
		LooseStuffedBPS:         a.LooseStuffedBPS,
		ReviewShareThresholdBPS: a.ReviewShareThresholdBPS,
		Result:                  string(a.Result), FactorBPS: a.FactorBPS,
	}
}

func fromWireAnswerStuffing(a protocol.V9AnswerStuffingGateEvidence) scoregates.AnswerStuffingEvidence {
	return scoregates.AnswerStuffingEvidence{
		AdministeredCases: a.AdministeredCases, EligibleCases: a.EligibleCases,
		StuffedCases: a.StuffedCases, CleanCases: a.CleanCases,
		AttributionComplete: a.AttributionComplete, ReviewRequired: a.ReviewRequired,
		Posture: scoregates.AnswerStuffingPosture(a.Posture), StuffedBPS: a.StuffedBPS, ThresholdBPS: a.ThresholdBPS,
		MinCases:                a.MinCases,
		LooseEligibleCases:      a.LooseEligibleCases,
		LooseStuffedCases:       a.LooseStuffedCases,
		LooseStuffedBPS:         a.LooseStuffedBPS,
		ReviewShareThresholdBPS: a.ReviewShareThresholdBPS,
		Result:                  scoregates.Result(a.Result), FactorBPS: a.FactorBPS,
	}
}

// toWireInferenceLatency emits the v12 inference-latency gate only for
// bench_version>=12 AND only when administered, so v9..v11 wire evidence carries
// the zero value (omitted) and stays byte-identical.
func toWireInferenceLatency(benchVersion int, l scoregates.InferenceLatencyEvidence) protocol.V9InferenceLatencyGateEvidence {
	if benchVersion < scoregates.BenchVersionV12 || l == (scoregates.InferenceLatencyEvidence{}) {
		return protocol.V9InferenceLatencyGateEvidence{}
	}
	return protocol.V9InferenceLatencyGateEvidence{
		AdministeredCases: l.AdministeredCases, EligibleCases: l.EligibleCases,
		FlaggedCases: l.FlaggedCases, UnflaggedCases: l.UnflaggedCases,
		FloorMS: l.FloorMS, AttributionComplete: l.AttributionComplete,
		Posture: string(l.Posture), SubFloorBPS: l.SubFloorBPS,
		ThresholdBPS: l.ThresholdBPS, Result: string(l.Result), FactorBPS: l.FactorBPS,
	}
}

func fromWireInferenceLatency(l protocol.V9InferenceLatencyGateEvidence) scoregates.InferenceLatencyEvidence {
	return scoregates.InferenceLatencyEvidence{
		AdministeredCases: l.AdministeredCases, EligibleCases: l.EligibleCases,
		FlaggedCases: l.FlaggedCases, UnflaggedCases: l.UnflaggedCases,
		FloorMS: l.FloorMS, AttributionComplete: l.AttributionComplete,
		Posture: scoregates.LatencyPosture(l.Posture), SubFloorBPS: l.SubFloorBPS,
		ThresholdBPS: l.ThresholdBPS, Result: scoregates.Result(l.Result), FactorBPS: l.FactorBPS,
	}
}

// toWireModelDependence emits the v12 dependence gate only for bench_version>=12
// so v9..v11 wire evidence carries the zero value (omitted) and stays
// byte-identical.
func toWireModelDependence(benchVersion int, d scoregates.ModelDependenceEvidence) protocol.V9ModelDependenceGateEvidence {
	if benchVersion < scoregates.BenchVersionV12 {
		return protocol.V9ModelDependenceGateEvidence{}
	}
	return protocol.V9ModelDependenceGateEvidence{
		AdministeredCases: d.AdministeredCases, EligibleCases: d.EligibleCases,
		DependentCases: d.DependentCases, IndependentCases: d.IndependentCases,
		SliceAttributionComplete: d.SliceAttributionComplete, DependenceBPS: d.DependenceBPS,
		ThresholdBPS: d.ThresholdBPS, Result: string(d.Result), FactorBPS: d.FactorBPS,
	}
}

func fromWireModelDependence(d protocol.V9ModelDependenceGateEvidence) scoregates.ModelDependenceEvidence {
	return scoregates.ModelDependenceEvidence{
		AdministeredCases: d.AdministeredCases, EligibleCases: d.EligibleCases,
		DependentCases: d.DependentCases, IndependentCases: d.IndependentCases,
		SliceAttributionComplete: d.SliceAttributionComplete, DependenceBPS: d.DependenceBPS,
		ThresholdBPS: d.ThresholdBPS, Result: scoregates.Result(d.Result), FactorBPS: d.FactorBPS,
	}
}

func fromWireEvidence(e protocol.V9ScoreGateEvidence) (scoregates.Evidence, error) {
	result := scoregates.Evidence{
		SchemaVersion: e.SchemaVersion, BenchVersion: e.BenchVersion, RolloutMode: scoregates.RolloutMode(e.RolloutMode),
		ThresholdProfile: scoregates.ThresholdProfile{ID: e.ThresholdProfile.ID, ManifestSHA256: e.ThresholdProfile.ManifestSHA256},
		ModelUse: scoregates.ModelUseEvidence{
			AdministeredCases: e.ModelUse.AdministeredCases, EligibleCases: e.ModelUse.EligibleCases,
			SuccessfulInferenceCases: e.ModelUse.SuccessfulInferenceCases, MissingInferenceCases: e.ModelUse.MissingInferenceCases,
			ObservedRequests: e.ModelUse.ObservedRequests, SuccessfulRequests: e.ModelUse.SuccessfulRequests,
			PromptTokens: e.ModelUse.PromptTokens, CompletionTokens: e.ModelUse.CompletionTokens,
			Excluded: scoregates.ExclusionCounts{
				Preflight: e.ModelUse.Excluded.Preflight, Ablation: e.ModelUse.Excluded.Ablation,
				Undelivered: e.ModelUse.Excluded.Undelivered, ValidatorFault: e.ModelUse.Excluded.ValidatorFault,
			},
			CaseAttributionComplete: e.ModelUse.CaseAttributionComplete,
			RequestCoverageBPS:      e.ModelUse.RequestCoverageBPS,
			CoverageBPS:             e.ModelUse.CoverageBPS, ThresholdBPS: e.ModelUse.ThresholdBPS,
			Result: scoregates.Result(e.ModelUse.Result), FactorBPS: e.ModelUse.FactorBPS,
		},
		AuthoritativeTool: scoregates.AuthoritativeToolEvidence{
			ExpectedExecutions: e.AuthoritativeTool.ExpectedExecutions, MatchedExecutions: e.AuthoritativeTool.MatchedExecutions,
			MissingExecutions: e.AuthoritativeTool.MissingExecutions, UnexpectedExecutions: e.AuthoritativeTool.UnexpectedExecutions,
			ObservedExecutions: e.AuthoritativeTool.ObservedExecutions, CoverageBPS: e.AuthoritativeTool.CoverageBPS,
			ThresholdBPS: e.AuthoritativeTool.ThresholdBPS, Result: scoregates.Result(e.AuthoritativeTool.Result), FactorBPS: e.AuthoritativeTool.FactorBPS,
		},
		ModelDependence:  fromWireModelDependence(e.ModelDependence),
		InferenceLatency: fromWireInferenceLatency(e.InferenceLatency),
		AnswerStuffing:   fromWireAnswerStuffing(e.AnswerStuffing),
	}
	if err := result.Validate(); err != nil {
		return scoregates.Evidence{}, err
	}
	return result, nil
}
