package codingphase

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"path/filepath"
	"strconv"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codinggateway"
	"github.com/ditto-assistant/dittobench-api/internal/codinggrantrevoke"
	"github.com/ditto-assistant/dittobench-api/internal/codingrelay"
)

type GatewayActivatorConfig struct {
	JournalRoot          string
	JournalMaxTotalBytes int64
	JournalMaxEntries    int
	Publisher            codinggateway.CapabilityPublisher
	Transport            http.RoundTripper
	Now                  func() time.Time
	NewRequestID         func() string
	NewNonce             func() string
	OperationTimeout     time.Duration
	CleanupTimeout       time.Duration
}

// GatewayActivator is the unwired adapter from phase authority to the reviewed
// inference gateway. Each ticket/grant generation receives one deterministic
// private journal directory and one separate revocation-only client.
type GatewayActivator struct {
	config GatewayActivatorConfig
}

func NewGatewayActivator(config GatewayActivatorConfig) (*GatewayActivator, error) {
	if !filepath.IsAbs(config.JournalRoot) || filepath.Clean(config.JournalRoot) == string(filepath.Separator) ||
		config.JournalMaxTotalBytes <= 0 || config.JournalMaxTotalBytes > 1<<40 ||
		config.JournalMaxEntries <= 0 || config.JournalMaxEntries > 4096 || nilLike(config.Publisher) ||
		(config.Transport != nil && nilLike(config.Transport)) || config.OperationTimeout < 0 ||
		config.OperationTimeout > 30*time.Second || config.CleanupTimeout < 0 ||
		(config.CleanupTimeout > 0 && config.CleanupTimeout < time.Second) ||
		config.CleanupTimeout > maximumCleanupTimeout {
		return nil, ErrInvalidConfig
	}
	return &GatewayActivator{config: config}, nil
}

func (activator *GatewayActivator) Activate(
	ctx context.Context,
	activation InferenceActivation,
) (InferenceGateway, error) {
	if activator == nil || ctx == nil || ctx.Err() != nil || nilLike(activation.Authorizer) {
		return nil, ErrInvalidConfig
	}
	revoker, err := codinggrantrevoke.New(codinggrantrevoke.Config{
		Capability: activation.Revocation, Binding: activation.Capability.Binding,
		Transport: activator.config.Transport, Now: activator.config.Now,
		Timeout: activator.config.CleanupTimeout,
	})
	if err != nil {
		return nil, errors.Join(ErrLifecycle, err)
	}
	journalRoot := filepath.Join(activator.config.JournalRoot, journalLeaf(activation.Capability.Binding))
	if err := ensureJournalDirectory(activator.config.JournalRoot, filepath.Base(journalRoot)); err != nil {
		return nil, activator.failBeforeGateway(ctx, revoker, activation.Capability.Binding, err)
	}
	maximumEntries := int(activation.Capability.Binding.RequestBudget) + int(activation.Policy.MaxRetries)
	if maximumEntries > activator.config.JournalMaxEntries {
		return nil, activator.failBeforeGateway(
			ctx, revoker, activation.Capability.Binding, errors.New("coding relay journal entry capacity is insufficient"),
		)
	}
	gateway, err := codinggateway.Activate(ctx, codinggateway.Config{
		Policy: activation.Policy, Capability: activation.Capability,
		JournalRoot: journalRoot, JournalMaxTotalBytes: activator.config.JournalMaxTotalBytes,
		JournalMaxEntries: maximumEntries, Authorizer: activation.Authorizer,
		Publisher: activator.config.Publisher, GrantRevoker: revoker,
		Transport: activator.config.Transport, Now: activator.config.Now,
		NewRequestID: activator.config.NewRequestID, NewNonce: activator.config.NewNonce,
		OperationTimeout: activator.config.OperationTimeout, CleanupTimeout: activator.config.CleanupTimeout,
	})
	if err != nil {
		return nil, activator.failBeforeGateway(ctx, revoker, activation.Capability.Binding, err)
	}
	return gateway, nil
}

func (activator *GatewayActivator) failBeforeGateway(
	ctx context.Context,
	revoker *codinggrantrevoke.Revoker,
	binding codingrelay.Binding,
	primary error,
) error {
	timeout := activator.config.CleanupTimeout
	if timeout == 0 {
		timeout = defaultCleanupTimeout
	}
	if timeout > maximumCleanupTimeout {
		timeout = maximumCleanupTimeout
	}
	cleanupContext, cancel := context.WithTimeout(context.WithoutCancel(ctx), timeout)
	revokeErr := revoker.Revoke(cleanupContext, codinggateway.GrantRevocation{
		TicketID: binding.TicketID, CaseID: binding.CaseID,
		ProfileCapabilityID: binding.ProfileCapabilityID, GrantID: binding.GrantID,
		Generation: binding.Generation, InferenceGrantSHA256: binding.InferenceGrantSHA256,
		Deadline: binding.Deadline,
	})
	cancel()
	closeErr := revoker.Close()
	return errors.Join(ErrLifecycle, primary, revokeErr, closeErr)
}

func journalLeaf(binding codingrelay.Binding) string {
	digest := sha256.Sum256([]byte(
		"dittobench-coding-relay-journal:v1\x00" + binding.TicketID + "\x00" +
			binding.GrantID + "\x00" + strconv.FormatUint(uint64(binding.Generation), 10),
	))
	return "relay-" + hex.EncodeToString(digest[:])
}

func (activator *GatewayActivator) String() string   { return "CodingPhaseGatewayActivator{private}" }
func (activator *GatewayActivator) GoString() string { return activator.String() }
func (activator *GatewayActivator) LogValue() slog.Value {
	return slog.StringValue("coding-phase-gateway-activator")
}
func (*GatewayActivator) MarshalJSON() ([]byte, error) { return nil, ErrInvalidConfig }

var _ InferenceActivator = (*GatewayActivator)(nil)
var _ json.Marshaler = (*GatewayActivator)(nil)
