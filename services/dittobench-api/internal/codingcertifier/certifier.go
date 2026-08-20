package codingcertifier

import (
	"context"
	"errors"
	"fmt"
	"io"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codinggrader"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
)

const (
	healthTimeout = 10 * time.Second
	seedTimeout   = 2 * time.Minute
)

// Executor is one trusted runtime adapter for both visible authoring commands
// and pristine grading. The existing codingexecutor.Executor satisfies it.
type Executor interface {
	codingrunner.CommandExecutor
	codinggrader.Executor
}

// Config supplies every trusted dependency for one shadow certifier. Runtime
// integration must provide a source-bound capability publisher and the
// sandbox-attested harness client separately.
type Config struct {
	Harness           HarnessClient
	Publisher         CapabilityPublisher
	Executor          Executor
	OpenVisibleBundle BundleOpener
	OpenGraderBundle  BundleOpener
	CertificationTTL  time.Duration
	Now               func() time.Time
}

// Certifier drives one no-retry active canary without touching production
// scoring or validator weights.
type Certifier struct {
	harness           HarnessClient
	publisher         CapabilityPublisher
	executor          Executor
	openVisibleBundle BundleOpener
	openGraderBundle  BundleOpener
	ttl               time.Duration
	now               func() time.Time
}

// New returns a fail-closed shadow capability certifier.
func New(config Config) (*Certifier, error) {
	if config.Harness == nil || config.Publisher == nil || config.Executor == nil ||
		config.OpenVisibleBundle == nil || config.OpenGraderBundle == nil {
		return nil, errors.New("coding certification dependencies are incomplete")
	}
	if config.CertificationTTL < minimumTTL || config.CertificationTTL > maximumTTL ||
		config.CertificationTTL%time.Second != 0 {
		return nil, errors.New("coding certification TTL is outside bounds")
	}
	if config.Now == nil {
		config.Now = time.Now
	}
	return &Certifier{
		harness: config.Harness, publisher: config.Publisher, executor: config.Executor,
		openVisibleBundle: config.OpenVisibleBundle, openGraderBundle: config.OpenGraderBundle,
		ttl: config.CertificationTTL, now: config.Now,
	}, nil
}

// Certify executes health -> seed -> run -> revoke -> freeze -> pristine grade.
// Trusted setup and infrastructure failures return an error and must not
// de-certify an artifact. Unsupported and candidate-attributable failures
// return a sealed receipt.
func (certifier *Certifier) Certify(
	ctx context.Context,
	request Request,
) (receipt Receipt, returnedErr error) {
	if ctx == nil {
		return Receipt{}, errors.New("coding certification context is required")
	}
	now := certifier.now().UTC().Truncate(time.Second)
	if err := request.validate(now); err != nil {
		return Receipt{}, err
	}
	receipt = newReceipt(request, now, certifier.ttl)
	healthContext, cancelHealth := context.WithTimeout(ctx, healthTimeout)
	health, err := certifier.harness.Health(healthContext)
	cancelHealth()
	if errors.Is(err, ErrCodingUnsupported) {
		return finalize(receipt, StatusUnsupported, StageHealth, "coding_endpoint_absent")
	}
	if err != nil {
		if ctx.Err() != nil {
			return Receipt{}, fmt.Errorf("coding health interrupted: %w", ctx.Err())
		}
		return finalize(receipt, StatusFailed, StageHealth, "coding_health_failed")
	}
	if err := health.validate(); err != nil {
		return finalize(receipt, StatusFailed, StageHealth, "coding_health_invalid")
	}
	health = health.normalized()
	receipt.SupportedCodingContractVersions = append([]int(nil), health.SupportedCodingContractVersions...)
	receipt.Capabilities = append([]string(nil), health.Capabilities...)
	if !health.supportsCodingV1() {
		return finalize(receipt, StatusUnsupported, StageHealth, "coding_contract_or_capability_missing")
	}
	firstSeedContext, cancelFirstSeed := context.WithTimeout(ctx, seedTimeout)
	firstSeed, err := certifier.harness.Seed(firstSeedContext, request.Seed)
	cancelFirstSeed()
	if err != nil || firstSeed.validate(request.Seed, false) != nil {
		if ctx.Err() != nil {
			return Receipt{}, fmt.Errorf("coding seed interrupted: %w", ctx.Err())
		}
		return finalize(receipt, StatusFailed, StageSeed, "coding_seed_failed")
	}
	secondSeedContext, cancelSecondSeed := context.WithTimeout(ctx, seedTimeout)
	secondSeed, err := certifier.harness.Seed(secondSeedContext, request.Seed)
	cancelSecondSeed()
	if err != nil || secondSeed.validate(request.Seed, true) != nil {
		if ctx.Err() != nil {
			return Receipt{}, fmt.Errorf("coding seed replay interrupted: %w", ctx.Err())
		}
		return finalize(receipt, StatusFailed, StageSeed, "coding_seed_idempotency_failed")
	}

	visible, err := certifier.openVisibleBundle()
	if err != nil {
		return Receipt{}, errors.New("open coding certification visible bundle")
	}
	session, sessionErr := codingrunner.NewSession(ctx, request.RunnerManifest, visible, certifier.executor)
	visibleCloseErr := visible.Close()
	if sessionErr != nil {
		return Receipt{}, errors.Join(errors.New("construct coding certification runner"), sessionErr, visibleCloseErr)
	}
	if visibleCloseErr != nil {
		return Receipt{}, errors.Join(errors.New("close coding certification visible bundle"), visibleCloseErr, session.Close())
	}
	defer func() {
		returnedErr = errors.Join(returnedErr, session.Close())
	}()

	binding := CapabilityBinding{
		HarnessInstanceID:   request.HarnessAttestation.HarnessInstanceID,
		AgentArtifactSHA256: request.AgentArtifactSHA256,
		TicketID:            request.Seed.TicketID, CaseID: request.Seed.CaseID,
		ProfileCapabilityID: request.Seed.ProfileCapabilityID,
	}
	capability, err := certifier.publisher.Publish(ctx, binding, session.Handler())
	if err != nil || capability == nil {
		return Receipt{}, errors.Join(errors.New("publish coding certification capability"), err)
	}
	defer func() {
		returnedErr = errors.Join(returnedErr, capability.Close())
	}()
	runRequest := request.runRequest(capability.URL())
	if err := runRequest.Validate(); err != nil {
		return Receipt{}, fmt.Errorf("published coding capability is invalid: %w", err)
	}
	runContext, cancelRun := context.WithTimeout(ctx, time.Duration(request.Budgets.WallTimeSeconds)*time.Second)
	runResponse, runErr := certifier.harness.Run(runContext, runRequest)
	if runErr == nil {
		runErr = runResponse.validate(runRequest)
	}
	cancelRun()

	revokeContext, cancelRevoke := context.WithTimeout(context.Background(), 30*time.Second)
	revokeErr := capability.Revoke(revokeContext)
	cancelRevoke()
	freeze := session.Freeze()
	transcript, transcriptErr := session.WriteTranscript(io.Discard)
	if revokeErr != nil || transcriptErr != nil {
		return Receipt{}, errors.Join(errors.New("revoke or snapshot coding certification capability"), revokeErr, transcriptErr)
	}
	receipt.AuthoringEventCount = transcript.Events
	if freeze.Submission == nil {
		if freeze.Failure == nil {
			return Receipt{}, errors.New("coding certification freeze returned no outcome")
		}
		receipt.ChangedPathRoot = stringPointer(freeze.Failure.ChangedPathRoot)
		receipt.FinalTreeSHA256 = stringPointer(freeze.Failure.FinalTreeSHA256)
		receipt.AuthoringEventRoot = stringPointer(freeze.Failure.AuthoringEventRoot)
		receipt.AuthoringTranscriptSHA256 = stringPointer(freeze.Failure.AuthoringTranscriptSHA256)
		receipt.AuthoringTranscriptBytes = freeze.Failure.AuthoringTranscriptBytes
		receipt.ProtectedPathsIntact = freeze.Failure.ProtectedPathsIntact
		if freeze.Failure.Kind == string(codingcontract.DomainValidatorInfrastructure) {
			return Receipt{}, fmt.Errorf("coding certification freeze infrastructure: %s", freeze.Failure.Code)
		}
		return finalize(receipt, StatusFailed, StageFreeze, stableFailureCode(freeze.Failure.Code, "coding_freeze_failed"))
	}
	submission := *freeze.Submission
	receipt.FrozenPatchSHA256 = stringPointer(submission.FrozenPatchSHA256)
	receipt.ChangedPathRoot = stringPointer(submission.ChangedPathRoot)
	receipt.FinalTreeSHA256 = stringPointer(submission.FinalTreeSHA256)
	receipt.AuthoringEventRoot = stringPointer(submission.AuthoringEventRoot)
	receipt.AuthoringTranscriptSHA256 = stringPointer(submission.AuthoringTranscriptSHA256)
	receipt.AuthoringTranscriptBytes = submission.AuthoringTranscriptBytes
	receipt.ProtectedPathsIntact = submission.ProtectedPathsIntact
	if len(submission.ChangedPaths) == 0 || transcript.Events == 0 {
		if runErr != nil {
			return finalize(receipt, StatusFailed, StageRun, "coding_run_failed")
		}
		return finalize(receipt, StatusFailed, StageFreeze, "coding_canary_no_authoritative_patch")
	}

	visibleForGrade, err := certifier.openVisibleBundle()
	if err != nil {
		return Receipt{}, errors.New("reopen coding certification visible bundle")
	}
	graderBundle, err := certifier.openGraderBundle()
	if err != nil {
		return Receipt{}, errors.Join(errors.New("open coding certification grader bundle"), visibleForGrade.Close())
	}
	grade := codinggrader.Grade(
		ctx, request.GraderManifest, submission, visibleForGrade, graderBundle, certifier.executor,
	)
	closeErr := errors.Join(visibleForGrade.Close(), graderBundle.Close())
	if closeErr != nil {
		return Receipt{}, errors.Join(errors.New("close coding certification bundles"), closeErr)
	}
	domain := grade.TerminalDomain
	receipt.CanaryTerminalDomain = &domain
	if grade.Evidence != nil {
		receipt.GraderExecutionReceiptRootSHA256 = stringPointer(grade.Evidence.ExecutionReceiptRootSHA256)
	}
	switch grade.TerminalDomain {
	case codingcontract.DomainValidatorInfrastructure, codingcontract.DomainTaskInvalid,
		codingcontract.DomainControlPlaneIntegrity:
		return Receipt{}, fmt.Errorf("coding certification grader is not candidate-attributable: %s/%s", grade.TerminalDomain, pointerValue(grade.FailureCode))
	}
	if runErr != nil {
		if ctx.Err() != nil {
			return Receipt{}, fmt.Errorf("coding run interrupted: %w", ctx.Err())
		}
		return finalize(receipt, StatusFailed, StageRun, "coding_run_failed")
	}
	switch grade.TerminalDomain {
	case codingcontract.DomainResolved:
		return finalizeSuccess(receipt)
	case codingcontract.DomainRepairFailure, codingcontract.DomainCandidateIntegrity:
		return finalize(receipt, StatusFailed, StageGrade, stableFailureCode(pointerValue(grade.FailureCode), "coding_canary_failed"))
	default:
		return Receipt{}, errors.New("coding certification grader returned an unknown terminal domain")
	}
}

func newReceipt(request Request, now time.Time, ttl time.Duration) Receipt {
	return Receipt{
		Schema: CertificationSchema, CodingContractVersion: codingcontract.ContractVersion,
		WeightEligible: false, CertificationID: request.CertificationID,
		AgentArtifactSHA256:  request.AgentArtifactSHA256,
		HarnessInstanceID:    request.HarnessAttestation.HarnessInstanceID,
		CanaryManifestSHA256: request.CanaryManifestSHA256,
		IssuedAtUnix:         now.Unix(), ExpiresAtUnix: now.Add(ttl).Unix(),
		SupportedCodingContractVersions: []int{}, Capabilities: []string{},
		MemoryBundleSHA256:  request.Seed.MemoryBundleSHA256,
		VisibleBundleSHA256: request.RunnerManifest.VisibleBundleSHA256,
		BaseTreeSHA256:      request.RunnerManifest.BaseTreeSHA256,
		GraderPlanSHA256:    request.GraderManifest.GraderPlanSHA256,
	}
}

func finalize(receipt Receipt, status Status, stage Stage, code string) (Receipt, error) {
	receipt.Status = status
	receipt.FailureStage = &stage
	receipt.FailureCode = stringPointer(code)
	if err := receipt.seal(); err != nil {
		return Receipt{}, err
	}
	return receipt, nil
}

func finalizeSuccess(receipt Receipt) (Receipt, error) {
	receipt.Status = StatusCertified
	receipt.FailureStage = nil
	receipt.FailureCode = nil
	if err := receipt.seal(); err != nil {
		return Receipt{}, err
	}
	return receipt, nil
}

func stringPointer(value string) *string {
	return &value
}

func pointerValue(value *string) string {
	if value == nil {
		return ""
	}
	return *value
}

func stableFailureCode(value string, fallback string) string {
	if validIdentifier(value, 128) {
		return value
	}
	return fallback
}
