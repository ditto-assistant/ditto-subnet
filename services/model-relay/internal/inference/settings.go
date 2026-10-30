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
	defaultChatPerTicketRPM                 = 1920
	defaultChatPerValidatorRPM              = 7680
	defaultChatGlobalRPM                    = 23040
	defaultEmbeddingPerTicketRPM            = 10_000
	defaultEmbeddingPerValidatorRPM         = 40_000
	defaultEmbeddingGlobalRPM               = 100_000
	maxRequestsPerMinute                    = 100_000
	defaultDecisionPerTicketConcurrency     = 8
	defaultDecisionPerValidatorConcurrency  = 32
	defaultDecisionGlobalConcurrency        = 64
	maxDecisionConcurrency                  = 512
	defaultDecisionPerTicketRPM             = 600
	defaultDecisionPerValidatorRPM          = 2400
	defaultDecisionGlobalRPM                = 6000
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
	ChatPerTicketRPM                 int
	ChatPerValidatorRPM              int
	ChatGlobalRPM                    int
	EmbeddingPerTicketRPM            int
	EmbeddingPerValidatorRPM         int
	EmbeddingGlobalRPM               int
	// Decision is the nested decision_lane object. Enabled is a live kill
	// switch here; the pinned model, price and walls are read from the grant.
	Decision decisionLaneSettings
	// ScoredProviderRouting mirrors scored_provider_routing: which OpenRouter
	// provider preference the aggregate scored chat route sends.
	ScoredProviderRouting string
}

// decisionLaneSettings mirrors DecisionLaneSettings' live fields.
type decisionLaneSettings struct {
	Enabled                 bool
	PerTicketConcurrency    int
	PerValidatorConcurrency int
	GlobalConcurrency       int
	PerTicketRPM            int
	PerValidatorRPM         int
	GlobalRPM               int
}

func defaultDecisionLaneSettings() decisionLaneSettings {
	return decisionLaneSettings{
		PerTicketConcurrency:    defaultDecisionPerTicketConcurrency,
		PerValidatorConcurrency: defaultDecisionPerValidatorConcurrency,
		GlobalConcurrency:       defaultDecisionGlobalConcurrency,
		PerTicketRPM:            defaultDecisionPerTicketRPM,
		PerValidatorRPM:         defaultDecisionPerValidatorRPM,
		GlobalRPM:               defaultDecisionGlobalRPM,
	}
}

// parseDecisionLane validates the nested decision_lane object with the same
// strict types, bounds and hierarchy as Pydantic. Unknown keys (model, price,
// budgets: grant-stamped at mint) are ignored.
func parseDecisionLane(raw json.RawMessage) (decisionLaneSettings, error) {
	out := defaultDecisionLaneSettings()
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(raw, &fields); err != nil {
		return out, err
	}
	if rawEnabled, ok := fields["enabled"]; ok {
		var enabled bool
		if err := json.Unmarshal(rawEnabled, &enabled); err != nil {
			return out, errors.New("decision_lane.enabled must be a boolean")
		}
		out.Enabled = enabled
	}
	if rawModel, ok := fields["model"]; ok && out.Enabled {
		var model *string
		if err := json.Unmarshal(rawModel, &model); err != nil || model == nil || *model == "" {
			return out, errors.New("an enabled decision_lane must pin a model")
		}
	} else if out.Enabled {
		return out, errors.New("an enabled decision_lane must pin a model")
	}
	for _, field := range []struct {
		key  string
		dst  *int
		maxv int64
	}{
		{"per_ticket_concurrency", &out.PerTicketConcurrency, maxDecisionConcurrency},
		{"per_validator_concurrency", &out.PerValidatorConcurrency, maxDecisionConcurrency},
		{"global_concurrency", &out.GlobalConcurrency, maxDecisionConcurrency},
		{"per_ticket_requests_per_minute", &out.PerTicketRPM, maxRequestsPerMinute},
		{"per_validator_requests_per_minute", &out.PerValidatorRPM, maxRequestsPerMinute},
		{"global_requests_per_minute", &out.GlobalRPM, maxRequestsPerMinute},
	} {
		rawValue, ok := fields[field.key]
		if !ok {
			continue
		}
		var num json.Number
		dec := json.NewDecoder(bytes.NewReader(rawValue))
		dec.UseNumber()
		if err := dec.Decode(&num); err != nil {
			return out, err
		}
		n, err := num.Int64()
		if err != nil {
			return out, err
		}
		if n < 1 || n > field.maxv {
			return out, errors.New("out of range: decision_lane." + field.key)
		}
		*field.dst = int(n)
	}
	if out.PerTicketConcurrency > out.PerValidatorConcurrency || out.PerValidatorConcurrency > out.GlobalConcurrency {
		return out, errors.New("decision concurrency hierarchy violated")
	}
	if out.PerTicketRPM > out.PerValidatorRPM || out.PerValidatorRPM > out.GlobalRPM {
		return out, errors.New("decision requests-per-minute hierarchy violated")
	}
	return out, nil
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
		ChatPerTicketRPM:                 defaultChatPerTicketRPM,
		ChatPerValidatorRPM:              defaultChatPerValidatorRPM,
		ChatGlobalRPM:                    defaultChatGlobalRPM,
		EmbeddingPerTicketRPM:            defaultEmbeddingPerTicketRPM,
		EmbeddingPerValidatorRPM:         defaultEmbeddingPerValidatorRPM,
		EmbeddingGlobalRPM:               defaultEmbeddingGlobalRPM,
		Decision:                         defaultDecisionLaneSettings(),
		ScoredProviderRouting:            config.ScoredProviderRoutingThroughput,
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
	chatTicketRPM, chatValidatorRPM, chatGlobalRPM := int64(out.ChatPerTicketRPM),
		int64(out.ChatPerValidatorRPM), int64(out.ChatGlobalRPM)
	embeddingTicketRPM, embeddingValidatorRPM, embeddingGlobalRPM := int64(out.EmbeddingPerTicketRPM),
		int64(out.EmbeddingPerValidatorRPM), int64(out.EmbeddingGlobalRPM)
	for _, field := range []struct {
		key string
		dst *int64
	}{
		{"chat_per_ticket_requests_per_minute", &chatTicketRPM},
		{"chat_per_validator_requests_per_minute", &chatValidatorRPM},
		{"chat_global_requests_per_minute", &chatGlobalRPM},
		{"embedding_per_ticket_requests_per_minute", &embeddingTicketRPM},
		{"embedding_per_validator_requests_per_minute", &embeddingValidatorRPM},
		{"embedding_global_requests_per_minute", &embeddingGlobalRPM},
	} {
		if err := intField(field.key, field.dst, 1, maxRequestsPerMinute); err != nil {
			return defaultConcurrencySettings(), err
		}
	}
	if chatTicketRPM > chatValidatorRPM || chatValidatorRPM > chatGlobalRPM {
		return defaultConcurrencySettings(), errors.New("chat requests-per-minute hierarchy violated")
	}
	if embeddingTicketRPM > embeddingValidatorRPM || embeddingValidatorRPM > embeddingGlobalRPM {
		return defaultConcurrencySettings(), errors.New("embedding requests-per-minute hierarchy violated")
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
	out.ChatPerTicketRPM = int(chatTicketRPM)
	out.ChatPerValidatorRPM = int(chatValidatorRPM)
	out.ChatGlobalRPM = int(chatGlobalRPM)
	out.EmbeddingPerTicketRPM = int(embeddingTicketRPM)
	out.EmbeddingPerValidatorRPM = int(embeddingValidatorRPM)
	out.EmbeddingGlobalRPM = int(embeddingGlobalRPM)
	if rawDecision, ok := raw["decision_lane"]; ok {
		decision, err := parseDecisionLane(rawDecision)
		if err != nil {
			return defaultConcurrencySettings(), err
		}
		out.Decision = decision
	}
	if rawRouting, ok := raw["scored_provider_routing"]; ok {
		var routing string
		if err := json.Unmarshal(rawRouting, &routing); err != nil {
			return defaultConcurrencySettings(), errors.New("scored_provider_routing must be a string")
		}
		out.ScoredProviderRouting = normalizeScoredProviderRouting(routing)
	}
	return out, nil
}

// normalizeScoredProviderRouting maps a stored routing mode onto one this
// relay knows. A mode written by a newer Platform falls back to throughput,
// the historical route, instead of failing the whole board and freezing every
// other admission limit at its last-known-good value.
func normalizeScoredProviderRouting(routing string) string {
	switch routing {
	case config.ScoredProviderRoutingBF16PriceOrder, config.ScoredProviderRoutingBF16Canary:
		return routing
	default:
		return config.ScoredProviderRoutingThroughput
	}
}

// effectiveScoredProviderRouting resolves the board mode for one ticket. The
// canary mode moves only benchmark_canary tickets, so an operator can compare
// a canary run against accepted scores before moving the fleet.
func effectiveScoredProviderRouting(routing, ticketPurpose string) string {
	switch routing {
	case config.ScoredProviderRoutingBF16PriceOrder:
		return config.ScoredProviderRoutingBF16PriceOrder
	case config.ScoredProviderRoutingBF16Canary:
		if ticketPurpose == ticketPurposeBenchmarkCanary {
			return config.ScoredProviderRoutingBF16PriceOrder
		}
	}
	return config.ScoredProviderRoutingThroughput
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
// grants compare their stamped columns; chat and embedding concurrency and
// requests-per-minute limits are live fields, as on the Platform side.
func applySettings(cfg config.InferenceProxyConfig, s concurrencySettings) config.InferenceProxyConfig {
	cfg.RequestBudget = int(s.ChatRequestBudget)
	cfg.TokenBudget = s.ChatTokenBudget
	cfg.TicketConcurrency = s.ChatPerTicketConcurrency
	cfg.ValidatorConcurrency = s.ChatPerValidatorConcurrency
	cfg.GlobalConcurrency = s.ChatGlobalConcurrency
	cfg.EmbeddingTicketConcurrency = s.EmbeddingPerTicketConcurrency
	cfg.EmbeddingValidatorConcurrency = s.EmbeddingPerValidatorConcurrency
	cfg.EmbeddingGlobalConcurrency = s.EmbeddingGlobalConcurrency
	cfg.TicketRPM = s.ChatPerTicketRPM
	cfg.ValidatorRPM = s.ChatPerValidatorRPM
	cfg.GlobalRPM = s.ChatGlobalRPM
	cfg.EmbeddingTicketRPM = s.EmbeddingPerTicketRPM
	cfg.EmbeddingValidatorRPM = s.EmbeddingPerValidatorRPM
	cfg.EmbeddingGlobalRPM = s.EmbeddingGlobalRPM
	cfg.DecisionEnabled = s.Decision.Enabled
	cfg.DecisionTicketConcurrency = s.Decision.PerTicketConcurrency
	cfg.DecisionValidatorConcurrency = s.Decision.PerValidatorConcurrency
	cfg.DecisionGlobalConcurrency = s.Decision.GlobalConcurrency
	cfg.DecisionTicketRPM = s.Decision.PerTicketRPM
	cfg.DecisionValidatorRPM = s.Decision.PerValidatorRPM
	cfg.DecisionGlobalRPM = s.Decision.GlobalRPM
	cfg.ScoredProviderRouting = s.ScoredProviderRouting
	return cfg
}
