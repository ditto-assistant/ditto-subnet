package codingcanary

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"strings"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcertifier"
	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codingexecutor"
	"github.com/ditto-assistant/dittobench-api/internal/codinggateway"
	"github.com/ditto-assistant/dittobench-api/internal/codingharness"
	"github.com/ditto-assistant/dittobench-api/internal/codingoutbox"
	"github.com/ditto-assistant/dittobench-api/internal/codingphase"
	"github.com/ditto-assistant/dittobench-api/internal/codingplatform"
	"github.com/ditto-assistant/dittobench-api/internal/codingrelay"
)

const (
	certificationTTL  = time.Hour
	publicCanaryEpoch = "practice-ledger-v2"
)

type canaryHarnesses interface {
	AcquireCanary(context.Context, codingharness.CanaryBinding) (codingphase.Harness, error)
}

type BackendConfig struct {
	Pack        PublicPack
	Harnesses   canaryHarnesses
	Publisher   codingcertifier.CapabilityPublisher
	Executors   *codingexecutor.PhaseFactory
	Outbox      *codingoutbox.Store
	Inference   codingphase.InferenceActivator
	Policy      codingcontract.InferencePolicy
	ImageDigest string
	Now         func() time.Time
}

type certifierBackend struct {
	pack        PublicPack
	harnesses   canaryHarnesses
	publisher   codingcertifier.CapabilityPublisher
	executors   *codingexecutor.PhaseFactory
	outbox      *codingoutbox.Store
	inference   codingphase.InferenceActivator
	policy      codingcontract.InferencePolicy
	imageDigest string
	now         func() time.Time
}

func NewBackend(config BackendConfig) (Backend, error) {
	if config.Pack.CanaryManifestSHA256 == "" || config.Pack.TaskID != publicCanaryTaskID ||
		config.Harnesses == nil || config.Publisher == nil || config.Executors == nil ||
		config.Outbox == nil || config.Inference == nil || config.Policy.Validate() != nil ||
		!strings.HasPrefix(config.ImageDigest, "sha256:") ||
		len(strings.TrimPrefix(config.ImageDigest, "sha256:")) != 64 {
		return nil, ErrInvalidConfig
	}
	now := config.Now
	if now == nil {
		now = time.Now
	}
	return &certifierBackend{
		pack: config.Pack, harnesses: config.Harnesses, publisher: config.Publisher,
		executors: config.Executors, outbox: config.Outbox, inference: config.Inference,
		policy: config.Policy, imageDigest: config.ImageDigest, now: now,
	}, nil
}

func (backend *certifierBackend) Certify(ctx context.Context, request Request) (outcome Outcome, err error) {
	outcome = Outcome{LeaseID: request.LeaseID, CapabilitiesRevoked: true, HarnessDestroyed: true}
	if backend == nil || ctx == nil || ctx.Err() != nil {
		return outcome, ErrInvalid
	}
	if !backend.pack.Matches(request) || request.LeaseID == "" {
		return outcome, ErrInvalid
	}
	now := backend.now().UTC()
	plans, err := backend.pack.executionPlans(now, request.Deadline, request.LeaseID, backend.imageDigest)
	if err != nil {
		return outcome, err
	}
	harness, err := backend.harnesses.AcquireCanary(ctx, codingharness.CanaryBinding{
		LeaseID: request.LeaseID, AgentID: request.AgentID, AgentArtifactSHA256: request.AgentArtifactSHA256,
		Deadline: request.Deadline, BenchVersion: request.BenchVersion, ScreenedImageSHA256: request.ScreenedImageSHA256,
		ScreenedImageSize: request.ScreenedImageSizeBytes, ScreenedImageID: request.ScreenedImageID,
		ScreenedImageRef: request.ScreenedImageRef, ScreeningPolicyVersion: request.ScreeningPolicyVersion,
		ImageURL: request.ImageURL, ImageExpiresAt: request.ImageExpiresAt,
	})
	if err != nil || harness == nil {
		return outcome, errors.Join(ErrUnavailable, err)
	}
	defer func() {
		cleanup, cancel := context.WithTimeout(context.WithoutCancel(ctx), 30*time.Second)
		destroyErr := harness.Destroy(cleanup)
		cancel()
		if destroyErr != nil {
			outcome.HarnessDestroyed = false
			err = errors.Join(err, destroyErr)
		}
	}()
	attempt, err := backend.outbox.Reserve(ctx, codingoutbox.Binding{
		Purpose: codingoutbox.PurposeCertification, ExecutionID: request.LeaseID,
		AgentArtifactSHA256: request.AgentArtifactSHA256, HarnessInstanceID: harness.InstanceID(),
		AuthoritySHA256: backend.pack.CanaryManifestSHA256, TicketID: request.LeaseID,
		CaseID: publicCanaryTaskID, ProfileCapabilityID: publicCanaryProfileID, Deadline: request.Deadline,
	}, plans.runner.Limits)
	if err != nil {
		return outcome, errors.Join(ErrUnavailable, err)
	}
	if err := harness.Activate(ctx); err != nil {
		return outcome, errors.Join(ErrUnavailable, err)
	}
	executor, err := backend.executors.Certification(ctx, plans.grader)
	if err != nil {
		return outcome, errors.Join(ErrUnavailable, err)
	}
	seed, err := publicCanarySeed(request.LeaseID)
	if err != nil {
		return outcome, err
	}
	policySHA, err := codingcontract.InferencePolicySHA256(backend.policy)
	requestBudget := codingcontract.EffectiveInferenceRequestBudget(16)
	if err != nil || request.Grant.InferenceGrantSHA256 != policySHA ||
		request.Grant.RequestBudget != requestBudget ||
		request.Grant.PromptTokenBudget != 10_000 ||
		request.Grant.CompletionTokenBudget != 2_000 ||
		request.Grant.CostBudgetUSDMicros != backend.policy.MaxCostUSDMicros {
		return outcome, ErrInvalid
	}
	privateKey, ok := decodeBrokerPrivateKey(request.Grant.BrokerPrivateKey)
	if !ok {
		return outcome, ErrInvalid
	}
	relayBinding := codingrelay.Binding{
		AttemptID: request.LeaseID, AgentArtifactSHA256: request.AgentArtifactSHA256,
		HarnessInstanceID: harness.InstanceID(), TicketID: request.LeaseID, CaseID: publicCanaryTaskID,
		ProfileCapabilityID: publicCanaryProfileID, GrantID: request.Grant.GrantID,
		Generation: request.Grant.Generation, InferenceGrantSHA256: request.Grant.InferenceGrantSHA256,
		IssuedAt: now, Deadline: request.Deadline, RequestBudget: request.Grant.RequestBudget,
		PromptTokenBudget: request.Grant.PromptTokenBudget, CompletionTokenBudget: request.Grant.CompletionTokenBudget,
		CostBudgetUSDMicros: request.Grant.CostBudgetUSDMicros,
	}
	activation := codingphase.InferenceActivation{
		Policy: backend.policy,
		Capability: codingplatform.GrantCapability{
			Binding: relayBinding, Bearer: request.Grant.Bearer, BrokerPublicKey: request.Grant.BrokerPublicKey,
			BrokerPrivateKey: ed25519.PrivateKey(append([]byte(nil), privateKey...)), ProxyURL: request.Grant.ProxyURL,
		},
		Revocation: codingplatform.RevocationCapability{
			GrantID: request.Grant.GrantID, TicketID: request.LeaseID, Generation: request.Grant.Generation,
			InferenceGrantSHA256: request.Grant.InferenceGrantSHA256, Deadline: request.Grant.ExpiresAt,
			Bearer: request.Grant.RevokeBearer, URL: request.Grant.RevokeURL,
		},
		Authorizer: reservedAuthorizer{
			outbox: backend.outbox,
			binding: codingoutbox.Binding{
				Purpose: codingoutbox.PurposeCertification, ExecutionID: request.LeaseID,
				AgentArtifactSHA256: request.AgentArtifactSHA256, HarnessInstanceID: harness.InstanceID(),
				AuthoritySHA256: backend.pack.CanaryManifestSHA256, TicketID: request.LeaseID,
				CaseID: publicCanaryTaskID, ProfileCapabilityID: publicCanaryProfileID, Deadline: request.Deadline,
			},
			relay: relayBinding,
		},
	}
	gateway, err := backend.inference.Activate(ctx, activation)
	zeroBytes(privateKey)
	zeroBytes(activation.Capability.BrokerPrivateKey)
	activation.Capability.Bearer = ""
	activation.Capability.ProxyURL = ""
	activation.Revocation.Bearer = ""
	activation.Revocation.URL = ""
	if err != nil || gateway == nil {
		return outcome, errors.Join(ErrUnavailable, err)
	}
	defer func() {
		cleanup, cancel := context.WithTimeout(context.WithoutCancel(ctx), 30*time.Second)
		revokeErr := gateway.Revoke(cleanup)
		closeErr := gateway.Close()
		cancel()
		if revokeErr != nil {
			outcome.CapabilitiesRevoked = false
			err = errors.Join(err, revokeErr)
		}
		if closeErr != nil && !errors.Is(closeErr, codinggateway.ErrNotRevoked) {
			err = errors.Join(err, closeErr)
		}
	}()
	inferenceURL, err := gateway.URL()
	if err != nil || inferenceURL == "" {
		return outcome, errors.Join(ErrUnavailable, err)
	}
	certRequest := codingcertifier.Request{
		CertificationID: request.LeaseID, AgentArtifactSHA256: request.AgentArtifactSHA256,
		HarnessAttestation: codingcertifier.HarnessAttestation{
			HarnessInstanceID: harness.InstanceID(), AgentArtifactSHA256: request.AgentArtifactSHA256,
			ReadOnlyRootFilesystem: true, NetworkPolicyEnforced: true, CapabilityEgressOnly: true,
			NoHostDockerSocket: true, CredentialsAbsent: true,
		},
		Seed: seed, Issue: codingcontract.Issue{
			Title: backend.pack.IssueTitle, Description: backend.pack.IssueDescription, Constraints: []string{},
		},
		RepositoryEpoch: publicCanaryEpoch, InferenceBaseURL: inferenceURL,
		InferenceGrantSHA256: request.Grant.InferenceGrantSHA256, SolverModel: codingcertifier.CertificationSolverModel,
		SolverProvider: backend.policy.ProviderAPI, ProviderRouteProfile: backend.policy.ProviderRouteProfile,
		Budgets: codingcontract.Budgets{
			ModelInputTokens: 10_000, ModelOutputTokens: 2_000, WorkspaceToolCalls: 16, WallTimeSeconds: 60,
		},
		RunnerManifest: plans.runner, GraderManifest: plans.grader,
	}
	certRequest.CanaryManifestSHA256, err = codingcertifier.CanaryManifestSHA256(certRequest)
	if err != nil {
		return outcome, err
	}
	certifier, err := codingcertifier.New(codingcertifier.Config{
		Harness: harness.Client(), Publisher: backend.publisher, Executor: executor,
		TranscriptSink: outboxTranscriptSink{attempt: attempt}, FrozenSubmissionSink: outboxFrozenSink{attempt: attempt},
		InferenceEvidence: canaryInference{gateway: gateway, costBudgetUSDMicros: request.Grant.CostBudgetUSDMicros},
		OpenVisibleBundle: bytesOpener(plans.visible), OpenGraderBundle: bytesOpener(plans.graderBundle),
		CertificationTTL: certificationTTL, Now: backend.now,
	})
	if err != nil {
		return outcome, err
	}
	receipt, err := certifier.Certify(ctx, certRequest)
	if err != nil {
		return outcome, err
	}
	outcome.Receipt = receipt
	return outcome, nil
}

type reservedAuthorizer struct {
	outbox  *codingoutbox.Store
	binding codingoutbox.Binding
	relay   codingrelay.Binding
}

func (authorizer reservedAuthorizer) Authorize(
	ctx context.Context,
	observed codinggateway.CapabilityBinding,
) error {
	_, record, err := authorizer.outbox.Lookup(ctx, authorizer.binding.Purpose, authorizer.binding.ExecutionID)
	if err != nil || record.State != codingoutbox.StateReserved || record.Binding != authorizer.binding ||
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
		return ErrUnavailable
	}
	return nil
}

type canaryInference struct {
	gateway             codingphase.InferenceGateway
	costBudgetUSDMicros uint64
}

func (source canaryInference) Evidence(
	ctx context.Context,
	binding codingcertifier.InferenceBinding,
) (codingcontract.ModelEvidence, error) {
	if err := source.gateway.Revoke(ctx); err != nil {
		return codingcontract.ModelEvidence{}, err
	}
	return source.gateway.Evidence(ctx, codingrelay.EvidenceBinding{
		AttemptID: binding.CertificationID, AgentArtifactSHA256: binding.AgentArtifactSHA256,
		HarnessInstanceID: binding.HarnessInstanceID, TicketID: binding.TicketID,
		CaseID: binding.CaseID, ProfileCapabilityID: binding.ProfileCapabilityID,
		InferenceGrantSHA256: binding.InferenceGrantSHA256, Deadline: binding.Deadline,
		RequestBudget:         binding.RequestBudget,
		PromptTokenBudget:     binding.Budgets.ModelInputTokens,
		CompletionTokenBudget: binding.Budgets.ModelOutputTokens,
		CostBudgetUSDMicros:   source.costBudgetUSDMicros,
	})
}

func zeroBytes(value []byte) {
	for index := range value {
		value[index] = 0
	}
}

func bytesOpener(body []byte) codingcertifier.BundleOpener {
	return func() (io.ReadCloser, error) {
		return io.NopCloser(bytes.NewReader(body)), nil
	}
}

func publicCanarySeed(leaseID string) (codingcontract.SeedRequest, error) {
	seed := codingcontract.SeedRequest{
		CodingContractVersion: codingcontract.ContractVersion,
		TicketID:              leaseID, CaseID: publicCanaryTaskID, ProfileCapabilityID: publicCanaryProfileID,
		Memories: []codingcontract.VisibleMemory{},
	}
	digest, err := memoryBundleDigest(seed.Memories)
	if err != nil {
		return codingcontract.SeedRequest{}, err
	}
	seed.MemoryBundleSHA256 = digest
	if err := seed.Validate(); err != nil {
		return codingcontract.SeedRequest{}, err
	}
	return seed, nil
}

func memoryBundleDigest(memories []codingcontract.VisibleMemory) (string, error) {
	var buffer strings.Builder
	encoder := json.NewEncoder(&buffer)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(struct {
		Memories []codingcontract.VisibleMemory `json:"memories"`
	}{Memories: memories}); err != nil {
		return "", err
	}
	digest := sha256.Sum256([]byte(buffer.String()))
	return hex.EncodeToString(digest[:]), nil
}

var _ Backend = (*certifierBackend)(nil)
var _ codinggateway.ActivationAuthorizer = reservedAuthorizer{}
var _ codingcertifier.InferenceEvidenceSource = canaryInference{}
