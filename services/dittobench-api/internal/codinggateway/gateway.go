package codinggateway

import (
	"context"
	"net/url"
	"reflect"
	"strings"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codingplatform"
	"github.com/ditto-assistant/dittobench-api/internal/codingrelay"
	"github.com/ditto-assistant/dittobench-api/internal/codingrelayjournal"
)

// Activate constructs and publishes one fresh source-bound relay capability.
// A non-empty journal is never republished; callers must use Recover instead.
func Activate(ctx context.Context, config Config) (*Gateway, error) {
	if ctx == nil || ctx.Err() != nil || invalidInterface(config.Authorizer) ||
		invalidInterface(config.Publisher) ||
		invalidInterface(config.GrantRevoker) || invalidCleanupTimeout(config.CleanupTimeout) {
		return nil, ErrInvalidConfig
	}
	cleanupTimeout := normalizedCleanupTimeout(config.CleanupTimeout)
	binding := cloneBinding(config.Capability.Binding)
	journal, err := codingrelayjournal.Open(codingrelayjournal.Config{
		Root: config.JournalRoot, Policy: config.Policy,
		MaxTotalBytes: config.JournalMaxTotalBytes, MaxEntries: config.JournalMaxEntries,
	})
	if err != nil {
		if revokeUnusedGrant(config.GrantRevoker, binding, cleanupTimeout) != nil {
			return nil, ErrCleanup
		}
		return nil, ErrActivation
	}
	snapshot, err := journal.Load(ctx, binding)
	if err != nil {
		cleanupErr := revokeUnusedGrant(config.GrantRevoker, binding, cleanupTimeout)
		closeErr := journal.Close()
		if cleanupErr != nil || closeErr != nil {
			return nil, ErrCleanup
		}
		return nil, ErrActivation
	}
	if snapshot.Binding != nil {
		cleanupErr := revokeUnusedGrant(config.GrantRevoker, binding, cleanupTimeout)
		closeErr := journal.Close()
		if cleanupErr != nil || closeErr != nil {
			return nil, ErrCleanup
		}
		return nil, ErrAlreadyUsed
	}
	if err := journal.Bind(ctx, binding); err != nil {
		return nil, cleanupActivationFailure(
			config.GrantRevoker, binding, cleanupTimeout, nil, nil, nil, journal, ErrActivation,
		)
	}
	if err := config.Authorizer.Authorize(ctx, capabilityBinding(binding)); err != nil {
		return nil, cleanupActivationFailure(
			config.GrantRevoker, binding, cleanupTimeout, nil, nil, nil, journal, ErrActivation,
		)
	}

	upstream, err := codingplatform.New(codingplatform.Config{
		Policy: config.Policy, Capability: config.Capability, Transport: config.Transport,
		Now: config.Now, NewNonce: config.NewNonce,
	})
	if err != nil {
		return nil, cleanupActivationFailure(
			config.GrantRevoker, binding, cleanupTimeout, nil, nil, nil, journal, ErrActivation,
		)
	}
	relay, err := codingrelay.New(ctx, codingrelay.Config{
		Policy: config.Policy, Binding: binding, Upstream: upstream, Journal: journal,
		Now: config.Now, NewRequestID: config.NewRequestID, OperationTimeout: config.OperationTimeout,
	})
	if err != nil {
		return nil, cleanupActivationFailure(
			config.GrantRevoker, binding, cleanupTimeout, nil, nil, upstream, journal, ErrActivation,
		)
	}
	published, err := config.Publisher.Publish(ctx, capabilityBinding(binding), relay.Handler())
	publishedURL := ""
	invalidPublished := invalidInterface(published)
	if err == nil && !invalidPublished {
		publishedURL = published.URL()
	}
	if invalidPublished {
		published = nil
	}
	if err != nil || invalidPublished || !validCapabilityBaseURL(publishedURL) {
		return nil, cleanupActivationFailure(
			config.GrantRevoker, binding, cleanupTimeout, published, relay, upstream, journal, ErrActivation,
		)
	}
	return &Gateway{
		binding: binding, relay: relay, upstream: upstream, journal: journal,
		published: published, revoker: config.GrantRevoker, capability: publishedURL,
	}, nil
}

// URL returns the opaque base URL supplied to RunRequest.InferenceBaseURL.
func (gateway *Gateway) URL() (string, error) {
	if gateway == nil {
		return "", ErrClosed
	}
	gateway.mu.Lock()
	defer gateway.mu.Unlock()
	if gateway.closed {
		return "", ErrClosed
	}
	if gateway.outerRevoked || gateway.localRevoked || gateway.grantRevoked || gateway.capability == "" {
		return "", ErrCapabilityRevoked
	}
	return gateway.capability, nil
}

// Revoke stops the outer route, waits for admitted local work to settle,
// durably revokes the relay, and finally revokes the exact Platform grant.
func (gateway *Gateway) Revoke(ctx context.Context) error {
	if gateway == nil || ctx == nil {
		return ErrRevocation
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	gateway.revokeMu.Lock()
	defer gateway.revokeMu.Unlock()

	gateway.mu.Lock()
	if gateway.closed {
		gateway.mu.Unlock()
		return ErrClosed
	}
	published := gateway.published
	outerRevoked := gateway.outerRevoked
	localRevoked := gateway.localRevoked
	grantRevoked := gateway.grantRevoked
	relay := gateway.relay
	revoker := gateway.revoker
	revocation := grantRevocation(gateway.binding)
	gateway.mu.Unlock()

	if !outerRevoked && published != nil {
		if err := published.Revoke(ctx); err != nil {
			return contextualOr(ctx, ErrRevocation)
		}
		gateway.mu.Lock()
		gateway.outerRevoked = true
		gateway.capability = ""
		gateway.mu.Unlock()
	}
	if !localRevoked {
		if relay == nil || relay.Revoke(ctx) != nil {
			return contextualOr(ctx, ErrRevocation)
		}
		gateway.mu.Lock()
		gateway.localRevoked = true
		gateway.mu.Unlock()
	}
	if !grantRevoked {
		if invalidInterface(revoker) || revoker.Revoke(ctx, revocation) != nil {
			return contextualOr(ctx, ErrRevocation)
		}
		gateway.mu.Lock()
		gateway.grantRevoked = true
		gateway.mu.Unlock()
	}
	return nil
}

// Evidence returns deterministic model evidence only after local and remote
// revocation have both committed.
func (gateway *Gateway) Evidence(
	ctx context.Context,
	binding codingrelay.EvidenceBinding,
) (codingcontract.ModelEvidence, error) {
	if gateway == nil || ctx == nil {
		return codingcontract.ModelEvidence{}, ErrEvidence
	}
	if err := ctx.Err(); err != nil {
		return codingcontract.ModelEvidence{}, err
	}
	gateway.mu.Lock()
	if gateway.closed {
		gateway.mu.Unlock()
		return codingcontract.ModelEvidence{}, ErrClosed
	}
	if !gateway.outerRevoked || !gateway.localRevoked || !gateway.grantRevoked {
		gateway.mu.Unlock()
		return codingcontract.ModelEvidence{}, ErrNotRevoked
	}
	relay := gateway.relay
	gateway.mu.Unlock()
	evidence, err := relay.Evidence(ctx, binding)
	if err != nil {
		return codingcontract.ModelEvidence{}, contextualOr(ctx, ErrEvidence)
	}
	return evidence, nil
}

// Close releases local resources after revocation. It deliberately retains the
// journal directory and never deletes evidence.
func (gateway *Gateway) Close() error {
	if gateway == nil {
		return nil
	}
	gateway.revokeMu.Lock()
	defer gateway.revokeMu.Unlock()
	gateway.mu.Lock()
	if gateway.closed {
		gateway.mu.Unlock()
		return nil
	}
	if !gateway.outerRevoked || !gateway.localRevoked || !gateway.grantRevoked {
		gateway.mu.Unlock()
		return ErrNotRevoked
	}
	published := gateway.published
	upstream := gateway.upstream
	journal := gateway.journal
	gateway.mu.Unlock()

	var failed bool
	if published != nil && published.Close() != nil {
		failed = true
	}
	if upstream != nil && upstream.Close() != nil {
		failed = true
	}
	if journal != nil && journal.Close() != nil {
		failed = true
	}
	if failed {
		return ErrCleanup
	}
	gateway.mu.Lock()
	gateway.closed = true
	gateway.capability = ""
	gateway.binding = codingrelay.Binding{}
	gateway.relay = nil
	gateway.published = nil
	gateway.upstream = nil
	gateway.journal = nil
	gateway.revoker = nil
	gateway.mu.Unlock()
	return nil
}

func cleanupActivationFailure(
	revoker GrantRevoker,
	binding codingrelay.Binding,
	timeout time.Duration,
	published PublishedCapability,
	relay *codingrelay.Relay,
	upstream *codingplatform.Client,
	journal journalStore,
	result error,
) error {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	var failed bool
	if published != nil && published.Revoke(ctx) != nil {
		failed = true
	}
	if relay != nil {
		if relay.Revoke(ctx) != nil {
			failed = true
		}
	} else if journal != nil && journal.Revoke(ctx, binding) != nil {
		failed = true
	}
	if invalidInterface(revoker) || revoker.Revoke(ctx, grantRevocation(binding)) != nil {
		failed = true
	}
	if published != nil && published.Close() != nil {
		failed = true
	}
	if upstream != nil && upstream.Close() != nil {
		failed = true
	}
	if journal != nil && journal.Close() != nil {
		failed = true
	}
	if failed {
		return ErrCleanup
	}
	return result
}

func revokeUnusedGrant(revoker GrantRevoker, binding codingrelay.Binding, timeout time.Duration) error {
	if invalidInterface(revoker) {
		return ErrCleanup
	}
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	if revoker.Revoke(ctx, grantRevocation(binding)) != nil {
		return ErrCleanup
	}
	return nil
}

func capabilityBinding(binding codingrelay.Binding) CapabilityBinding {
	return CapabilityBinding{
		AttemptID: binding.AttemptID, AgentArtifactSHA256: binding.AgentArtifactSHA256,
		HarnessInstanceID: binding.HarnessInstanceID, TicketID: binding.TicketID,
		CaseID: binding.CaseID, ProfileCapabilityID: binding.ProfileCapabilityID,
		GrantID: binding.GrantID, Generation: binding.Generation,
		InferenceGrantSHA256: binding.InferenceGrantSHA256,
		IssuedAt:             binding.IssuedAt, Deadline: binding.Deadline,
		RequestBudget: binding.RequestBudget, PromptTokenBudget: binding.PromptTokenBudget,
		CompletionTokenBudget: binding.CompletionTokenBudget,
	}
}

func grantRevocation(binding codingrelay.Binding) GrantRevocation {
	return GrantRevocation{
		TicketID: binding.TicketID, CaseID: binding.CaseID,
		ProfileCapabilityID: binding.ProfileCapabilityID, GrantID: binding.GrantID,
		Generation: binding.Generation, InferenceGrantSHA256: binding.InferenceGrantSHA256,
		Deadline: binding.Deadline,
	}
}

func cloneBinding(binding codingrelay.Binding) codingrelay.Binding {
	binding.IssuedAt = binding.IssuedAt.UTC()
	binding.Deadline = binding.Deadline.UTC()
	return binding
}

func validCapabilityBaseURL(value string) bool {
	if value == "" || len(value) > 4096 {
		return false
	}
	parsed, err := url.ParseRequestURI(value)
	if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.Host == "" ||
		parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" {
		return false
	}
	return !strings.HasSuffix(strings.TrimRight(parsed.Path, "/"), "/chat/completions")
}

func invalidInterface(value any) bool {
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

func invalidCleanupTimeout(value time.Duration) bool {
	return value < 0 || value > maximumCleanupTimeout
}

func normalizedCleanupTimeout(value time.Duration) time.Duration {
	if value == 0 {
		return defaultCleanupTimeout
	}
	return value
}

func contextualOr(ctx context.Context, fallback error) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	return fallback
}
