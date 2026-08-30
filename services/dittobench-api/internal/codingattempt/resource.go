package codingattempt

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codinggrader"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
)

const maximumResourceProfileBytes = 4 << 20

type limitsWire struct {
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

type resourceWire struct {
	Schema               string     `json:"schema"`
	CandidateLimits      limitsWire `json:"candidate_limits"`
	ProtectedLimits      limitsWire `json:"protected_limits"`
	MaxCombinedDiskBytes int64      `json:"max_combined_disk_bytes"`
	MemoryLimitBytes     uint64     `json:"memory_limit_bytes"`
	ScratchLimitBytes    uint64     `json:"scratch_limit_bytes"`
	PidsLimit            uint32     `json:"pids_limit"`
	CPUQuotaMillis       uint32     `json:"cpu_quota_millis"`
}

func decodeResourceProfile(reader io.Reader, expectedSHA256 string) (codinggrader.ResourcePolicy, error) {
	if reader == nil {
		return codinggrader.ResourcePolicy{}, errors.New("coding resource profile reader is required")
	}
	body, err := io.ReadAll(io.LimitReader(reader, maximumResourceProfileBytes+1))
	if err != nil || len(body) == 0 || len(body) > maximumResourceProfileBytes {
		return codinggrader.ResourcePolicy{}, errors.New("coding resource profile size is invalid")
	}
	if err := codingcontract.ValidateJSONDocument(body, maximumResourceProfileBytes); err != nil {
		return codinggrader.ResourcePolicy{}, errors.New("coding resource profile JSON is invalid")
	}
	decoder := json.NewDecoder(bytes.NewReader(body))
	var wire resourceWire
	if err := decoder.Decode(&wire); err != nil || wire.Schema != "dittobench-coding-grader-resource-v1" {
		return codinggrader.ResourcePolicy{}, errors.New("coding resource profile schema is invalid")
	}
	policy := codinggrader.ResourcePolicy{
		CandidateLimits:      wire.CandidateLimits.policy(),
		ProtectedLimits:      wire.ProtectedLimits.policy(),
		MaxCombinedDiskBytes: wire.MaxCombinedDiskBytes,
		MemoryLimitBytes:     wire.MemoryLimitBytes,
		ScratchLimitBytes:    wire.ScratchLimitBytes,
		PidsLimit:            wire.PidsLimit,
		CPUQuotaMillis:       wire.CPUQuotaMillis,
	}
	if err := policy.Validate(); err != nil {
		return codinggrader.ResourcePolicy{}, errors.New("coding resource profile policy is invalid")
	}
	digest, err := codinggrader.ResourceProfileSHA256(policy)
	if err != nil || digest != expectedSHA256 {
		return codinggrader.ResourcePolicy{}, errors.New("coding resource profile digest is invalid")
	}
	return policy, nil
}

func (wire limitsWire) policy() codingrunner.Limits {
	return codingrunner.Limits{
		MaxBundleBytes: wire.MaxBundleBytes, MaxWorkspaceBytes: wire.MaxWorkspaceBytes,
		MaxFileBytes: wire.MaxFileBytes, MaxPatchBytes: wire.MaxPatchBytes,
		MaxEntries: wire.MaxEntries, MaxToolCalls: wire.MaxToolCalls,
		MaxReadBytes: wire.MaxReadBytes, MaxResponseBytes: wire.MaxResponseBytes,
		MaxSearchResults: wire.MaxSearchResults, MaxReplayCacheBytes: wire.MaxReplayCacheBytes,
		MaxTranscriptBytes: wire.MaxTranscriptBytes,
	}
}
