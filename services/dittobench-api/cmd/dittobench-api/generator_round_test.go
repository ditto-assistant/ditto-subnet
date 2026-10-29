package main

import (
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/challenge"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestGeneratorRoundAdmission(t *testing.T) {
	valid := submitRequest{
		BenchVersion:          13,
		PrivateDatasetMode:    generatorRoundMode,
		PrivateDatasetBytes:   []byte(`{"contract":"x"}`),
		ExpectedDatasetSHA256: strings.Repeat("a", 64),
	}
	// Round admission follows its own gate, never the private V13 one.
	if err := validatePrivateDatasetRequest(valid, false, true); err != nil {
		t.Fatalf("valid round request refused: %v", err)
	}
	if validatePrivateDatasetRequest(valid, true, false) == nil {
		t.Fatal("disabled round mode admitted")
	}
	// A round runs at every supported pack grading policy.
	v14 := valid
	v14.BenchVersion = 14
	if err := validatePrivateDatasetRequest(v14, false, true); err != nil {
		t.Fatalf("v14 round request refused: %v", err)
	}
	for _, edit := range []func(*submitRequest){
		func(r *submitRequest) { r.BenchVersion = 12 },
		func(r *submitRequest) { r.BenchVersion = 15 },
		func(r *submitRequest) { r.PrivateDatasetBytes = nil },
		func(r *submitRequest) { r.ExpectedDatasetSHA256 = "ABC" },
		func(r *submitRequest) { r.PrivateDatasetBytes = make([]byte, challenge.MaxRoundDatasetBytes+1) },
	} {
		r := valid
		edit(&r)
		if validatePrivateDatasetRequest(r, true, true) == nil {
			t.Fatalf("invalid round request admitted: %+v", r.BenchVersion)
		}
	}
}

// The validator parses this feature into generator_round_max_cases, and
// Platform leases a round only when it covers the round's column size; the
// spelling and the number are a cross-component contract.
func TestGeneratorRoundMaxCasesFeatureNamesThePackContractBound(t *testing.T) {
	if got := generatorRoundMaxCasesFeature(); got != "generator-round-max-cases-80" || challenge.MaxCases != 80 {
		t.Fatalf("feature %q for challenge.MaxCases %d", got, challenge.MaxCases)
	}
}

// The validator parses these features into generator_round_bench_versions,
// and Platform leases a round frozen at version n only to a scorer that
// advertises n: the spelling is a cross-component contract.
func TestGeneratorRoundBenchFeaturesNameEverySupportedPolicy(t *testing.T) {
	got := generatorRoundBenchFeatures()
	want := []string{"generator-round-bench-13", "generator-round-bench-14"}
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatalf("features %v, want %v", got, want)
	}
	for _, v := range challenge.SupportedGradingBenchVersions() {
		if !runsGeneratorRoundAt(v) {
			t.Fatalf("v%d is a pack policy but not a round version", v)
		}
	}
	if runsGeneratorRoundAt(12) || runsGeneratorRoundAt(15) {
		t.Fatal("round versions outside the pack policies")
	}
}

func TestGeneratorRoundReportExcludesFaultsFromTheMean(t *testing.T) {
	req := submitRequest{BenchVersion: 13, RunSize: "full", ExpectedDatasetSHA256: strings.Repeat("b", 64)}
	perCase := []protocol.CaseScore{
		{CaseID: "c0/a", Category: challenge.RoundCaseCategory, Kind: "memory", Score: 1},
		{CaseID: "c0/b", Category: challenge.RoundCaseCategory, Kind: "memory", Score: 0.5},
		{CaseID: "c1/a", Category: challenge.RoundFaultCategory, Kind: "memory"},
	}
	r := generatorRoundReport("run-1", 9, req, perCase, protocol.TokenUsage{})
	if r.MemoryMean != 0.75 || r.Composite != 0.75 || len(r.PerCase) != 3 || r.Seed != 9 {
		t.Fatalf("report: %+v", r)
	}
	if r.Details == nil || r.Details.BenchVersion != 13 || r.Details.DatasetSHA256 != req.ExpectedDatasetSHA256 {
		t.Fatalf("details: %+v", r.Details)
	}
}
