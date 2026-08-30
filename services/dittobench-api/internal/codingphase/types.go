// Package codingphase composes one private shadow coding attempt from the
// reviewed workspace, memory, inference, outbox, and pristine-grading ports.
// It owns no listener, scheduler, Platform publication, score, or weight path.
package codingphase

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"sync"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingattempt"
	"github.com/ditto-assistant/dittobench-api/internal/codingcertifier"
	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codinggateway"
	"github.com/ditto-assistant/dittobench-api/internal/codinggrader"
	"github.com/ditto-assistant/dittobench-api/internal/codingoutbox"
	"github.com/ditto-assistant/dittobench-api/internal/codingplatform"
	"github.com/ditto-assistant/dittobench-api/internal/codingrelay"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
	"github.com/ditto-assistant/dittobench-api/internal/codingseed"
)

const (
	defaultCleanupTimeout = 30 * time.Second
	maximumCleanupTimeout = 2 * time.Minute
	maximumLeaseBytes     = 6 << 20
)

var (
	ErrInvalidConfig = errors.New("coding phase runner configuration is invalid")
	ErrInvalid       = errors.New("coding phase authority is invalid")
	ErrLifecycle     = errors.New("coding phase lifecycle failed")
	ErrRecovery      = errors.New("coding phase recovery is ambiguous")
)

// AuthoringSession is the lifecycle projection used by the phase runner.
type AuthoringSession interface {
	Handler() http.Handler
	SeedProjection() codingseed.Projection
	Freeze(context.Context, codingattempt.CapabilityRevoker) (codingrunner.FreezeResult, error)
	WriteTranscript(io.Writer) (codingrunner.TranscriptIdentity, error)
	Close() error
}

// AttemptRuntime separates orchestration tests from workspace materialization.
type AttemptRuntime interface {
	BeginAuthoring(context.Context, codingattempt.AuthoringSpec) (AuthoringSession, error)
	Grade(context.Context, codingattempt.GradingSpec, codingrunner.FrozenSubmission) (codinggrader.Result, error)
}

// RuntimeAdapter exposes one concrete reviewed codingattempt.Runtime through
// the narrow phase-runner port.
type RuntimeAdapter struct {
	runtime *codingattempt.Runtime
}

func NewRuntimeAdapter(runtime *codingattempt.Runtime) (RuntimeAdapter, error) {
	if runtime == nil {
		return RuntimeAdapter{}, ErrInvalidConfig
	}
	return RuntimeAdapter{runtime: runtime}, nil
}

func (adapter RuntimeAdapter) BeginAuthoring(
	ctx context.Context,
	spec codingattempt.AuthoringSpec,
) (AuthoringSession, error) {
	if adapter.runtime == nil {
		return nil, ErrInvalidConfig
	}
	return adapter.runtime.BeginAuthoring(ctx, spec)
}

func (adapter RuntimeAdapter) Grade(
	ctx context.Context,
	spec codingattempt.GradingSpec,
	submission codingrunner.FrozenSubmission,
) (codinggrader.Result, error) {
	if adapter.runtime == nil {
		return codinggrader.Result{}, ErrInvalidConfig
	}
	return adapter.runtime.Grade(ctx, spec, submission)
}

// SeedDeliverer is satisfied by codingseed.Projector. Projection happens in
// codingattempt; delivery happens only after the durable attempt marker.
type SeedDeliverer interface {
	Deliver(context.Context, codingseed.SeedClient, codingseed.Projection) (codingseed.Delivery, error)
}

// HarnessFactory acquires one dormant sandbox handle. Acquire must not execute
// candidate code; Activate is called only after the durable collecting marker.
type HarnessFactory interface {
	Acquire(context.Context, HarnessBinding) (Harness, error)
}

// Harness is one validator-owned lifecycle handle. Destroy must be idempotent
// and stop the sandbox before it returns.
type Harness interface {
	InstanceID() string
	Client() codingcertifier.HarnessClient
	Activate(context.Context) error
	Destroy(context.Context) error
}

type HarnessBinding struct {
	ExecutionID            string
	AgentID                string
	RunRowID               string
	AgentArtifactSHA256    string
	TicketID               string
	CaseID                 string
	ProfileCapabilityID    string
	Deadline               time.Time
	BenchVersion           int
	ScreenedImageSHA256    string
	ScreenedImageSize      int64
	ScreenedImageID        string
	ScreenedImageRef       string
	ScreeningPolicyVersion int
	ImageURL               string
	ImageExpiresAt         time.Time
}

// InferenceGateway is satisfied by codinggateway.Gateway.
type InferenceGateway interface {
	URL() (string, error)
	Revoke(context.Context) error
	Evidence(context.Context, codingrelay.EvidenceBinding) (codingcontract.ModelEvidence, error)
	Close() error
}

// InferenceActivation contains the complete private capability and the
// outbox-backed activation proof. It must not be serialized or retained.
type InferenceActivation struct {
	Policy     codingcontract.InferencePolicy
	Capability codingplatform.GrantCapability
	Revocation codingplatform.RevocationCapability
	Authorizer codinggateway.ActivationAuthorizer
}

type InferenceActivator interface {
	// Activate must return a revocable handle on partial publication. A nil
	// handle with an error certifies that it completed all cleanup itself.
	Activate(context.Context, InferenceActivation) (InferenceGateway, error)
}

type Config struct {
	Attempts        AttemptRuntime
	Outbox          *codingoutbox.Store
	Seeds           SeedDeliverer
	Harnesses       HarnessFactory
	WorkspaceRoutes codingcertifier.CapabilityPublisher
	Inference       InferenceActivator
	InferencePolicy codingcontract.InferencePolicy
	Now             func() time.Time
	CleanupTimeout  time.Duration
}

// Runner implements codingsupervisor.PhaseRunner. Durable grading, abort, and
// recovery resolve the outbox record by ticket. Unrevoked inference gateways
// are retained until Recover can finish revocation.
type Runner struct {
	mu              sync.Mutex
	attempts        AttemptRuntime
	outbox          *codingoutbox.Store
	seeds           SeedDeliverer
	harnesses       HarnessFactory
	workspaceRoutes codingcertifier.CapabilityPublisher
	inference       InferenceActivator
	policy          codingcontract.InferencePolicy
	now             func() time.Time
	cleanupTimeout  time.Duration
	liveGateways    map[string]InferenceGateway
}

func (binding HarnessBinding) String() string   { return "CodingPhaseHarnessBinding{private}" }
func (binding HarnessBinding) GoString() string { return binding.String() }
func (binding HarnessBinding) LogValue() slog.Value {
	return slog.StringValue("coding-phase-harness-binding")
}
func (HarnessBinding) MarshalJSON() ([]byte, error) { return nil, ErrInvalid }

func (activation InferenceActivation) String() string {
	return "CodingPhaseInferenceActivation{private}"
}
func (activation InferenceActivation) GoString() string { return activation.String() }
func (activation InferenceActivation) LogValue() slog.Value {
	return slog.StringValue("coding-phase-inference-activation")
}
func (InferenceActivation) MarshalJSON() ([]byte, error) { return nil, ErrInvalid }

func (config Config) String() string        { return "CodingPhaseConfig{private}" }
func (config Config) GoString() string      { return config.String() }
func (config Config) LogValue() slog.Value  { return slog.StringValue("coding-phase-config") }
func (Config) MarshalJSON() ([]byte, error) { return nil, ErrInvalid }

func (runner *Runner) String() string {
	if runner == nil {
		return "CodingPhaseRunner{nil=true}"
	}
	return "CodingPhaseRunner{private}"
}
func (runner *Runner) GoString() string      { return runner.String() }
func (runner *Runner) LogValue() slog.Value  { return slog.StringValue("coding-phase-runner") }
func (*Runner) MarshalJSON() ([]byte, error) { return nil, ErrInvalid }

var _ json.Marshaler = HarnessBinding{}
var _ json.Marshaler = InferenceActivation{}
var _ json.Marshaler = Config{}
var _ json.Marshaler = (*Runner)(nil)
var _ AttemptRuntime = RuntimeAdapter{}
