package efficiency

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/catalog"
	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestV8DatasetKnownVectorMatchesPinnedGenerator(t *testing.T) {
	const seed = int64(123456789)
	profile, ok := gen.ProfileForVersion("full", protocol.BenchVersionV8)
	if !ok {
		t.Fatal("resolve full v8 profile")
	}
	dataset, err := gen.GenerateDataset(seed, profile, protocol.BenchVersionV8)
	if err != nil {
		t.Fatalf("generate full v8 dataset: %v", err)
	}
	got, _, err := dataset.SHA256Hex()
	if err != nil {
		t.Fatalf("hash full v8 dataset: %v", err)
	}
	if got != v8DatasetKnownVector {
		t.Fatalf("pinned v8 generator produced %s, want %s", got, v8DatasetKnownVector)
	}
	if got := V8ContractSnapshot().DatasetKnownVector; got != v8DatasetKnownVector {
		t.Fatalf("embedded v8 contract vector %s, want %s", got, v8DatasetKnownVector)
	}
	for _, tc := range dataset.ToolCases {
		for _, expected := range tc.ExpectedTools {
			if expected.Name == "get_agent_job_status" {
				t.Fatalf("v8 case %s requires app-owned job status polling", tc.ID)
			}
		}
	}
	for _, tool := range catalog.CatalogForVersion(protocol.BenchVersionV8) {
		if tool.Name == "get_agent_job_status" {
			t.Fatal("v8 catalog advertises app-owned job status polling")
		}
	}
}

func TestV8QualityOnlyContractFailsClosedOnIdentityDrift(t *testing.T) {
	contract := V8ContractSnapshot()
	if !ReadyForV8QualityOnly(contract) {
		t.Fatal("embedded v8 contract is not ready")
	}
	mutations := []func(*QualityOnlyContract){
		func(c *QualityOnlyContract) { c.SchemaVersion++ },
		func(c *QualityOnlyContract) { c.DatasetKnownVector = "" },
		func(c *QualityOnlyContract) { c.ScorerTokenPolicy = "absolute-baseline" },
		func(c *QualityOnlyContract) { c.EfficiencyAuthority = "scorer" },
		func(c *QualityOnlyContract) { c.Harness.Model = "attacker/model" },
		func(c *QualityOnlyContract) { c.Embedding.Model = "attacker/model" },
	}
	for i, mutate := range mutations {
		candidate := V8ContractSnapshot()
		mutate(&candidate)
		if ReadyForV8QualityOnly(candidate) {
			t.Fatalf("mutation %d did not fail closed", i)
		}
	}
}

func TestV8InheritsV7QualityOnlyTokenContract(t *testing.T) {
	report := testReport(0.91)
	usage := completeUsage(12_000_000, 500_000)
	got := ApplyForVersion(protocol.BenchVersionV8, report, usage, nil)
	if got.Multiplier != 1 || got.AdjustedComposite != report.Composite || got.PenaltyApplied {
		t.Fatalf("v8 token usage moved quality score: %+v", got)
	}
}
