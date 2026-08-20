package codinggrader

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
)

// Grade reconstructs and grades one frozen submission in a fresh workspace.
// It never accepts authoring state or exposes grader bytes to the miner.
func Grade(
	ctx context.Context,
	manifest Manifest,
	submission codingrunner.FrozenSubmission,
	visibleBundle io.Reader,
	graderBundle io.Reader,
	executor Executor,
) (result Result) {
	if ctx == nil {
		return failure(codingcontract.DomainValidatorInfrastructure, "grader_context_missing")
	}
	manifest = cloneManifest(manifest)
	if err := manifest.validate(time.Now()); err != nil {
		return failure(codingcontract.DomainControlPlaneIntegrity, "grader_manifest_invalid")
	}
	if submission.CaseID != manifest.CaseID || submission.VisibleBundleSHA256 != manifest.VisibleBundleSHA256 ||
		submission.BaseTreeSHA256 != manifest.BaseTreeSHA256 {
		return failure(codingcontract.DomainControlPlaneIntegrity, "grader_submission_identity_mismatch")
	}
	if executor == nil {
		return failure(codingcontract.DomainValidatorInfrastructure, "grader_executor_unavailable")
	}
	leaseContext, leaseCancel := context.WithDeadline(ctx, manifest.Deadline)
	defer leaseCancel()
	attestation, err := executor.Preflight(leaseContext, manifest.GraderPlanSHA256)
	if err != nil {
		if result, failed := setupContextFailure(ctx, leaseContext); failed {
			return result
		}
		return failure(codingcontract.DomainValidatorInfrastructure, "grader_preflight_failed")
	}
	if err := attestation.validate(manifest); err != nil {
		return failure(codingcontract.DomainControlPlaneIntegrity, "grader_attestation_invalid")
	}
	workspace, err := codingrunner.ReplayFrozenSubmission(
		leaseContext, submission, visibleBundle, manifest.ResourcePolicy.CandidateLimits,
	)
	if err != nil {
		if result, failed := setupContextFailure(ctx, leaseContext); failed {
			return result
		}
		return failure(codingcontract.DomainControlPlaneIntegrity, "frozen_replay_invalid")
	}
	defer func() {
		if err := workspace.Close(); err != nil {
			result = failure(codingcontract.DomainValidatorInfrastructure, "grader_cleanup")
		}
	}()
	replayedTree, err := workspace.TreeSHA256(leaseContext)
	if err != nil || replayedTree != submission.FinalTreeSHA256 {
		if result, failed := setupContextFailure(ctx, leaseContext); failed {
			return result
		}
		return failure(codingcontract.DomainControlPlaneIntegrity, "replayed_tree_invalid")
	}
	protectedBundle, err := workspace.MaterializeProtectedBundle(
		leaseContext, manifest.GraderBundleSHA256, graderBundle, manifest.ResourcePolicy.ProtectedLimits,
	)
	if err != nil {
		if result, failed := setupContextFailure(ctx, leaseContext); failed {
			return result
		}
		return failure(codingcontract.DomainControlPlaneIntegrity, "grader_bundle_invalid")
	}
	defer func() {
		if err := protectedBundle.Close(); err != nil {
			result = failure(codingcontract.DomainValidatorInfrastructure, "grader_cleanup")
		}
	}()
	protectedTree := protectedBundle.InitialTreeSHA256()
	trustedPath, err := workspace.TrustedPath()
	if err != nil {
		return failure(codingcontract.DomainValidatorInfrastructure, "grader_workspace_unavailable")
	}
	protectedPath, err := protectedBundle.TrustedPath()
	if err != nil {
		return failure(codingcontract.DomainValidatorInfrastructure, "grader_bundle_unavailable")
	}
	integrityBefore := integrityRoot(replayedTree, protectedTree)
	if result, failed := setupContextFailure(ctx, leaseContext); failed {
		return result
	}
	executionContext, executionCancel := context.WithTimeout(leaseContext, manifest.ExecutionTimeout)
	defer executionCancel()

	buildPassed := !manifest.Build.Required
	testEvidence := make([]codingcontract.TestGroupEvidence, len(manifest.TestGroups))
	groupIndexes := make(map[string]int, len(manifest.TestGroups))
	groupsByName := make(map[string]TestGroupSpec, len(manifest.TestGroups))
	for index, group := range manifest.TestGroups {
		testEvidence[index] = codingcontract.TestGroupEvidence{Group: group.Group, Total: group.ExpectedTotal}
		groupIndexes[group.Group] = index
		groupsByName[group.Group] = group
	}
	receipts := make([]ExecutionReceipt, 0, 1+len(manifest.TestGroups))
	receiptRoot := initialReceiptRoot
	terminal := codingcontract.DomainRepairFailure
	failureCode := "tests_failed"
	infrastructureFailure := false
	controlPlaneFailure := false
	if manifest.Build.Required {
		buildRun, buildErr := executor.Build(executionContext, trustedPath, manifest.Build.Command)
		if buildErr != nil {
			infrastructureFailure = true
			failureCode = "grader_build_executor"
		} else {
			commandSHA, digestErr := CommandSHA256(
				manifest.Build.Command.ID, manifest.Build.Command.Argv, manifest.Build.Command.Timeout.Milliseconds(),
			)
			if digestErr != nil || buildRun.CommandID != manifest.Build.Command.ID || buildRun.CommandSHA256 != commandSHA ||
				buildRun.ExecutorInstanceID != attestation.ExecutorInstanceID {
				controlPlaneFailure = true
				failureCode = "grader_build_receipt"
			} else {
				receipts, receiptRoot, err = appendReceipt(receipts, receiptRoot, ExecutionReceipt{
					Schema: "dittobench-coding-grader-receipt-v1", Phase: "build", CommandID: buildRun.CommandID,
					CommandSHA256: commandSHA, ExecutorInstanceID: buildRun.ExecutorInstanceID,
					ReturnCode: buildRun.ReturnCode, Completed: buildRun.Completed, TimedOut: buildRun.TimedOut,
				})
				if err != nil {
					controlPlaneFailure = true
					failureCode = "grader_receipt_encode"
				}
			}
			buildPassed = buildRun.Completed && !buildRun.TimedOut && buildRun.ReturnCode == 0
			if !buildPassed {
				failureCode = "build_failed"
			}
		}
	}

	allTestsPassed := buildPassed && !infrastructureFailure && !controlPlaneFailure
	if allTestsPassed {
		for _, groupName := range executionOrder {
			group := groupsByName[groupName]
			index := groupIndexes[groupName]
			run, runErr := executor.Test(executionContext, trustedPath, protectedPath, group)
			if runErr != nil {
				infrastructureFailure = true
				failureCode = "grader_test_executor"
				allTestsPassed = false
				break
			}
			commandSHA, digestErr := CommandSHA256(group.Command.ID, group.Command.Argv, group.Command.Timeout.Milliseconds())
			if digestErr != nil || run.CommandID != group.Command.ID || run.CommandSHA256 != commandSHA ||
				run.ExecutorInstanceID != attestation.ExecutorInstanceID {
				controlPlaneFailure = true
				failureCode = "grader_test_receipt"
				allTestsPassed = false
				break
			}
			if run.Total != group.ExpectedTotal || run.Passed > run.Total {
				controlPlaneFailure = true
				failureCode = "grader_test_count"
				allTestsPassed = false
				break
			}
			testEvidence[index].Passed = run.Passed
			groupCopy := group.Group
			receipts, receiptRoot, err = appendReceipt(receipts, receiptRoot, ExecutionReceipt{
				Schema: "dittobench-coding-grader-receipt-v1", Phase: "test", Group: &groupCopy,
				CommandID: run.CommandID, CommandSHA256: commandSHA, ExecutorInstanceID: run.ExecutorInstanceID,
				ReturnCode: run.ReturnCode, Passed: run.Passed, Total: run.Total,
				Completed: run.Completed, TimedOut: run.TimedOut,
			})
			if err != nil {
				controlPlaneFailure = true
				failureCode = "grader_receipt_encode"
				allTestsPassed = false
				break
			}
			groupPassed := run.Completed && !run.TimedOut && run.ReturnCode == 0 && run.Passed == run.Total
			if !groupPassed {
				allTestsPassed = false
				failureCode = "tests_failed"
				break
			}
		}
	}

	integrityContext, integrityCancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer integrityCancel()
	afterTree, afterErr := workspace.TreeSHA256(integrityContext)
	protectedAfterTree, protectedErr := protectedBundle.TreeSHA256(integrityContext)
	if afterErr != nil || protectedErr != nil {
		return failure(codingcontract.DomainValidatorInfrastructure, "grader_integrity_snapshot")
	}
	integrityAfter := integrityRoot(afterTree, protectedAfterTree)
	evidence := &codingcontract.GraderEvidence{
		GraderContractSHA256:        manifest.GraderContractSHA256,
		GraderBundleSHA256:          manifest.GraderBundleSHA256,
		GraderImageDigest:           attestation.GraderImageDigest,
		GraderPlatform:              attestation.GraderPlatform,
		TestManifestSHA256:          manifest.TestManifestSHA256,
		GraderPlanSHA256:            manifest.GraderPlanSHA256,
		ResourceProfileSHA256:       attestation.ResourceProfileSHA256,
		ExecutionReceiptRootSHA256:  receiptRoot,
		ExecutionReceiptCount:       uint32(len(receipts)),
		GraderIntegrityBeforeSHA256: integrityBefore,
		GraderIntegrityAfterSHA256:  integrityAfter,
		Build: codingcontract.BuildEvidence{
			CommandID: manifest.Build.Command.ID,
			Required:  manifest.Build.Required,
			Passed:    buildPassed,
		},
		TestGroups: testEvidence,
	}
	if err := evidence.Validate(); err != nil {
		return failure(codingcontract.DomainControlPlaneIntegrity, "grader_evidence_invalid")
	}
	result = Result{
		TerminalDomain:             terminal,
		FailureCode:                &failureCode,
		Evidence:                   evidence,
		ReplayedFinalTreeSHA256:    replayedTree,
		ProtectedGraderTreeSHA256:  protectedTree,
		ExecutionReceiptRootSHA256: receiptRoot,
		ExecutionReceipts:          cloneReceipts(receipts),
	}
	if protectedTree != protectedAfterTree {
		result.TerminalDomain = codingcontract.DomainControlPlaneIntegrity
		code := "grader_bundle_mutation"
		result.FailureCode = &code
		return result
	}
	if replayedTree != afterTree {
		result.TerminalDomain = codingcontract.DomainCandidateIntegrity
		code := "grader_workspace_mutation"
		result.FailureCode = &code
		return result
	}
	if controlPlaneFailure {
		result.TerminalDomain = codingcontract.DomainControlPlaneIntegrity
		return result
	}
	if domain, code, failed := executionContextFailure(ctx, leaseContext, executionContext); failed {
		result.TerminalDomain = domain
		result.FailureCode = &code
		return result
	}
	if infrastructureFailure {
		result.TerminalDomain = codingcontract.DomainValidatorInfrastructure
		return result
	}
	if buildPassed && allTestsPassed {
		result.TerminalDomain = codingcontract.DomainResolved
		result.FailureCode = nil
		result.RepairScoreMicros = codingcontract.ResolvedRepairScoreMicros
	}
	return result
}

func cloneManifest(manifest Manifest) Manifest {
	manifest.Build.Command.Argv = append([]string(nil), manifest.Build.Command.Argv...)
	manifest.TestGroups = append([]TestGroupSpec(nil), manifest.TestGroups...)
	for index := range manifest.TestGroups {
		manifest.TestGroups[index].Command.Argv = append([]string(nil), manifest.TestGroups[index].Command.Argv...)
	}
	return manifest
}

func integrityRoot(candidateTree, protectedTree string) string {
	hasher := sha256.New()
	_, _ = hasher.Write([]byte("dittobench-coding-grader-integrity-v1\x00"))
	_, _ = hasher.Write([]byte(candidateTree))
	_, _ = hasher.Write([]byte{'\x00'})
	_, _ = hasher.Write([]byte(protectedTree))
	return hex.EncodeToString(hasher.Sum(nil))
}

func appendReceipt(
	receipts []ExecutionReceipt,
	previous string,
	receipt ExecutionReceipt,
) ([]ExecutionReceipt, string, error) {
	receipt.Sequence = uint32(len(receipts) + 1)
	receipt.PreviousReceiptSHA256 = previous
	digest, err := digestCanonical(receipt)
	if err != nil {
		return receipts, previous, err
	}
	return append(receipts, cloneReceipt(receipt)), digest, nil
}

func cloneReceipt(receipt ExecutionReceipt) ExecutionReceipt {
	if receipt.Group != nil {
		group := *receipt.Group
		receipt.Group = &group
	}
	return receipt
}

func cloneReceipts(receipts []ExecutionReceipt) []ExecutionReceipt {
	result := make([]ExecutionReceipt, len(receipts))
	for index, receipt := range receipts {
		result[index] = cloneReceipt(receipt)
	}
	return result
}

func setupContextFailure(parent context.Context, lease context.Context) (Result, bool) {
	switch {
	case errors.Is(parent.Err(), context.DeadlineExceeded):
		return failure(codingcontract.DomainValidatorInfrastructure, "grader_parent_deadline"), true
	case errors.Is(parent.Err(), context.Canceled):
		return failure(codingcontract.DomainValidatorInfrastructure, "grader_cancelled"), true
	case errors.Is(lease.Err(), context.DeadlineExceeded):
		return failure(codingcontract.DomainValidatorInfrastructure, "grader_setup_deadline"), true
	case errors.Is(lease.Err(), context.Canceled):
		return failure(codingcontract.DomainValidatorInfrastructure, "grader_cancelled"), true
	default:
		return Result{}, false
	}
}

func executionContextFailure(
	parent context.Context,
	lease context.Context,
	execution context.Context,
) (codingcontract.TerminalDomain, string, bool) {
	switch {
	case errors.Is(parent.Err(), context.DeadlineExceeded):
		return codingcontract.DomainValidatorInfrastructure, "grader_parent_deadline", true
	case errors.Is(parent.Err(), context.Canceled):
		return codingcontract.DomainValidatorInfrastructure, "grader_cancelled", true
	case errors.Is(lease.Err(), context.DeadlineExceeded):
		return codingcontract.DomainValidatorInfrastructure, "grader_lease_deadline", true
	case errors.Is(lease.Err(), context.Canceled):
		return codingcontract.DomainValidatorInfrastructure, "grader_cancelled", true
	case errors.Is(execution.Err(), context.DeadlineExceeded):
		return codingcontract.DomainRepairFailure, "grader_deadline", true
	case errors.Is(execution.Err(), context.Canceled):
		return codingcontract.DomainValidatorInfrastructure, "grader_cancelled", true
	default:
		return "", "", false
	}
}
