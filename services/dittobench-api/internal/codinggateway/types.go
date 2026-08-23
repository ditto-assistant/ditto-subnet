package codinggateway

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"sync"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codingplatform"
	"github.com/ditto-assistant/dittobench-api/internal/codingrelay"
)

const (
	defaultCleanupTimeout = 30 * time.Second
	maximumCleanupTimeout = 2 * time.Minute
)

var (
	ErrInvalidConfig       = errors.New("coding inference gateway configuration is invalid")
	ErrActivation          = errors.New("coding inference gateway activation failed")
	ErrAlreadyUsed         = errors.New("coding inference gateway journal was already used")
	ErrRecoveryUnavailable = errors.New("coding inference gateway recovery is unavailable")
	ErrAmbiguousRecovery   = errors.New("coding inference gateway recovery is ambiguous")
	ErrCapabilityRevoked   = errors.New("coding inference gateway capability is revoked")
	ErrRevocation          = errors.New("coding inference gateway revocation failed")
	ErrNotRevoked          = errors.New("coding inference gateway must be revoked first")
	ErrEvidence            = errors.New("coding inference gateway evidence is unavailable")
	ErrClosed              = errors.New("coding inference gateway is closed")
	ErrCleanup             = errors.New("coding inference gateway cleanup failed")
	ErrSecretSerialization = errors.New("coding inference gateway private state cannot be serialized")
)

// CapabilityBinding is the non-secret route identity supplied to the trusted
// outer publisher. It is never accepted from the miner.
type CapabilityBinding struct {
	AttemptID             string
	AgentArtifactSHA256   string
	HarnessInstanceID     string
	TicketID              string
	CaseID                string
	ProfileCapabilityID   string
	GrantID               string
	Generation            uint32
	InferenceGrantSHA256  string
	IssuedAt              time.Time
	Deadline              time.Time
	RequestBudget         uint32
	PromptTokenBudget     uint64
	CompletionTokenBudget uint64
}

// PublishedCapability is one source-bound outer route. URL is the base URL;
// the coding harness appends /chat/completions. Revoke must stop new requests
// and wait until an already-admitted handler invocation no longer owns the
// route. Close releases only local publisher resources.
type PublishedCapability interface {
	URL() string
	Revoke(context.Context) error
	Close() error
}

// CapabilityPublisher mounts a relay handler behind an opaque, source-bound
// route. On a partial failure it must return a revocable capability handle; a
// nil result certifies that no route was published. A plain public listener is
// not a production implementation.
type CapabilityPublisher interface {
	Publish(context.Context, CapabilityBinding, http.Handler) (PublishedCapability, error)
}

// ActivationAuthorizer proves that the outer attempt owner has already
// committed its durable, non-rerunnable authoring marker. The gateway checks
// this trusted port before binding or publishing the inference capability.
type ActivationAuthorizer interface {
	Authorize(context.Context, CapabilityBinding) error
}

// GrantRevocation is the exact non-secret Platform authority revoked after
// local admission has stopped and every admitted dispatch has settled.
type GrantRevocation struct {
	TicketID             string
	CaseID               string
	ProfileCapabilityID  string
	GrantID              string
	Generation           uint32
	InferenceGrantSHA256 string
	Deadline             time.Time
}

// GrantRevoker durably revokes one exact Platform grant generation. Identical
// calls must be idempotent because a response can be lost after remote commit.
type GrantRevoker interface {
	Revoke(context.Context, GrantRevocation) error
}

// Config activates a fresh capability from one already-exchanged, proof-bound
// Platform grant. JournalRoot must be a pre-created private mode-0700 directory.
type Config struct {
	Policy               codingcontract.InferencePolicy
	Capability           codingplatform.GrantCapability
	JournalRoot          string
	JournalMaxTotalBytes int64
	JournalMaxEntries    int
	Authorizer           ActivationAuthorizer
	Publisher            CapabilityPublisher
	GrantRevoker         GrantRevoker
	Transport            http.RoundTripper
	Now                  func() time.Time
	NewRequestID         func() string
	NewNonce             func() string
	OperationTimeout     time.Duration
	CleanupTimeout       time.Duration
}

// RecoveryConfig opens an existing journal without any provider credential or
// publisher. Recovery never recreates a miner-visible route.
type RecoveryConfig struct {
	Policy               codingcontract.InferencePolicy
	Binding              codingrelay.Binding
	JournalRoot          string
	JournalMaxTotalBytes int64
	JournalMaxEntries    int
	GrantRevoker         GrantRevoker
	Now                  func() time.Time
	OperationTimeout     time.Duration
	CleanupTimeout       time.Duration
}

// Gateway owns one local capability lifecycle. The durable journal directory
// remains on disk after Close for the later evidence/outbox retention owner.
type Gateway struct {
	binding codingrelay.Binding

	relay      *codingrelay.Relay
	upstream   *codingplatform.Client
	journal    journalStore
	published  PublishedCapability
	revoker    GrantRevoker
	capability string

	revokeMu     sync.Mutex
	mu           sync.Mutex
	outerRevoked bool
	localRevoked bool
	grantRevoked bool
	closed       bool
}

type journalStore interface {
	codingrelay.Journal
	Bind(context.Context, codingrelay.Binding) error
	Close() error
}

func (binding CapabilityBinding) String() string { return "CodingGatewayCapabilityBinding{private}" }

func (binding CapabilityBinding) GoString() string { return binding.String() }

func (binding CapabilityBinding) LogValue() slog.Value {
	return slog.StringValue("coding-gateway-capability-binding")
}

func (binding CapabilityBinding) MarshalJSON() ([]byte, error) {
	return nil, ErrSecretSerialization
}

func (revocation GrantRevocation) String() string { return "CodingGatewayGrantRevocation{private}" }

func (revocation GrantRevocation) GoString() string { return revocation.String() }

func (revocation GrantRevocation) LogValue() slog.Value {
	return slog.StringValue("coding-gateway-grant-revocation")
}

func (revocation GrantRevocation) MarshalJSON() ([]byte, error) {
	return nil, ErrSecretSerialization
}

func (config Config) String() string { return "CodingGatewayConfig{private}" }

func (config Config) GoString() string { return config.String() }

func (config Config) LogValue() slog.Value { return slog.StringValue("coding-gateway-config") }

func (config Config) MarshalJSON() ([]byte, error) { return nil, ErrSecretSerialization }

func (config RecoveryConfig) String() string { return "CodingGatewayRecoveryConfig{private}" }

func (config RecoveryConfig) GoString() string { return config.String() }

func (config RecoveryConfig) LogValue() slog.Value {
	return slog.StringValue("coding-gateway-recovery-config")
}

func (config RecoveryConfig) MarshalJSON() ([]byte, error) {
	return nil, ErrSecretSerialization
}

func (gateway *Gateway) String() string {
	if gateway == nil {
		return "CodingInferenceGateway{nil=true}"
	}
	return "CodingInferenceGateway{private}"
}

func (gateway *Gateway) GoString() string { return gateway.String() }

func (gateway *Gateway) LogValue() slog.Value {
	return slog.StringValue("coding-inference-gateway")
}

func (gateway *Gateway) MarshalJSON() ([]byte, error) { return nil, ErrSecretSerialization }

var _ json.Marshaler = Config{}
var _ json.Marshaler = RecoveryConfig{}
var _ json.Marshaler = CapabilityBinding{}
var _ json.Marshaler = GrantRevocation{}
var _ json.Marshaler = (*Gateway)(nil)
