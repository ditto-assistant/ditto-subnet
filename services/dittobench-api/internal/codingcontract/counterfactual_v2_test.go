package codingcontract

import (
	"bytes"
	"encoding/json"
	"testing"
)

func TestCounterfactualV2RemainsShadowOnlyAndBlinded(t *testing.T) {
	assignment := CounterfactualAssignmentV2{
		Schema:                        "dittobench-coding-counterfactual-assignment-v2",
		CodingContractVersion:         CounterfactualV2Version,
		AgentArtifactSHA256:           repeat("a", 64),
		OpaqueAssignmentID:            repeat("b", 64),
		RepositoryEpoch:               "epoch-1",
		MemoryBundleSHA256:            repeat("c", 64),
		SeededMemoryBytes:             1,
		ModelVisibleMemoryTokenBudget: 1,
		MemoryVolumeTier:              "large",
	}
	if err := assignment.Validate(); err != nil {
		t.Fatal(err)
	}
	body, err := json.Marshal(assignment)
	if err != nil {
		t.Fatal(err)
	}
	parsed, err := ParseCounterfactualAssignmentV2(body)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := Digest(parsed); err != nil {
		t.Fatal(err)
	}
	if assignment.WeightEligible {
		t.Fatal("v2 assignment must stay shadow-only")
	}
	for _, forbidden := range [][]byte{
		[]byte(`"condition"`), []byte(`"base_task_group"`),
		[]byte(`"quorum_group"`), []byte(`"replicate_id"`),
	} {
		if bytes.Contains(body, forbidden) {
			t.Fatalf("miner assignment exposed %s", forbidden)
		}
	}
}

func TestCounterfactualV2ParsesGraderPrivateResult(t *testing.T) {
	result := CounterfactualResultV2{
		Schema:                       "dittobench-coding-counterfactual-result-v2",
		CodingContractVersion:        CounterfactualV2Version,
		AgentArtifactSHA256:          repeat("a", 64),
		BaseTaskGroupID:              "group-1",
		Condition:                    CounterfactualV3,
		RepositoryEpoch:              "epoch-1",
		QuorumGroupID:                "quorum-1",
		ReplicateID:                  1,
		TaskEvidenceSHA256:           repeat("b", 64),
		Resolved:                     true,
		ModelVisibleMemoryTokenCount: 4096,
	}
	body, err := json.Marshal(result)
	if err != nil {
		t.Fatal(err)
	}
	parsed, err := ParseCounterfactualResultV2(body)
	if err != nil {
		t.Fatal(err)
	}
	if parsed.Condition != CounterfactualV3 {
		t.Fatalf("condition = %q", parsed.Condition)
	}
	if _, err := Digest(parsed); err != nil {
		t.Fatal(err)
	}
}

func TestCounterfactualV2RejectsWeightEligibilityAndUnknownCondition(t *testing.T) {
	result := CounterfactualResultV2{
		Schema:                "dittobench-coding-counterfactual-result-v2",
		CodingContractVersion: CounterfactualV2Version,
		WeightEligible:        true,
		AgentArtifactSHA256:   repeat("a", 64),
		BaseTaskGroupID:       "group-1",
		Condition:             "unknown",
		RepositoryEpoch:       "epoch-1",
		QuorumGroupID:         "quorum-1",
		ReplicateID:           1,
		TaskEvidenceSHA256:    repeat("b", 64),
	}
	if err := result.Validate(); err == nil {
		t.Fatal("expected invalid result")
	}
}

func repeat(value string, count int) string {
	result := ""
	for range count {
		result += value
	}
	return result
}
