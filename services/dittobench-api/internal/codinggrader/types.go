package codinggrader

import (
	"context"
	"encoding/hex"
	"errors"
	"slices"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
)

var evidenceGroups = []string{"adversarial", "fail_to_pass", "hidden", "integrity", "pass_to_pass"}

var executionOrder = []string{"fail_to_pass", "pass_to_pass", "hidden", "adversarial", "integrity"}

const initialReceiptRoot = "0000000000000000000000000000000000000000000000000000000000000000"

// BuildSpec is the one manifest-owned build command.
type BuildSpec struct {
	Required bool
	Command  codingrunner.CommandSpec
}

// TestGroupSpec binds one required group to a fixed command and exact expected
// test count.
type TestGroupSpec struct {
	Group         string
	Command       codingrunner.CommandSpec
	ExpectedTotal uint32
}

// Manifest is the complete trusted input for one pristine grade.
type Manifest struct {
	CodingContractVersion int
	CaseID                string
	VariantID             string
	VisibleBundleSHA256   string
	BaseTreeSHA256        string
	GraderContractSHA256  string
	GraderBundleSHA256    string
	GraderImageDigest     string
	GraderPlatform        string
	TestManifestSHA256    string
	GraderPlanSHA256      string
	ResourceProfileSHA256 string
	Deadline              time.Time
	ExecutionTimeout      time.Duration
	ResourcePolicy        ResourcePolicy
	Build                 BuildSpec
	TestGroups            []TestGroupSpec
}

// ResourcePolicy is the complete signed candidate/protected/sandbox envelope.
type ResourcePolicy struct {
	CandidateLimits      codingrunner.Limits
	ProtectedLimits      codingrunner.Limits
	MaxCombinedDiskBytes int64
	MemoryLimitBytes     uint64
	ScratchLimitBytes    uint64
	PidsLimit            uint32
	CPUQuotaMillis       uint32
}

func (manifest Manifest) validate(now time.Time) error {
	return manifest.validateProfile(now, false)
}

func (manifest Manifest) validateProfile(now time.Time, hosted bool) error {
	version, groupsRequired, contractSHA, lifetime := codingrunner.ContractVersion, evidenceGroups, GraderContractSHA256(), 2*time.Hour
	resourceDigestFunc, planDigestFunc := ResourceProfileSHA256, GraderPlanSHA256
	if hosted {
		version, groupsRequired, contractSHA, lifetime = codingrunner.HostedContractVersion, hostedEvidenceGroups, HostedGraderContractSHA256(), time.Hour
		resourceDigestFunc = HostedResourceProfileSHA256
		planDigestFunc = func(m Manifest) (string, error) { return HostedGraderPlanSHA256(HostedManifest(m)) }
	}
	if manifest.CodingContractVersion != version || !validIdentifier(manifest.CaseID, 256) ||
		!validIdentifier(manifest.VariantID, 256) || !lowerSHA256(manifest.VisibleBundleSHA256) ||
		!lowerSHA256(manifest.BaseTreeSHA256) ||
		!lowerSHA256(manifest.GraderContractSHA256) || !lowerSHA256(manifest.GraderBundleSHA256) ||
		!ociDigest(manifest.GraderImageDigest) || manifest.GraderPlatform != "linux/amd64" ||
		!lowerSHA256(manifest.TestManifestSHA256) ||
		!lowerSHA256(manifest.GraderPlanSHA256) || !lowerSHA256(manifest.ResourceProfileSHA256) {
		return errors.New("coding grader manifest identity is invalid")
	}
	if manifest.Deadline.IsZero() || !manifest.Deadline.After(now) || manifest.Deadline.After(now.Add(lifetime)) {
		return errors.New("coding grader deadline is outside its bounded lifetime")
	}
	if manifest.ExecutionTimeout <= 0 || manifest.ExecutionTimeout > time.Hour ||
		manifest.ExecutionTimeout%time.Millisecond != 0 {
		return errors.New("coding grader execution timeout is outside contract bounds")
	}
	if manifest.GraderContractSHA256 != contractSHA {
		return errors.New("coding grader contract digest does not match the compiled contract")
	}
	if err := manifest.ResourcePolicy.validate(); err != nil {
		return err
	}
	resourceDigest, err := resourceDigestFunc(manifest.ResourcePolicy)
	if err != nil || resourceDigest != manifest.ResourceProfileSHA256 {
		return errors.New("coding grader resource profile digest mismatch")
	}
	if err := manifest.Build.Command.Validate(); err != nil {
		return err
	}
	if len(manifest.TestGroups) != len(groupsRequired) {
		return errors.New("coding grader test groups are incomplete")
	}
	commandIDs := map[string]struct{}{manifest.Build.Command.ID: {}}
	groups := make([]string, len(manifest.TestGroups))
	for index, group := range manifest.TestGroups {
		groups[index] = group.Group
		if group.Group != groupsRequired[index] || group.ExpectedTotal == 0 {
			return errors.New("coding grader test groups must be complete and sorted")
		}
		if err := group.Command.Validate(); err != nil {
			return err
		}
		if hosted && (group.ExpectedTotal > 1_000_000 || group.Command.Argv[0] != "dittobench-test-driver") {
			return errors.New("hosted grader requires a trusted bounded test driver")
		}
		if _, duplicate := commandIDs[group.Command.ID]; duplicate {
			return errors.New("coding grader command IDs must be unique")
		}
		commandIDs[group.Command.ID] = struct{}{}
	}
	if !slices.Equal(groups, groupsRequired) {
		return errors.New("coding grader group order is invalid")
	}
	planDigest, err := planDigestFunc(manifest)
	if err != nil || planDigest != manifest.GraderPlanSHA256 {
		return errors.New("coding grader plan digest mismatch")
	}
	return nil
}

// Validate checks one complete grader manifest at the supplied trusted time.
// It is exported for the separately reviewed sandbox executor adapter.
func (manifest Manifest) Validate(now time.Time) error {
	return manifest.validate(now)
}

// BuildRun is the trusted executor's out-of-process build completion receipt.
type BuildRun struct {
	CommandID          string
	CommandSHA256      string
	ExecutorInstanceID string
	ReturnCode         int
	Completed          bool
	TimedOut           bool
}

// TestRun is the trusted executor's out-of-process group completion receipt.
type TestRun struct {
	CommandID          string
	CommandSHA256      string
	ExecutorInstanceID string
	ReturnCode         int
	Passed             uint32
	Total              uint32
	Completed          bool
	TimedOut           bool
}

// ExecutorAttestation proves the concrete sandbox identity and isolation used
// for this grade before any candidate or grader bytes are consumed.
type ExecutorAttestation struct {
	ExecutorInstanceID     string
	GraderImageDigest      string
	GraderPlatform         string
	GraderContractSHA256   string
	GraderPlanSHA256       string
	ResourceProfileSHA256  string
	NetworkDisabled        bool
	CandidateMountReadOnly bool
	ProtectedMountHidden   bool
	ProcessGroupsIsolated  bool
}

func (attestation ExecutorAttestation) validate(manifest Manifest) error {
	if !validIdentifier(attestation.ExecutorInstanceID, 256) ||
		attestation.GraderImageDigest != manifest.GraderImageDigest ||
		attestation.GraderPlatform != manifest.GraderPlatform ||
		attestation.GraderContractSHA256 != manifest.GraderContractSHA256 ||
		attestation.GraderPlanSHA256 != manifest.GraderPlanSHA256 ||
		attestation.ResourceProfileSHA256 != manifest.ResourceProfileSHA256 ||
		!attestation.NetworkDisabled || !attestation.CandidateMountReadOnly ||
		!attestation.ProtectedMountHidden || !attestation.ProcessGroupsIsolated {
		return errors.New("coding grader executor attestation does not satisfy the plan")
	}
	return nil
}

// Executor runs fixed manifest commands in a networkless, fresh grader
// sandbox. The supplied paths are trusted host mount inputs: candidate code
// must see a fixed read-only /workspace and must never see protectedGrader.
// Errors represent trusted infrastructure failure; candidate build failure,
// crash, timeout, and test failure must be returned as completed receipts. The
// executor must not derive success from candidate-controlled stdout.
type Executor interface {
	Preflight(ctx context.Context, expectedPlanSHA256 string) (ExecutorAttestation, error)
	Build(ctx context.Context, workspace string, command codingrunner.CommandSpec) (BuildRun, error)
	Test(ctx context.Context, workspace string, protectedGrader string, group TestGroupSpec) (TestRun, error)
}

// ExecutionReceipt is one canonical executor completion record.
type ExecutionReceipt struct {
	Schema                string  `json:"schema"`
	Sequence              uint32  `json:"sequence"`
	Phase                 string  `json:"phase"`
	Group                 *string `json:"group"`
	CommandID             string  `json:"command_id"`
	CommandSHA256         string  `json:"command_sha256"`
	ExecutorInstanceID    string  `json:"executor_instance_id"`
	ReturnCode            int     `json:"returncode"`
	Passed                uint32  `json:"passed"`
	Total                 uint32  `json:"total"`
	Completed             bool    `json:"completed"`
	TimedOut              bool    `json:"timed_out"`
	PreviousReceiptSHA256 string  `json:"previous_receipt_sha256"`
}

// Result is the deterministic grader outcome later embedded in task evidence.
type Result struct {
	TerminalDomain             codingcontract.TerminalDomain  `json:"terminal_domain"`
	FailureCode                *string                        `json:"failure_code"`
	RepairScoreMicros          uint32                         `json:"repair_score_micros"`
	Evidence                   *codingcontract.GraderEvidence `json:"grader"`
	ReplayedFinalTreeSHA256    string                         `json:"replayed_final_tree_sha256"`
	ProtectedGraderTreeSHA256  string                         `json:"protected_grader_tree_sha256"`
	ExecutionReceiptRootSHA256 string                         `json:"execution_receipt_root_sha256"`
	ExecutionReceipts          []ExecutionReceipt             `json:"execution_receipts"`
}

func failure(domain codingcontract.TerminalDomain, code string) Result {
	return Result{TerminalDomain: domain, FailureCode: &code}
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

func lowerSHA256(value string) bool {
	if len(value) != 64 || value != strings.ToLower(value) {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}

func ociDigest(value string) bool {
	return strings.HasPrefix(value, "sha256:") && lowerSHA256(strings.TrimPrefix(value, "sha256:"))
}
