package codingphase

import (
	"context"
	"encoding/json"
	"errors"
	"math"
	"reflect"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingattempt"
	"github.com/ditto-assistant/dittobench-api/internal/codingcertifier"
	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codinggateway"
	"github.com/ditto-assistant/dittobench-api/internal/codingoutbox"
	"github.com/ditto-assistant/dittobench-api/internal/codingrelay"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
	"github.com/ditto-assistant/dittobench-api/internal/codingsupervisor"
)

func New(config Config) (*Runner, error) {
	if nilLike(config.Attempts) || config.Outbox == nil || nilLike(config.Seeds) ||
		nilLike(config.Harnesses) || nilLike(config.WorkspaceRoutes) || nilLike(config.Inference) ||
		config.InferencePolicy.Validate() != nil || config.CleanupTimeout < 0 ||
		config.CleanupTimeout > maximumCleanupTimeout {
		return nil, ErrInvalidConfig
	}
	switch adapter := config.Attempts.(type) {
	case RuntimeAdapter:
		if adapter.runtime == nil {
			return nil, ErrInvalidConfig
		}
	case *RuntimeAdapter:
		if adapter.runtime == nil {
			return nil, ErrInvalidConfig
		}
	}
	if config.Now == nil {
		config.Now = time.Now
	}
	if config.CleanupTimeout == 0 {
		config.CleanupTimeout = defaultCleanupTimeout
	}
	return &Runner{
		attempts: config.Attempts, outbox: config.Outbox, seeds: config.Seeds,
		harnesses: config.Harnesses, workspaceRoutes: config.WorkspaceRoutes,
		inference: config.Inference, policy: config.InferencePolicy,
		now: config.Now, cleanupTimeout: config.CleanupTimeout,
	}, nil
}

func (runner *Runner) Author(
	ctx context.Context,
	input codingsupervisor.AuthoringInput,
) (codingsupervisor.AuthoringOutcome, error) {
	if runner == nil || ctx == nil || ctx.Err() != nil {
		return codingsupervisor.AuthoringOutcome{}, ErrLifecycle
	}
	now := runner.now().UTC()
	authority, err := parseAuthoringAuthority(input.Request, runner.policy, now)
	if err != nil {
		return codingsupervisor.AuthoringOutcome{}, err
	}
	harness, err := runner.harnesses.Acquire(ctx, HarnessBinding{
		ExecutionID: input.Request.TicketID, AgentID: authority.harness.AgentID,
		RunRowID: authority.harness.RunRowID, AgentArtifactSHA256: authority.manifest.AgentArtifactSHA256,
		TicketID: input.Request.TicketID, CaseID: authority.task.CaseID, Deadline: input.Request.Deadline,
		BenchVersion: authority.harness.BenchVersion, ScreenedImageSHA256: authority.harness.ScreenedImageSHA256,
		ScreenedImageSize: authority.harness.ScreenedImageSizeBytes,
		ScreenedImageID:   authority.harness.ScreenedImageID, ScreenedImageRef: authority.harness.ScreenedImageRef,
		ScreeningPolicyVersion: authority.harness.ScreeningPolicyVersion,
		ImageURL:               authority.harness.ImageURL, ImageExpiresAt: authority.harness.ExpiresAt,
	})
	if err != nil || nilLike(harness) || !validIdentifier(harness.InstanceID(), 256) || nilLike(harness.Client()) {
		var destroyErr error
		if !nilLike(harness) {
			cleanupContext, cancel := runner.cleanupContext(ctx)
			destroyErr = harness.Destroy(cleanupContext)
			cancel()
		}
		return codingsupervisor.AuthoringOutcome{}, errors.Join(ErrLifecycle, err, destroyErr)
	}

	binding := codingoutbox.Binding{
		Purpose: codingoutbox.PurposeShadowAttempt, ExecutionID: input.Request.TicketID,
		AgentArtifactSHA256: authority.manifest.AgentArtifactSHA256,
		HarnessInstanceID:   harness.InstanceID(), AuthoritySHA256: authority.lease.RunManifestSHA256,
		HarnessAuthoritySHA256: authority.harnessSHA256,
		ScreenedImageSHA256:    authority.harness.ScreenedImageSHA256,
		TicketID:               input.Request.TicketID, CaseID: authority.task.CaseID,
		ProfileCapabilityID: authority.task.ProfileCapabilityID, Deadline: input.Request.Deadline,
	}
	attempt, err := runner.outbox.Reserve(ctx, binding, authority.runnerManifest.Limits)
	if err != nil {
		return codingsupervisor.AuthoringOutcome{}, runner.destroyHarness(ctx, harness, err)
	}
	writer, err := attempt.BeginTranscript(ctx)
	if err != nil {
		return codingsupervisor.AuthoringOutcome{}, runner.destroyHarness(ctx, harness, err)
	}
	spec := codingattempt.AuthoringSpec{
		Binding: codingattempt.Binding{
			TicketID: input.Request.TicketID, CaseID: authority.task.CaseID,
			ProfileCapabilityID: authority.task.ProfileCapabilityID, Deadline: input.Request.Deadline,
		},
		VisibleBundle: authority.visible, MemoryBundle: authority.memory, ResourceProfile: authority.resource,
		RunnerManifest: authority.runnerManifest, CandidateLimits: authority.runnerManifest.Limits,
		MemoryBundleSHA256:    authority.task.MemoryBundleSHA256,
		ResourceProfileSHA256: authority.task.ResourceProfileSHA256,
	}
	session, err := runner.attempts.BeginAuthoring(ctx, spec)
	if err != nil {
		return codingsupervisor.AuthoringOutcome{}, errors.Join(ErrLifecycle, err, writer.Abort(), runner.destroyHarness(ctx, harness, nil))
	}

	state := &authoringState{attempt: attempt, writer: writer, session: session, harness: harness}
	client := harness.Client()
	primary := harness.Activate(ctx)
	var health codingcertifier.HealthResponse
	var healthErr error
	if primary == nil {
		health, healthErr = client.Health(ctx)
		if healthErr != nil || !health.SupportsCodingV1() {
			primary = errors.Join(ErrLifecycle, healthErr)
		}
	}
	if primary == nil {
		_, primary = runner.seeds.Deliver(ctx, client, session.SeedProjection())
	}
	var grant parsedGrant
	if primary == nil {
		grant, primary = parseGrant(
			input.Request.Grant, input.Request, authority, runner.policy, harness.InstanceID(), input.SessionID,
			input.BrokerPublicKey, input.BrokerPrivateKey, runner.now().UTC(),
		)
	}
	if primary == nil {
		activation := InferenceActivation{
			Policy: runner.policy, Capability: grant.capability, Revocation: grant.revocation,
			Authorizer: markerAuthorizer{
				outbox: runner.outbox, binding: binding, relay: grant.capability.Binding,
			},
		}
		state.gateway, primary = runner.inference.Activate(ctx, activation)
		zero(grant.capability.BrokerPrivateKey)
		if primary == nil && nilLike(state.gateway) {
			primary = ErrLifecycle
		}
	}
	if primary == nil {
		state.inferenceURL, primary = state.gateway.URL()
	}
	if primary == nil {
		published, publishErr := runner.workspaceRoutes.Publish(ctx, codingcertifier.CapabilityBinding{
			HarnessInstanceID: harness.InstanceID(), AgentArtifactSHA256: authority.manifest.AgentArtifactSHA256,
			TicketID: input.Request.TicketID, CaseID: authority.task.CaseID,
			ProfileCapabilityID: authority.task.ProfileCapabilityID,
		}, session.Handler())
		if !nilLike(published) {
			state.workspace = published
		}
		if publishErr != nil || nilLike(published) {
			primary = errors.Join(ErrLifecycle, publishErr)
		} else {
			state.workspaceURL = published.URL()
		}
	}
	if primary == nil {
		runRequest := codingcontract.RunRequest{
			CodingContractVersion: codingcontract.ContractVersion,
			TicketID:              input.Request.TicketID, CaseID: authority.task.CaseID,
			ProfileCapabilityID: authority.task.ProfileCapabilityID,
			RepositoryEpoch:     authority.lease.RepositoryEpoch,
			VisibleBundleSHA256: authority.task.VisibleBundleSHA256,
			Issue:               authority.lease.Issue, RuntimePolicy: authority.lease.RuntimePolicy,
			WorkspaceCapabilityURL: state.workspaceURL, InferenceBaseURL: state.inferenceURL,
			Budgets: authority.lease.Budgets,
		}
		if runRequest.Validate() != nil {
			primary = ErrInvalid
		} else {
			_, primary = client.Run(ctx, runRequest)
		}
	}
	return runner.finalizeAuthoring(ctx, input.Request, authority, state, grant.capability.Binding, primary)
}

type authoringState struct {
	attempt      *codingoutbox.Attempt
	writer       codingoutbox.TranscriptWriter
	session      AuthoringSession
	harness      Harness
	workspace    codingcertifier.PublishedCapability
	gateway      InferenceGateway
	workspaceURL string
	inferenceURL string
}

func (runner *Runner) finalizeAuthoring(
	ctx context.Context,
	request codingsupervisor.Request,
	authority authoringAuthority,
	state *authoringState,
	relayBinding codingrelay.Binding,
	primary error,
) (codingsupervisor.AuthoringOutcome, error) {
	cleanupContext, cancel := runner.cleanupContext(ctx)
	revocations := revocationSet{
		workspace: state.workspace, inference: state.gateway,
	}
	result, freezeErr := state.session.Freeze(cleanupContext, revocations)
	cancel()
	if freezeErr != nil {
		retryContext, retryCancel := runner.cleanupContext(ctx)
		freezeErr = errors.Join(freezeErr, revocations.Revoke(retryContext))
		retryCancel()
	}

	identity, transcriptErr := state.session.WriteTranscript(state.writer)
	var transcript codingoutbox.TranscriptArtifact
	if transcriptErr == nil {
		commitContext, commitCancel := runner.cleanupContext(ctx)
		transcript, transcriptErr = state.writer.Commit(commitContext, identity)
		commitCancel()
	}
	if transcriptErr != nil {
		transcriptErr = errors.Join(transcriptErr, state.writer.Abort())
	}

	var frozen codingoutbox.FrozenArtifact
	var persistErr error
	if transcriptErr == nil {
		persistContext, persistCancel := runner.cleanupContext(ctx)
		if result.Submission != nil {
			frozen, persistErr = state.attempt.StoreFrozen(persistContext, *result.Submission)
			if persistErr == nil {
				_, persistErr = state.attempt.Seal(persistContext, result)
			}
		} else if result.Failure != nil {
			_, persistErr = state.attempt.Seal(persistContext, result)
		} else {
			persistErr = ErrLifecycle
		}
		persistCancel()
	}

	var model codingcontract.ModelEvidence
	var evidenceErr error
	if state.gateway != nil && freezeErr == nil {
		evidenceContext, evidenceCancel := runner.cleanupContext(ctx)
		model, evidenceErr = state.gateway.Evidence(evidenceContext, evidenceBinding(relayBinding))
		evidenceCancel()
		if evidenceErr == nil {
			evidenceErr = validateModelEvidence(runner.policy, relayBinding, model)
		}
	}

	cleanupErr := runner.closeAuthoring(ctx, request.TicketID, state)
	allErr := errors.Join(primary, freezeErr, transcriptErr, persistErr, evidenceErr, cleanupErr)
	if allErr != nil || result.Submission == nil {
		return codingsupervisor.AuthoringOutcome{}, errors.Join(ErrLifecycle, allErr)
	}
	evidence, err := buildAuthoringEvidence(model, *result.Submission)
	if err != nil {
		return codingsupervisor.AuthoringOutcome{}, errors.Join(ErrLifecycle, err)
	}
	body, err := codingcontract.AuthoringEvidenceJSON(evidence)
	if err != nil {
		return codingsupervisor.AuthoringOutcome{}, errors.Join(ErrLifecycle, err)
	}
	return codingsupervisor.AuthoringOutcome{
		Evidence: body, AuthoringTranscriptObjectKey: transcript.ObjectKey,
		AuthoringTranscriptBytes: transcript.SizeBytes, AuthoringEventCount: transcript.Events,
		FrozenSubmissionObjectKey: frozen.ObjectKey, CapabilitiesRevoked: true,
		AuthoringEnvironmentDestroyed: true,
	}, nil
}

func (runner *Runner) Grade(
	ctx context.Context,
	request codingsupervisor.Request,
) (codingsupervisor.GradingOutcome, error) {
	if runner == nil || ctx == nil || ctx.Err() != nil {
		return codingsupervisor.GradingOutcome{}, ErrLifecycle
	}
	authority, err := parseGradingAuthority(request, runner.policy, runner.now().UTC())
	if err != nil {
		return codingsupervisor.GradingOutcome{}, err
	}
	attempt, record, err := runner.outbox.Lookup(ctx, codingoutbox.PurposeShadowAttempt, request.TicketID)
	if err != nil || !recordMatchesGrading(record, authority, request) {
		return codingsupervisor.GradingOutcome{}, errors.Join(ErrLifecycle, err)
	}
	submission, err := runner.outbox.LoadFrozen(ctx, attempt.ID())
	if err != nil || !submissionMatchesAuthoring(submission, record, authority) {
		return codingsupervisor.GradingOutcome{}, errors.Join(ErrLifecycle, err)
	}
	result, err := runner.attempts.Grade(ctx, authority.spec, submission)
	if err != nil {
		return codingsupervisor.GradingOutcome{}, errors.Join(ErrLifecycle, err)
	}
	evidence := codingcontract.TaskEvidence{
		Schema: "dittobench-coding-task-evidence-v1", CodingContractVersion: codingcontract.ContractVersion,
		WeightEligible: false, CodingRunID: authority.manifest.CodingRunID,
		ValidatorTicketID: request.TicketID, AgentID: authority.manifest.AgentID,
		AgentArtifactSHA256: authority.manifest.AgentArtifactSHA256,
		CorpusReleaseID:     authority.manifest.CorpusReleaseID, TaskSetID: authority.manifest.TaskSetID,
		TaskSetManifestSHA256: authority.manifest.TaskSetManifestSHA256, Task: authority.task,
		Authoring: &authority.authoring, Grader: result.Evidence,
		TerminalDomain: result.TerminalDomain, FailureCode: cloneString(result.FailureCode),
		RepairScoreMicros: result.RepairScoreMicros,
	}
	body, err := codingcontract.TaskEvidenceJSON(authority.manifest, request.TicketID, evidence)
	if err != nil {
		return codingsupervisor.GradingOutcome{}, errors.Join(ErrLifecycle, err)
	}
	return codingsupervisor.GradingOutcome{
		TaskEvidence: []json.RawMessage{body}, GradingEnvironmentDestroyed: true,
	}, nil
}

func (runner *Runner) AbortAuthoring(ctx context.Context, request codingsupervisor.Request) error {
	if runner == nil || ctx == nil || ctx.Err() != nil {
		return ErrLifecycle
	}
	attempt, record, err := runner.outbox.Lookup(ctx, codingoutbox.PurposeShadowAttempt, request.TicketID)
	if errors.Is(err, codingoutbox.ErrInvalid) {
		return nil
	}
	if err != nil {
		return errors.Join(ErrLifecycle, err)
	}
	switch record.State {
	case codingoutbox.StateReserved:
		writer, beginErr := attempt.BeginTranscript(ctx)
		if beginErr != nil {
			return errors.Join(ErrLifecycle, beginErr)
		}
		return writer.Abort()
	case codingoutbox.StateExpired, codingoutbox.StateReady,
		codingoutbox.StateTerminalWithoutPatch, codingoutbox.StateReleased:
		return nil
	default:
		return ErrRecovery
	}
}

func (runner *Runner) AbortGrading(ctx context.Context, request codingsupervisor.Request) error {
	if runner == nil || ctx == nil || ctx.Err() != nil {
		return ErrLifecycle
	}
	_, _, err := runner.outbox.Lookup(ctx, codingoutbox.PurposeShadowAttempt, request.TicketID)
	if errors.Is(err, codingoutbox.ErrInvalid) {
		return nil
	}
	return err
}

func (runner *Runner) Recover(
	ctx context.Context,
	request codingsupervisor.Request,
) (codingsupervisor.RecoveryOutcome, error) {
	if runner == nil || ctx == nil || ctx.Err() != nil {
		return codingsupervisor.RecoveryOutcome{}, ErrLifecycle
	}
	_, record, err := runner.outbox.Lookup(ctx, codingoutbox.PurposeShadowAttempt, request.TicketID)
	if errors.Is(err, codingoutbox.ErrInvalid) {
		return codingsupervisor.RecoveryOutcome{State: "none"}, nil
	}
	if err != nil {
		return codingsupervisor.RecoveryOutcome{}, errors.Join(ErrLifecycle, err)
	}
	if record.Binding.TicketID != request.TicketID || !record.Binding.Deadline.Equal(request.Deadline) {
		return codingsupervisor.RecoveryOutcome{}, ErrInvalid
	}
	cleanupContext, cancel := runner.cleanupContext(ctx)
	revokeErr := runner.revokeLiveGateway(cleanupContext, request.TicketID)
	cancel()
	if revokeErr != nil {
		return codingsupervisor.RecoveryOutcome{}, errors.Join(ErrLifecycle, revokeErr)
	}
	switch record.State {
	case codingoutbox.StateReserved:
		return codingsupervisor.RecoveryOutcome{State: "ambiguous"}, nil
	case codingoutbox.StateExpired:
		return codingsupervisor.RecoveryOutcome{State: "expired"}, nil
	case codingoutbox.StateReleased:
		return codingsupervisor.RecoveryOutcome{State: "released"}, nil
	case codingoutbox.StateCollecting:
		return codingsupervisor.RecoveryOutcome{State: "ambiguous"}, nil
	case codingoutbox.StateReady, codingoutbox.StateTerminalWithoutPatch:
		if outcome, ok := pendingRecovery(record); ok {
			return outcome, nil
		}
		return codingsupervisor.RecoveryOutcome{State: "ambiguous"}, nil
	default:
		return codingsupervisor.RecoveryOutcome{}, ErrRecovery
	}
}

func buildAuthoringEvidence(
	model codingcontract.ModelEvidence,
	submission codingrunner.FrozenSubmission,
) (codingcontract.AuthoringEvidence, error) {
	if len(submission.ChangedPaths) > 10_000 {
		return codingcontract.AuthoringEvidence{}, ErrInvalid
	}
	changedBytes, err := submissionChangedBytes(submission)
	if err != nil {
		return codingcontract.AuthoringEvidence{}, err
	}
	evidence := codingcontract.AuthoringEvidence{
		Model: model, AuthoringEventRoot: submission.AuthoringEventRoot,
		AuthoringTranscriptSHA256: submission.AuthoringTranscriptSHA256,
		FrozenPatchSHA256:         submission.FrozenPatchSHA256, ChangedPathRoot: submission.ChangedPathRoot,
		FinalTreeSHA256: submission.FinalTreeSHA256, ChangedPathCount: uint32(len(submission.ChangedPaths)),
		ChangedBytes: changedBytes, ProtectedPathsIntact: submission.ProtectedPathsIntact,
	}
	if err := evidence.Validate(); err != nil {
		return codingcontract.AuthoringEvidence{}, err
	}
	return evidence, nil
}

func validateModelEvidence(
	policy codingcontract.InferencePolicy,
	binding codingrelay.Binding,
	evidence codingcontract.ModelEvidence,
) error {
	costBudget := binding.CostBudgetUSDMicros
	if costBudget == 0 {
		costBudget = policy.MaxCostUSDMicros
	}
	if _, err := codingcontract.InferenceModelEvidenceSHA256(policy, evidence); err != nil ||
		evidence.Requests > uint64(binding.RequestBudget) ||
		evidence.PromptTokens > binding.PromptTokenBudget ||
		evidence.CompletionTokens > binding.CompletionTokenBudget ||
		evidence.CostUSDMicros > costBudget {
		return ErrInvalid
	}
	return nil
}

func recordMatchesGrading(
	record codingoutbox.Record,
	authority gradingAuthority,
	request codingsupervisor.Request,
) bool {
	binding := record.Binding
	return binding.Purpose == codingoutbox.PurposeShadowAttempt && binding.ExecutionID == request.TicketID &&
		binding.AgentArtifactSHA256 == authority.manifest.AgentArtifactSHA256 &&
		binding.AuthoritySHA256 == authority.lease.RunManifestSHA256 && binding.TicketID == request.TicketID &&
		binding.CaseID == authority.task.CaseID && binding.ProfileCapabilityID == authority.task.ProfileCapabilityID &&
		binding.Deadline.Equal(request.Deadline) &&
		(record.State == codingoutbox.StateReady || record.State == codingoutbox.StateReleased) &&
		record.Transcript != nil && record.Frozen != nil
}

func submissionMatchesAuthoring(
	submission codingrunner.FrozenSubmission,
	record codingoutbox.Record,
	authority gradingAuthority,
) bool {
	changedBytes, err := submissionChangedBytes(submission)
	return err == nil && record.Transcript != nil && record.Frozen != nil &&
		authority.outcome.AuthoringTranscriptObjectKey == record.Transcript.ObjectKey &&
		authority.outcome.AuthoringTranscriptBytes == record.Transcript.SizeBytes &&
		authority.outcome.AuthoringEventCount == record.Transcript.Events &&
		authority.outcome.FrozenSubmissionObjectKey == record.Frozen.Artifact.ObjectKey &&
		submission.FrozenPatchSHA256 == authority.authoring.FrozenPatchSHA256 &&
		submission.ChangedPathRoot == authority.authoring.ChangedPathRoot &&
		submission.FinalTreeSHA256 == authority.authoring.FinalTreeSHA256 &&
		submission.AuthoringEventRoot == authority.authoring.AuthoringEventRoot &&
		submission.AuthoringTranscriptSHA256 == authority.authoring.AuthoringTranscriptSHA256 &&
		uint32(len(submission.ChangedPaths)) == authority.authoring.ChangedPathCount &&
		changedBytes == authority.authoring.ChangedBytes &&
		submission.ProtectedPathsIntact == authority.authoring.ProtectedPathsIntact
}

func submissionChangedBytes(submission codingrunner.FrozenSubmission) (uint64, error) {
	var changedBytes uint64
	for _, change := range submission.Changes {
		if uint64(len(change.AfterContent)) > math.MaxUint64-changedBytes {
			return 0, ErrInvalid
		}
		changedBytes += uint64(len(change.AfterContent))
	}
	return changedBytes, nil
}

func pendingRecovery(record codingoutbox.Record) (codingsupervisor.RecoveryOutcome, bool) {
	publications := []struct {
		state string
		stage codingoutbox.PublicationStage
		value *codingoutbox.PublicationRecord
	}{
		{"terminal_pending", codingoutbox.PublicationTerminalResult, record.TerminalPublication},
		{"authoring_pending", codingoutbox.PublicationAuthoringFreeze, record.AuthoringPublication},
	}
	for _, publication := range publications {
		if publication.value == nil || publication.value.Acknowledgement != nil {
			continue
		}
		stage, digest := string(publication.stage), publication.value.Request.SHA256
		if lowerSHA256(digest) {
			return codingsupervisor.RecoveryOutcome{
				State: publication.state, PublicationStage: &stage, RequestSHA256: &digest,
			}, true
		}
	}
	return codingsupervisor.RecoveryOutcome{}, false
}

func evidenceBinding(binding codingrelay.Binding) codingrelay.EvidenceBinding {
	return codingrelay.EvidenceBinding{
		AttemptID: binding.AttemptID, AgentArtifactSHA256: binding.AgentArtifactSHA256,
		HarnessInstanceID: binding.HarnessInstanceID, TicketID: binding.TicketID,
		CaseID: binding.CaseID, ProfileCapabilityID: binding.ProfileCapabilityID,
		InferenceGrantSHA256: binding.InferenceGrantSHA256, Deadline: binding.Deadline,
		RequestBudget: binding.RequestBudget, PromptTokenBudget: binding.PromptTokenBudget,
		CompletionTokenBudget: binding.CompletionTokenBudget, CostBudgetUSDMicros: binding.CostBudgetUSDMicros,
	}
}

type markerAuthorizer struct {
	outbox  *codingoutbox.Store
	binding codingoutbox.Binding
	relay   codingrelay.Binding
}

func (authorizer markerAuthorizer) Authorize(
	ctx context.Context,
	observed codinggateway.CapabilityBinding,
) error {
	_, record, err := authorizer.outbox.Lookup(ctx, authorizer.binding.Purpose, authorizer.binding.ExecutionID)
	if err != nil || record.State != codingoutbox.StateCollecting || record.Binding != authorizer.binding ||
		observed.AttemptID != authorizer.relay.AttemptID ||
		observed.AgentArtifactSHA256 != authorizer.relay.AgentArtifactSHA256 ||
		observed.HarnessInstanceID != authorizer.relay.HarnessInstanceID ||
		observed.TicketID != authorizer.relay.TicketID || observed.CaseID != authorizer.relay.CaseID ||
		observed.ProfileCapabilityID != authorizer.relay.ProfileCapabilityID ||
		observed.GrantID != authorizer.relay.GrantID || observed.Generation != authorizer.relay.Generation ||
		observed.InferenceGrantSHA256 != authorizer.relay.InferenceGrantSHA256 ||
		!observed.IssuedAt.Equal(authorizer.relay.IssuedAt) || !observed.Deadline.Equal(authorizer.relay.Deadline) ||
		observed.RequestBudget != authorizer.relay.RequestBudget ||
		observed.PromptTokenBudget != authorizer.relay.PromptTokenBudget ||
		observed.CompletionTokenBudget != authorizer.relay.CompletionTokenBudget {
		return ErrLifecycle
	}
	return nil
}

type revocationSet struct {
	workspace codingcertifier.PublishedCapability
	inference InferenceGateway
}

func (set revocationSet) Revoke(ctx context.Context) error {
	var workspaceErr, inferenceErr error
	if !nilLike(set.workspace) {
		workspaceErr = set.workspace.Revoke(ctx)
	}
	if !nilLike(set.inference) {
		inferenceErr = set.inference.Revoke(ctx)
	}
	return errors.Join(workspaceErr, inferenceErr)
}

func (runner *Runner) closeAuthoring(ctx context.Context, ticketID string, state *authoringState) error {
	var workspaceErr, gatewayErr, sessionErr, harnessErr error
	if !nilLike(state.workspace) {
		workspaceErr = state.workspace.Close()
	}
	if !nilLike(state.gateway) {
		gatewayErr = state.gateway.Close()
		if errors.Is(gatewayErr, codinggateway.ErrNotRevoked) {
			runner.keepLiveGateway(ticketID, state.gateway)
		}
	}
	if state.session != nil {
		sessionErr = state.session.Close()
	}
	if !nilLike(state.harness) {
		cleanupContext, cancel := runner.cleanupContext(ctx)
		harnessErr = state.harness.Destroy(cleanupContext)
		cancel()
	}
	return errors.Join(workspaceErr, gatewayErr, sessionErr, harnessErr)
}

func (runner *Runner) keepLiveGateway(ticketID string, gateway InferenceGateway) {
	if runner == nil || ticketID == "" || nilLike(gateway) {
		return
	}
	runner.mu.Lock()
	if runner.liveGateways == nil {
		runner.liveGateways = map[string]InferenceGateway{}
	}
	runner.liveGateways[ticketID] = gateway
	runner.mu.Unlock()
}

func (runner *Runner) revokeLiveGateway(ctx context.Context, ticketID string) error {
	if runner == nil {
		return nil
	}
	runner.mu.Lock()
	gateway := runner.liveGateways[ticketID]
	delete(runner.liveGateways, ticketID)
	runner.mu.Unlock()
	if nilLike(gateway) {
		return nil
	}
	if err := gateway.Revoke(ctx); err != nil {
		runner.keepLiveGateway(ticketID, gateway)
		return err
	}
	return gateway.Close()
}

func (runner *Runner) destroyHarness(ctx context.Context, harness Harness, primary error) error {
	cleanupContext, cancel := runner.cleanupContext(ctx)
	destroyErr := harness.Destroy(cleanupContext)
	cancel()
	return errors.Join(ErrLifecycle, primary, destroyErr)
}

func (runner *Runner) cleanupContext(parent context.Context) (context.Context, context.CancelFunc) {
	return context.WithTimeout(context.WithoutCancel(parent), runner.cleanupTimeout)
}

func cloneString(value *string) *string {
	if value == nil {
		return nil
	}
	copy := *value
	return &copy
}

func zero(value []byte) {
	for index := range value {
		value[index] = 0
	}
}

func nilLike(value any) bool {
	if value == nil {
		return true
	}
	reflected := reflect.ValueOf(value)
	switch reflected.Kind() {
	case reflect.Chan, reflect.Func, reflect.Interface, reflect.Map, reflect.Pointer, reflect.Slice:
		return reflected.IsNil()
	default:
		return false
	}
}

var _ codingsupervisor.PhaseRunner = (*Runner)(nil)
var _ codinggateway.ActivationAuthorizer = markerAuthorizer{}
var _ codingattempt.CapabilityRevoker = revocationSet{}
