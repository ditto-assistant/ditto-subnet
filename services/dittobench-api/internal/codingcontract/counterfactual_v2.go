package codingcontract

import (
	"errors"
	"fmt"
)

// CounterfactualV2Version is shadow-only and intentionally separate from v1.
const CounterfactualV2Version = 2

type CounterfactualCondition string

const (
	CounterfactualV0 CounterfactualCondition = "v0_none"
	CounterfactualV1 CounterfactualCondition = "v1_relevant"
	CounterfactualV2 CounterfactualCondition = "v2_irrelevant"
	CounterfactualV3 CounterfactualCondition = "v3_stale_conflict"
	CounterfactualV4 CounterfactualCondition = "v4_current_override"
)

// CounterfactualAssignmentV2 is miner-facing. Condition class and base-group
// linkage are deliberately represented only by opaque commitments here.
type CounterfactualAssignmentV2 struct {
	Schema                        string `json:"schema"`
	CodingContractVersion         int    `json:"coding_contract_version"`
	WeightEligible                bool   `json:"weight_eligible"`
	AgentArtifactSHA256           string `json:"agent_artifact_sha256"`
	OpaqueAssignmentID            string `json:"opaque_assignment_id"`
	OpaqueBaseTaskGroupCommitment string `json:"opaque_base_task_group_commitment"`
	PrivateConditionCommitment    string `json:"private_condition_commitment"`
	RepositoryEpoch               string `json:"repository_epoch"`
	QuorumGroupID                 string `json:"quorum_group_id"`
	ReplicateID                   uint32 `json:"replicate_id"`
	SeededMemoryBytes             uint64 `json:"seeded_memory_bytes"`
	ModelVisibleMemoryTokenBudget uint64 `json:"model_visible_memory_token_budget"`
	MemoryVolumeTier              string `json:"memory_volume_tier"`
}

func (assignment CounterfactualAssignmentV2) Validate() error {
	if assignment.Schema != "dittobench-coding-counterfactual-assignment-v2" ||
		assignment.CodingContractVersion != CounterfactualV2Version ||
		assignment.WeightEligible {
		return errors.New("counterfactual assignment is not shadow-only v2")
	}
	for label, value := range map[string]string{
		"agent_artifact_sha256":             assignment.AgentArtifactSHA256,
		"opaque_base_task_group_commitment": assignment.OpaqueBaseTaskGroupCommitment,
		"private_condition_commitment":      assignment.PrivateConditionCommitment,
	} {
		if !validSHA256(value) {
			return fmt.Errorf("%s is not lowercase SHA-256", label)
		}
	}
	if !validIdentifier(assignment.OpaqueAssignmentID, 256) ||
		!validIdentifier(assignment.RepositoryEpoch, 256) ||
		!validIdentifier(assignment.QuorumGroupID, 256) ||
		assignment.ReplicateID == 0 ||
		assignment.ModelVisibleMemoryTokenBudget == 0 ||
		assignment.SeededMemoryBytes == 0 ||
		assignment.MemoryVolumeTier != "small" &&
			assignment.MemoryVolumeTier != "medium" &&
			assignment.MemoryVolumeTier != "large" {
		return errors.New("counterfactual assignment fields are invalid")
	}
	return nil
}

// CounterfactualResultV2 is grader-private evidence. It must never be sent to
// the miner, because it exposes the condition class and matched task grouping.
type CounterfactualResultV2 struct {
	Schema                       string                  `json:"schema"`
	CodingContractVersion        int                     `json:"coding_contract_version"`
	WeightEligible               bool                    `json:"weight_eligible"`
	AgentArtifactSHA256          string                  `json:"agent_artifact_sha256"`
	BaseTaskGroupID              string                  `json:"base_task_group_id"`
	Condition                    CounterfactualCondition `json:"condition"`
	RepositoryEpoch              string                  `json:"repository_epoch"`
	QuorumGroupID                string                  `json:"quorum_group_id"`
	ReplicateID                  uint32                  `json:"replicate_id"`
	TaskEvidenceSHA256           string                  `json:"task_evidence_sha256"`
	Resolved                     bool                    `json:"resolved"`
	ModelVisibleMemoryTokenCount uint64                  `json:"model_visible_memory_token_count"`
}

func (result CounterfactualResultV2) Validate() error {
	if result.Schema != "dittobench-coding-counterfactual-result-v2" ||
		result.CodingContractVersion != CounterfactualV2Version || result.WeightEligible {
		return errors.New("counterfactual result is not shadow-only v2")
	}
	if result.Condition != CounterfactualV0 && result.Condition != CounterfactualV1 &&
		result.Condition != CounterfactualV2 && result.Condition != CounterfactualV3 &&
		result.Condition != CounterfactualV4 {
		return errors.New("counterfactual condition is invalid")
	}
	for label, value := range map[string]string{
		"agent_artifact_sha256": result.AgentArtifactSHA256,
		"task_evidence_sha256":  result.TaskEvidenceSHA256,
	} {
		if !validSHA256(value) {
			return fmt.Errorf("%s is not lowercase SHA-256", label)
		}
	}
	if !validIdentifier(result.BaseTaskGroupID, 256) ||
		!validIdentifier(result.RepositoryEpoch, 256) ||
		!validIdentifier(result.QuorumGroupID, 256) || result.ReplicateID == 0 {
		return errors.New("counterfactual result fields are invalid")
	}
	return nil
}
