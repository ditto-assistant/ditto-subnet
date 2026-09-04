package codingcontract

import (
	"bytes"
	"encoding/json"
	"os"
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

func TestCounterfactualV2RejectsWeightEligibility(t *testing.T) {
	result := validResult()
	result.WeightEligible = true
	if err := result.Validate(); err == nil {
		t.Fatal("expected weight-eligible result to fail")
	}
}

func TestCounterfactualV2RejectsUnknownCondition(t *testing.T) {
	result := validResult()
	result.Condition = "unknown"
	if err := result.Validate(); err == nil {
		t.Fatal("expected unknown condition to fail")
	}
}

func TestCounterfactualV2ContractVectorsParse(t *testing.T) {
	assignmentBody, err := os.ReadFile("../../../../packages/dittobench-coding-contract/testdata/coding_counterfactual_assignment_v2.json")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := ParseCounterfactualAssignmentV2(assignmentBody); err != nil {
		t.Fatalf("assignment vector: %v", err)
	}
	resultBody, err := os.ReadFile("../../../../packages/dittobench-coding-contract/testdata/coding_counterfactual_result_v2.json")
	if err != nil {
		t.Fatal(err)
	}
	parsed, err := ParseCounterfactualResultV2(resultBody)
	if err != nil {
		t.Fatalf("result vector: %v", err)
	}
	if parsed.Condition != CounterfactualV3 {
		t.Fatalf("condition = %q", parsed.Condition)
	}
}

func TestCounterfactualAssignmentRejectsPrivateGroupingFields(t *testing.T) {
	body, err := os.ReadFile("../../../../packages/dittobench-coding-contract/testdata/coding_counterfactual_assignment_v2.json")
	if err != nil {
		t.Fatal(err)
	}
	var object map[string]any
	if err := json.Unmarshal(body, &object); err != nil {
		t.Fatal(err)
	}
	for _, key := range []string{
		"condition", "base_task_group_id", "quorum_group_id", "replicate_id",
		"opaque_group_commitment", "private_condition_commitment",
	} {
		hostileObject := cloneJSONObject(object)
		hostileObject[key] = "v1_relevant"
		hostile, err := json.Marshal(hostileObject)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := ParseCounterfactualAssignmentV2(hostile); err == nil {
			t.Fatalf("expected private grouping field %s to fail closed", key)
		}
	}
}

func TestCounterfactualAssignmentAllowsUnknownNonGroupingField(t *testing.T) {
	body, err := os.ReadFile("../../../../packages/dittobench-coding-contract/testdata/coding_counterfactual_assignment_v2.json")
	if err != nil {
		t.Fatal(err)
	}
	var object map[string]any
	if err := json.Unmarshal(body, &object); err != nil {
		t.Fatal(err)
	}
	object["future_unsigned_diagnostic"] = 1
	extended, err := json.Marshal(object)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := ParseCounterfactualAssignmentV2(extended); err != nil {
		t.Fatalf("unknown non-grouping field: %v", err)
	}
}

func validResult() CounterfactualResultV2 {
	return CounterfactualResultV2{
		Schema:                "dittobench-coding-counterfactual-result-v2",
		CodingContractVersion: CounterfactualV2Version,
		AgentArtifactSHA256:   repeat("a", 64),
		BaseTaskGroupID:       "group-1",
		Condition:             CounterfactualV3,
		RepositoryEpoch:       "epoch-1",
		QuorumGroupID:         "quorum-1",
		ReplicateID:           1,
		TaskEvidenceSHA256:    repeat("b", 64),
	}
}

func cloneJSONObject(value map[string]any) map[string]any {
	cloned := make(map[string]any, len(value))
	for key, item := range value {
		cloned[key] = item
	}
	return cloned
}

func repeat(value string, count int) string {
	result := ""
	for range count {
		result += value
	}
	return result
}
