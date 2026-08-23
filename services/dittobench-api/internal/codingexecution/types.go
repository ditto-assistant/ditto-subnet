// Package codingexecution defines the phase-separated private execution-plan
// contract. It owns no catalog transport, lease endpoint, workspace, or worker.
package codingexecution

import (
	"log/slog"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codinggrader"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
)

const (
	RunnerPlanSchema = "dittobench-coding-runner-plan-v1"
	GraderPlanSchema = "dittobench-coding-grader-plan-v1"
	ResourceSchema   = "dittobench-coding-grader-resource-v1"
)

type Command struct {
	ID                  string   `json:"id"`
	Argv                []string `json:"argv"`
	TimeoutMilliseconds int64    `json:"timeout_milliseconds"`
}

type Limits struct {
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

// RunnerPlan is task-static authoring authority. Ticket, deadline, profile,
// memory, inference grant, and every protected grader field are absent.
type RunnerPlan struct {
	Schema                string    `json:"schema"`
	CodingContractVersion int       `json:"coding_contract_version"`
	CaseID                string    `json:"case_id"`
	VisibleBundleSHA256   string    `json:"visible_bundle_sha256"`
	BaseTreeSHA256        string    `json:"base_tree_sha256"`
	EditablePaths         []string  `json:"editable_paths"`
	CreatablePaths        []string  `json:"creatable_paths"`
	DeletablePaths        []string  `json:"deletable_paths"`
	TestCommands          []Command `json:"test_commands"`
	BuildCommands         []Command `json:"build_commands"`
	Limits                Limits    `json:"limits"`
}

type TestGroup struct {
	Group         string  `json:"group"`
	Command       Command `json:"command"`
	ExpectedTotal uint32  `json:"expected_total"`
}

// GraderPlan contains validator-only protected execution authority. It must
// never be delivered in an authoring lease or miner-facing request.
type GraderPlan struct {
	Schema                       string      `json:"schema"`
	CodingContractVersion        int         `json:"coding_contract_version"`
	CaseID                       string      `json:"case_id"`
	VariantID                    string      `json:"variant_id"`
	VisibleBundleSHA256          string      `json:"visible_bundle_sha256"`
	BaseTreeSHA256               string      `json:"base_tree_sha256"`
	GraderContractSHA256         string      `json:"grader_contract_sha256"`
	GraderBundleSHA256           string      `json:"grader_bundle_sha256"`
	GraderImageDigest            string      `json:"grader_image_digest"`
	GraderPlatform               string      `json:"grader_platform"`
	TestManifestSHA256           string      `json:"test_manifest_sha256"`
	ResourceProfileSHA256        string      `json:"resource_profile_sha256"`
	ExecutionTimeoutMilliseconds int64       `json:"execution_timeout_milliseconds"`
	BuildRequired                bool        `json:"build_required"`
	BuildCommand                 Command     `json:"build_command"`
	TestGroups                   []TestGroup `json:"test_groups"`
	ExecutionOrder               []string    `json:"execution_order"`
}

type ResourceProfile struct {
	Schema               string `json:"schema"`
	CandidateLimits      Limits `json:"candidate_limits"`
	ProtectedLimits      Limits `json:"protected_limits"`
	MaxCombinedDiskBytes int64  `json:"max_combined_disk_bytes"`
	MemoryLimitBytes     uint64 `json:"memory_limit_bytes"`
	ScratchLimitBytes    uint64 `json:"scratch_limit_bytes"`
	PidsLimit            uint32 `json:"pids_limit"`
	CPUQuotaMillis       uint32 `json:"cpu_quota_millis"`
}

type Bundle struct {
	Runner        RunnerPlan
	RuntimePolicy codingcontract.RuntimePolicy
	Grader        GraderPlan
	Resource      ResourceProfile
}

type RunnerBinding struct {
	TicketID            string
	ProfileCapabilityID string
	Deadline            time.Time
}

func (command Command) String() string   { return "CodingExecutionCommand{private=true}" }
func (command Command) GoString() string { return command.String() }
func (command Command) LogValue() slog.Value {
	return slog.StringValue("coding-execution-command-private")
}

func (group TestGroup) String() string   { return "CodingGraderTestGroup{private=true}" }
func (group TestGroup) GoString() string { return group.String() }
func (group TestGroup) LogValue() slog.Value {
	return slog.StringValue("coding-grader-test-group-private")
}

func (plan RunnerPlan) String() string       { return "CodingRunnerPlan{private=true}" }
func (plan RunnerPlan) GoString() string     { return plan.String() }
func (plan RunnerPlan) LogValue() slog.Value { return slog.StringValue("coding-runner-plan-private") }

func (plan GraderPlan) String() string       { return "CodingGraderPlan{private=true}" }
func (plan GraderPlan) GoString() string     { return plan.String() }
func (plan GraderPlan) LogValue() slog.Value { return slog.StringValue("coding-grader-plan-private") }

func (profile ResourceProfile) String() string   { return "CodingResourceProfile{private=true}" }
func (profile ResourceProfile) GoString() string { return profile.String() }
func (profile ResourceProfile) LogValue() slog.Value {
	return slog.StringValue("coding-resource-profile-private")
}

func (bundle Bundle) String() string   { return "CodingExecutionPlanBundle{private=true}" }
func (bundle Bundle) GoString() string { return bundle.String() }
func (bundle Bundle) LogValue() slog.Value {
	return slog.StringValue("coding-execution-plan-bundle-private")
}

func (command Command) runner() codingrunner.CommandSpec {
	return codingrunner.CommandSpec{
		ID: command.ID, Argv: append([]string(nil), command.Argv...),
		Timeout: time.Duration(command.TimeoutMilliseconds) * time.Millisecond,
	}
}

func (limits Limits) runner() codingrunner.Limits {
	return codingrunner.Limits{
		MaxBundleBytes: limits.MaxBundleBytes, MaxWorkspaceBytes: limits.MaxWorkspaceBytes,
		MaxFileBytes: limits.MaxFileBytes, MaxPatchBytes: limits.MaxPatchBytes,
		MaxEntries: limits.MaxEntries, MaxToolCalls: limits.MaxToolCalls,
		MaxReadBytes: limits.MaxReadBytes, MaxResponseBytes: limits.MaxResponseBytes,
		MaxSearchResults: limits.MaxSearchResults, MaxReplayCacheBytes: limits.MaxReplayCacheBytes,
		MaxTranscriptBytes: limits.MaxTranscriptBytes,
	}
}

func (profile ResourceProfile) policy() codinggrader.ResourcePolicy {
	return codinggrader.ResourcePolicy{
		CandidateLimits: profile.CandidateLimits.runner(), ProtectedLimits: profile.ProtectedLimits.runner(),
		MaxCombinedDiskBytes: profile.MaxCombinedDiskBytes, MemoryLimitBytes: profile.MemoryLimitBytes,
		ScratchLimitBytes: profile.ScratchLimitBytes, PidsLimit: profile.PidsLimit,
		CPUQuotaMillis: profile.CPUQuotaMillis,
	}
}
