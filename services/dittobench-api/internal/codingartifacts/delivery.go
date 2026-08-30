package codingartifacts

import (
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"regexp"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
)

const (
	capabilitySchema        = "dittobench-coding-artifact-capability-v1"
	maximumDeliveryBytes    = 32 << 10
	deliveryContractVersion = 1
)

var rfc3339Microseconds = regexp.MustCompile(
	`^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$`,
)

// WireCapability is the validator-only transport DTO. It may be serialized;
// ToCapability converts it into the fetcher's non-serializable internal type.
type WireCapability struct {
	Schema                string        `json:"schema"`
	CodingContractVersion int           `json:"coding_contract_version"`
	WeightEligible        bool          `json:"weight_eligible"`
	TicketID              string        `json:"ticket_id"`
	TicketDeadline        time.Time     `json:"ticket_deadline"`
	DeliveryPhase         DeliveryPhase `json:"delivery_phase"`
	ArtifactKind          Kind          `json:"artifact_kind"`
	Audience              Audience      `json:"audience"`
	SHA256                string        `json:"sha256"`
	SizeBytes             int64         `json:"size_bytes"`
	URL                   string        `json:"url"`
	ExpiresAt             time.Time     `json:"expires_at"`
}

// String deliberately omits the bearer URL.
func (capability WireCapability) String() string {
	return fmt.Sprintf(
		"CodingArtifactWireCapability{ticket=%q phase=%q kind=%q audience=%q size_bytes=%d expires_at=%q}",
		capability.TicketID, capability.DeliveryPhase, capability.ArtifactKind,
		capability.Audience, capability.SizeBytes, capability.ExpiresAt.UTC().Format(time.RFC3339),
	)
}

// GoString keeps %#v diagnostics redacted.
func (capability WireCapability) GoString() string {
	return capability.String()
}

// LogValue keeps structured slog output redacted.
func (capability WireCapability) LogValue() slog.Value {
	return slog.GroupValue(
		slog.String("ticket", capability.TicketID),
		slog.String("phase", string(capability.DeliveryPhase)),
		slog.String("kind", string(capability.ArtifactKind)),
		slog.String("audience", string(capability.Audience)),
		slog.Int64("size_bytes", capability.SizeBytes),
		slog.Time("expires_at", capability.ExpiresAt.UTC()),
	)
}

// DecodeWireCapability rejects malformed, duplicate, missing, or incoherent
// known fields while ignoring unknown fields for rolling compatibility.
func DecodeWireCapability(body []byte) (WireCapability, error) {
	var zero WireCapability
	if err := codingcontract.ValidateJSONDocument(body, maximumDeliveryBytes); err != nil {
		return zero, err
	}
	var shape map[string]json.RawMessage
	if err := json.Unmarshal(body, &shape); err != nil {
		return zero, err
	}
	for _, field := range []string{
		"schema", "coding_contract_version", "weight_eligible", "ticket_id",
		"ticket_deadline", "delivery_phase", "artifact_kind", "audience",
		"sha256", "size_bytes", "url", "expires_at",
	} {
		if _, present := shape[field]; !present {
			return zero, fmt.Errorf("coding artifact capability is missing field %q", field)
		}
	}
	if err := validateWireTimestamp(shape["ticket_deadline"]); err != nil {
		return zero, err
	}
	if err := validateWireTimestamp(shape["expires_at"]); err != nil {
		return zero, err
	}
	var decoded WireCapability
	if err := json.Unmarshal(body, &decoded); err != nil {
		return zero, err
	}
	if _, err := decoded.ToCapability(); err != nil {
		return zero, err
	}
	return decoded, nil
}

func validateWireTimestamp(raw json.RawMessage) error {
	var value string
	if err := json.Unmarshal(raw, &value); err != nil || !rfc3339Microseconds.MatchString(value) {
		return errors.New("coding artifact timestamp must be RFC3339 with at most microsecond precision")
	}
	return nil
}

// ToCapability validates the transport fields and strips wire serialization.
func (capability WireCapability) ToCapability() (Capability, error) {
	if capability.Schema != capabilitySchema ||
		capability.CodingContractVersion != deliveryContractVersion ||
		capability.WeightEligible {
		return Capability{}, errors.New("coding artifact wire authority is invalid")
	}
	internal := Capability{
		TicketID: capability.TicketID, Phase: capability.DeliveryPhase,
		Kind: capability.ArtifactKind, Audience: capability.Audience,
		SHA256: capability.SHA256, SizeBytes: capability.SizeBytes, URL: capability.URL,
		ExpiresAt: capability.ExpiresAt, TicketDeadline: capability.TicketDeadline,
	}
	if err := validateCapabilityKnownFields(internal); err != nil {
		return Capability{}, err
	}
	if _, _, err := validateCapabilityURL(internal); err != nil {
		return Capability{}, err
	}
	return internal, nil
}
