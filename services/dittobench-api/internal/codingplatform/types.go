package codingplatform

import (
	"crypto/ed25519"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codingrelay"
)

const (
	dispatchRequestSchema  = "dittobench-coding-inference-dispatch-v1"
	dispatchResponseSchema = "dittobench-coding-inference-dispatch-result-v1"
	dispatchAPIPath        = "/api/v1/inference/coding/chat/completions"
	maximumEnvelopeBytes   = 64 << 10
)

var (
	ErrInvalidConfig       = errors.New("coding Platform upstream configuration is invalid")
	ErrInvalidRequest      = errors.New("coding Platform upstream request is invalid")
	ErrCapabilityClosed    = errors.New("coding Platform upstream capability is closed")
	ErrCapabilityExpired   = errors.New("coding Platform upstream capability is expired")
	ErrClockRollback       = errors.New("coding Platform upstream clock moved backwards")
	ErrTransport           = errors.New("coding Platform upstream transport failed")
	ErrUnsettledResponse   = errors.New("coding Platform upstream response is unsettled")
	ErrResponseIntegrity   = errors.New("coding Platform upstream response is invalid")
	ErrSecretSerialization = errors.New("coding Platform upstream secrets cannot be serialized")
)

// GrantCapability is the exact private Platform exchange result plus the
// validator-owned signing key. It must never be given to the miner harness.
type GrantCapability struct {
	Binding          codingrelay.Binding
	Bearer           string
	BrokerPublicKey  string
	BrokerPrivateKey ed25519.PrivateKey
	ProxyURL         string
}

// Config constructs one validator-side codingrelay.Upstream adapter. It does
// not contain a provider credential or a live-provider routing choice.
type Config struct {
	Policy     codingcontract.InferencePolicy
	Capability GrantCapability
	Transport  http.RoundTripper
	Now        func() time.Time
	NewNonce   func() string
}

func (capability GrantCapability) String() string { return "CodingPlatformGrantCapability{private}" }

func (capability GrantCapability) GoString() string { return capability.String() }

func (capability GrantCapability) LogValue() slog.Value {
	return slog.StringValue("coding-platform-grant-capability")
}

func (capability GrantCapability) MarshalJSON() ([]byte, error) {
	return nil, ErrSecretSerialization
}

func (config Config) String() string { return "CodingPlatformConfig{private}" }

func (config Config) GoString() string { return config.String() }

func (config Config) LogValue() slog.Value {
	return slog.StringValue("coding-platform-config")
}

func (config Config) MarshalJSON() ([]byte, error) {
	return nil, ErrSecretSerialization
}

// dispatchRequest is the proof-bound validator-to-Platform wire. The locked
// request is model-visible; every other field is control-plane authority and
// must be stripped before provider forwarding.
type dispatchRequest struct {
	Schema                string                                `json:"schema"`
	CodingContractVersion int                                   `json:"coding_contract_version"`
	WeightEligible        bool                                  `json:"weight_eligible"`
	TicketID              string                                `json:"ticket_id"`
	CaseID                string                                `json:"case_id"`
	ProfileCapabilityID   string                                `json:"profile_capability_id"`
	InferenceGrantSHA256  string                                `json:"inference_grant_sha256"`
	GrantID               string                                `json:"grant_id"`
	Generation            uint32                                `json:"generation"`
	Sequence              uint32                                `json:"sequence"`
	RequestSequence       uint32                                `json:"request_sequence"`
	Attempt               uint32                                `json:"attempt"`
	RequestID             string                                `json:"request_id"`
	LockedRequestSHA256   string                                `json:"locked_request_sha256"`
	LockedRequest         codingcontract.InferenceLockedRequest `json:"locked_request"`
	Deadline              string                                `json:"deadline"`
}

type dispatchResponse struct {
	Schema                    string
	CodingContractVersion     int
	WeightEligible            bool
	Sequence                  uint32
	Settlement                codingcontract.InferenceProviderSettlement
	NormalizedResponse        []byte
	FailureResponseProjection []byte
}

func (response dispatchResponse) String() string {
	return "CodingPlatformDispatchResponse{private}"
}

func (response dispatchResponse) GoString() string { return response.String() }

func (response dispatchResponse) LogValue() slog.Value {
	return slog.GroupValue(
		slog.Uint64("sequence", uint64(response.Sequence)),
		slog.String("outcome", string(response.Settlement.Outcome)),
	)
}

func (response dispatchResponse) MarshalJSON() ([]byte, error) {
	return nil, errors.New("coding Platform dispatch responses cannot be serialized as diagnostics")
}

var _ json.Marshaler = GrantCapability{}
var _ json.Marshaler = Config{}
