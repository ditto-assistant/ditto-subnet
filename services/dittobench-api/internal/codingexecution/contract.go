package codingexecution

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"path"
	"slices"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codinggrader"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
	"github.com/google/uuid"
)

var ErrInvalid = errors.New("coding execution plan is invalid")

var evidenceGroups = []string{"adversarial", "fail_to_pass", "hidden", "integrity", "pass_to_pass"}
var executionOrder = []string{"fail_to_pass", "pass_to_pass", "hidden", "adversarial", "integrity"}

func ParseRunnerPlan(body []byte) (RunnerPlan, error) {
	return parseDocument[RunnerPlan](body, []string{
		"schema", "coding_contract_version", "case_id", "visible_bundle_sha256", "base_tree_sha256",
		"editable_paths", "creatable_paths", "deletable_paths", "test_commands", "build_commands", "limits",
	}, validateRunnerPlan)
}

func ParseGraderPlan(body []byte) (GraderPlan, error) {
	return parseDocument[GraderPlan](body, []string{
		"schema", "coding_contract_version", "case_id", "variant_id", "visible_bundle_sha256",
		"base_tree_sha256", "grader_contract_sha256", "grader_bundle_sha256", "grader_image_digest",
		"grader_platform", "test_manifest_sha256", "resource_profile_sha256",
		"execution_timeout_milliseconds", "build_required", "build_command", "test_groups", "execution_order",
	}, validateGraderPlan)
}

func ParseResourceProfile(body []byte) (ResourceProfile, error) {
	return parseDocument[ResourceProfile](body, []string{
		"schema", "candidate_limits", "protected_limits", "max_combined_disk_bytes",
		"memory_limit_bytes", "scratch_limit_bytes", "pids_limit", "cpu_quota_millis",
	}, validateResourceProfile)
}

func RunnerPlanSHA256(plan RunnerPlan) (string, error) {
	plan = cloneRunnerPlan(plan)
	if err := validateRunnerPlan(plan); err != nil {
		return "", err
	}
	return digest(plan)
}

func GraderPlanSHA256(plan GraderPlan) (string, error) {
	plan = cloneGraderPlan(plan)
	if err := validateGraderPlan(plan); err != nil {
		return "", err
	}
	return codinggrader.GraderPlanSHA256(plan.manifest(ResourceProfile{}, time.Time{}))
}

func ResourceProfileSHA256(profile ResourceProfile) (string, error) {
	if err := validateResourceProfile(profile); err != nil {
		return "", err
	}
	return codinggrader.ResourceProfileSHA256(profile.policy())
}

// ValidateBundle proves the authoring runner projection, model-visible policy,
// protected grader plan, and resource profile share one task identity. The
// caller still delivers the runner and grader projections in separate phases.
func ValidateBundle(bundle Bundle) error {
	bundle.Runner = cloneRunnerPlan(bundle.Runner)
	bundle.Grader = cloneGraderPlan(bundle.Grader)
	bundle.RuntimePolicy = cloneRuntimePolicy(bundle.RuntimePolicy)
	if err := validateRunnerPlan(bundle.Runner); err != nil {
		return err
	}
	if err := bundle.RuntimePolicy.Validate(); err != nil {
		return fmt.Errorf("%w: runtime policy", ErrInvalid)
	}
	if err := validateGraderPlan(bundle.Grader); err != nil {
		return err
	}
	if err := validateResourceProfile(bundle.Resource); err != nil {
		return err
	}
	resourceSHA, err := ResourceProfileSHA256(bundle.Resource)
	if err != nil {
		return err
	}
	paths := append([]string(nil), bundle.Runner.EditablePaths...)
	paths = append(paths, bundle.Runner.CreatablePaths...)
	paths = append(paths, bundle.Runner.DeletablePaths...)
	slices.Sort(paths)
	if bundle.Runner.CaseID != bundle.Grader.CaseID ||
		bundle.Runner.VisibleBundleSHA256 != bundle.Grader.VisibleBundleSHA256 ||
		bundle.Runner.BaseTreeSHA256 != bundle.Grader.BaseTreeSHA256 ||
		bundle.Runner.Limits != bundle.Resource.CandidateLimits ||
		!slices.Equal(paths, bundle.RuntimePolicy.EditablePaths) ||
		!slices.Equal(commandIDs(bundle.Runner.TestCommands), bundle.RuntimePolicy.TestCommandIDs) ||
		!slices.Equal(commandIDs(bundle.Runner.BuildCommands), bundle.RuntimePolicy.BuildCommandIDs) ||
		bundle.Grader.ResourceProfileSHA256 != resourceSHA ||
		bundle.Grader.GraderContractSHA256 != codinggrader.GraderContractSHA256() {
		return fmt.Errorf("%w: phase authority disagrees", ErrInvalid)
	}
	return nil
}

func (plan RunnerPlan) Manifest(
	binding RunnerBinding,
	now time.Time,
) (codingrunner.Manifest, error) {
	plan = cloneRunnerPlan(plan)
	if !canonicalUUID(binding.TicketID) || !validIdentifier(binding.ProfileCapabilityID, 256) {
		return codingrunner.Manifest{}, fmt.Errorf("%w: runner binding", ErrInvalid)
	}
	manifest := codingrunner.Manifest{
		CodingContractVersion: plan.CodingContractVersion,
		TicketID:              binding.TicketID, CaseID: plan.CaseID,
		ProfileCapabilityID: binding.ProfileCapabilityID,
		VisibleBundleSHA256: plan.VisibleBundleSHA256, BaseTreeSHA256: plan.BaseTreeSHA256,
		Deadline: binding.Deadline.UTC(), EditablePaths: cloneStrings(plan.EditablePaths),
		CreatablePaths: cloneStrings(plan.CreatablePaths),
		DeletablePaths: cloneStrings(plan.DeletablePaths),
		TestCommands:   convertCommands(plan.TestCommands), BuildCommands: convertCommands(plan.BuildCommands),
		Limits: plan.Limits.runner(),
	}
	if err := validateRunnerPlan(plan); err != nil || manifest.Validate(now.UTC()) != nil {
		return codingrunner.Manifest{}, fmt.Errorf("%w: runner manifest binding", ErrInvalid)
	}
	return manifest, nil
}

func (plan GraderPlan) Manifest(
	profile ResourceProfile,
	deadline time.Time,
	now time.Time,
) (codinggrader.Manifest, error) {
	plan = cloneGraderPlan(plan)
	manifest := plan.manifest(profile, deadline.UTC())
	planSHA, digestErr := GraderPlanSHA256(plan)
	manifest.GraderPlanSHA256 = planSHA
	resourceSHA, resourceErr := ResourceProfileSHA256(profile)
	if validateGraderPlan(plan) != nil || digestErr != nil || resourceErr != nil ||
		plan.GraderContractSHA256 != codinggrader.GraderContractSHA256() ||
		plan.ResourceProfileSHA256 != resourceSHA || manifest.Validate(now.UTC()) != nil {
		return codinggrader.Manifest{}, fmt.Errorf("%w: grader manifest binding", ErrInvalid)
	}
	return manifest, nil
}

func (plan GraderPlan) manifest(profile ResourceProfile, deadline time.Time) codinggrader.Manifest {
	groups := make([]codinggrader.TestGroupSpec, len(plan.TestGroups))
	for index, group := range plan.TestGroups {
		groups[index] = codinggrader.TestGroupSpec{
			Group: group.Group, Command: group.Command.runner(), ExpectedTotal: group.ExpectedTotal,
		}
	}
	return codinggrader.Manifest{
		CodingContractVersion: plan.CodingContractVersion, CaseID: plan.CaseID, VariantID: plan.VariantID,
		VisibleBundleSHA256: plan.VisibleBundleSHA256, BaseTreeSHA256: plan.BaseTreeSHA256,
		GraderContractSHA256: plan.GraderContractSHA256, GraderBundleSHA256: plan.GraderBundleSHA256,
		GraderImageDigest: plan.GraderImageDigest, GraderPlatform: plan.GraderPlatform,
		TestManifestSHA256: plan.TestManifestSHA256, ResourceProfileSHA256: plan.ResourceProfileSHA256,
		ExecutionTimeout: time.Duration(plan.ExecutionTimeoutMilliseconds) * time.Millisecond,
		ResourcePolicy:   profile.policy(),
		Build:            codinggrader.BuildSpec{Required: plan.BuildRequired, Command: plan.BuildCommand.runner()},
		TestGroups:       groups, Deadline: deadline,
	}
}

func validateRunnerPlan(plan RunnerPlan) error {
	if plan.Schema != RunnerPlanSchema || plan.CodingContractVersion != codingcontract.ContractVersion ||
		!validIdentifier(plan.CaseID, 256) || !lowerSHA(plan.VisibleBundleSHA256) || !lowerSHA(plan.BaseTreeSHA256) {
		return fmt.Errorf("%w: runner identity", ErrInvalid)
	}
	if plan.EditablePaths == nil || plan.CreatablePaths == nil || plan.DeletablePaths == nil ||
		plan.TestCommands == nil || plan.BuildCommands == nil || len(plan.EditablePaths) > 64 ||
		len(plan.CreatablePaths) > 64 || len(plan.DeletablePaths) > 64 ||
		len(plan.TestCommands) > 64 || len(plan.BuildCommands) > 64 {
		return fmt.Errorf("%w: runner collection shape", ErrInvalid)
	}
	if err := plan.Limits.runner().Validate(); err != nil {
		return fmt.Errorf("%w: runner limits: %v", ErrInvalid, err)
	}
	seenPaths := make(map[string]struct{})
	for _, values := range [][]string{plan.EditablePaths, plan.CreatablePaths, plan.DeletablePaths} {
		if !slices.IsSorted(values) {
			return fmt.Errorf("%w: runner paths are not sorted", ErrInvalid)
		}
		for _, value := range values {
			if !safeRelativePath(value) {
				return fmt.Errorf("%w: runner path is unsafe", ErrInvalid)
			}
			if _, exists := seenPaths[value]; exists {
				return fmt.Errorf("%w: runner paths overlap", ErrInvalid)
			}
			seenPaths[value] = struct{}{}
		}
	}
	if len(seenPaths) > plan.Limits.MaxEntries {
		return fmt.Errorf("%w: runner path count", ErrInvalid)
	}
	seenCommands := make(map[string]struct{})
	for _, values := range [][]Command{plan.TestCommands, plan.BuildCommands} {
		previous := ""
		for _, command := range values {
			if err := command.runner().Validate(); err != nil {
				return fmt.Errorf("%w: runner command: %v", ErrInvalid, err)
			}
			if previous != "" && command.ID <= previous {
				return fmt.Errorf("%w: runner commands are not sorted", ErrInvalid)
			}
			if _, exists := seenCommands[command.ID]; exists {
				return fmt.Errorf("%w: runner command IDs overlap", ErrInvalid)
			}
			seenCommands[command.ID] = struct{}{}
			previous = command.ID
		}
	}
	if len(seenCommands) > int(plan.Limits.MaxToolCalls) {
		return fmt.Errorf("%w: runner command count", ErrInvalid)
	}
	return nil
}

func validateGraderPlan(plan GraderPlan) error {
	if plan.Schema != GraderPlanSchema || plan.CodingContractVersion != codingcontract.ContractVersion ||
		!validIdentifier(plan.CaseID, 256) || !validIdentifier(plan.VariantID, 256) ||
		!lowerSHA(plan.VisibleBundleSHA256) || !lowerSHA(plan.BaseTreeSHA256) ||
		!lowerSHA(plan.GraderContractSHA256) || !lowerSHA(plan.GraderBundleSHA256) ||
		!ociDigest(plan.GraderImageDigest) || plan.GraderPlatform != "linux/amd64" ||
		!lowerSHA(plan.TestManifestSHA256) || !lowerSHA(plan.ResourceProfileSHA256) ||
		plan.ExecutionTimeoutMilliseconds <= 0 || plan.ExecutionTimeoutMilliseconds > 3_600_000 ||
		plan.BuildCommand.runner().Validate() != nil || len(plan.TestGroups) != len(evidenceGroups) ||
		!slices.Equal(plan.ExecutionOrder, executionOrder) {
		return ErrInvalid
	}
	seen := map[string]struct{}{plan.BuildCommand.ID: {}}
	for index, group := range plan.TestGroups {
		if group.Group != evidenceGroups[index] || group.ExpectedTotal == 0 || group.Command.runner().Validate() != nil {
			return ErrInvalid
		}
		if _, exists := seen[group.Command.ID]; exists {
			return ErrInvalid
		}
		seen[group.Command.ID] = struct{}{}
	}
	return nil
}

func validateResourceProfile(profile ResourceProfile) error {
	if profile.Schema != ResourceSchema || profile.policy().Validate() != nil {
		return ErrInvalid
	}
	return nil
}

func parseDocument[T any](body []byte, required []string, validate func(T) error) (T, error) {
	var zero T
	if len(body) == 0 || len(body) > codingcontract.MaxCanonicalJSONBytes ||
		codingcontract.ValidateJSONDocument(body, codingcontract.MaxCanonicalJSONBytes) != nil {
		return zero, ErrInvalid
	}
	var shape map[string]json.RawMessage
	if err := json.Unmarshal(body, &shape); err != nil {
		return zero, ErrInvalid
	}
	for _, field := range required {
		if _, exists := shape[field]; !exists {
			return zero, ErrInvalid
		}
	}
	var value T
	if err := json.Unmarshal(body, &value); err != nil || validate(value) != nil {
		return zero, ErrInvalid
	}
	return value, nil
}

func digest(value any) (string, error) {
	body, err := canonicalJSON(value)
	if err != nil {
		return "", err
	}
	digest := sha256.Sum256(body)
	return hex.EncodeToString(digest[:]), nil
}

func canonicalJSON(value any) ([]byte, error) {
	raw, err := json.Marshal(value)
	if err != nil {
		return nil, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var projected any
	if err := decoder.Decode(&projected); err != nil {
		return nil, err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return nil, ErrInvalid
	}
	var output bytes.Buffer
	encoder := json.NewEncoder(&output)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(projected); err != nil {
		return nil, err
	}
	if output.Len() > codingcontract.MaxCanonicalJSONBytes {
		return nil, ErrInvalid
	}
	return output.Bytes(), nil
}

func convertCommands(values []Command) []codingrunner.CommandSpec {
	result := make([]codingrunner.CommandSpec, len(values))
	for index, value := range values {
		result[index] = value.runner()
	}
	return result
}

func commandIDs(values []Command) []string {
	result := make([]string, len(values))
	for index, value := range values {
		result[index] = value.ID
	}
	return result
}

func cloneRuntimePolicy(policy codingcontract.RuntimePolicy) codingcontract.RuntimePolicy {
	policy.EditablePaths = cloneStrings(policy.EditablePaths)
	policy.TestCommandIDs = cloneStrings(policy.TestCommandIDs)
	policy.BuildCommandIDs = cloneStrings(policy.BuildCommandIDs)
	return policy
}

func cloneRunnerPlan(plan RunnerPlan) RunnerPlan {
	plan.EditablePaths = cloneStrings(plan.EditablePaths)
	plan.CreatablePaths = cloneStrings(plan.CreatablePaths)
	plan.DeletablePaths = cloneStrings(plan.DeletablePaths)
	plan.TestCommands = cloneCommands(plan.TestCommands)
	plan.BuildCommands = cloneCommands(plan.BuildCommands)
	return plan
}

func cloneGraderPlan(plan GraderPlan) GraderPlan {
	plan.BuildCommand.Argv = append([]string(nil), plan.BuildCommand.Argv...)
	plan.TestGroups = append([]TestGroup(nil), plan.TestGroups...)
	for index := range plan.TestGroups {
		plan.TestGroups[index].Command.Argv = append([]string(nil), plan.TestGroups[index].Command.Argv...)
	}
	plan.ExecutionOrder = append([]string(nil), plan.ExecutionOrder...)
	return plan
}

func cloneCommands(values []Command) []Command {
	if values == nil {
		return nil
	}
	result := make([]Command, len(values))
	copy(result, values)
	for index := range result {
		result[index].Argv = cloneStrings(result[index].Argv)
	}
	return result
}

func cloneStrings(values []string) []string {
	if values == nil {
		return nil
	}
	result := make([]string, len(values))
	copy(result, values)
	return result
}

func safeRelativePath(value string) bool {
	if value == "" || len(value) > 256 || !utf8.ValidString(value) || strings.Contains(value, "\\") || path.IsAbs(value) {
		return false
	}
	for _, part := range strings.Split(value, "/") {
		if part == "" || part == "." || part == ".." || part == ".git" {
			return false
		}
	}
	for _, character := range value {
		if unicode.IsControl(character) {
			return false
		}
	}
	return true
}

func validIdentifier(value string, maximum int) bool {
	if value == "" || len(value) > maximum || !utf8.ValidString(value) {
		return false
	}
	for _, character := range value {
		if unicode.IsSpace(character) || unicode.IsControl(character) {
			return false
		}
	}
	return true
}

func lowerSHA(value string) bool {
	if len(value) != 64 || value != strings.ToLower(value) {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}

func ociDigest(value string) bool {
	return strings.HasPrefix(value, "sha256:") && lowerSHA(strings.TrimPrefix(value, "sha256:"))
}

func canonicalUUID(value string) bool {
	parsed, err := uuid.Parse(value)
	return err == nil && parsed != uuid.Nil && parsed.String() == value
}
