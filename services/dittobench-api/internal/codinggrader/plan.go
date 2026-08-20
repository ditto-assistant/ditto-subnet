package codinggrader

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
)

type commandProjection struct {
	ID                  string   `json:"id"`
	Argv                []string `json:"argv"`
	TimeoutMilliseconds int64    `json:"timeout_milliseconds"`
}

type limitsProjection struct {
	MaxBundleBytes      int64  `json:"max_bundle_bytes"`
	MaxWorkspaceBytes   int64  `json:"max_workspace_bytes"`
	MaxFileBytes        int64  `json:"max_file_bytes"`
	MaxPatchBytes       int64  `json:"max_patch_bytes"`
	MaxEntries          int    `json:"max_entries"`
	MaxToolCalls        uint32 `json:"max_tool_calls"`
	MaxReadBytes        int    `json:"max_read_bytes"`
	MaxResponseBytes    int    `json:"max_response_bytes"`
	MaxSearchResults    int    `json:"max_search_results"`
	MaxReplayCacheBytes int64  `json:"max_replay_cache_bytes"`
	MaxTranscriptBytes  int64  `json:"max_transcript_bytes"`
}

type resourceProjection struct {
	Schema               string           `json:"schema"`
	CandidateLimits      limitsProjection `json:"candidate_limits"`
	ProtectedLimits      limitsProjection `json:"protected_limits"`
	MaxCombinedDiskBytes int64            `json:"max_combined_disk_bytes"`
	MemoryLimitBytes     uint64           `json:"memory_limit_bytes"`
	ScratchLimitBytes    uint64           `json:"scratch_limit_bytes"`
	PidsLimit            uint32           `json:"pids_limit"`
	CPUQuotaMillis       uint32           `json:"cpu_quota_millis"`
}

type groupProjection struct {
	Group         string            `json:"group"`
	Command       commandProjection `json:"command"`
	ExpectedTotal uint32            `json:"expected_total"`
}

type planProjection struct {
	Schema                string            `json:"schema"`
	CodingContractVersion int               `json:"coding_contract_version"`
	CaseID                string            `json:"case_id"`
	VariantID             string            `json:"variant_id"`
	VisibleBundleSHA256   string            `json:"visible_bundle_sha256"`
	BaseTreeSHA256        string            `json:"base_tree_sha256"`
	GraderContractSHA256  string            `json:"grader_contract_sha256"`
	GraderBundleSHA256    string            `json:"grader_bundle_sha256"`
	GraderImageDigest     string            `json:"grader_image_digest"`
	GraderPlatform        string            `json:"grader_platform"`
	TestManifestSHA256    string            `json:"test_manifest_sha256"`
	ResourceProfileSHA256 string            `json:"resource_profile_sha256"`
	ExecutionTimeoutMS    int64             `json:"execution_timeout_milliseconds"`
	BuildRequired         bool              `json:"build_required"`
	BuildCommand          commandProjection `json:"build_command"`
	TestGroups            []groupProjection `json:"test_groups"`
	ExecutionOrder        []string          `json:"execution_order"`
}

func commandValue(commandID string, argv []string, timeoutMilliseconds int64) commandProjection {
	return commandProjection{ID: commandID, Argv: append([]string(nil), argv...), TimeoutMilliseconds: timeoutMilliseconds}
}

func canonicalJSON(value any) ([]byte, error) {
	raw, err := json.Marshal(value)
	if err != nil {
		return nil, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var projection any
	if err := decoder.Decode(&projection); err != nil {
		return nil, err
	}
	var output bytes.Buffer
	encoder := json.NewEncoder(&output)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(projection); err != nil {
		return nil, err
	}
	return output.Bytes(), nil
}

func digestCanonical(value any) (string, error) {
	body, err := canonicalJSON(value)
	if err != nil {
		return "", err
	}
	digest := sha256.Sum256(body)
	return hex.EncodeToString(digest[:]), nil
}

// GraderContractSHA256 returns the compiled immutable grader-v1 policy root.
func GraderContractSHA256() string {
	digest, err := digestCanonical(map[string]any{
		"schema":                  "dittobench-coding-grader-contract-v1",
		"coding_contract_version": 1,
		"plan_schema":             "dittobench-coding-grader-plan-v1",
		"resource_schema":         "dittobench-coding-grader-resource-v1",
		"plan_fields": []string{
			"schema", "coding_contract_version", "case_id", "variant_id", "visible_bundle_sha256",
			"base_tree_sha256", "grader_contract_sha256", "grader_bundle_sha256", "grader_image_digest",
			"grader_platform", "test_manifest_sha256", "resource_profile_sha256",
			"execution_timeout_milliseconds", "build_required", "build_command", "test_groups", "execution_order",
		},
		"resource_fields": []string{
			"schema", "candidate_limits", "protected_limits", "max_combined_disk_bytes", "memory_limit_bytes",
			"scratch_limit_bytes", "pids_limit", "cpu_quota_millis",
		},
		"limits_fields": []string{
			"max_bundle_bytes", "max_workspace_bytes", "max_file_bytes", "max_patch_bytes", "max_entries",
			"max_tool_calls", "max_read_bytes", "max_response_bytes", "max_search_results",
			"max_replay_cache_bytes", "max_transcript_bytes",
		},
		"receipt_fields": []string{
			"schema", "sequence", "phase", "group", "command_id", "command_sha256", "executor_instance_id",
			"returncode", "passed", "total", "completed", "timed_out", "previous_receipt_sha256",
		},
		"initial_receipt_root_sha256": initialReceiptRoot,
		"deadline_policy":             "lease_setup_then_bounded_execution-v1",
		"evidence_groups":             evidenceGroups,
		"grader_evidence_fields": []string{
			"grader_contract_sha256", "grader_bundle_sha256", "grader_image_digest", "grader_platform", "test_manifest_sha256",
			"grader_plan_sha256", "resource_profile_sha256", "execution_receipt_root_sha256",
			"execution_receipt_count",
			"grader_integrity_before_sha256", "grader_integrity_after_sha256", "build", "test_groups",
		},
		"execution_order":           executionOrder,
		"fail_fast":                 true,
		"protected_bundle_separate": true,
		"receipt_schema":            "dittobench-coding-grader-receipt-v1",
	})
	if err != nil {
		panic(err)
	}
	return digest
}

// ResourceProfileSHA256 hashes the complete sandbox and artifact envelope.
func ResourceProfileSHA256(policy ResourcePolicy) (string, error) {
	return digestCanonical(resourceProjection{
		Schema: "dittobench-coding-grader-resource-v1",
		CandidateLimits: limitsProjection{
			MaxBundleBytes: policy.CandidateLimits.MaxBundleBytes, MaxWorkspaceBytes: policy.CandidateLimits.MaxWorkspaceBytes,
			MaxFileBytes: policy.CandidateLimits.MaxFileBytes, MaxPatchBytes: policy.CandidateLimits.MaxPatchBytes,
			MaxEntries: policy.CandidateLimits.MaxEntries, MaxToolCalls: policy.CandidateLimits.MaxToolCalls,
			MaxReadBytes: policy.CandidateLimits.MaxReadBytes, MaxResponseBytes: policy.CandidateLimits.MaxResponseBytes,
			MaxSearchResults: policy.CandidateLimits.MaxSearchResults, MaxReplayCacheBytes: policy.CandidateLimits.MaxReplayCacheBytes,
			MaxTranscriptBytes: policy.CandidateLimits.MaxTranscriptBytes,
		},
		ProtectedLimits: limitsProjection{
			MaxBundleBytes: policy.ProtectedLimits.MaxBundleBytes, MaxWorkspaceBytes: policy.ProtectedLimits.MaxWorkspaceBytes,
			MaxFileBytes: policy.ProtectedLimits.MaxFileBytes, MaxPatchBytes: policy.ProtectedLimits.MaxPatchBytes,
			MaxEntries: policy.ProtectedLimits.MaxEntries, MaxToolCalls: policy.ProtectedLimits.MaxToolCalls,
			MaxReadBytes: policy.ProtectedLimits.MaxReadBytes, MaxResponseBytes: policy.ProtectedLimits.MaxResponseBytes,
			MaxSearchResults: policy.ProtectedLimits.MaxSearchResults, MaxReplayCacheBytes: policy.ProtectedLimits.MaxReplayCacheBytes,
			MaxTranscriptBytes: policy.ProtectedLimits.MaxTranscriptBytes,
		},
		MaxCombinedDiskBytes: policy.MaxCombinedDiskBytes,
		MemoryLimitBytes:     policy.MemoryLimitBytes,
		ScratchLimitBytes:    policy.ScratchLimitBytes,
		PidsLimit:            policy.PidsLimit,
		CPUQuotaMillis:       policy.CPUQuotaMillis,
	})
}

// GraderPlanSHA256 hashes every command, test count, identity, and execution
// policy used for one task.
func GraderPlanSHA256(manifest Manifest) (string, error) {
	groups := make([]groupProjection, len(manifest.TestGroups))
	for index, group := range manifest.TestGroups {
		groups[index] = groupProjection{
			Group:         group.Group,
			Command:       commandValue(group.Command.ID, group.Command.Argv, group.Command.Timeout.Milliseconds()),
			ExpectedTotal: group.ExpectedTotal,
		}
	}
	return digestCanonical(planProjection{
		Schema:                "dittobench-coding-grader-plan-v1",
		CodingContractVersion: manifest.CodingContractVersion,
		CaseID:                manifest.CaseID,
		VariantID:             manifest.VariantID,
		VisibleBundleSHA256:   manifest.VisibleBundleSHA256,
		BaseTreeSHA256:        manifest.BaseTreeSHA256,
		GraderContractSHA256:  manifest.GraderContractSHA256,
		GraderBundleSHA256:    manifest.GraderBundleSHA256,
		GraderImageDigest:     manifest.GraderImageDigest,
		GraderPlatform:        manifest.GraderPlatform,
		TestManifestSHA256:    manifest.TestManifestSHA256,
		ResourceProfileSHA256: manifest.ResourceProfileSHA256,
		ExecutionTimeoutMS:    manifest.ExecutionTimeout.Milliseconds(),
		BuildRequired:         manifest.Build.Required,
		BuildCommand:          commandValue(manifest.Build.Command.ID, manifest.Build.Command.Argv, manifest.Build.Command.Timeout.Milliseconds()),
		TestGroups:            groups,
		ExecutionOrder:        append([]string(nil), executionOrder...),
	})
}

// CommandSHA256 identifies the exact fixed command used in a receipt.
func CommandSHA256(commandID string, argv []string, timeoutMilliseconds int64) (string, error) {
	if commandID == "" || timeoutMilliseconds <= 0 {
		return "", errors.New("command projection is invalid")
	}
	return digestCanonical(commandValue(commandID, argv, timeoutMilliseconds))
}

func (policy ResourcePolicy) validate() error {
	if err := policy.CandidateLimits.Validate(); err != nil {
		return err
	}
	if err := policy.ProtectedLimits.Validate(); err != nil {
		return err
	}
	peak := policy.CandidateLimits.MaxWorkspaceBytes + policy.ProtectedLimits.MaxWorkspaceBytes
	if policy.CandidateLimits.MaxBundleBytes > policy.ProtectedLimits.MaxBundleBytes {
		peak += policy.CandidateLimits.MaxBundleBytes
	} else {
		peak += policy.ProtectedLimits.MaxBundleBytes
	}
	if policy.ScratchLimitBytes > 8<<30 {
		return fmt.Errorf("coding grader scratch policy is outside hard bounds")
	}
	peak += int64(policy.ScratchLimitBytes)
	if peak <= 0 || policy.MaxCombinedDiskBytes < peak || policy.MaxCombinedDiskBytes > 8<<30 ||
		policy.MemoryLimitBytes < 256<<20 || policy.MemoryLimitBytes > 64<<30 ||
		policy.ScratchLimitBytes == 0 ||
		policy.PidsLimit == 0 || policy.PidsLimit > 4096 ||
		policy.CPUQuotaMillis < 100 || policy.CPUQuotaMillis > 64_000 {
		return fmt.Errorf("coding grader resource policy is outside hard bounds")
	}
	return nil
}
