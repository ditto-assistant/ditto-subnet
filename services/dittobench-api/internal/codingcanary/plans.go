package codingcanary

import (
	"archive/tar"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"os"
	"path/filepath"
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
	visible, err := tarDirectory(pack.VisibleDir)
	if err != nil {
		return zero, err
	}
	graderBundle, err := tarDirectory(pack.GraderDir)
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

func tarDirectory(root string) ([]byte, error) {
	var buffer bytes.Buffer
	writer := tar.NewWriter(&buffer)
	err := filepath.Walk(root, func(path string, info os.FileInfo, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if info.IsDir() {
			return nil
		}
		if !info.Mode().IsRegular() {
			return ErrInvalid
		}
		rel, err := filepath.Rel(root, path)
		if err != nil {
			return err
		}
		body, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		header := &tar.Header{
			Name: filepath.ToSlash(rel), Mode: 0o644, Size: int64(len(body)), Typeflag: tar.TypeReg,
		}
		if err := writer.WriteHeader(header); err != nil {
			return err
		}
		_, err = writer.Write(body)
		return err
	})
	if err != nil {
		return nil, err
	}
	if err := writer.Close(); err != nil {
		return nil, err
	}
	if buffer.Len() == 0 {
		return nil, ErrInvalid
	}
	return buffer.Bytes(), nil
}
