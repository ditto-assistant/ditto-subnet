package codinggrader

import (
	"strings"
	"testing"
)

func runtimeFixture() *RuntimeEvidence {
	return &RuntimeEvidence{Schema: "dittobench-coding-rust-runtime-v1", AuthoritySHA256: strings.Repeat("1", 64), InputsSHA256: strings.Repeat("2", 64), ImageSHA256: strings.Repeat("3", 64), ProgramSHA256: strings.Repeat("4", 64), CompilerSHA256: strings.Repeat("5", 64), BridgeLibrarySHA256: strings.Repeat("6", 64), Outcome: "evaluated", BuildSHA256: strings.Repeat("7", 64), ArtifactSHA256: strings.Repeat("8", 64)}
}
func TestRuntimeEvidenceRejectsIncompleteOrContradictoryProof(t *testing.T) {
	if runtimeFixture().Validate() != nil {
		t.Fatal("valid evidence rejected")
	}
	for _, change := range []func(*RuntimeEvidence){func(v *RuntimeEvidence) { v.Schema = "wrong" }, func(v *RuntimeEvidence) { v.ArtifactSHA256 = "" }, func(v *RuntimeEvidence) { v.Outcome = "unknown" }, func(v *RuntimeEvidence) { v.InputsSHA256 = strings.Repeat("A", 64) }, func(v *RuntimeEvidence) { v.Outcome = "compile_failed" }} {
		value := runtimeFixture()
		change(value)
		if value.Validate() == nil {
			t.Fatal("invalid evidence accepted")
		}
	}
	failed := runtimeFixture()
	failed.Outcome = "compile_failed"
	failed.BuildSHA256 = ""
	failed.ArtifactSHA256 = ""
	if failed.Validate() != nil {
		t.Fatal("compile failure rejected")
	}
}
func TestRuntimeCommitmentsAreInReceiptHashAndCloned(t *testing.T) {
	proof := runtimeFixture()
	receipt := ExecutionReceipt{Schema: "dittobench-coding-grader-receipt-v2", Runtime: proof}
	first, err := digestCanonical(receipt)
	if err != nil {
		t.Fatal(err)
	}
	cloned := cloneReceipt(receipt)
	proof.ArtifactSHA256 = strings.Repeat("9", 64)
	second, err := digestCanonical(receipt)
	if err != nil || first == second {
		t.Fatal("artifact not bound into receipt")
	}
	if cloned.Runtime.ArtifactSHA256 != strings.Repeat("8", 64) {
		t.Fatal("runtime proof aliased executor storage")
	}
}
