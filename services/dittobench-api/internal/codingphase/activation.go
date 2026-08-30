package codingphase

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codinggateway"
	"github.com/ditto-assistant/dittobench-api/internal/codinggrantrevoke"
	"github.com/ditto-assistant/dittobench-api/internal/codingrelay"
)

type GatewayActivatorConfig struct {
	JournalRoot           string
	JournalMaxTotalBytes  int64
	JournalMaxEntries     int
	JournalMaxDirectories int
	Publisher             codinggateway.CapabilityPublisher
	Transport             http.RoundTripper
	Now                   func() time.Time
	NewRequestID          func() string
	NewNonce              func() string
	OperationTimeout      time.Duration
	CleanupTimeout        time.Duration
}

// GatewayActivator is the unwired adapter from phase authority to the reviewed
// inference gateway. Each ticket/grant generation receives one deterministic
// private journal directory and one separate revocation-only client.
type GatewayActivator struct {
	mu     sync.Mutex
	config GatewayActivatorConfig
}

func NewGatewayActivator(config GatewayActivatorConfig) (*GatewayActivator, error) {
	if config.JournalMaxDirectories == 0 {
		config.JournalMaxDirectories = 64
	}
	if !filepath.IsAbs(config.JournalRoot) || filepath.Clean(config.JournalRoot) == string(filepath.Separator) ||
		config.JournalMaxTotalBytes <= 0 || config.JournalMaxTotalBytes > 1<<40 ||
		config.JournalMaxEntries <= 0 || config.JournalMaxEntries > 4096 || nilLike(config.Publisher) ||
		config.JournalMaxDirectories < 1 || config.JournalMaxDirectories > 4096 ||
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
	if activator == nil {
		return nil, ErrInvalidConfig
	}
	if ctx == nil {
		ctx = context.Background()
	}
	revoker, err := codinggrantrevoke.New(codinggrantrevoke.Config{
		Capability: activation.Revocation, Binding: activation.Capability.Binding,
		Transport: activator.config.Transport, Now: activator.config.Now,
		Timeout: activator.config.CleanupTimeout,
	})
	if err != nil {
		return nil, errors.Join(ErrLifecycle, err)
	}
	if ctx.Err() != nil || nilLike(activation.Authorizer) {
		return nil, activator.failBeforeGateway(ctx, revoker, activation.Capability.Binding, ErrInvalidConfig)
	}
	journalRoot := filepath.Join(activator.config.JournalRoot, journalLeaf(activation.Capability.Binding))
	activator.mu.Lock()
	directoryErr := ensureBoundedJournalDirectory(
		activator.config.JournalRoot,
		filepath.Base(journalRoot),
		activator.config.JournalMaxDirectories,
	)
	activator.mu.Unlock()
	if directoryErr != nil {
		return nil, activator.failBeforeGateway(ctx, revoker, activation.Capability.Binding, directoryErr)
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

func ensureBoundedJournalDirectory(root, leaf string, maximum int) error {
	entries, err := os.ReadDir(root)
	if err != nil {
		return err
	}
	exists := false
	count := 0
	for _, entry := range entries {
		name := entry.Name()
		if !strings.HasPrefix(name, "relay-") || len(name) != len("relay-")+64 ||
			!lowerSHA256(strings.TrimPrefix(name, "relay-")) || entry.Type()&os.ModeSymlink != 0 || !entry.IsDir() {
			return errors.New("coding relay journal root contains an unexpected entry")
		}
		count++
		exists = exists || name == leaf
	}
	if !exists && count >= maximum {
		return errors.New("coding relay journal directory capacity is exhausted")
	}
	return ensureJournalDirectory(root, leaf)
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
