package codingcontract

import (
	"errors"
	"fmt"
)

// PrivateCatalogV2Version is an additive, shadow-only catalog leaf contract.
const PrivateCatalogV2Version = 2

type PrivateCatalogConditionV2 string

const (
	PrivateCatalogV0 PrivateCatalogConditionV2 = "v0_none"
	PrivateCatalogV1 PrivateCatalogConditionV2 = "v1_relevant"
	PrivateCatalogV2 PrivateCatalogConditionV2 = "v2_irrelevant"
	PrivateCatalogV3 PrivateCatalogConditionV2 = "v3_stale_conflict"
	PrivateCatalogV4 PrivateCatalogConditionV2 = "v4_current_override"
)

// PrivateCatalogTaskV2 binds a release-group authority to exactly one private
// condition. It is not a v1 catalog record and cannot activate scoring.
type PrivateCatalogTaskV2 struct {
	Schema                    string                    `json:"schema"`
	CodingContractVersion     int                       `json:"coding_contract_version"`
	WeightEligible            bool                      `json:"weight_eligible"`
	CorpusReleaseID           string                    `json:"corpus_release_id"`
	CatalogIndex              uint64                    `json:"catalog_index"`
	TaskVersionID             string                    `json:"task_version_id"`
	BaseTaskGroupID           string                    `json:"base_task_group_id"`
	Condition                 PrivateCatalogConditionV2 `json:"condition"`
	RepositoryEpoch           string                    `json:"repository_epoch"`
	PrivateReleaseSHA256      string                    `json:"private_release_sha256"`
	GroupManifestSHA256       string                    `json:"group_manifest_sha256"`
	VisibleSnapshotTreeSHA256 string                    `json:"visible_snapshot_tree_sha256"`
	HiddenGraderTreeSHA256    string                    `json:"hidden_grader_tree_sha256"`
	MemoryBundleSHA256        string                    `json:"memory_bundle_sha256"`
	RuntimePolicySHA256       string                    `json:"runtime_policy_sha256"`
	ResourceProfileSHA256     string                    `json:"resource_profile_sha256"`
	CalibrationSHA256         string                    `json:"calibration_sha256"`
	SemanticReviewSHA256      string                    `json:"semantic_review_sha256"`
	RunnerProfileSHA256       string                    `json:"runner_profile_sha256"`
	TaskCommitmentSHA256      string                    `json:"task_commitment_sha256"`
}

func (task PrivateCatalogTaskV2) Validate() error {
	if err := validatePrivateCatalogTaskV2Fields(task); err != nil {
		return err
	}
	digest, err := PrivateCatalogTaskV2Digest(task)
	if err != nil || digest != task.TaskCommitmentSHA256 {
		return errors.New("private catalog task commitment is invalid")
	}
	return nil
}

func validatePrivateCatalogTaskV2Fields(task PrivateCatalogTaskV2) error {
	if task.Schema != "dittobench-coding-private-catalog-task-v2" ||
		task.CodingContractVersion != PrivateCatalogV2Version || task.WeightEligible {
		return errors.New("private catalog task is not shadow-only v2")
	}
	if task.CatalogIndex > 999999 || !validIdentifier(task.CorpusReleaseID, 256) ||
		!validIdentifier(task.TaskVersionID, 256) ||
		!validIdentifier(task.BaseTaskGroupID, 256) ||
		!validIdentifier(task.RepositoryEpoch, 256) {
		return errors.New("private catalog task identifiers are invalid")
	}
	if task.Condition != PrivateCatalogV0 && task.Condition != PrivateCatalogV1 &&
		task.Condition != PrivateCatalogV2 && task.Condition != PrivateCatalogV3 &&
		task.Condition != PrivateCatalogV4 {
		return errors.New("private catalog condition is invalid")
	}
	for label, value := range map[string]string{
		"private_release_sha256":       task.PrivateReleaseSHA256,
		"group_manifest_sha256":        task.GroupManifestSHA256,
		"visible_snapshot_tree_sha256": task.VisibleSnapshotTreeSHA256,
		"hidden_grader_tree_sha256":    task.HiddenGraderTreeSHA256,
		"memory_bundle_sha256":         task.MemoryBundleSHA256,
		"runtime_policy_sha256":        task.RuntimePolicySHA256,
		"resource_profile_sha256":      task.ResourceProfileSHA256,
		"calibration_sha256":           task.CalibrationSHA256,
		"semantic_review_sha256":       task.SemanticReviewSHA256,
		"runner_profile_sha256":        task.RunnerProfileSHA256,
		"task_commitment_sha256":       task.TaskCommitmentSHA256,
	} {
		if !validSHA256(value) {
			return fmt.Errorf("%s is not lowercase SHA-256", label)
		}
	}
	return nil
}

// PrivateCatalogTaskV2Digest hashes the sorted known-field projection without
// the self-referential commitment field.
func PrivateCatalogTaskV2Digest(task PrivateCatalogTaskV2) (string, error) {
	if err := validatePrivateCatalogTaskV2Fields(task); err != nil {
		return "", err
	}
	projection := map[string]any{
		"schema":                       task.Schema,
		"coding_contract_version":      task.CodingContractVersion,
		"weight_eligible":              task.WeightEligible,
		"corpus_release_id":            task.CorpusReleaseID,
		"catalog_index":                task.CatalogIndex,
		"task_version_id":              task.TaskVersionID,
		"base_task_group_id":           task.BaseTaskGroupID,
		"condition":                    task.Condition,
		"repository_epoch":             task.RepositoryEpoch,
		"private_release_sha256":       task.PrivateReleaseSHA256,
		"group_manifest_sha256":        task.GroupManifestSHA256,
		"visible_snapshot_tree_sha256": task.VisibleSnapshotTreeSHA256,
		"hidden_grader_tree_sha256":    task.HiddenGraderTreeSHA256,
		"memory_bundle_sha256":         task.MemoryBundleSHA256,
		"runtime_policy_sha256":        task.RuntimePolicySHA256,
		"resource_profile_sha256":      task.ResourceProfileSHA256,
		"calibration_sha256":           task.CalibrationSHA256,
		"semantic_review_sha256":       task.SemanticReviewSHA256,
		"runner_profile_sha256":        task.RunnerProfileSHA256,
	}
	return digestUnchecked(projection)
}
