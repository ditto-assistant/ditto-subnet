package codinggateway

import (
	"context"
	"errors"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingrelay"
	"github.com/ditto-assistant/dittobench-api/internal/codingrelayjournal"
)

type recoveryUpstream struct{}

func (recoveryUpstream) Complete(context.Context, codingrelay.UpstreamRequest) (codingrelay.UpstreamResult, error) {
	return codingrelay.UpstreamResult{}, codingrelay.ErrUpstreamUnsettled
}

// Recover opens a previously used journal without publishing a capability or
// owning a provider credential. Completed dispatches can be revoked and
// finalized; an incomplete dispatch is terminally ambiguous and never retried.
func Recover(ctx context.Context, config RecoveryConfig) (*Gateway, error) {
	if ctx == nil || ctx.Err() != nil || invalidInterface(config.GrantRevoker) ||
		invalidCleanupTimeout(config.CleanupTimeout) {
		return nil, ErrInvalidConfig
	}
	cleanupTimeout := normalizedCleanupTimeout(config.CleanupTimeout)
	binding := cloneBinding(config.Binding)
	journal, err := codingrelayjournal.Open(codingrelayjournal.Config{
		Root: config.JournalRoot, Policy: config.Policy,
		MaxTotalBytes: config.JournalMaxTotalBytes, MaxEntries: config.JournalMaxEntries,
	})
	if err != nil {
		if revokeUnusedGrant(config.GrantRevoker, binding, cleanupTimeout) != nil {
			return nil, ErrCleanup
		}
		return nil, ErrRecoveryUnavailable
	}
	snapshot, err := journal.Load(ctx, binding)
	if err != nil || snapshot.Binding == nil {
		cleanupErr := revokeUnusedGrant(config.GrantRevoker, binding, cleanupTimeout)
		closeErr := journal.Close()
		if cleanupErr != nil || closeErr != nil {
			return nil, ErrCleanup
		}
		return nil, ErrRecoveryUnavailable
	}
	for _, entry := range snapshot.Entries {
		if !entry.Completed {
			cleanupContext, cancel := context.WithTimeout(context.Background(), cleanupTimeout)
			localErr := journal.Revoke(cleanupContext, binding)
			remoteErr := config.GrantRevoker.Revoke(cleanupContext, grantRevocation(binding))
			cancel()
			closeErr := journal.Close()
			if localErr != nil || remoteErr != nil || closeErr != nil {
				return nil, ErrCleanup
			}
			return nil, ErrAmbiguousRecovery
		}
	}
	relay, err := codingrelay.New(ctx, codingrelay.Config{
		Policy: config.Policy, Binding: binding, Upstream: recoveryUpstream{}, Journal: journal,
		Now: config.Now, OperationTimeout: config.OperationTimeout,
	})
	if err != nil {
		result := ErrRecoveryUnavailable
		if errors.Is(err, codingrelay.ErrAmbiguousDispatch) {
			result = ErrAmbiguousRecovery
		}
		return nil, cleanupActivationFailure(
			config.GrantRevoker, binding, cleanupTimeout, nil, nil, nil, journal, result,
		)
	}
	gateway := &Gateway{
		binding: binding, relay: relay, journal: journal,
		revoker: config.GrantRevoker, outerRevoked: true,
	}
	if err := gateway.Revoke(ctx); err != nil {
		if gateway.forceCloseAfterRecoveryFailure(cleanupTimeout) != nil {
			return nil, ErrCleanup
		}
		return nil, err
	}
	return gateway, nil
}

func (gateway *Gateway) forceCloseAfterRecoveryFailure(timeout time.Duration) error {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	var failed bool
	if gateway.relay != nil && gateway.relay.Revoke(ctx) != nil {
		failed = true
	}
	if gateway.revoker != nil && gateway.revoker.Revoke(ctx, grantRevocation(gateway.binding)) != nil {
		failed = true
	}
	if gateway.journal != nil && gateway.journal.Close() != nil {
		failed = true
	}
	if failed {
		return ErrCleanup
	}
	return nil
}
