package codingcanary

import (
	"archive/tar"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codinggrader"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
)

type executionPlans struct {
	runner       codingrunner.Manifest
	grader       codinggrader.Manifest
	visible      []byte
	graderBundle []byte
}

func (pack PublicPack) executionPlans(
	now time.Time,
	deadline time.Time,
	leaseID string,
	imageDigest string,
) (executionPlans, error) {
	var zero executionPlans
	visibleFiles, graderFiles, err := pack.verifiedTrees()
	if err != nil {
		return zero, err
	}
	visible, err := tarPackFiles(visibleFiles)
	if err != nil {
		return zero, err
	}
	graderBundle, err := tarPackFiles(graderFiles)
	if err != nil {
		return zero, err
	}
	limits := codingrunner.DefaultLimits()
	identity, err := codingrunner.InspectBundle(
		context.Background(), bytes.NewReader(visible), limits,
	)
	if err != nil {
		return zero, err
	}
	runner := codingrunner.Manifest{
		CodingContractVersion: codingrunner.ContractVersion,
		TicketID:              leaseID, CaseID: publicCanaryTaskID, ProfileCapabilityID: publicCanaryProfileID,
		VisibleBundleSHA256: identity.VisibleBundleSHA256, BaseTreeSHA256: identity.TreeSHA256,
		Deadline: deadline, EditablePaths: append([]string(nil), pack.EditablePaths...),
		CreatablePaths: []string{}, DeletablePaths: []string{},
		TestCommands: []codingrunner.CommandSpec{{
			ID: "visible-unit", Argv: []string{"python", "-m", "pytest", "tests/test_visible.py"}, Timeout: time.Minute,
		}},
		BuildCommands: []codingrunner.CommandSpec{{
			ID: "python-compile", Argv: []string{"python", "-m", "compileall", "app.py"}, Timeout: time.Minute,
		}},
		Limits: limits,
	}
	protected := codingrunner.DefaultLimits()
	protected.MaxBundleBytes = 8 << 20
	protected.MaxWorkspaceBytes = 16 << 20
	protected.MaxFileBytes = 4 << 20
	protected.MaxPatchBytes = 4 << 20
	policy := codinggrader.ResourcePolicy{
		CandidateLimits: limits, ProtectedLimits: protected,
		MaxCombinedDiskBytes: limits.MaxWorkspaceBytes + protected.MaxWorkspaceBytes + limits.MaxBundleBytes + 1<<30,
		MemoryLimitBytes:     pack.MemoryLimitBytes, ScratchLimitBytes: uint64(limits.MaxWorkspaceBytes),
		PidsLimit: pack.PidsLimit, CPUQuotaMillis: pack.CPUQuotaMillis,
	}
	resourceSHA, err := codinggrader.ResourceProfileSHA256(policy)
	if err != nil {
		return zero, err
	}
	groups := make([]codinggrader.TestGroupSpec, 0, len(codingGraderGroups))
	for _, group := range codingGraderGroups {
		groups = append(groups, codinggrader.TestGroupSpec{
			Group: group,
			Command: codingrunner.CommandSpec{
				ID: "cert-" + group, Argv: []string{"dittobench-test-driver", group}, Timeout: time.Minute,
			},
			ExpectedTotal: 2,
		})
	}
	graderDigest := sha256.Sum256(graderBundle)
	testDigest := sha256.Sum256(graderBundle)
	grader := codinggrader.Manifest{
		CodingContractVersion: codingrunner.ContractVersion,
		CaseID:                publicCanaryTaskID, VariantID: publicCanaryProfileID,
		VisibleBundleSHA256: identity.VisibleBundleSHA256, BaseTreeSHA256: identity.TreeSHA256,
		GraderContractSHA256: codinggrader.GraderContractSHA256(),
		GraderBundleSHA256:   hex.EncodeToString(graderDigest[:]),
		GraderImageDigest:    imageDigest, GraderPlatform: "linux/amd64",
		TestManifestSHA256: hex.EncodeToString(testDigest[:]), ResourceProfileSHA256: resourceSHA,
		Deadline: deadline, ExecutionTimeout: 30 * time.Minute, ResourcePolicy: policy,
		Build: codinggrader.BuildSpec{Required: true, Command: codingrunner.CommandSpec{
			ID: "cert-build", Argv: []string{"python", "-m", "compileall", "app.py"}, Timeout: time.Minute,
		}},
		TestGroups: groups,
	}
	grader.GraderPlanSHA256, err = codinggrader.GraderPlanSHA256(grader)
	if err != nil {
		return zero, err
	}
	if err := runner.Validate(now); err != nil {
		return zero, err
	}
	if err := grader.Validate(now); err != nil {
		return zero, err
	}
	return executionPlans{runner: runner, grader: grader, visible: visible, graderBundle: graderBundle}, nil
}

var codingGraderGroups = []string{"adversarial", "fail_to_pass", "hidden", "integrity", "pass_to_pass"}

// tarPackFiles bundles an already verified tree in its lexical walk order.
func tarPackFiles(files []packFile) ([]byte, error) {
	if len(files) == 0 {
		return nil, ErrInvalid
	}
	var buffer bytes.Buffer
	writer := tar.NewWriter(&buffer)
	for _, file := range files {
		header := &tar.Header{
			Name: file.path, Mode: 0o644, Size: int64(len(file.body)), Typeflag: tar.TypeReg,
		}
		if err := writer.WriteHeader(header); err != nil {
			return nil, err
		}
		if _, err := writer.Write(file.body); err != nil {
			return nil, err
		}
	}
	if err := writer.Close(); err != nil {
		return nil, err
	}
	return buffer.Bytes(), nil
}
