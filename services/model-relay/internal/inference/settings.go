package inference

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"sync"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/ditto-assistant/model-relay/internal/config"
	"github.com/ditto-assistant/model-relay/internal/postgres"
)

// Shipped defaults and hard ceilings, mirroring
// ditto/api_models/inference_concurrency_settings.py. The defaults are
// identical to the boot-time config defaults, so an empty settings board and
// an absent resolver behave the same.
const (
	defaultChatRequestBudget                = 8192
	maxChatRequestBudget                    = 16384
	defaultChatTokenBudget                  = 25_000_000
	maxChatTokenBudget                      = 100_000_000
	defaultEmbeddingPerTicketConcurrency    = 12
	defaultEmbeddingPerValidatorConcurrency = 48
	defaultEmbeddingGlobalConcurrency       = 96
	maxEmbeddingConcurrency                 = 128
	settingsTTL                             = 5 * time.Second
)

// concurrencySettings is the whole stored admission policy
// (InferenceConcurrencySettings).
type concurrencySettings struct {
	ChatRequestBudget                int64
	ChatTokenBudget                  int64
	EmbeddingPerTicketConcurrency    int
	EmbeddingPerValidatorConcurrency int
	EmbeddingGlobalConcurrency       int
}

func defaultConcurrencySettings() concurrencySettings {
	return concurrencySettings{
		ChatRequestBudget:                defaultChatRequestBudget,
		ChatTokenBudget:                  defaultChatTokenBudget,
		EmbeddingPerTicketConcurrency:    defaultEmbeddingPerTicketConcurrency,
		EmbeddingPerValidatorConcurrency: defaultEmbeddingPerValidatorConcurrency,
		EmbeddingGlobalConcurrency:       defaultEmbeddingGlobalConcurrency,
	}
}

// parseConcurrencySettings validates a stored settings payload with the same
// strictness Pydantic applies (strict=True, extra="forbid", per-field bounds,
// hierarchy validator). Any violation returns an error and the caller falls
// open to the shipped defaults.
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
	var perTicket, perValidator, global int64 = int64(out.EmbeddingPerTicketConcurrency),
		int64(out.EmbeddingPerValidatorConcurrency), int64(out.EmbeddingGlobalConcurrency)
	if err := intField("chat_request_budget", &out.ChatRequestBudget, 1, maxChatRequestBudget); err != nil {
		return defaultConcurrencySettings(), err
	}
	if err := intField("chat_token_budget", &out.ChatTokenBudget, 1, maxChatTokenBudget); err != nil {
		return defaultConcurrencySettings(), err
	}
	if err := intField("embedding_per_ticket_concurrency", &perTicket, 1, maxEmbeddingConcurrency); err != nil {
		return defaultConcurrencySettings(), err
	}
	if err := intField("embedding_per_validator_concurrency", &perValidator, 1, maxEmbeddingConcurrency); err != nil {
		return defaultConcurrencySettings(), err
	}
	if err := intField("embedding_global_concurrency", &global, 1, maxEmbeddingConcurrency); err != nil {
		return defaultConcurrencySettings(), err
	}
	if len(raw) > 0 {
		return defaultConcurrencySettings(), errors.New("unknown settings fields")
	}
	if perTicket > perValidator || perValidator > global {
		return defaultConcurrencySettings(), errors.New("embedding concurrency hierarchy violated")
	}
	out.EmbeddingPerTicketConcurrency = int(perTicket)
	out.EmbeddingPerValidatorConcurrency = int(perValidator)
	out.EmbeddingGlobalConcurrency = int(global)
	return out, nil
}

// SettingsResolver is the 5s-TTL cache over the operator concurrency board
// (InferenceConcurrencySettingsResolver). It reads on its own pooled
// connection, NEVER inside the admission transaction. DB errors serve the
// shipped defaults WITHOUT caching them; corrupt payloads fail open to the
// defaults (which are cached, matching Python, since the read succeeded).
type SettingsResolver struct {
	queries *postgres.Queries
	logger  *slog.Logger
	ttl     time.Duration

	mu       sync.Mutex
	cached   *concurrencySettings
	loadedAt time.Time
}

// NewSettingsResolver builds the resolver over the shared pool-backed
// queries.
func NewSettingsResolver(queries *postgres.Queries, logger *slog.Logger) *SettingsResolver {
	return &SettingsResolver{queries: queries, logger: logger, ttl: settingsTTL}
}

// Resolve returns the current settings, cached for the TTL.
func (r *SettingsResolver) Resolve(ctx context.Context) concurrencySettings {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.cached != nil && time.Since(r.loadedAt) < r.ttl {
		return *r.cached
	}
	row, err := r.queries.GetLatestInferenceConcurrencySettings(ctx)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			// No operator override yet: shipped defaults, cached (the read
			// itself succeeded).
			settings := defaultConcurrencySettings()
			r.cached = &settings
			r.loadedAt = time.Now()
			return settings
		}
		// A database blip must not fail an inference request. Serve the
		// defaults and do NOT cache them, so the resolver recovers on the
		// next admission rather than pinning defaults for a full TTL.
		r.logger.Warn("could not read inference concurrency settings; using defaults",
			slog.String("error", err.Error()))
		return defaultConcurrencySettings()
	}
	settings, perr := parseConcurrencySettings(row.Settings)
	if perr != nil {
		r.logger.Warn("inference concurrency settings revision is invalid; using defaults",
			slog.Int("revision", int(row.Revision)), slog.String("error", perr.Error()))
		settings = defaultConcurrencySettings()
	}
	r.cached = &settings
	r.loadedAt = time.Now()
	return settings
}

// applySettings overlays the resolved policy onto a boot config copy
// (apply_settings). The overlaid chat budgets are inert at admission (grants
// compare their stamped columns); the three embedding concurrency limits are
// the live fields.
func applySettings(cfg config.InferenceProxyConfig, s concurrencySettings) config.InferenceProxyConfig {
	cfg.RequestBudget = int(s.ChatRequestBudget)
	cfg.TokenBudget = s.ChatTokenBudget
	cfg.EmbeddingTicketConcurrency = s.EmbeddingPerTicketConcurrency
	cfg.EmbeddingValidatorConcurrency = s.EmbeddingPerValidatorConcurrency
	cfg.EmbeddingGlobalConcurrency = s.EmbeddingGlobalConcurrency
	return cfg
}
