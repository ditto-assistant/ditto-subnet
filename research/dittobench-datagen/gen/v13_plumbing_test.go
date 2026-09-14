package gen

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

// v13GraderOnlyWireKeys are the JSON keys the v13 grader-only protocol fields
// would take if they were ever serialized. None may appear in the hashed
// artifact, on /seed, or on /run.
var v13GraderOnlyWireKeys = []string{
	"claims", "twin_relation", "required_arg_claims", "restraint", "restraint_claim",
	"forbidden_tools", "critical", "weight", "unit", "forbidden", "grounding",
	"effect_answer", "effect_forbidden", "alternative_expected_tools", "run_after_case_id",
}

func v13PopulatedMemoryCase() protocol.MemoryCase {
	return protocol.MemoryCase{
		BenchVersion:   protocol.BenchVersionV13,
		ID:             "case-v13-mem",
		QuestionID:     "q-v13",
		QuestionType:   "world-contact-current",
		Question:       "Who is the current contact for the onboarding workstream?",
		ExpectedAnswer: "Dana Whitfield",
		AnswerKind:     protocol.AnswerAbsence,
		TwinGroup:      "twin-v13",
		TwinRelation:   protocol.TwinRelationDecision,
		Claims: []protocol.Claim{
			{Kind: "entity", Expected: "Dana Whitfield", Accept: []string{"Dana", "D. Whitfield"}, Critical: true},
			{Kind: "quantity", Expected: "411067", Unit: "cents", Weight: 0.5},
		},
	}
}

func v13PopulatedToolCase() protocol.ToolCase {
	return protocol.ToolCase{
		ID:       "case-v13-tool",
		Category: "restraint_triplet",
		Prompt:   "Set the theme to whatever you think is best.",
		ExpectedTools: []protocol.ToolSpec{{
			Name:         "set_theme",
			RequiredArgs: map[string]string{"theme": "dark"},
			RequiredArgClaims: map[string]protocol.Claim{
				"theme": {Kind: "enum", Expected: "dark", Accept: []string{"Dark", "night"}},
			},
		}},
		MaxToolCalls:    1,
		TwinRelation:    protocol.TwinRelationDecision,
		TwinGroup:       "restraint-group-v13",
		ForbiddenTools:  []string{"set_accent_color"},
		EffectAnswer:    "LFU-229",
		EffectForbidden: []string{"QRZ-880"},
		RunAfterCaseID:  "case-v13-mutation",
		AlternativeExpectedTools: [][]protocol.ToolSpec{{
			{Name: "delete_memory", RequiredArgs: map[string]string{"pair_id": "cnote"}},
			{Name: "save_memory", RequiredArgClaims: map[string]protocol.Claim{"content": {Kind: "fact_update", Expected: "handoff is Monday", Forbidden: []string{"Friday"}}}},
		}},
		Restraint: &protocol.RestraintClaim{
			Kind:           protocol.RestraintClarifyFirst,
			ForbiddenTools: []string{"set_theme"},
			Accept:         []string{"which theme"},
			Grounding:      []string{"midnight"},
		},
	}
}

func assertNoGraderOnlyKeys(t *testing.T, surface string, body []byte) {
	t.Helper()
	var generic any
	if err := json.Unmarshal(body, &generic); err != nil {
		t.Fatalf("%s: %v", surface, err)
	}
	var walk func(path string, v any)
	walk = func(path string, v any) {
		switch node := v.(type) {
		case map[string]any:
			for k, child := range node {
				for _, banned := range v13GraderOnlyWireKeys {
					if k == banned {
						t.Fatalf("%s: grader-only key %q reached the wire at %s", surface, k, path)
					}
				}
				walk(path+"."+k, child)
			}
		case []any:
			for i, child := range node {
				walk(path+"[]", child)
				_ = i
			}
		}
	}
	walk("$", generic)
	lower := strings.ToLower(string(body))
	for _, leak := range []string{"decision_twin", "as_of_twin", "clarify_first", "d. whitfield", "which theme", "lfu-229", "qrz-880", "restraint-group-v13", "case-v13-mutation", "handoff is monday"} {
		if strings.Contains(lower, leak) {
			t.Fatalf("%s: grader-only value %q reached the wire", surface, leak)
		}
	}
}

// TestV13GraderOnlyFieldsNeverReachHarnessWire pins the v13 plumbing invariant
// (issue #1824): MemoryCase.Claims / TwinRelation, ToolCase.TwinRelation /
// Restraint, and ToolSpec.RequiredArgClaims are grader-only. They must survive
// in memory through BuildArtifactForVersion (the grader reads them from the
// staged case) but never serialize — not into the hashed DatasetArtifact, not
// into the /seed SeedRequest, not into the /run RunRequest. A v12 artifact
// assembled from the same staged cases must hash identically with and without
// the fields populated, which is the byte-identity half of the invariant.
func TestV13GraderOnlyFieldsNeverReachHarnessWire(t *testing.T) {
	mc := v13PopulatedMemoryCase()
	tc := v13PopulatedToolCase()
	staged := []StagedCase{{Case: mc, UserID: PrimaryUser, RunAfterWave: 0, V10Provenance: &universe.V10CaseProvenance{Relation: "metamorphic"}}}
	waves := []protocol.SeedRequest{{UserID: PrimaryUser, Wave: 0, Pairs: []protocol.MemoryPair{{PairID: "pair-1", SessionID: "s-1", Prompt: "Dana Whitfield now runs onboarding.", Response: "Noted."}}}}

	for _, version := range []int{protocol.BenchVersionV12, protocol.BenchVersionV13} {
		artifact, err := BuildArtifactForVersion(7, version, []protocol.ToolCase{tc}, staged, waves)
		if err != nil {
			t.Fatalf("v%d build: %v", version, err)
		}
		body, err := artifact.Marshal()
		if err != nil {
			t.Fatal(err)
		}
		assertNoGraderOnlyKeys(t, "v"+string(rune('0'+version/10))+string(rune('0'+version%10))+" artifact", body)
		// The fields are still there for the grader.
		got := artifact.MemoryCases[0]
		if len(got.Claims) != 2 || got.TwinRelation != protocol.TwinRelationDecision || got.AnswerKind != protocol.AnswerAbsence {
			t.Fatalf("v%d: grader-only memory fields did not survive assembly: %+v", version, got.MemoryCase)
		}
		gotTool := artifact.ToolCases[0]
		if gotTool.Restraint == nil || gotTool.TwinRelation != protocol.TwinRelationDecision || gotTool.ExpectedTools[0].RequiredArgClaims["theme"].Expected != "dark" ||
			gotTool.TwinGroup == "" || gotTool.EffectAnswer == "" || len(gotTool.EffectForbidden) == 0 || gotTool.RunAfterCaseID == "" || len(gotTool.ForbiddenTools) == 0 || len(gotTool.AlternativeExpectedTools) != 1 || len(gotTool.Restraint.Grounding) == 0 {
			t.Fatalf("v%d: grader-only tool fields did not survive assembly: %+v", version, gotTool)
		}
	}

	// Byte identity: stripping the grader-only fields must not move the hash.
	bare := staged[0]
	bare.Case.Claims = nil
	bare.Case.TwinRelation = ""
	bareTool := tc
	bareTool.TwinRelation = ""
	bareTool.TwinGroup = ""
	bareTool.ForbiddenTools = nil
	bareTool.EffectAnswer = ""
	bareTool.EffectForbidden = nil
	bareTool.RunAfterCaseID = ""
	bareTool.AlternativeExpectedTools = nil
	bareTool.Restraint = nil
	bareTool.ExpectedTools = []protocol.ToolSpec{{Name: "set_theme", RequiredArgs: map[string]string{"theme": "dark"}}}
	for _, version := range []int{protocol.BenchVersionV12, protocol.BenchVersionV13} {
		full, _ := BuildArtifactForVersion(7, version, []protocol.ToolCase{tc}, staged, waves)
		stripped, _ := BuildArtifactForVersion(7, version, []protocol.ToolCase{bareTool}, []StagedCase{bare}, waves)
		a, _, _ := full.SHA256Hex()
		b, _, _ := stripped.SHA256Hex()
		if a != b {
			t.Fatalf("v%d: grader-only fields changed the artifact hash: %s vs %s", version, a, b)
		}
	}

	// The harness surfaces: /seed carries pairs, /run carries case id + question.
	seedBody, _ := json.Marshal(waves[0])
	assertNoGraderOnlyKeys(t, "/seed", seedBody)
	runBody, _ := json.Marshal(protocol.RunRequest{CaseID: mc.ID, UserInput: mc.Question, BenchVersion: protocol.BenchVersionV9})
	assertNoGraderOnlyKeys(t, "/run", runBody)
	toolRunBody, _ := json.Marshal(protocol.RunRequest{CaseID: tc.ID, UserInput: tc.Prompt, BenchVersion: protocol.BenchVersionV9})
	assertNoGraderOnlyKeys(t, "/run tool", toolRunBody)
	// And the bare protocol structs themselves.
	mcBody, _ := json.Marshal(mc)
	assertNoGraderOnlyKeys(t, "MemoryCase", mcBody)
	tcBody, _ := json.Marshal(tc)
	assertNoGraderOnlyKeys(t, "ToolCase", tcBody)
}

// TestV13ProfileAndEnvelope pins the plumbing acceptance: ProfileForVersion
// accepts v13 for every public run size, the full envelope is 100 tool cases
// and exactly 250 memory cases (224 primary-envelope + 9 isolation), and v13
// does not inherit v10's 251-case envelope.
func TestV13ProfileAndEnvelope(t *testing.T) {
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
	if got, ok := ProfileForVersion("unknown", protocol.BenchVersionV13); ok || got != want["small"] {
		t.Errorf("unknown v13 run size=(%+v,%v), want explicit small fallback plus false", got, ok)
	}
	prof := want["full"]
	artifact, err := GenerateDataset(41, prof, protocol.BenchVersionV13)
	if err != nil {
		t.Fatalf("v13 full generate: %v", err)
	}
	if len(artifact.ToolCases) != 100 || len(artifact.MemoryCases) != 250 {
		t.Fatalf("v13 full envelope = %d tool / %d memory cases, want 100 / 250", len(artifact.ToolCases), len(artifact.MemoryCases))
	}
	isolation := 0
	for _, c := range artifact.MemoryCases {
		if c.QuestionType == "world-isolation-contact-current" {
			isolation++
		}
		if c.BenchVersion != protocol.BenchVersionV13 {
			t.Fatalf("case %s stamped bench_version %d, want 13", c.ID, c.BenchVersion)
		}
	}
	if isolation != 9 {
		t.Fatalf("v13 full isolation cases = %d, want 9", isolation)
	}
	// The v13 stamp loop is gated >= 13 and must not change what v12 already
	// does: the v10..v12 plan loop stamps every planned case with the run's
	// contract, so a v12 full artifact is stamped 12 throughout (and never 13).
	// The loop only exists so no v13 case can fall back to a builder's v12 stamp.
	prof12, _ := ProfileForVersion("full", protocol.BenchVersionV12)
	v12, err := GenerateDataset(41, prof12, protocol.BenchVersionV12)
	if err != nil {
		t.Fatalf("v12 full generate: %v", err)
	}
	stamps := map[int]int{}
	for _, c := range v12.MemoryCases {
		stamps[c.BenchVersion]++
	}
	if len(stamps) != 1 || stamps[protocol.BenchVersionV12] != len(v12.MemoryCases) {
		t.Fatalf("v12 full artifact stamps = %v, want every one of %d cases stamped 12", stamps, len(v12.MemoryCases))
	}
	if protocol.CurrentBenchVersion != protocol.BenchVersionV8 {
		t.Fatalf("v13 plumbing activated a version: current=%d", protocol.CurrentBenchVersion)
	}
}

// TestV13SurfacePassStartsAsV12Copy pins that the v13 surface pass is today a
// byte-for-byte copy of the v12 pass: applied to the same assembled artifact,
// both produce identical bytes. Delete this test in the PR that makes the v13
// pass diverge (the public pre-pass for the private surface pass); until then a
// failure here means one of the two frozen bank tables drifted.
func TestV13SurfacePassStartsAsV12Copy(t *testing.T) {
	prof, _ := ProfileForVersion("medium", protocol.BenchVersionV12)
	rng, err := NewRNGForVersion(5, protocol.BenchVersionV12)
	if err != nil {
		t.Fatal(err)
	}
	toolCases, _ := GenerateToolsForVersion(rng, 5, prof.Tools, protocol.BenchVersionV12)
	suite, err := GenerateMemorySuiteForVersion(rng, 5, prof.Mem, prof.Waves, prof.RawPairsFrac, protocol.BenchVersionV12)
	if err != nil {
		t.Fatal(err)
	}
	build := func() DatasetArtifact {
		flat := make([]ArtifactCase, 0, len(suite.Cases))
		for _, sc := range suite.Cases {
			flat = append(flat, ArtifactCase{MemoryCase: sc.Case, UserID: sc.UserID})
		}
		tools, waves := cloneV8TranscriptSurfaces(toolCases, suite.Waves)
		return DatasetArtifact{Seed: 5, ToolCases: tools, MemoryWaves: waves, MemoryCases: flat}
	}
	viaV12 := build()
	V12ApplyArtifactSurfaceNoise(5, protocol.BenchVersionV12, &viaV12)
	viaV13 := build()
	V13ApplyArtifactSurfacePass(5, protocol.BenchVersionV13, &viaV13)
	a, _, _ := viaV12.SHA256Hex()
	b, _, _ := viaV13.SHA256Hex()
	if a != b {
		t.Fatalf("v13 surface pass diverged from the v12 pass: %s vs %s", a, b)
	}
	untouched := build()
	V13ApplyArtifactSurfacePass(5, protocol.BenchVersionV12, &untouched)
	c, _, _ := untouched.SHA256Hex()
	raw := build()
	d, _, _ := raw.SHA256Hex()
	if c != d {
		t.Fatal("v13 surface pass must be a no-op below bench_version 13")
	}
}

// TestMemoryExposureAuditIsVersionExplicit pins that the exposure audit names
// its contract: the artifact version must match the requested version, the
// v10 entry point still works, and from v13 correction/join families count as
// computed even when the final token is verbatim in the evidence.
func TestMemoryExposureAuditIsVersionExplicit(t *testing.T) {
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV10)
	v10, err := GenerateDataset(41, prof, protocol.BenchVersionV10)
	if err != nil {
		t.Fatal(err)
	}
	legacy, err := AuditV10MemoryExposure(v10)
	if err != nil {
		t.Fatal(err)
	}
	explicit, err := AuditMemoryExposureForVersion(v10, protocol.BenchVersionV10)
	if err != nil || explicit != legacy {
		t.Fatalf("explicit v10 audit %+v (err %v) != legacy %+v", explicit, err, legacy)
	}
	if _, err := AuditMemoryExposureForVersion(v10, protocol.BenchVersionV13); err == nil {
		t.Fatal("audit accepted a v10 artifact under the v13 contract")
	}
	if _, err := AuditMemoryExposureForVersion(v10, protocol.BenchVersionV9); err == nil {
		t.Fatal("audit accepted a pre-evidence-binding version")
	}

	prof13, _ := ProfileForVersion("full", protocol.BenchVersionV13)
	v13, err := GenerateDataset(41, prof13, protocol.BenchVersionV13)
	if err != nil {
		t.Fatal(err)
	}
	result, err := AuditMemoryExposureForVersion(v13, protocol.BenchVersionV13)
	if err != nil {
		t.Fatal(err)
	}
	if result.Eligible < 200 || result.TransformedShare() < 0.50 {
		t.Fatalf("v13 exposure audit: %+v", result)
	}
	// The v13 rule only ever moves a case from verbatim to computed.
	verbatimRule := MemoryExposureResult{}
	for _, c := range v13.MemoryCases {
		if c.ExpectedAnswer == "" || len(c.V10EvidencePairIDs) == 0 {
			continue
		}
		verbatimRule.Eligible++
		if v13ComputedQuestionType(c.QuestionType) {
			verbatimRule.Transformed++
		}
	}
	if verbatimRule.Eligible != result.Eligible || verbatimRule.Transformed == 0 || verbatimRule.Transformed > result.Transformed {
		t.Fatalf("v13 computed families %d of %d eligible, audit transformed %d", verbatimRule.Transformed, verbatimRule.Eligible, result.Transformed)
	}
	// The strict (exact verbatim-rule) share must stay reported alongside the
	// v13 classification: ComputedByRule is exactly the verbatim hits the rule
	// overrode, so Transformed - ComputedByRule is the number the v10..v12 audit
	// would have produced, and it must remain a positive, informative share.
	if result.ComputedByRule <= 0 || result.ComputedByRule > verbatimRule.Transformed {
		t.Fatalf("v13 ComputedByRule = %d, want within (0, %d]", result.ComputedByRule, verbatimRule.Transformed)
	}
	strict := result.Transformed - result.ComputedByRule
	if strict <= 0 || result.StrictTransformedShare() != float64(strict)/float64(result.Eligible) || result.StrictTransformedShare() >= result.TransformedShare() {
		t.Fatalf("v13 strict transformed %d/%d = %.4f, classified %.4f", strict, result.Eligible, result.StrictTransformedShare(), result.TransformedShare())
	}
	if legacy.ComputedByRule != 0 || legacy.StrictTransformedShare() != legacy.TransformedShare() {
		t.Fatalf("v10 audit applied the v13 rule: %+v", legacy)
	}
}
