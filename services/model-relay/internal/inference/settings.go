package inference

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"sync/atomic"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/omniaura/go-kit/tasker"

	"github.com/ditto-assistant/model-relay/internal/config"
	"github.com/ditto-assistant/model-relay/internal/postgres"
)

// Shipped defaults and hard ceilings, mirroring
// ditto/api_models/inference_concurrency_settings.py. The defaults are
// identical to the config fallbacks, so an empty settings board and an absent
// resolver behave the same.
const (
	defaultChatRequestBudget                = 16384
	maxChatRequestBudget                    = 32768
	defaultChatTokenBudget                  = 25_000_000
	maxChatTokenBudget                      = 200_000_000
	defaultChatPerTicketConcurrency         = 16
	defaultChatPerValidatorConcurrency      = 48
	defaultChatGlobalConcurrency            = 96
	maxChatConcurrency                      = 512
	defaultEmbeddingPerTicketConcurrency    = 12
	defaultEmbeddingPerValidatorConcurrency = 48
	defaultEmbeddingGlobalConcurrency       = 96
	maxEmbeddingConcurrency                 = 512
	settingsRefreshInterval                 = 5 * time.Second
)

// concurrencySettings is the whole stored admission policy
// (InferenceConcurrencySettings).
type concurrencySettings struct {
	ChatRequestBudget                int64
	ChatTokenBudget                  int64
	ChatPerTicketConcurrency         int
	ChatPerValidatorConcurrency      int
	ChatGlobalConcurrency            int
	EmbeddingPerTicketConcurrency    int
	EmbeddingPerValidatorConcurrency int
	EmbeddingGlobalConcurrency       int
}

func defaultConcurrencySettings() concurrencySettings {
	return concurrencySettings{
		ChatRequestBudget:                defaultChatRequestBudget,
		ChatTokenBudget:                  defaultChatTokenBudget,
		ChatPerTicketConcurrency:         defaultChatPerTicketConcurrency,
		ChatPerValidatorConcurrency:      defaultChatPerValidatorConcurrency,
		ChatGlobalConcurrency:            defaultChatGlobalConcurrency,
		EmbeddingPerTicketConcurrency:    defaultEmbeddingPerTicketConcurrency,
		EmbeddingPerValidatorConcurrency: defaultEmbeddingPerValidatorConcurrency,
		EmbeddingGlobalConcurrency:       defaultEmbeddingGlobalConcurrency,
	}
}

// parseConcurrencySettings validates known settings with the same strict types,
// field bounds, and hierarchy rules as Pydantic. Unknown fields are ignored so
// an older relay can consume a revision written by a newer Platform. Any known
// field violation returns an error and preserves the last-known-good snapshot.
func parseConcurrencySettings(payload []byte) (concurrencySettings, error) {
	out := defaultConcurrencySettings()
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(payload, &raw); err != nil {
		return out, err
	}
	intField := func(key string, dst *int64, minv, maxv int64) error {
		rawValue, ok := raw[key]
		if !ok {
			return nil
		}
		delete(raw, key)
		var num json.Number
		dec := json.NewDecoder(bytes.NewReader(rawValue))
		dec.UseNumber()
		if err := dec.Decode(&num); err != nil {
			return err
		}
		n, err := num.Int64()
		if err != nil {
			return err
		}
		// Pydantic strict ints reject floats; json.Number.Int64 rejects
		// fractional literals already ("12.5" errors). "12.0" also errors,
		// matching strict mode.
		if n < minv || n > maxv {
			return errors.New("out of range: " + key)
		}
		*dst = n
		return nil
	}
	var chatPerTicket, chatPerValidator, chatGlobal int64 = int64(out.ChatPerTicketConcurrency),
		int64(out.ChatPerValidatorConcurrency), int64(out.ChatGlobalConcurrency)
	var embeddingPerTicket, embeddingPerValidator, embeddingGlobal int64 = int64(out.EmbeddingPerTicketConcurrency),
		int64(out.EmbeddingPerValidatorConcurrency), int64(out.EmbeddingGlobalConcurrency)
	if err := intField("chat_request_budget", &out.ChatRequestBudget, 1, maxChatRequestBudget); err != nil {
		return defaultConcurrencySettings(), err
	}
	if err := intField("chat_token_budget", &out.ChatTokenBudget, 1, maxChatTokenBudget); err != nil {
		return defaultConcurrencySettings(), err
	}
	if err := intField("chat_per_ticket_concurrency", &chatPerTicket, 1, maxChatConcurrency); err != nil {
		return defaultConcurrencySettings(), err
	}
	if err := intField("chat_per_validator_concurrency", &chatPerValidator, 1, maxChatConcurrency); err != nil {
		return defaultConcurrencySettings(), err
	}
	if err := intField("chat_global_concurrency", &chatGlobal, 1, maxChatConcurrency); err != nil {
		return defaultConcurrencySettings(), err
	}
	if err := intField("embedding_per_ticket_concurrency", &embeddingPerTicket, 1, maxEmbeddingConcurrency); err != nil {
		return defaultConcurrencySettings(), err
	}
	if err := intField("embedding_per_validator_concurrency", &embeddingPerValidator, 1, maxEmbeddingConcurrency); err != nil {
		return defaultConcurrencySettings(), err
	}
	if err := intField("embedding_global_concurrency", &embeddingGlobal, 1, maxEmbeddingConcurrency); err != nil {
		return defaultConcurrencySettings(), err
	}
	if chatPerTicket > chatPerValidator || chatPerValidator > chatGlobal {
		return defaultConcurrencySettings(), errors.New("chat concurrency hierarchy violated")
	}
	if embeddingPerTicket > embeddingPerValidator || embeddingPerValidator > embeddingGlobal {
		return defaultConcurrencySettings(), errors.New("embedding concurrency hierarchy violated")
	}
	out.ChatPerTicketConcurrency = int(chatPerTicket)
	out.ChatPerValidatorConcurrency = int(chatPerValidator)
	out.ChatGlobalConcurrency = int(chatGlobal)
	out.EmbeddingPerTicketConcurrency = int(embeddingPerTicket)
	out.EmbeddingPerValidatorConcurrency = int(embeddingPerValidator)
	out.EmbeddingGlobalConcurrency = int(embeddingGlobal)
	return out, nil
}

// SettingsResolver owns a last-known-good atomic snapshot of the operator
// concurrency board. A go-kit/tasker refreshes it every five seconds on its own
// pooled connection, never from request traffic and never inside an admission
// transaction. Refresh failures preserve the last-known-good policy.
type SettingsResolver struct {
	load    func(context.Context) (postgres.InferenceConcurrencySettingsRevision, error)
	logger  *slog.Logger
	current atomic.Pointer[concurrencySettings]
}

// NewSettingsResolver builds the resolver over the shared pool-backed
// queries.
func NewSettingsResolver(queries *postgres.Queries, logger *slog.Logger) *SettingsResolver {
	return newSettingsResolver(queries.GetLatestInferenceConcurrencySettings, logger)
}

func newSettingsResolver(
	load func(context.Context) (postgres.InferenceConcurrencySettingsRevision, error),
	logger *slog.Logger,
) *SettingsResolver {
	r := &SettingsResolver{load: load, logger: logger}
	defaults := defaultConcurrencySettings()
	r.current.Store(&defaults)
	return r
}

// Resolve returns the current in-memory policy without locking or querying DB.
func (r *SettingsResolver) Resolve() concurrencySettings {
	if current := r.current.Load(); current != nil {
		return *current
	}
	return defaultConcurrencySettings()
}

// Refresh loads and validates the latest whole-policy revision. It only swaps
// the atomic snapshot after a successful read and decode.
func (r *SettingsResolver) Refresh(ctx context.Context) error {
	row, err := r.load(ctx)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			settings := defaultConcurrencySettings()
			r.current.Store(&settings)
			return nil
		}
		return fmt.Errorf("read inference concurrency settings: %w", err)
	}
	settings, perr := parseConcurrencySettings(row.Settings)
	if perr != nil {
		return fmt.Errorf("decode inference concurrency settings revision %d: %w", row.Revision, perr)
	}
	r.current.Store(&settings)
	return nil
}

// StartRefresh starts the go-kit/tasker loop. A transient refresh failure is
// logged and swallowed so the loop continues while admissions keep using the
// last-known-good snapshot.
func (r *SettingsResolver) StartRefresh(ctx context.Context) (<-chan error, error) {
	return r.startRefresh(ctx, settingsRefreshInterval)
}

func (r *SettingsResolver) startRefresh(ctx context.Context, interval time.Duration) (<-chan error, error) {
	refresh, err := tasker.New(r, func(ctx context.Context, resolver *SettingsResolver) error {
		if err := resolver.Refresh(ctx); err != nil {
			resolver.logger.Warn("could not refresh inference concurrency settings; keeping last-known-good policy",
				slog.String("error", err.Error()))
		}
		return nil
	}, tasker.WithInterval(interval))
	if err != nil {
		return nil, err
	}
	return refresh.Start(ctx), nil
}

// applySettings overlays the resolved policy onto a fallback config copy
// (apply_settings). The overlaid chat budgets are inert at admission because
// grants compare their stamped columns; both chat and embedding concurrency
// limits are live fields.
func applySettings(cfg config.InferenceProxyConfig, s concurrencySettings) config.InferenceProxyConfig {
	cfg.RequestBudget = int(s.ChatRequestBudget)
	cfg.TokenBudget = s.ChatTokenBudget
	cfg.TicketConcurrency = s.ChatPerTicketConcurrency
	cfg.ValidatorConcurrency = s.ChatPerValidatorConcurrency
	cfg.GlobalConcurrency = s.ChatGlobalConcurrency
	cfg.EmbeddingTicketConcurrency = s.EmbeddingPerTicketConcurrency
	cfg.EmbeddingValidatorConcurrency = s.EmbeddingPerValidatorConcurrency
	cfg.EmbeddingGlobalConcurrency = s.EmbeddingGlobalConcurrency
	return cfg
}
