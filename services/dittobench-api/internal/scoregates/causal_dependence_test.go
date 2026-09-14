package scoregates

import (
	"bytes"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestCausalDependenceVerdicts(t *testing.T) {
	claim := TokenSet{}
	claim.Add("4110.67")
	harnessFirst := TokenSet{}
	harnessFirst.AddText("reply exactly: 4110.67")
	if !CausalDependence(claim, ResidualHarnessTokens(harnessFirst)) {
		t.Fatal("compute-then-launder must flag answer_in_prompt")
	}
	// Record exemption: the same value quoted from a delivered record is fine.
	records := TokenSet{}
	records.AddText("Approved figure: $4,110.67")
	if CausalDependence(claim, ResidualHarnessTokens(harnessFirst, records)) {
		t.Fatal("a value present in a delivered record must be exempt")
	}
	// Tool-result exemption behaves the same way.
	toolResult := TokenSet{}
	toolResult.AddText(`{"result":"balance 4110.67"}`)
	if CausalDependence(claim, ResidualHarnessTokens(harnessFirst, toolResult)) {
		t.Fatal("a value the validator served as a tool result must be exempt")
	}
	// A multi-token claim flags only when EVERY token was harness-authored.
	multi := TokenSet{}
	multi.Add("lisbon")
	multi.Add("2019")
	partial := TokenSet{}
	partial.AddText("the city is lisbon")
	if CausalDependence(multi, ResidualHarnessTokens(partial)) {
		t.Fatal("a partially harness-authored claim is not answer_in_prompt")
	}
	partial.AddText("since 2019")
	if !CausalDependence(multi, ResidualHarnessTokens(partial)) {
		t.Fatal("a wholly harness-authored multi-token claim must flag")
	}
	if CausalDependence(TokenSet{}, harnessFirst) {
		t.Fatal("an empty claim never flags")
	}
}

func buildV13(t *testing.T, mode RolloutMode) Evidence {
	t.Helper()
	e, err := Build(BenchVersionV13, validModelInput(), validToolInput(), validThresholdsV12(), mode, dependenceInput(10, 10))
	if err != nil {
		t.Fatalf("Build(v13) error = %v", err)
	}
	return e
}

func settledProvenanceInput(posture ClaimProvenancePosture) ClaimProvenanceInput {
	return ClaimProvenanceInput{
		AdministeredCases: 12, EligibleCases: 10, NotModelEmittedCases: 2, AnswerInPromptCases: 1, FlaggedCases: 3,
		Posture: posture, TelemetryComplete: true, AttributionComplete: true,
	}
}

// v13 inherits the v12 gate stack unchanged: without the attach, a v13 Evidence
// has the same canonical layout as v12 (only the version line differs) and the
// same combined factor. Attaching the gate appends a claim_provenance block,
// binds it into the digest, and never moves the factor.
func TestClaimProvenanceAttachAndCanonicalBytes(t *testing.T) {
	v12 := buildV12(t, RolloutEnforce, dependenceInput(10, 10))
	v13 := buildV13(t, RolloutEnforce)
	b12, err := v12.CanonicalBytes()
	if err != nil {
		t.Fatalf("v12 canonical: %v", err)
	}
	b13, err := v13.CanonicalBytes()
	if err != nil {
		t.Fatalf("v13 canonical: %v", err)
	}
	if bytes.Contains(b12, []byte("claim_provenance")) || bytes.Contains(b13, []byte("claim_provenance")) {
		t.Fatal("an Evidence built without the gate must not carry claim_provenance bytes")
	}
	want := bytes.Replace(b12, []byte("bench_version=12\n"), []byte("bench_version=13\n"), 1)
	if !bytes.Equal(b13, want) {
		t.Fatalf("v13 without the gate must be the v12 layout with the version line changed:\n%s\n---\n%s", b13, want)
	}

	attached, err := AttachClaimProvenance(v13, settledProvenanceInput(ClaimProvenanceShadow))
	if err != nil {
		t.Fatalf("AttachClaimProvenance: %v", err)
	}
	if attached.ClaimProvenance.Result != ResultClaimProvenanceFlagged || attached.ClaimProvenance.FactorBPS != BasisPointScale {
		t.Fatalf("result/factor = %s/%d, want flagged/full", attached.ClaimProvenance.Result, attached.ClaimProvenance.FactorBPS)
	}
	if attached.ClaimProvenance.FlaggedBPS != 3_000 {
		t.Fatalf("flagged_bps = %d, want 3000 (the UNION 3/10, not max(2,1))", attached.ClaimProvenance.FlaggedBPS)
	}
	ab, err := attached.CanonicalBytes()
	if err != nil {
		t.Fatalf("attached canonical: %v", err)
	}
	if !bytes.HasPrefix(ab, b13) || !bytes.Contains(ab, []byte("claim_provenance.result=claim_provenance_flagged\nclaim_provenance.factor_bps=10000\n")) {
		t.Fatalf("attached canonical bytes must extend the base layout with the gate block:\n%s", ab)
	}
	d13, _ := v13.DigestHex()
	dAttached, _ := attached.DigestHex()
	if d13 == dAttached {
		t.Fatal("attaching the gate must change the digest")
	}
	factor, err := attached.CombinedFactorBPS()
	if err != nil || factor != BasisPointScale {
		t.Fatalf("CombinedFactorBPS = %d, %v; the claim gate is an identity term", factor, err)
	}
	score := Score{Composite: 0.9, CompositeStderr: 0.01}
	if got, err := ApplyForVersion(BenchVersionV13, score, &attached); err != nil || got != score {
		t.Fatalf("ApplyForVersion(v13 attached) = %+v, %v", got, err)
	}
	// Enforce posture with zeroed cases validates and round-trips.
	enforce := settledProvenanceInput(ClaimProvenanceEnforce)
	enforce.ZeroedCases = 2
	if _, err := AttachClaimProvenance(v13, enforce); err != nil {
		t.Fatalf("enforce attach: %v", err)
	}
}

func TestClaimProvenanceAttachContracts(t *testing.T) {
	v12 := buildV12(t, RolloutEnforce, dependenceInput(10, 10))
	if _, err := AttachClaimProvenance(v12, settledProvenanceInput(ClaimProvenanceShadow)); !errors.Is(err, ErrInvalidEvidence) {
		t.Fatalf("v12 attach error = %v, want ErrInvalidEvidence", err)
	}
	v13 := buildV13(t, RolloutEnforce)
	bad := map[string]func(*ClaimProvenanceInput){
		"posture":            func(in *ClaimProvenanceInput) { in.Posture = "penalize" },
		"eligible+unsettled": func(in *ClaimProvenanceInput) { in.UnsettledCases = 5 },
		"flagged>eligible":   func(in *ClaimProvenanceInput) { in.NotModelEmittedCases = 11; in.FlaggedCases = 11 },
		"zeroed in shadow":   func(in *ClaimProvenanceInput) { in.ZeroedCases = 1 },
		"complete+unsettled": func(in *ClaimProvenanceInput) { in.UnsettledCases = 1; in.EligibleCases = 9 },
		"incomplete+settled": func(in *ClaimProvenanceInput) { in.AttributionComplete = false },
		"union<max":          func(in *ClaimProvenanceInput) { in.FlaggedCases = 1 },
		"union>sum":          func(in *ClaimProvenanceInput) { in.FlaggedCases = 4 },
		"unattributed>unsettled": func(in *ClaimProvenanceInput) {
			in.UnattributedCallCases = 1
		},
		"zeroed>flagged+unattributed": func(in *ClaimProvenanceInput) {
			in.Posture = ClaimProvenanceEnforce
			in.ZeroedCases = 4
		},
		"negative": func(in *ClaimProvenanceInput) { in.AnswerInPromptCases = -1 },
	}
	for name, mutate := range bad {
		in := settledProvenanceInput(ClaimProvenanceShadow)
		mutate(&in)
		if _, err := AttachClaimProvenance(v13, in); !errors.Is(err, ErrInvalidEvidence) {
			t.Errorf("%s: error = %v, want ErrInvalidEvidence", name, err)
		}
	}
	in := settledProvenanceInput(ClaimProvenanceShadow)
	in.TelemetryComplete = false
	if _, err := AttachClaimProvenance(v13, in); !errors.Is(err, ErrTelemetryUnavailable) {
		t.Fatalf("telemetry error = %v", err)
	}
	// Incomplete attribution publishes insufficient_evidence with a full factor.
	open := settledProvenanceInput(ClaimProvenanceShadow)
	open.AttributionComplete, open.UnsettledCases, open.EligibleCases = false, 2, 8
	e, err := AttachClaimProvenance(v13, open)
	if err != nil || e.ClaimProvenance.Result != ResultInsufficientEvidence || e.ClaimProvenance.FactorBPS != BasisPointScale {
		t.Fatalf("incomplete attribution = %+v, %v", e.ClaimProvenance, err)
	}
	// Unattributed harness calls under enforce: the affected cases are zeroed
	// (fail CLOSED) and counted inside the unsettled population.
	closed := settledProvenanceInput(ClaimProvenanceEnforce)
	closed.AttributionComplete, closed.UnsettledCases, closed.UnattributedCallCases, closed.EligibleCases = false, 2, 2, 8
	closed.ZeroedCases = 3 + 2
	e, err = AttachClaimProvenance(v13, closed)
	if err != nil || e.ClaimProvenance.ZeroedCases != 5 || e.ClaimProvenance.UnattributedCallCases != 2 {
		t.Fatalf("unattributed-call enforce = %+v, %v", e.ClaimProvenance, err)
	}
	// No applicable claim: not_applicable.
	none := ClaimProvenanceInput{AdministeredCases: 3, Posture: ClaimProvenanceShadow, TelemetryComplete: true, AttributionComplete: true}
	e, err = AttachClaimProvenance(v13, none)
	if err != nil || e.ClaimProvenance.Result != ResultNotApplicable {
		t.Fatalf("no applicable claim = %+v, %v", e.ClaimProvenance, err)
	}
	// Clean run: passed.
	clean := settledProvenanceInput(ClaimProvenanceShadow)
	clean.NotModelEmittedCases, clean.AnswerInPromptCases, clean.FlaggedCases = 0, 0, 0
	e, err = AttachClaimProvenance(v13, clean)
	if err != nil || e.ClaimProvenance.Result != ResultPassed {
		t.Fatalf("clean = %+v, %v", e.ClaimProvenance, err)
	}
	// A tampered attached evidence fails Validate.
	e.ClaimProvenance.NotModelEmittedCases, e.ClaimProvenance.FlaggedCases = 3, 3
	if err := e.Validate(); err == nil || !strings.Contains(err.Error(), "derived evidence") {
		t.Fatalf("tampered evidence Validate = %v", err)
	}
	// A v12 evidence carrying the gate is rejected.
	v12.ClaimProvenance = e.ClaimProvenance
	if err := v12.Validate(); !errors.Is(err, ErrInvalidEvidence) {
		t.Fatalf("v12 with claim gate Validate = %v", err)
	}
}

// The Platform-side mirror (ditto_screening_protocol.bench_v9) re-derives the
// score-gate digest from the same fields; a v13 evidence carrying the claim
// gate must hash identically on both sides or every v13 run fails ingestion
// with "score_gates_sha256 does not match". This test pins the Go side of that
// pair in testdata/v13_claim_provenance_evidence.json (JSON evidence + digest),
// which tests/test_bench_v9.py re-derives. Regenerate with
// SCOREGATES_UPDATE_GOLDEN=1 only when the canonical layout changes on purpose,
// and change the Python mirror in the same commit.
func TestClaimProvenanceEvidenceBitPairedFixture(t *testing.T) {
	v13 := buildV13(t, RolloutEnforce)
	in := settledProvenanceInput(ClaimProvenanceEnforce)
	in.ZeroedCases = 3
	attached, err := AttachClaimProvenance(v13, in)
	if err != nil {
		t.Fatalf("AttachClaimProvenance: %v", err)
	}
	digest, err := attached.DigestHex()
	if err != nil {
		t.Fatalf("DigestHex: %v", err)
	}
	canonical, _ := attached.CanonicalBytes()
	factor, _ := attached.CombinedFactorBPS()
	got, err := json.MarshalIndent(map[string]any{
		"evidence":            attached,
		"digest_hex":          digest,
		"canonical_bytes":     string(canonical),
		"combined_factor_bps": factor,
	}, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	got = append(got, '\n')
	path := filepath.Join("testdata", "v13_claim_provenance_evidence.json")
	if os.Getenv("SCOREGATES_UPDATE_GOLDEN") == "1" {
		if err := os.MkdirAll("testdata", 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, got, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	want, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read golden (set SCOREGATES_UPDATE_GOLDEN=1 to write it): %v", err)
	}
	if !bytes.Equal(got, want) {
		t.Fatalf("v13 claim-provenance fixture drifted; the Python mirror must move with it:\n%s\n--- want ---\n%s", got, want)
	}
}
