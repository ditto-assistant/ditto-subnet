package codingcontract

import "testing"

func TestCounterfactualV2RemainsShadowOnlyAndBlinded(t *testing.T) {
	assignment := CounterfactualAssignmentV2{
		Schema:                        "dittobench-coding-counterfactual-assignment-v2",
		CodingContractVersion:         CounterfactualV2Version,
		AgentArtifactSHA256:           repeat("a", 64),
		OpaqueAssignmentID:            "assignment-1",
		OpaqueBaseTaskGroupCommitment: repeat("b", 64),
		PrivateConditionCommitment:    repeat("c", 64),
		RepositoryEpoch:               "epoch-1",
		QuorumGroupID:                 "quorum-1",
		ReplicateID:                   1,
		SeededMemoryBytes:             1,
		ModelVisibleMemoryTokenBudget: 1,
		MemoryVolumeTier:              "large",
	}
	if err := assignment.Validate(); err != nil {
		t.Fatal(err)
	}
	if assignment.WeightEligible {
		t.Fatal("v2 assignment must stay shadow-only")
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
