package codingcanary

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
)

const (
	publicCanarySchema          = "dittobench-coding-public-certification-canary-v1"
	publicCanaryTaskID          = "PRACTICE-LEDGER-001"
	publicCanaryProfileID       = "public-certification-v1"
	maximumCanonicalBytes       = 1 << 20
	lockedInferencePolicySHA256 = "6dd79225817b56ebf155f8344cd5faf752c8dd57802b21d6d2cbbae9cc2ff0b4"
)

type PublicPack struct {
	Root                  string
	CanaryManifestSHA256  string
	RunnerPlanSHA256      string
	GraderPlanSHA256      string
	ResourceProfileSHA256 string
	InferencePolicySHA256 string
	TaskID                string
	VisibleDir            string
	GraderDir             string
	LockedPolicyPath      string
	IssueTitle            string
	IssueDescription      string
	EditablePaths         []string
	TestCommandIDs        []string
	BuildCommandIDs       []string
	CPUQuotaMillis        uint32
	MemoryLimitBytes      uint64
	PidsLimit             uint32
}

func LoadPublicPack(repoRoot string) (PublicPack, error) {
	var zero PublicPack
	if repoRoot == "" || !filepath.IsAbs(repoRoot) {
		return zero, ErrInvalid
	}
	manifestPath := filepath.Join(repoRoot, "research", "dittobench-coding-datagen", "certification", "v1", "manifest.json")
	body, err := os.ReadFile(manifestPath)
	if err != nil || len(body) == 0 || len(body) > maximumCanonicalBytes {
		return zero, ErrInvalid
	}
	var manifest map[string]any
	if err := json.Unmarshal(body, &manifest); err != nil {
		return zero, ErrInvalid
	}
	canonical, err := canonicalJSON(manifest)
	if err != nil || string(body) != string(canonical) {
		return zero, ErrInvalid
	}
	if schemaOf(manifest) != publicCanarySchema || intOf(manifest["coding_contract_version"]) != 1 ||
		boolOf(manifest["weight_eligible"]) || stringOf(manifest["corpus_scope"]) != "public_certification" {
		return zero, ErrInvalid
	}
	runner, _ := manifest["runner_plan"].(map[string]any)
	grader, _ := manifest["grader_plan"].(map[string]any)
	resource, _ := manifest["resource_profile"].(map[string]any)
	inference, _ := manifest["inference_policy"].(map[string]any)
	if stringOf(runner["task_id"]) != publicCanaryTaskID || stringOf(grader["task_id"]) != publicCanaryTaskID ||
		stringOf(resource["network"]) != "none" {
		return zero, ErrInvalid
	}
	cpus := intOf(resource["cpus_milli"])
	memoryMiB := intOf(resource["memory_mib"])
	pids := intOf(resource["pids"])
	if cpus < 1 || cpus > 256_000 || memoryMiB < 1 || memoryMiB > 1_048_576 || pids < 1 || pids > 1_000_000 {
		return zero, ErrInvalid
	}
	policyPath := filepath.Join(repoRoot, filepath.FromSlash(stringOf(inference["path"])))
	policyBody, err := os.ReadFile(policyPath)
	if err != nil {
		return zero, ErrInvalid
	}
	policyDigest := sha256.Sum256(policyBody)
	if hex.EncodeToString(policyDigest[:]) != lockedInferencePolicySHA256 ||
		stringOf(inference["sha256"]) != lockedInferencePolicySHA256 {
		return zero, ErrInvalid
	}
	runnerSHA, err := digestCanonicalObject(runner)
	if err != nil {
		return zero, err
	}
	graderSHA, err := digestCanonicalObject(grader)
	if err != nil {
		return zero, err
	}
	resourceSHA, err := digestCanonicalObject(resource)
	if err != nil {
		return zero, err
	}
	manifestSHA := sha256.Sum256(body)
	visible := filepath.Join(repoRoot, "research", "dittobench-coding-datagen", "practice", "v1", "capsules", publicCanaryTaskID, "visible", "workspace")
	graderDir := filepath.Join(repoRoot, "research", "dittobench-coding-datagen", "practice", "v1", "capsules", publicCanaryTaskID, "grader")
	if _, err := os.Stat(visible); err != nil {
		return zero, ErrInvalid
	}
	if _, err := os.Stat(graderDir); err != nil {
		return zero, ErrInvalid
	}
	return PublicPack{
		Root: repoRoot, CanaryManifestSHA256: hex.EncodeToString(manifestSHA[:]),
		RunnerPlanSHA256: runnerSHA, GraderPlanSHA256: graderSHA,
		ResourceProfileSHA256: resourceSHA, InferencePolicySHA256: lockedInferencePolicySHA256,
		TaskID: publicCanaryTaskID, VisibleDir: visible, GraderDir: graderDir, LockedPolicyPath: policyPath,
		IssueTitle: "Preserve reference identity", IssueDescription: "Reference normalization removes surrounding whitespace but currently changes valid reference identity. Preserve the reference while normalizing it.",
		EditablePaths: stringSlice(runner["editable_paths"]), TestCommandIDs: stringSlice(runner["test_command_ids"]),
		BuildCommandIDs: stringSlice(runner["build_command_ids"]),
		CPUQuotaMillis:  uint32(cpus), MemoryLimitBytes: uint64(memoryMiB) * 1024 * 1024, PidsLimit: uint32(pids),
	}, nil
}

func (pack PublicPack) Matches(request Request) bool {
	return pack.CanaryManifestSHA256 == request.CanaryManifestSHA256 &&
		pack.RunnerPlanSHA256 == request.RunnerPlanSHA256 &&
		pack.GraderPlanSHA256 == request.GraderPlanSHA256 &&
		pack.ResourceProfileSHA256 == request.ResourceProfileSHA256 &&
		pack.InferencePolicySHA256 == request.InferencePolicySHA256
}

func digestCanonicalObject(value map[string]any) (string, error) {
	body, err := canonicalJSON(value)
	if err != nil {
		return "", err
	}
	digest := sha256.Sum256(body)
	return hex.EncodeToString(digest[:]), nil
}

func canonicalJSON(value any) ([]byte, error) {
	normalized, err := normalizeJSON(value)
	if err != nil {
		return nil, err
	}
	body, err := json.Marshal(normalized)
	if err != nil {
		return nil, err
	}
	return append(body, '\n'), nil
}

func normalizeJSON(value any) (any, error) {
	switch typed := value.(type) {
	case map[string]any:
		keys := make([]string, 0, len(typed))
		for key := range typed {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		normalized := make(map[string]any, len(keys))
		for _, key := range keys {
			child, err := normalizeJSON(typed[key])
			if err != nil {
				return nil, err
			}
			normalized[key] = child
		}
		return normalized, nil
	case []any:
		normalized := make([]any, len(typed))
		for index, item := range typed {
			child, err := normalizeJSON(item)
			if err != nil {
				return nil, err
			}
			normalized[index] = child
		}
		return normalized, nil
	default:
		return typed, nil
	}
}

func schemaOf(value map[string]any) string { return stringOf(value["schema"]) }

func stringOf(value any) string {
	text, _ := value.(string)
	return text
}

func intOf(value any) int {
	switch typed := value.(type) {
	case float64:
		return int(typed)
	case int:
		return typed
	default:
		return 0
	}
}

func boolOf(value any) bool {
	flag, _ := value.(bool)
	return flag
}

func stringSlice(value any) []string {
	items, _ := value.([]any)
	result := make([]string, 0, len(items))
	for _, item := range items {
		text, _ := item.(string)
		if text != "" {
			result = append(result, text)
		}
	}
	return result
}
