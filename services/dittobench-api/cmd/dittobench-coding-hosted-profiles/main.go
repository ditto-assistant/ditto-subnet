// Operator helper that derives the hosted-v2 execution and grading profiles for
// one private catalog index and proves them against the worker's launch-time
// checks. Run only on an owner-controlled machine with the verified private
// payload; outputs are review inputs, never an approval or an assignment.
package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"reflect"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codinggrader"
	"github.com/ditto-assistant/dittobench-api/internal/codinghostedinput"
	"github.com/ditto-assistant/dittobench-api/internal/codinghostedworker"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
)

var errRejected = errors.New("hosted profile request rejected")

type command struct {
	ID                  string   `json:"id"`
	Argv                []string `json:"argv"`
	TimeoutMilliseconds int64    `json:"timeout_milliseconds"`
}

type testGroup struct {
	Group         string  `json:"group"`
	Command       command `json:"command"`
	ExpectedTotal uint32  `json:"expected_total"`
}

type request struct {
	Schema               string                 `json:"schema"`
	CatalogIndex         *int                   `json:"catalog_index"`
	ImageDigest          string                 `json:"image_digest"`
	CandidateLimits      codingrunner.Limits    `json:"candidate_limits"`
	ProtectedLimits      codingrunner.Limits    `json:"protected_limits"`
	MaxCombinedDiskBytes int64                  `json:"max_combined_disk_bytes"`
	Budgets              codingcontract.Budgets `json:"budgets"`
	Build                *struct {
		Required bool     `json:"required"`
		Command  *command `json:"command"`
	} `json:"build"`
	TestGroups                   []testGroup `json:"test_groups"`
	ExecutionTimeoutMilliseconds int64       `json:"execution_timeout_milliseconds"`
	ShadowOnly                   *bool       `json:"shadow_only"`
	WeightEligible               *bool       `json:"weight_eligible"`
}

type payloadAuthority struct {
	Schema                string `json:"schema"`
	CodingContractVersion int    `json:"coding_contract_version"`
	WeightEligible        *bool  `json:"weight_eligible"`
	PayloadSHA256         string `json:"payload_sha256"`
	TaskAssets            []struct {
		CatalogIndex         int               `json:"catalog_index"`
		TaskVersionID        string            `json:"task_version_id"`
		TaskCommitmentSHA256 string            `json:"task_commitment_sha256"`
		Artifacts            map[string]string `json:"artifacts"`
	} `json:"task_assets"`
	Objects []struct {
		SHA256    string `json:"sha256"`
		SizeBytes int64  `json:"size_bytes"`
	} `json:"objects"`
}

type resourceProfile struct {
	Schema  string `json:"schema"`
	CPUs    uint32 `json:"cpus_milli"`
	Memory  uint64 `json:"memory_mib"`
	Disk    uint64 `json:"disk_mib"`
	Pids    uint32 `json:"pids"`
	Wall    uint64 `json:"wall_time_seconds"`
	Network string `json:"network"`
}

type runtimePolicy struct {
	Builds []struct {
		ID                  string            `json:"id"`
		Argv                []string          `json:"argv"`
		Environment         map[string]string `json:"environment"`
		TimeoutMilliseconds int64             `json:"timeout_milliseconds"`
	} `json:"build_commands"`
}

func main() {
	requestPath := flag.String("request", "", "reviewed profile request JSON")
	payloadPath := flag.String("payload-authority", "", "verified private payload-authority.json")
	objectsPath := flag.String("objects", "", "directory of verified <sha256>.bin payload objects")
	outputPath := flag.String("output", "", "new private output directory")
	flag.Parse()
	receipt, err := run(context.Background(), *requestPath, *payloadPath, *objectsPath, *outputPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, "hosted profile request rejected")
		os.Exit(70)
	}
	os.Stdout.Write(receipt)
}

func run(ctx context.Context, requestPath, payloadPath, objectsPath, outputPath string) ([]byte, error) {
	for _, value := range []string{requestPath, payloadPath, objectsPath, outputPath} {
		if value == "" || !filepath.IsAbs(value) || filepath.Clean(value) != value {
			return nil, errRejected
		}
	}
	if _, err := os.Lstat(outputPath); !errors.Is(err, os.ErrNotExist) {
		return nil, errRejected
	}
	requestBody, err := readBounded(requestPath, 1<<20)
	if err != nil {
		return nil, errRejected
	}
	var req request
	if strictDecode(requestBody, &req) != nil || req.Schema != "dittobench-coding-hosted-profile-request-v1" || req.CatalogIndex == nil || req.ShadowOnly == nil || !*req.ShadowOnly || req.WeightEligible == nil || *req.WeightEligible || req.Build == nil || req.Build.Command == nil {
		return nil, errRejected
	}
	payloadBody, err := readBounded(payloadPath, 64<<20)
	if err != nil {
		return nil, errRejected
	}
	var payload payloadAuthority
	if json.Unmarshal(payloadBody, &payload) != nil || payload.Schema != "dittobench-coding-private-payload-v2" || payload.CodingContractVersion != 2 || payload.WeightEligible == nil || *payload.WeightEligible {
		return nil, errRejected
	}
	index := -1
	for i, task := range payload.TaskAssets {
		if task.CatalogIndex == *req.CatalogIndex {
			if index >= 0 {
				return nil, errRejected
			}
			index = i
		}
	}
	if index < 0 {
		return nil, errRejected
	}
	task := payload.TaskAssets[index]
	sizes := map[string]int64{}
	for _, object := range payload.Objects {
		sizes[object.SHA256] = object.SizeBytes
	}
	objects := map[string][]byte{}
	for _, role := range []string{"catalog_record", "issue", "visible_bundle", "memory_bundle", "runtime_policy", "resource_profile", "grader_bundle"} {
		digest := task.Artifacts[role]
		size, listed := sizes[digest]
		if !listed || size <= 0 {
			return nil, errRejected
		}
		body, err := readBounded(filepath.Join(objectsPath, digest+".bin"), 128<<20)
		if err != nil || int64(len(body)) != size || sha(body) != digest {
			return nil, errRejected
		}
		objects[role] = body
	}
	var catalog codingcontract.PrivateCatalogTaskV2
	if json.Unmarshal(objects["catalog_record"], &catalog) != nil || catalog.TaskCommitmentSHA256 != task.TaskCommitmentSHA256 || int(catalog.CatalogIndex) != task.CatalogIndex || catalog.TaskVersionID != task.TaskVersionID {
		return nil, errRejected
	}
	var resource resourceProfile
	var runtime runtimePolicy
	if json.Unmarshal(objects["resource_profile"], &resource) != nil || json.Unmarshal(objects["runtime_policy"], &runtime) != nil || resource.Schema != "dittobench-coding-private-resource-profile-v2" || resource.Memory > 1<<20 || resource.Disk > 1<<20 {
		return nil, errRejected
	}
	policy := codinggrader.ResourcePolicy{
		CandidateLimits: req.CandidateLimits, ProtectedLimits: req.ProtectedLimits,
		MaxCombinedDiskBytes: req.MaxCombinedDiskBytes, MemoryLimitBytes: resource.Memory << 20,
		ScratchLimitBytes: resource.Disk << 20, PidsLimit: resource.Pids, CPUQuotaMillis: resource.CPUs,
	}
	profile := codinghostedinput.Profile{Schema: "dittobench-coding-hosted-authoring-profile-v2", ImageDigest: req.ImageDigest, ResourcePolicy: policy, Budgets: req.Budgets}
	executionSHA, err := codinghostedinput.ProfileDigest(profile)
	if err != nil || profile.Budgets.Validate() != nil || profile.Budgets.WallTimeSeconds > resource.Wall {
		return nil, errRejected
	}
	executionBody, err := executionBytes(profile)
	if err != nil || sha(executionBody) != executionSHA {
		return nil, errRejected
	}
	build := codinggrader.BuildSpec{Required: req.Build.Required, Command: spec(*req.Build.Command)}
	switch len(runtime.Builds) {
	case 0:
		if build.Required {
			return nil, errRejected
		}
	case 1:
		declared := runtime.Builds[0]
		if !build.Required || len(declared.Environment) != 0 || declared.ID != build.Command.ID || !reflect.DeepEqual(declared.Argv, build.Command.Argv) || declared.TimeoutMilliseconds != req.Build.Command.TimeoutMilliseconds {
			return nil, errRejected
		}
	default:
		return nil, errRejected
	}
	groups := make([]codinggrader.TestGroupSpec, 0, len(req.TestGroups))
	for _, group := range req.TestGroups {
		groups = append(groups, codinggrader.TestGroupSpec{Group: group.Group, Command: spec(group.Command), ExpectedTotal: group.ExpectedTotal})
	}
	grading := codinghostedworker.GradingProfile{
		Schema: "dittobench-coding-hosted-grading-profile-v2", ImageDigest: req.ImageDigest,
		GraderContractSHA256: codinggrader.HostedGraderContractSHA256(),
		GraderBundleSHA256:   task.Artifacts["grader_bundle"],
		ResourcePolicy:       policy, Build: build, TestGroups: groups,
		ExecutionTimeout: time.Duration(req.ExecutionTimeoutMilliseconds) * time.Millisecond,
	}
	gradingBody, err := codinghostedworker.GradingProfileBytes(grading)
	if err != nil {
		return nil, errRejected
	}
	taskObjects := map[string][]byte{}
	for _, role := range []string{"catalog_record", "issue", "visible_bundle", "memory_bundle", "runtime_policy", "resource_profile"} {
		taskObjects[role] = objects[role]
	}
	if codinghostedinput.CheckTaskProfile(ctx, codinghostedinput.TaskBinding{CatalogIndex: task.CatalogIndex, TaskCommitmentSHA256: task.TaskCommitmentSHA256, PrivateReleaseSHA256: catalog.PrivateReleaseSHA256, CorpusReleaseID: catalog.CorpusReleaseID, MaxPatchBytes: policy.CandidateLimits.MaxPatchBytes}, profile, taskObjects) != nil {
		return nil, errRejected
	}
	gradingObjects := map[string][]byte{}
	for _, role := range []string{"catalog_record", "visible_bundle", "grader_bundle", "runtime_policy", "resource_profile"} {
		gradingObjects[role] = objects[role]
	}
	if codinghostedworker.CheckGradingProfile(ctx, codinghostedworker.GradingBinding{CatalogIndex: task.CatalogIndex, TaskCommitmentSHA256: task.TaskCommitmentSHA256, PrivateReleaseSHA256: catalog.PrivateReleaseSHA256}, gradingBody, gradingObjects) != nil {
		return nil, errRejected
	}
	receipt, err := canonical(map[string]any{
		"schema": "dittobench-coding-hosted-profile-receipt-v1", "catalog_index": task.CatalogIndex,
		"task_version_id": task.TaskVersionID, "task_commitment_sha256": task.TaskCommitmentSHA256,
		"corpus_release_id": catalog.CorpusReleaseID, "private_release_sha256": catalog.PrivateReleaseSHA256,
		"payload_sha256": payload.PayloadSHA256, "payload_authority_file_sha256": sha(payloadBody),
		"request_sha256": sha(requestBody), "image_digest": req.ImageDigest,
		"execution_profile_sha256": executionSHA, "grading_profile_sha256": sha(gradingBody),
		"max_patch_bytes": policy.CandidateLimits.MaxPatchBytes, "grader_bundle_sha256": grading.GraderBundleSHA256,
		"grader_contract_sha256": grading.GraderContractSHA256,
		"launch_checks_passed":   true, "approved": false, "shadow_only": true, "weight_eligible": false,
	})
	if err != nil {
		return nil, errRejected
	}
	if os.Mkdir(outputPath, 0o700) != nil {
		return nil, errRejected
	}
	for name, body := range map[string][]byte{"execution-profile.json": executionBody, "grading-profile.json": gradingBody, "receipt.json": receipt} {
		if writeExclusive(filepath.Join(outputPath, name), body) != nil {
			return nil, errRejected
		}
	}
	return receipt, nil
}

func spec(value command) codingrunner.CommandSpec {
	return codingrunner.CommandSpec{ID: value.ID, Argv: value.Argv, Timeout: time.Duration(value.TimeoutMilliseconds) * time.Millisecond}
}

// executionBytes mirrors ProfileDigest's projection; callers compare its hash
// with ProfileDigest so any drift fails closed.
func executionBytes(profile codinghostedinput.Profile) ([]byte, error) {
	body, err := canonical(profile)
	if err != nil || codingcontract.RequireExactCanonicalJSON(body) != nil {
		return nil, errRejected
	}
	return body, nil
}

func canonical(value any) ([]byte, error) {
	raw, err := json.Marshal(value)
	if err != nil {
		return nil, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var object any
	if err := decoder.Decode(&object); err != nil {
		return nil, err
	}
	var out bytes.Buffer
	encoder := json.NewEncoder(&out)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(object); err != nil {
		return nil, err
	}
	return out.Bytes(), nil
}

func strictDecode(body []byte, target any) error {
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		return err
	}
	if _, err := decoder.Token(); err != io.EOF {
		return errRejected
	}
	return nil
}

func readBounded(path string, limit int64) ([]byte, error) {
	info, err := os.Lstat(path)
	if err != nil || !info.Mode().IsRegular() || info.Size() > limit {
		return nil, errRejected
	}
	return os.ReadFile(path)
}

func writeExclusive(path string, body []byte) error {
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		return err
	}
	if _, err := file.Write(body); err != nil {
		file.Close()
		return err
	}
	if err := file.Sync(); err != nil {
		file.Close()
		return err
	}
	return file.Close()
}

func sha(body []byte) string { return fmt.Sprintf("%x", sha256.Sum256(body)) }
