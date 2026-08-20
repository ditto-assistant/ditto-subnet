package codingcontract

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"net/url"
	"slices"
	"strings"
	"unicode"
	"unicode/utf8"
)

var requiredTestGroups = []string{
	"adversarial", "fail_to_pass", "hidden", "integrity", "pass_to_pass",
}

func validSHA256(value string) bool {
	if len(value) != sha256.Size*2 {
		return false
	}
	decoded, err := hex.DecodeString(value)
	return err == nil && hex.EncodeToString(decoded) == value
}

func validOCIDigest(value string) bool {
	return strings.HasPrefix(value, "sha256:") && validSHA256(strings.TrimPrefix(value, "sha256:"))
}

func validBlockHash(value string) bool {
	return strings.HasPrefix(value, "0x") && validSHA256(strings.TrimPrefix(value, "0x"))
}

func validIdentifier(value string, max int) bool {
	if value == "" || len(value) > max || !utf8.ValidString(value) {
		return false
	}
	for _, character := range value {
		if unicode.IsSpace(character) || !unicode.IsPrint(character) {
			return false
		}
	}
	return true
}

func validCapabilityURL(value string) bool {
	if value == "" || len(value) > 4096 {
		return false
	}
	parsed, err := url.ParseRequestURI(value)
	return err == nil && (parsed.Scheme == "http" || parsed.Scheme == "https") && parsed.Host != "" &&
		parsed.User == nil && parsed.Fragment == ""
}

func validRelativePath(value string) bool {
	if value == "" || len(value) > 256 || strings.HasPrefix(value, "/") || strings.Contains(value, `\`) {
		return false
	}
	for _, part := range strings.Split(value, "/") {
		if part == "" || part == "." || part == ".." || part == ".git" {
			return false
		}
	}
	for _, character := range value {
		if !unicode.IsPrint(character) {
			return false
		}
	}
	return true
}

func uniqueStrings(values []string) bool {
	seen := make(map[string]struct{}, len(values))
	for _, value := range values {
		if _, duplicate := seen[value]; duplicate {
			return false
		}
		seen[value] = struct{}{}
	}
	return true
}

func (task ManifestTask) Validate() error {
	if !validIdentifier(task.CaseID, 256) || !validIdentifier(task.VariantID, 256) ||
		!validIdentifier(task.ProfileCapabilityID, 256) {
		return errors.New("manifest task identity is invalid")
	}
	for label, digest := range map[string]string{
		"visible_bundle_sha256":   task.VisibleBundleSHA256,
		"base_tree_sha256":        task.BaseTreeSHA256,
		"memory_bundle_sha256":    task.MemoryBundleSHA256,
		"resource_profile_sha256": task.ResourceProfileSHA256,
		"grader_bundle_sha256":    task.GraderBundleSHA256,
		"test_manifest_sha256":    task.TestManifestSHA256,
	} {
		if !validSHA256(digest) {
			return fmt.Errorf("%s is not lowercase SHA-256", label)
		}
	}
	if !validOCIDigest(task.EnvironmentImageDigest) || !validOCIDigest(task.GraderImageDigest) {
		return errors.New("manifest task image digest is invalid")
	}
	if task.EnvironmentPlatform != "linux/amd64" {
		return errors.New("environment_platform must be linux/amd64")
	}
	return nil
}

func (manifest RunManifest) Validate() error {
	if manifest.Schema != "dittobench-coding-run-manifest-v1" ||
		manifest.CodingContractVersion != ContractVersion || manifest.WeightEligible {
		return errors.New("run manifest is not shadow coding contract v1")
	}
	if !validIdentifier(manifest.TicketID, 256) || !validIdentifier(manifest.AgentID, 256) ||
		!validIdentifier(manifest.CorpusReleaseID, 256) || !validIdentifier(manifest.TaskSetID, 256) ||
		!validIdentifier(manifest.SelectionDerivationID, 128) {
		return errors.New("run manifest identity is invalid")
	}
	if !validSHA256(manifest.AgentArtifactSHA256) || !validSHA256(manifest.CatalogMerkleRoot) ||
		!validSHA256(manifest.TaskSetManifestSHA256) || !validBlockHash(manifest.SelectionBlockHash) ||
		manifest.SelectionBlockNumber == 0 {
		return errors.New("run manifest commitment is invalid")
	}
	if len(manifest.Tasks) == 0 || len(manifest.Tasks) > 100 {
		return errors.New("run manifest must contain 1..=100 tasks")
	}
	previous := ""
	for _, task := range manifest.Tasks {
		if err := task.Validate(); err != nil {
			return err
		}
		identity := task.CaseID + "\x00" + task.VariantID
		if previous != "" && identity <= previous {
			return errors.New("manifest tasks must be unique and sorted by case_id, variant_id")
		}
		previous = identity
	}
	return nil
}

func (memory VisibleMemory) Validate() error {
	if !validIdentifier(memory.MemoryID, 256) || !validIdentifier(memory.Scope, 128) ||
		!validIdentifier(memory.Type, 128) || memory.Content == "" || !utf8.ValidString(memory.Content) ||
		len(memory.Content) > 16*1024 ||
		memory.ConfidenceMicros > 1_000_000 {
		return errors.New("visible memory is outside contract bounds")
	}
	for _, value := range []*string{
		memory.RepositoryCapabilityID, memory.FactGroupID, memory.ValidFromEpoch, memory.ValidUntilEpoch,
	} {
		if value != nil && !validIdentifier(*value, 256) {
			return errors.New("visible memory optional identity is invalid")
		}
	}
	if !slices.IsSorted(memory.Supersedes) || !uniqueStrings(memory.Supersedes) ||
		slices.Contains(memory.Supersedes, memory.MemoryID) || len(memory.Supersedes) > 64 {
		return errors.New("visible memory supersedes must be unique and sorted")
	}
	for _, value := range memory.Supersedes {
		if !validIdentifier(value, 256) {
			return errors.New("visible memory supersedes identity is invalid")
		}
	}
	return nil
}

func (request SeedRequest) Validate() error {
	if request.CodingContractVersion != ContractVersion || !validIdentifier(request.TicketID, 256) ||
		!validIdentifier(request.CaseID, 256) || !validIdentifier(request.ProfileCapabilityID, 256) ||
		!validSHA256(request.MemoryBundleSHA256) || len(request.Memories) > 128 {
		return errors.New("coding seed request identity is invalid")
	}
	previous := ""
	for _, memory := range request.Memories {
		if err := memory.Validate(); err != nil {
			return err
		}
		if previous != "" && memory.MemoryID <= previous {
			return errors.New("memories must be unique and sorted by memory_id")
		}
		previous = memory.MemoryID
	}
	projection := struct {
		Memories []VisibleMemory `json:"memories"`
	}{Memories: request.Memories}
	digest, err := digestUnchecked(projection)
	if err != nil {
		return err
	}
	if digest != request.MemoryBundleSHA256 {
		return errors.New("memory_bundle_sha256 does not match canonical memories")
	}
	return nil
}

func (request RunRequest) Validate() error {
	if request.CodingContractVersion != ContractVersion || !validIdentifier(request.TicketID, 256) ||
		!validIdentifier(request.CaseID, 256) || !validIdentifier(request.ProfileCapabilityID, 256) ||
		!validIdentifier(request.RepositoryEpoch, 256) || !validSHA256(request.VisibleBundleSHA256) {
		return errors.New("coding run request identity is invalid")
	}
	if !utf8.ValidString(request.Issue.Title) || !utf8.ValidString(request.Issue.Description) ||
		len(request.Issue.Title) > 1024 || request.Issue.Description == "" ||
		len(request.Issue.Description) > 64*1024 || len(request.Issue.Constraints) > 64 {
		return errors.New("coding issue is outside contract bounds")
	}
	for _, constraint := range request.Issue.Constraints {
		if constraint == "" || !utf8.ValidString(constraint) || len(constraint) > 4096 {
			return errors.New("coding issue constraint is outside contract bounds")
		}
	}
	if len(request.RuntimePolicy.EditablePaths) > 64 || len(request.RuntimePolicy.TestCommandIDs) > 64 ||
		len(request.RuntimePolicy.BuildCommandIDs) > 64 || !uniqueStrings(request.RuntimePolicy.EditablePaths) ||
		!uniqueStrings(request.RuntimePolicy.TestCommandIDs) || !uniqueStrings(request.RuntimePolicy.BuildCommandIDs) {
		return errors.New("runtime policy entries are invalid")
	}
	for _, path := range request.RuntimePolicy.EditablePaths {
		if !validRelativePath(path) {
			return errors.New("runtime policy contains an unsafe path")
		}
	}
	for _, values := range [][]string{request.RuntimePolicy.TestCommandIDs, request.RuntimePolicy.BuildCommandIDs} {
		for _, value := range values {
			if !validIdentifier(value, 80) {
				return errors.New("runtime policy command identity is invalid")
			}
		}
	}
	if !validCapabilityURL(request.WorkspaceCapabilityURL) || !validCapabilityURL(request.InferenceBaseURL) {
		return errors.New("coding run capability URL is invalid")
	}
	budget := request.Budgets
	if budget.ModelInputTokens == 0 || budget.ModelInputTokens > 2_000_000 ||
		budget.ModelOutputTokens == 0 || budget.ModelOutputTokens > 250_000 ||
		budget.WorkspaceToolCalls == 0 || budget.WorkspaceToolCalls > 1_000 ||
		budget.WallTimeSeconds == 0 || budget.WallTimeSeconds > 7_200 {
		return errors.New("coding run budget is outside contract bounds")
	}
	return nil
}

func (evidence ModelEvidence) Validate() error {
	if !validIdentifier(evidence.Model, 128) || !validIdentifier(evidence.Provider, 128) ||
		!validIdentifier(evidence.ProviderRouteProfile, 128) || evidence.ReasoningEffort != "medium" ||
		!validSHA256(evidence.PromptSHA256) || !validSHA256(evidence.ToolSchemaSHA256) ||
		!validSHA256(evidence.ProviderReceiptSetSHA256) || evidence.Requests == 0 || evidence.Requests > 10_000 ||
		evidence.RetryCount > 100 {
		return errors.New("model evidence identity or accounting is invalid")
	}
	if evidence.PromptTokens > ^uint64(0)-evidence.CompletionTokens ||
		evidence.TotalTokens != evidence.PromptTokens+evidence.CompletionTokens {
		return errors.New("model evidence token totals are inconsistent")
	}
	return nil
}

func (evidence AuthoringEvidence) Validate() error {
	if err := evidence.Model.Validate(); err != nil {
		return err
	}
	if !validSHA256(evidence.AuthoringEventRoot) || !validSHA256(evidence.AuthoringTranscriptSHA256) ||
		!validSHA256(evidence.FrozenPatchSHA256) || !validSHA256(evidence.ChangedPathRoot) ||
		!validSHA256(evidence.FinalTreeSHA256) || evidence.ChangedPathCount > 10_000 ||
		evidence.ChangedBytes > 1<<30 {
		return errors.New("authoring evidence is invalid")
	}
	return nil
}

func (evidence GraderEvidence) Validate() error {
	if !validSHA256(evidence.GraderBundleSHA256) || !validOCIDigest(evidence.GraderImageDigest) ||
		!validSHA256(evidence.TestManifestSHA256) || !validSHA256(evidence.GraderIntegrityBeforeSHA256) ||
		!validSHA256(evidence.GraderIntegrityAfterSHA256) || !validIdentifier(evidence.Build.CommandID, 80) {
		return errors.New("grader evidence identity is invalid")
	}
	if len(evidence.TestGroups) != len(requiredTestGroups) {
		return errors.New("grader test groups are incomplete")
	}
	for index, group := range evidence.TestGroups {
		if group.Group != requiredTestGroups[index] || group.Total == 0 || group.Passed > group.Total {
			return errors.New("grader test groups must be complete, sorted, and coherent")
		}
	}
	return nil
}

func (evidence GraderEvidence) resolved() bool {
	if evidence.Build.Required && !evidence.Build.Passed {
		return false
	}
	if evidence.GraderIntegrityBeforeSHA256 != evidence.GraderIntegrityAfterSHA256 {
		return false
	}
	for _, group := range evidence.TestGroups {
		if group.Passed != group.Total {
			return false
		}
	}
	return true
}

func (evidence TaskEvidence) Validate() error {
	if evidence.Schema != "dittobench-coding-task-evidence-v1" ||
		evidence.CodingContractVersion != ContractVersion || evidence.WeightEligible ||
		!validIdentifier(evidence.TicketID, 256) || !validIdentifier(evidence.AgentID, 256) ||
		!validIdentifier(evidence.CorpusReleaseID, 256) || !validIdentifier(evidence.TaskSetID, 256) ||
		!validSHA256(evidence.AgentArtifactSHA256) || !validSHA256(evidence.TaskSetManifestSHA256) {
		return errors.New("task evidence identity is invalid")
	}
	if err := evidence.Task.Validate(); err != nil {
		return err
	}
	if evidence.Authoring != nil {
		if err := evidence.Authoring.Validate(); err != nil {
			return err
		}
	}
	if evidence.Grader != nil {
		if err := evidence.Grader.Validate(); err != nil {
			return err
		}
		if evidence.Grader.GraderBundleSHA256 != evidence.Task.GraderBundleSHA256 ||
			evidence.Grader.GraderImageDigest != evidence.Task.GraderImageDigest ||
			evidence.Grader.TestManifestSHA256 != evidence.Task.TestManifestSHA256 {
			return errors.New("grader evidence does not match manifest task")
		}
	}
	if evidence.TerminalDomain == DomainResolved {
		if evidence.FailureCode != nil || evidence.Authoring == nil || evidence.Grader == nil ||
			!evidence.Authoring.ProtectedPathsIntact || !evidence.Grader.resolved() ||
			evidence.RepairScoreMicros != ResolvedRepairScoreMicros {
			return errors.New("resolved evidence must contain a complete passing repair")
		}
		return nil
	}
	if !validTerminalDomain(evidence.TerminalDomain) || evidence.FailureCode == nil ||
		!validIdentifier(*evidence.FailureCode, 128) || evidence.RepairScoreMicros != 0 {
		return errors.New("non-resolved evidence requires a failure_code and zero score")
	}
	if evidence.TerminalDomain == DomainRepairFailure && evidence.Authoring == nil {
		return errors.New("repair_failure requires authoritative authoring evidence")
	}
	return nil
}

func validTerminalDomain(domain TerminalDomain) bool {
	switch domain {
	case DomainResolved, DomainRepairFailure, DomainValidatorInfrastructure, DomainTaskInvalid, DomainIntegrityIncident:
		return true
	default:
		return false
	}
}

func (evidence RunEvidence) Validate() error {
	if evidence.Schema != "dittobench-coding-run-evidence-v1" ||
		evidence.CodingContractVersion != ContractVersion || evidence.WeightEligible ||
		!validSHA256(evidence.RunManifestSHA256) || !validSHA256(evidence.TaskSetManifestSHA256) ||
		len(evidence.Tasks) == 0 || len(evidence.Tasks) > 100 {
		return errors.New("run evidence identity is invalid")
	}
	counts := map[TerminalDomain]uint32{}
	var scoreSum uint64
	previous := ""
	for _, task := range evidence.Tasks {
		if !validIdentifier(task.CaseID, 256) || !validIdentifier(task.VariantID, 256) ||
			!validSHA256(task.TaskEvidenceSHA256) || !validTerminalDomain(task.TerminalDomain) {
			return errors.New("run task result identity is invalid")
		}
		identity := task.CaseID + "\x00" + task.VariantID
		if previous != "" && identity <= previous {
			return errors.New("run tasks must be unique and sorted by case_id, variant_id")
		}
		previous = identity
		counts[task.TerminalDomain]++
		if task.TerminalDomain == DomainResolved {
			if task.RepairScoreMicros != ResolvedRepairScoreMicros {
				return errors.New("resolved task result must score 1000000")
			}
		} else if task.RepairScoreMicros != 0 {
			return errors.New("non-resolved task result must score zero")
		}
		if task.TerminalDomain != DomainValidatorInfrastructure && task.TerminalDomain != DomainTaskInvalid {
			scoreSum += uint64(task.RepairScoreMicros)
		}
	}
	scoreable := counts[DomainResolved] + counts[DomainRepairFailure] + counts[DomainIntegrityIncident]
	if evidence.ResolvedCount != counts[DomainResolved] ||
		evidence.RepairFailureCount != counts[DomainRepairFailure] ||
		evidence.InfrastructureCount != counts[DomainValidatorInfrastructure] ||
		evidence.InvalidCount != counts[DomainTaskInvalid] ||
		evidence.IntegrityIncidentCount != counts[DomainIntegrityIncident] ||
		evidence.ScoreableTaskCount != scoreable {
		return errors.New("run aggregate counts do not match task evidence")
	}
	wantMean := uint64(0)
	if scoreable != 0 {
		wantMean = scoreSum / uint64(scoreable)
	}
	if uint64(evidence.RepairMeanMicros) != wantMean {
		return errors.New("repair_mean_micros does not match the scoreable task vector")
	}
	return nil
}

// ValidateAgainst binds one task-evidence object to exactly one selected task.
func (evidence TaskEvidence) ValidateAgainst(manifest RunManifest) error {
	if err := manifest.Validate(); err != nil {
		return err
	}
	if err := evidence.Validate(); err != nil {
		return err
	}
	var selected *ManifestTask
	for index := range manifest.Tasks {
		task := &manifest.Tasks[index]
		if task.CaseID == evidence.Task.CaseID && task.VariantID == evidence.Task.VariantID {
			if selected != nil {
				return errors.New("task evidence matches more than one manifest task")
			}
			selected = task
		}
	}
	if selected == nil || *selected != evidence.Task {
		return errors.New("task evidence does not match exactly one manifest task")
	}
	if evidence.TicketID != manifest.TicketID || evidence.AgentID != manifest.AgentID ||
		evidence.AgentArtifactSHA256 != manifest.AgentArtifactSHA256 ||
		evidence.CorpusReleaseID != manifest.CorpusReleaseID || evidence.TaskSetID != manifest.TaskSetID ||
		evidence.TaskSetManifestSHA256 != manifest.TaskSetManifestSHA256 {
		return errors.New("task evidence identity does not match run manifest")
	}
	return nil
}

// ValidateAgainst replays run aggregation against the manifest and task roots.
func (evidence RunEvidence) ValidateAgainst(manifest RunManifest, tasks []TaskEvidence) error {
	if err := manifest.Validate(); err != nil {
		return err
	}
	if err := evidence.Validate(); err != nil {
		return err
	}
	manifestDigest, err := Digest(manifest)
	if err != nil {
		return err
	}
	if evidence.RunManifestSHA256 != manifestDigest ||
		evidence.TaskSetManifestSHA256 != manifest.TaskSetManifestSHA256 {
		return errors.New("run evidence does not bind the canonical manifest")
	}
	if len(evidence.Tasks) != len(manifest.Tasks) || len(tasks) != len(manifest.Tasks) {
		return errors.New("run evidence cardinality does not match manifest")
	}
	byIdentity := make(map[string]TaskEvidence, len(tasks))
	for _, task := range tasks {
		if err := task.ValidateAgainst(manifest); err != nil {
			return err
		}
		identity := task.Task.CaseID + "\x00" + task.Task.VariantID
		if _, duplicate := byIdentity[identity]; duplicate {
			return errors.New("duplicate per-task evidence identity")
		}
		byIdentity[identity] = task
	}
	for index, result := range evidence.Tasks {
		selected := manifest.Tasks[index]
		if result.CaseID != selected.CaseID || result.VariantID != selected.VariantID {
			return errors.New("run evidence task order does not match manifest")
		}
		task, ok := byIdentity[result.CaseID+"\x00"+result.VariantID]
		if !ok {
			return errors.New("run task result has no per-task evidence")
		}
		taskDigest, err := Digest(task)
		if err != nil {
			return err
		}
		if result.TaskEvidenceSHA256 != taskDigest || result.TerminalDomain != task.TerminalDomain ||
			result.RepairScoreMicros != task.RepairScoreMicros {
			return errors.New("run task result does not match per-task evidence")
		}
	}
	return nil
}
