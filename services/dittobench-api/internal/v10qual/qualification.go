// Package v10qual evaluates the private, source-aware Bench v10 release gate.
// It intentionally models only content addresses and aggregate measurements;
// source excerpts, prompts, challenge values, and miner identities have no
// representable field in this package.
package v10qual

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"regexp"
	"sort"
	"strings"
)

const SchemaVersion = 1

type BehaviorClass string

const (
	StableWorldCompiler      BehaviorClass = "stable_world_compiler"
	StableRoleCompiler       BehaviorClass = "stable_role_compiler"
	FiniteRouter             BehaviorClass = "finite_router"
	ModelRetryExpectedValue  BehaviorClass = "model_retry_expected_value"
	PostModelAnswerInsertion BehaviorClass = "post_model_answer_insertion"
	PreselectedToolExecution BehaviorClass = "preselected_tool_execution"
	SemanticRetrieval        BehaviorClass = "semantic_retrieval"
	SemanticReasoning        BehaviorClass = "semantic_reasoning"
	GenericStateInduction    BehaviorClass = "generic_state_induction"
	TransportControl         BehaviorClass = "transport_control"
	InconclusiveControl      BehaviorClass = "inconclusive_control"
)

var requiredClasses = []BehaviorClass{
	StableWorldCompiler, StableRoleCompiler, FiniteRouter, ModelRetryExpectedValue,
	PostModelAnswerInsertion, PreselectedToolExecution, SemanticRetrieval,
	SemanticReasoning, GenericStateInduction, TransportControl, InconclusiveControl,
}

type Cohort string

const (
	CohortCompiler  Cohort = "compiler_shaped"
	CohortReference Cohort = "starter_reference"
	CohortSemantic  Cohort = "semantic_agent"
	CohortControl   Cohort = "control"
)

type ExpectedOutcome string

const (
	CompilerLosesAdvantage ExpectedOutcome = "compiler_loses_advantage"
	SemanticRetainsSkill   ExpectedOutcome = "semantic_retains_skill"
	UncertaintyOnly        ExpectedOutcome = "uncertainty_only"
)

type Thresholds struct {
	MaxCompilerV10BPS           int `json:"max_compiler_v10_bps"`
	MinCompilerAdvantageLossBPS int `json:"min_compiler_advantage_loss_bps"`
	MinSemanticV10BPS           int `json:"min_semantic_v10_bps"`
	MinRendererInvarianceBPS    int `json:"min_renderer_invariance_bps"`
	MinCausalSensitivityBPS     int `json:"min_causal_sensitivity_bps"`
	MinModelDependenceBPS       int `json:"min_model_dependence_bps"`
	MinToolSelectionBPS         int `json:"min_tool_selection_bps"`
	MinEndpointExecutionBPS     int `json:"min_endpoint_execution_bps"`
	MinResultUseBPS             int `json:"min_result_use_bps"`
	MaxPostModelInsertionBPS    int `json:"max_post_model_insertion_bps"`
}

type Fixture struct {
	ID                    string          `json:"id"`
	SourceSafeLabel       string          `json:"source_safe_label"`
	ArtifactSHA256        string          `json:"artifact_sha256"`
	PrivateEvidenceSHA256 string          `json:"private_evidence_sha256"`
	MutationSetSHA256     string          `json:"mutation_set_sha256"`
	LineageID             string          `json:"lineage_id"`
	LineageRevision       int             `json:"lineage_revision"`
	BehaviorClass         BehaviorClass   `json:"behavior_class"`
	Cohort                Cohort          `json:"cohort"`
	ExpectedOutcome       ExpectedOutcome `json:"expected_outcome"`
	ToolAxis              bool            `json:"tool_axis"`
	MemoryAxis            bool            `json:"memory_axis"`
}

type Manifest struct {
	SchemaVersion int        `json:"schema_version"`
	Revision      string     `json:"revision"`
	BenchVersion  int        `json:"bench_version"`
	Thresholds    Thresholds `json:"thresholds"`
	Fixtures      []Fixture  `json:"fixtures"`
}

type ResultStatus string

const (
	ResultComplete        ResultStatus = "complete"
	ResultTransportFailed ResultStatus = "transport_failed"
	ResultInconclusive    ResultStatus = "inconclusive"
)

type Measurement struct {
	FixtureID             string       `json:"fixture_id"`
	ArtifactSHA256        string       `json:"artifact_sha256"`
	MutationSetSHA256     string       `json:"mutation_set_sha256"`
	Status                ResultStatus `json:"status"`
	VariantCount          int          `json:"variant_count"`
	V8CompositeBPS        int          `json:"v8_composite_bps"`
	V10CompositeBPS       int          `json:"v10_composite_bps"`
	RawToolBPS            int          `json:"raw_tool_bps"`
	RawMemoryBPS          int          `json:"raw_memory_bps"`
	RendererInvarianceBPS int          `json:"renderer_invariance_bps"`
	CausalSensitivityBPS  int          `json:"causal_sensitivity_bps"`
	ModelDependenceBPS    int          `json:"model_dependence_bps"`
	ModelToolSelectionBPS int          `json:"model_tool_selection_bps"`
	EndpointExecutionBPS  int          `json:"endpoint_execution_bps"`
	ResultUseBPS          int          `json:"result_use_bps"`
	PostModelInsertionBPS int          `json:"post_model_insertion_bps"`
}

type Results struct {
	SchemaVersion  int           `json:"schema_version"`
	ManifestSHA256 string        `json:"manifest_sha256"`
	Measurements   []Measurement `json:"measurements"`
}

type FixtureVerdict struct {
	FixtureID string `json:"fixture_id"`
	Outcome   string `json:"outcome"`
	Reason    string `json:"reason"`
}

type Report struct {
	SchemaVersion  int              `json:"schema_version"`
	ManifestSHA256 string           `json:"manifest_sha256"`
	Ready          bool             `json:"ready"`
	CohortCounts   map[Cohort]int   `json:"cohort_counts"`
	Verdicts       []FixtureVerdict `json:"verdicts"`
}

var safeID = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`)

func DecodeManifest(reader io.Reader) (Manifest, error) {
	var manifest Manifest
	if err := decodeStrict(reader, &manifest); err != nil {
		return Manifest{}, err
	}
	if err := manifest.Validate(); err != nil {
		return Manifest{}, err
	}
	return manifest, nil
}

func DecodeResults(reader io.Reader) (Results, error) {
	var results Results
	if err := decodeStrict(reader, &results); err != nil {
		return Results{}, err
	}
	if results.SchemaVersion != SchemaVersion || !validSHA256(results.ManifestSHA256) {
		return Results{}, errors.New("qualification results identity is invalid")
	}
	return results, nil
}

func decodeStrict(reader io.Reader, target any) error {
	decoder := json.NewDecoder(reader)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		return err
	}
	if decoder.Decode(&struct{}{}) != io.EOF {
		return errors.New("JSON contains trailing content")
	}
	return nil
}

func (m Manifest) Validate() error {
	if m.SchemaVersion != SchemaVersion || m.BenchVersion != 10 || strings.TrimSpace(m.Revision) == "" {
		return errors.New("manifest must be schema 1 for bench_version 10 with a revision")
	}
	for name, value := range map[string]int{
		"max compiler": m.Thresholds.MaxCompilerV10BPS, "min advantage loss": m.Thresholds.MinCompilerAdvantageLossBPS,
		"min semantic": m.Thresholds.MinSemanticV10BPS, "renderer invariance": m.Thresholds.MinRendererInvarianceBPS,
		"causal sensitivity": m.Thresholds.MinCausalSensitivityBPS, "model dependence": m.Thresholds.MinModelDependenceBPS,
		"tool selection": m.Thresholds.MinToolSelectionBPS, "endpoint execution": m.Thresholds.MinEndpointExecutionBPS,
		"result use": m.Thresholds.MinResultUseBPS, "post-model insertion": m.Thresholds.MaxPostModelInsertionBPS,
	} {
		if value < 0 || value > 10_000 {
			return fmt.Errorf("%s threshold is outside basis-point bounds", name)
		}
	}
	if m.Thresholds.MaxCompilerV10BPS >= 10_000 || m.Thresholds.MinCompilerAdvantageLossBPS == 0 ||
		m.Thresholds.MinSemanticV10BPS == 0 || m.Thresholds.MinRendererInvarianceBPS == 0 ||
		m.Thresholds.MinCausalSensitivityBPS == 0 || m.Thresholds.MinModelDependenceBPS == 0 ||
		m.Thresholds.MinToolSelectionBPS == 0 || m.Thresholds.MinEndpointExecutionBPS == 0 ||
		m.Thresholds.MinResultUseBPS == 0 || m.Thresholds.MaxPostModelInsertionBPS >= 10_000 {
		return errors.New("qualification thresholds do not define a restrictive release gate")
	}
	seenIDs := map[string]bool{}
	seenClasses := map[BehaviorClass]bool{}
	seenCohorts := map[Cohort]bool{}
	lineageRevisions := map[string]map[int]bool{}
	knownClasses := map[BehaviorClass]bool{}
	for _, class := range requiredClasses {
		knownClasses[class] = true
	}
	for _, fixture := range m.Fixtures {
		if !safeID.MatchString(fixture.ID) || !safeID.MatchString(fixture.LineageID) || strings.TrimSpace(fixture.SourceSafeLabel) == "" {
			return errors.New("fixture id, lineage id, and source-safe label are required")
		}
		if seenIDs[fixture.ID] || fixture.LineageRevision < 1 || !validSHA256(fixture.ArtifactSHA256) ||
			!validSHA256(fixture.PrivateEvidenceSHA256) || !validSHA256(fixture.MutationSetSHA256) {
			return fmt.Errorf("fixture %q has duplicate or invalid content-bound identity", fixture.ID)
		}
		if !knownClasses[fixture.BehaviorClass] || !fixture.ToolAxis && !fixture.MemoryAxis {
			return fmt.Errorf("fixture %q has an unknown behavior class or no measured axis", fixture.ID)
		}
		seenIDs[fixture.ID] = true
		seenClasses[fixture.BehaviorClass] = true
		seenCohorts[fixture.Cohort] = true
		if lineageRevisions[fixture.LineageID] == nil {
			lineageRevisions[fixture.LineageID] = map[int]bool{}
		}
		if lineageRevisions[fixture.LineageID][fixture.LineageRevision] {
			return fmt.Errorf("lineage %q repeats revision %d", fixture.LineageID, fixture.LineageRevision)
		}
		lineageRevisions[fixture.LineageID][fixture.LineageRevision] = true
		if err := validateFixtureSemantics(fixture); err != nil {
			return err
		}
	}
	for _, class := range requiredClasses {
		if !seenClasses[class] {
			return fmt.Errorf("manifest omits required behavior class %q", class)
		}
	}
	for _, cohort := range []Cohort{CohortCompiler, CohortReference, CohortSemantic, CohortControl} {
		if !seenCohorts[cohort] {
			return fmt.Errorf("manifest omits required cohort %q", cohort)
		}
	}
	hasLineageRevision := false
	for _, revisions := range lineageRevisions {
		hasLineageRevision = hasLineageRevision || len(revisions) > 1
	}
	if !hasLineageRevision {
		return errors.New("manifest does not independently represent any lineage revision")
	}
	return nil
}

func validateFixtureSemantics(fixture Fixture) error {
	controlClass := fixture.BehaviorClass == TransportControl || fixture.BehaviorClass == InconclusiveControl
	if controlClass != (fixture.ExpectedOutcome == UncertaintyOnly) {
		return fmt.Errorf("fixture %q control class and expected outcome disagree", fixture.ID)
	}
	compilerClass := fixture.BehaviorClass == StableWorldCompiler || fixture.BehaviorClass == StableRoleCompiler ||
		fixture.BehaviorClass == FiniteRouter || fixture.BehaviorClass == ModelRetryExpectedValue ||
		fixture.BehaviorClass == PostModelAnswerInsertion || fixture.BehaviorClass == PreselectedToolExecution
	if compilerClass != (fixture.ExpectedOutcome == CompilerLosesAdvantage) && !controlClass {
		return fmt.Errorf("fixture %q behavior class and expected outcome disagree", fixture.ID)
	}
	switch fixture.ExpectedOutcome {
	case CompilerLosesAdvantage:
		if fixture.Cohort != CohortCompiler {
			return fmt.Errorf("fixture %q compiler outcome has wrong cohort", fixture.ID)
		}
	case SemanticRetainsSkill:
		if fixture.Cohort != CohortSemantic && fixture.Cohort != CohortReference {
			return fmt.Errorf("fixture %q semantic outcome has wrong cohort", fixture.ID)
		}
	case UncertaintyOnly:
		if fixture.Cohort != CohortControl {
			return fmt.Errorf("fixture %q uncertainty outcome has wrong cohort", fixture.ID)
		}
	default:
		return fmt.Errorf("fixture %q has unknown expected outcome", fixture.ID)
	}
	return nil
}

func ManifestSHA256(manifest Manifest) (string, error) {
	if err := manifest.Validate(); err != nil {
		return "", err
	}
	normalized := manifest
	normalized.Fixtures = append([]Fixture(nil), manifest.Fixtures...)
	sort.Slice(normalized.Fixtures, func(i, j int) bool { return normalized.Fixtures[i].ID < normalized.Fixtures[j].ID })
	raw, err := json.Marshal(normalized)
	if err != nil {
		return "", err
	}
	digest := sha256.Sum256(raw)
	return hex.EncodeToString(digest[:]), nil
}

func Evaluate(manifest Manifest, results Results, approvedManifestSHA256 string) (Report, error) {
	digest, err := ManifestSHA256(manifest)
	if err != nil {
		return Report{}, err
	}
	if digest != approvedManifestSHA256 || results.ManifestSHA256 != digest {
		return Report{}, errors.New("manifest does not match the approved content address")
	}
	byID := make(map[string]Measurement, len(results.Measurements))
	for _, measurement := range results.Measurements {
		if _, duplicate := byID[measurement.FixtureID]; duplicate {
			return Report{}, fmt.Errorf("duplicate result for fixture %q", measurement.FixtureID)
		}
		if err := validateMeasurement(measurement); err != nil {
			return Report{}, err
		}
		byID[measurement.FixtureID] = measurement
	}
	report := Report{SchemaVersion: SchemaVersion, ManifestSHA256: digest, Ready: true, CohortCounts: map[Cohort]int{}}
	for _, fixture := range manifest.Fixtures {
		report.CohortCounts[fixture.Cohort]++
		measurement, ok := byID[fixture.ID]
		if !ok || measurement.ArtifactSHA256 != fixture.ArtifactSHA256 || measurement.MutationSetSHA256 != fixture.MutationSetSHA256 {
			return Report{}, fmt.Errorf("fixture %q result is missing or identity-bound to different bytes", fixture.ID)
		}
		verdict := evaluateFixture(fixture, measurement, manifest.Thresholds)
		report.Verdicts = append(report.Verdicts, verdict)
		if fixture.ExpectedOutcome != UncertaintyOnly && verdict.Outcome != "passed" {
			report.Ready = false
		}
	}
	if len(byID) != len(manifest.Fixtures) {
		return Report{}, errors.New("results contain fixtures outside the private manifest")
	}
	sort.Slice(report.Verdicts, func(i, j int) bool { return report.Verdicts[i].FixtureID < report.Verdicts[j].FixtureID })
	return report, nil
}

func validateMeasurement(measurement Measurement) error {
	if !safeID.MatchString(measurement.FixtureID) || !validSHA256(measurement.ArtifactSHA256) || !validSHA256(measurement.MutationSetSHA256) {
		return errors.New("measurement identity is invalid")
	}
	if measurement.Status != ResultComplete && measurement.Status != ResultTransportFailed && measurement.Status != ResultInconclusive {
		return fmt.Errorf("fixture %q has unknown result status", measurement.FixtureID)
	}
	for _, value := range []int{measurement.V8CompositeBPS, measurement.V10CompositeBPS, measurement.RawToolBPS,
		measurement.RawMemoryBPS, measurement.RendererInvarianceBPS, measurement.CausalSensitivityBPS,
		measurement.ModelDependenceBPS, measurement.ModelToolSelectionBPS, measurement.EndpointExecutionBPS,
		measurement.ResultUseBPS, measurement.PostModelInsertionBPS} {
		if value < 0 || value > 10_000 {
			return fmt.Errorf("fixture %q has an out-of-range measurement", measurement.FixtureID)
		}
	}
	if measurement.Status == ResultComplete && measurement.VariantCount < 2 {
		return fmt.Errorf("fixture %q complete result lacks paired variants", measurement.FixtureID)
	}
	return nil
}

func evaluateFixture(fixture Fixture, measurement Measurement, thresholds Thresholds) FixtureVerdict {
	if measurement.Status != ResultComplete {
		return FixtureVerdict{FixtureID: fixture.ID, Outcome: "uncertain", Reason: string(measurement.Status)}
	}
	if fixture.ExpectedOutcome == UncertaintyOnly {
		return FixtureVerdict{FixtureID: fixture.ID, Outcome: "uncertain", Reason: "control_not_release_evidence"}
	}
	if fixture.ExpectedOutcome == CompilerLosesAdvantage {
		loss := measurement.V8CompositeBPS - measurement.V10CompositeBPS
		if measurement.V10CompositeBPS <= thresholds.MaxCompilerV10BPS && loss >= thresholds.MinCompilerAdvantageLossBPS {
			return FixtureVerdict{FixtureID: fixture.ID, Outcome: "passed", Reason: "compiler_advantage_removed"}
		}
		return FixtureVerdict{FixtureID: fixture.ID, Outcome: "failed", Reason: "compiler_advantage_retained"}
	}
	toolPassed := !fixture.ToolAxis || (measurement.ModelToolSelectionBPS >= thresholds.MinToolSelectionBPS &&
		measurement.EndpointExecutionBPS >= thresholds.MinEndpointExecutionBPS && measurement.ResultUseBPS >= thresholds.MinResultUseBPS)
	memoryPassed := !fixture.MemoryAxis || (measurement.RendererInvarianceBPS >= thresholds.MinRendererInvarianceBPS &&
		measurement.CausalSensitivityBPS >= thresholds.MinCausalSensitivityBPS)
	passed := measurement.V10CompositeBPS >= thresholds.MinSemanticV10BPS &&
		memoryPassed && measurement.ModelDependenceBPS >= thresholds.MinModelDependenceBPS && toolPassed &&
		measurement.PostModelInsertionBPS <= thresholds.MaxPostModelInsertionBPS
	if passed {
		return FixtureVerdict{FixtureID: fixture.ID, Outcome: "passed", Reason: "semantic_skill_retained"}
	}
	return FixtureVerdict{FixtureID: fixture.ID, Outcome: "failed", Reason: "semantic_or_provenance_floor_missed"}
}

func validSHA256(value string) bool {
	if len(value) != sha256.Size*2 || strings.ToLower(value) != value {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}
