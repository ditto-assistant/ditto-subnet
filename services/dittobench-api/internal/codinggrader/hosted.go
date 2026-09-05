package codinggrader

import (
	"context"
	"errors"
	"io"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
)

var hostedEvidenceGroups = []string{"hidden", "visible"}
var hostedExecutionOrder = []string{"visible", "hidden"}

// HostedManifest is deliberately distinct from the permanently v1 Manifest API.
// Commands, images, complete resource policy and expected counts are trusted
// Platform/curator inputs, never candidate-authored claims.
type HostedManifest Manifest

// HostedGradingAuthority is obtained from the committed assignment and freeze,
// independently of the supplied plan and submission.
type HostedGradingAuthority struct {
	codingrunner.HostedReplayAuthority
	GraderPlanSHA256 string
	Deadline         time.Time
}

func (manifest HostedManifest) Validate(now time.Time) error {
	return Manifest(manifest).validateProfile(now, true)
}

func HostedGraderContractSHA256() string {
	value, err := digestCanonical(map[string]any{
		"schema":                              "dittobench-coding-hosted-grader-contract-v2",
		"coding_contract_version":             2,
		"inherited_isolation_contract_sha256": GraderContractSHA256(),
		"plan_schema":                         "dittobench-coding-grader-plan-v2",
		"resource_schema":                     "dittobench-coding-grader-resource-v2",
		"receipt_schema":                      "dittobench-coding-grader-receipt-v2",
		"evidence_groups":                     hostedEvidenceGroups, "execution_order": hostedExecutionOrder,
		"maximum_group_tests": 1_000_000, "maximum_lifetime_seconds": 3600,
		"committed_patch_required": true, "trusted_test_driver_required": true,
		"shadow_only": true, "weight_eligible": false,
	})
	if err != nil {
		panic(err)
	}
	return value
}

func HostedResourceProfileSHA256(policy ResourcePolicy) (string, error) {
	return resourceProfileSHA256(policy, "dittobench-coding-grader-resource-v2")
}

func HostedGraderPlanSHA256(manifest HostedManifest) (string, error) {
	return graderPlanSHA256(Manifest(manifest), "dittobench-coding-grader-plan-v2", hostedExecutionOrder)
}

// HostedResult is Platform-private grading evidence, not the signed validator
// terminal receipt. No existing scoring route accepts this versioned wrapper.
type HostedResult struct {
	Schema                string `json:"schema"`
	CodingContractVersion int    `json:"coding_contract_version"`
	ShadowOnly            bool   `json:"shadow_only"`
	WeightEligible        bool   `json:"weight_eligible"`
	AssignmentSHA256      string `json:"assignment_sha256"`
	FrozenPatchSHA256     string `json:"frozen_patch_sha256"`
	Result                Result `json:"result"`
}

// GradeHosted verifies the committed freeze before any private bundle is opened,
// then uses the same pristine replay, attested executor, receipt, integrity and
// cleanup machinery as v1 with explicit native v2 groups and receipt domains.
func GradeHosted(ctx context.Context, authority HostedGradingAuthority, manifest HostedManifest, submission codingrunner.FrozenSubmission, visibleBundle io.Reader, openProtected ProtectedBundleOpener, executor Executor) HostedResult {
	result := failure(codingcontract.DomainControlPlaneIntegrity, "hosted_grading_plan_mismatch")
	if lowerSHA256(authority.GraderPlanSHA256) && authority.GraderPlanSHA256 == manifest.GraderPlanSHA256 &&
		!authority.Deadline.IsZero() && authority.Deadline.Equal(manifest.Deadline) {
		result = grade(ctx, Manifest(manifest), submission, visibleBundle, openProtected, executor, &authority.HostedReplayAuthority)
	}
	assignmentSHA, patchSHA := authority.AssignmentSHA256, authority.FrozenPatchSHA256
	if !lowerSHA256(assignmentSHA) {
		assignmentSHA = ""
	}
	if !lowerSHA256(patchSHA) {
		patchSHA = ""
	}
	return HostedResult{
		Schema: "dittobench-coding-hosted-grading-result-v2", CodingContractVersion: 2,
		ShadowOnly: true, WeightEligible: false, AssignmentSHA256: assignmentSHA,
		FrozenPatchSHA256: patchSHA, Result: result,
	}
}

func validateHostedEvidence(evidence *codingcontract.GraderEvidence) error {
	if evidence == nil || evidence.GraderContractSHA256 != HostedGraderContractSHA256() ||
		!lowerSHA256(evidence.GraderBundleSHA256) || !ociDigest(evidence.GraderImageDigest) ||
		evidence.GraderPlatform != "linux/amd64" || !lowerSHA256(evidence.TestManifestSHA256) ||
		!lowerSHA256(evidence.GraderPlanSHA256) || !lowerSHA256(evidence.ResourceProfileSHA256) ||
		!lowerSHA256(evidence.ExecutionReceiptRootSHA256) || !lowerSHA256(evidence.GraderIntegrityBeforeSHA256) ||
		!lowerSHA256(evidence.GraderIntegrityAfterSHA256) || !validIdentifier(evidence.Build.CommandID, 80) ||
		evidence.ExecutionReceiptCount > 3 || len(evidence.TestGroups) != len(hostedEvidenceGroups) {
		return errors.New("hosted grader evidence identity is invalid")
	}
	for index, group := range evidence.TestGroups {
		if group.Group != hostedEvidenceGroups[index] || group.Total == 0 || group.Total > 1_000_000 || group.Passed > group.Total {
			return errors.New("hosted grader evidence counts are invalid")
		}
	}
	return nil
}
