package inference

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"sync/atomic"
	"testing"
	"time"

	"github.com/ditto-assistant/model-relay/internal/config"
	"github.com/ditto-assistant/model-relay/internal/postgres"
)

func settingsRow(revision int32, payload string) postgres.InferenceConcurrencySettingsRevision {
	return postgres.InferenceConcurrencySettingsRevision{
		Revision: revision,
		Settings: []byte(payload),
	}
}

func discardLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

func TestParseConcurrencySettingsChatRequestCeiling(t *testing.T) {
	payload := []byte(`{
		"chat_request_budget": 32768,
		"chat_token_budget": 25000000,
		"embedding_per_ticket_concurrency": 8,
		"embedding_per_validator_concurrency": 24,
		"embedding_global_concurrency": 32
	}`)
	settings, err := parseConcurrencySettings(payload)
	if err != nil {
		t.Fatalf("32768 hard ceiling must parse: %v", err)
	}
	if settings.ChatRequestBudget != 32768 {
		t.Fatalf("chat request budget = %d, want 32768", settings.ChatRequestBudget)
	}

	payload = []byte(`{
		"chat_request_budget": 32769,
		"chat_token_budget": 25000000,
		"embedding_per_ticket_concurrency": 8,
		"embedding_per_validator_concurrency": 24,
		"embedding_global_concurrency": 32
	}`)
	if _, err := parseConcurrencySettings(payload); err == nil {
		t.Fatal("over-ceiling chat request budget must be rejected")
	}
}

func TestParseConcurrencySettingsChatTokenCeiling(t *testing.T) {
	payload := []byte(`{
		"chat_request_budget": 8192,
		"chat_token_budget": 200000000,
		"embedding_per_ticket_concurrency": 8,
		"embedding_per_validator_concurrency": 24,
		"embedding_global_concurrency": 32
	}`)
	settings, err := parseConcurrencySettings(payload)
	if err != nil {
		t.Fatalf("100M hard ceiling must parse: %v", err)
	}
	if settings.ChatTokenBudget != 200_000_000 {
		t.Fatalf("chat token budget = %d, want 200000000", settings.ChatTokenBudget)
	}

	payload = []byte(`{
		"chat_request_budget": 8192,
		"chat_token_budget": 200000001,
		"embedding_per_ticket_concurrency": 8,
		"embedding_per_validator_concurrency": 24,
		"embedding_global_concurrency": 32
	}`)
	if _, err := parseConcurrencySettings(payload); err == nil {
		t.Fatal("over-ceiling chat token budget must be rejected")
	}
}

func TestParseConcurrencySettingsBackfillsChatDefaultsForOlderRevision(t *testing.T) {
	settings, err := parseConcurrencySettings([]byte(`{
		"chat_request_budget":8192,
		"chat_token_budget":50000000,
		"embedding_per_ticket_concurrency":8,
		"embedding_per_validator_concurrency":24,
		"embedding_global_concurrency":32
	}`))
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if settings.ChatPerTicketConcurrency != 16 || settings.ChatPerValidatorConcurrency != 48 || settings.ChatGlobalConcurrency != 96 {
		t.Fatalf("older revision must inherit chat concurrency defaults: %+v", settings)
	}
}

func TestParseConcurrencySettingsReadsChatHierarchy(t *testing.T) {
	settings, err := parseConcurrencySettings([]byte(`{
		"chat_per_ticket_concurrency":24,
		"chat_per_validator_concurrency":64,
		"chat_global_concurrency":112
	}`))
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if settings.ChatPerTicketConcurrency != 24 || settings.ChatPerValidatorConcurrency != 64 || settings.ChatGlobalConcurrency != 112 {
		t.Fatalf("chat concurrency mismatch: %+v", settings)
	}
}

func TestParseConcurrencySettingsIgnoresUnknownFields(t *testing.T) {
	settings, err := parseConcurrencySettings([]byte(`{
		"chat_per_ticket_concurrency":24,
		"chat_per_validator_concurrency":64,
		"chat_global_concurrency":112,
		"future_admission_control":true
	}`))
	if err != nil {
		t.Fatalf("additive settings field must be ignored: %v", err)
	}
	if settings.ChatGlobalConcurrency != 112 {
		t.Fatalf("known settings must still parse: %+v", settings)
	}
}

func TestParseConcurrencySettingsAcceptsShared512Ceiling(t *testing.T) {
	settings, err := parseConcurrencySettings([]byte(`{
		"chat_per_ticket_concurrency":512,
		"chat_per_validator_concurrency":512,
		"chat_global_concurrency":512,
		"embedding_per_ticket_concurrency":512,
		"embedding_per_validator_concurrency":512,
		"embedding_global_concurrency":512
	}`))
	if err != nil {
		t.Fatalf("parse shared hard ceiling: %v", err)
	}
	if settings.ChatGlobalConcurrency != 512 || settings.EmbeddingGlobalConcurrency != 512 {
		t.Fatalf("shared hard ceiling mismatch: %+v", settings)
	}

	if _, err := parseConcurrencySettings([]byte(`{
		"chat_per_ticket_concurrency":513,
		"chat_per_validator_concurrency":513,
		"chat_global_concurrency":513
	}`)); err == nil {
		t.Fatal("over-ceiling chat concurrency must be rejected")
	}
}

func TestParseConcurrencySettingsRejectsChatHierarchy(t *testing.T) {
	_, err := parseConcurrencySettings([]byte(`{
		"chat_per_ticket_concurrency":64,
		"chat_per_validator_concurrency":32,
		"chat_global_concurrency":96
	}`))
	if err == nil {
		t.Fatal("inverted chat hierarchy must be rejected")
	}
}

func TestSettingsResolverKeepsLastKnownGoodOnRefreshFailure(t *testing.T) {
	var calls atomic.Int32
	resolver := newSettingsResolver(func(context.Context) (postgres.InferenceConcurrencySettingsRevision, error) {
		if calls.Add(1) == 1 {
			return settingsRow(1, `{
				"chat_per_ticket_concurrency":24,
				"chat_per_validator_concurrency":64,
				"chat_global_concurrency":112
			}`), nil
		}
		return postgres.InferenceConcurrencySettingsRevision{}, errors.New("database unavailable")
	}, discardLogger())

	if err := resolver.Refresh(context.Background()); err != nil {
		t.Fatalf("first refresh: %v", err)
	}
	if err := resolver.Refresh(context.Background()); err == nil {
		t.Fatal("second refresh must report the database failure")
	}
	if got := resolver.Resolve().ChatGlobalConcurrency; got != 112 {
		t.Fatalf("refresh failure replaced last-known-good policy: got %d", got)
	}
}

func TestSettingsResolverTaskerRefreshesInBackground(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	var calls atomic.Int32
	resolver := newSettingsResolver(func(context.Context) (postgres.InferenceConcurrencySettingsRevision, error) {
		call := calls.Add(1)
		global := "96"
		if call >= 2 {
			global = "112"
			cancel()
		}
		return settingsRow(call, `{
			"chat_per_ticket_concurrency":16,
			"chat_per_validator_concurrency":48,
			"chat_global_concurrency":`+global+`
		}`), nil
	}, discardLogger())

	errs, err := resolver.startRefresh(ctx, time.Millisecond)
	if err != nil {
		t.Fatalf("start refresh: %v", err)
	}
	select {
	case refreshErr, ok := <-errs:
		if ok {
			t.Fatalf("refresh task stopped with error: %v", refreshErr)
		}
	case <-time.After(time.Second):
		t.Fatal("timed out waiting for background refresh")
	}
	if got := resolver.Resolve().ChatGlobalConcurrency; got != 112 {
		t.Fatalf("background refresh did not publish latest policy: got %d", got)
	}
}

func TestBoardRequestsPerMinuteReachAdmission(t *testing.T) {
	settings, err := parseConcurrencySettings([]byte(`{
		"chat_per_ticket_requests_per_minute":5,
		"chat_per_validator_requests_per_minute":50,
		"chat_global_requests_per_minute":500,
		"embedding_per_ticket_requests_per_minute":6,
		"embedding_per_validator_requests_per_minute":60,
		"embedding_global_requests_per_minute":600
	}`))
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	// Boot-time env values must not win over the live Backroom board.
	boot := config.InferenceProxyConfig{
		TicketRPM: 1920, ValidatorRPM: 7680, GlobalRPM: 23040,
		EmbeddingTicketRPM: 10_000, EmbeddingValidatorRPM: 40_000, EmbeddingGlobalRPM: 100_000,
	}
	chat := limitsForKind(applySettings(boot, settings), kindChat)
	if chat.perTicketRPM != 5 || chat.perValidatorRPM != 50 || chat.globalRPM != 500 {
		t.Fatalf("chat admission RPM = %+v, want board 5/50/500", chat)
	}
	embedding := limitsForKind(applySettings(boot, settings), kindEmbedding)
	if embedding.perTicketRPM != 6 || embedding.perValidatorRPM != 60 || embedding.globalRPM != 600 {
		t.Fatalf("embedding admission RPM = %+v, want board 6/60/600", embedding)
	}
}

func TestOlderBoardRevisionUsesShippedRequestsPerMinuteDefaults(t *testing.T) {
	settings, err := parseConcurrencySettings([]byte(`{"chat_per_ticket_concurrency":16}`))
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	chat := limitsForKind(applySettings(config.InferenceProxyConfig{}, settings), kindChat)
	if chat.perTicketRPM != 1920 || chat.perValidatorRPM != 7680 || chat.globalRPM != 23040 {
		t.Fatalf("chat RPM defaults = %+v, want 1920/7680/23040", chat)
	}
	embedding := limitsForKind(applySettings(config.InferenceProxyConfig{}, settings), kindEmbedding)
	if embedding.perTicketRPM != 10_000 || embedding.perValidatorRPM != 40_000 || embedding.globalRPM != 100_000 {
		t.Fatalf("embedding RPM defaults = %+v, want 10000/40000/100000", embedding)
	}
}

func TestParseConcurrencySettingsRejectsInvalidRequestsPerMinute(t *testing.T) {
	for name, payload := range map[string]string{
		"zero":                `{"chat_per_ticket_requests_per_minute":0}`,
		"over ceiling":        `{"embedding_global_requests_per_minute":100001}`,
		"chat hierarchy":      `{"chat_per_ticket_requests_per_minute":8000,"chat_per_validator_requests_per_minute":7680}`,
		"embedding hierarchy": `{"embedding_per_validator_requests_per_minute":100000,"embedding_global_requests_per_minute":50000}`,
		"fractional":          `{"chat_global_requests_per_minute":23040.5}`,
	} {
		if _, err := parseConcurrencySettings([]byte(payload)); err == nil {
			t.Fatalf("%s: invalid requests-per-minute board must be rejected", name)
		}
	}
}
