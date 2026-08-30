package codingcanary

import (
	"bytes"
	"context"
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
	"github.com/ditto-assistant/dittobench-api/internal/codingharness"
	"github.com/ditto-assistant/dittobench-api/internal/codingoutbox"
	"github.com/ditto-assistant/dittobench-api/internal/codingphase"
)

const (
	certificationTTL      = time.Hour
	publicCanaryEpoch     = "practice-ledger-v2"
	unobservedInferenceID = "coding-certification-inference-unobserved-v1"
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
	policy      codingcontract.InferencePolicy
	imageDigest string
	now         func() time.Time
}

func NewBackend(config BackendConfig) (Backend, error) {
	if config.Pack.CanaryManifestSHA256 == "" || config.Pack.TaskID != publicCanaryTaskID ||
		config.Harnesses == nil || config.Publisher == nil || config.Executors == nil ||
		config.Outbox == nil || config.Policy.Validate() != nil ||
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
		executors: config.Executors, outbox: config.Outbox, policy: config.Policy,
		imageDigest: config.ImageDigest, now: now,
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
	grantSHA := unobservedGrantSHA256(request.LeaseID, backend.pack.InferencePolicySHA256)
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
		RepositoryEpoch: publicCanaryEpoch, InferenceBaseURL: "http://inference.invalid/capability",
		InferenceGrantSHA256: grantSHA, SolverModel: codingcertifier.CertificationSolverModel,
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
		InferenceEvidence: unobservedInference{}, OpenVisibleBundle: bytesOpener(plans.visible),
		OpenGraderBundle: bytesOpener(plans.graderBundle), CertificationTTL: certificationTTL, Now: backend.now,
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

type unobservedInference struct{}

func (unobservedInference) Evidence(
	context.Context,
	codingcertifier.InferenceBinding,
) (codingcontract.ModelEvidence, error) {
	return codingcontract.ModelEvidence{}, codingcertifier.ErrInferenceNotObserved
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

func unobservedGrantSHA256(leaseID, policySHA string) string {
	digest := sha256.Sum256([]byte(unobservedInferenceID + "\x00" + leaseID + "\x00" + policySHA))
	return hex.EncodeToString(digest[:])
}

var _ Backend = (*certifierBackend)(nil)
