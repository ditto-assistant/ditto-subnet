package inference

import "testing"

func TestParseConcurrencySettingsChatTokenCeiling(t *testing.T) {
	payload := []byte(`{
		"chat_request_budget": 8192,
		"chat_token_budget": 100000000,
		"embedding_per_ticket_concurrency": 8,
		"embedding_per_validator_concurrency": 24,
		"embedding_global_concurrency": 32
	}`)
	settings, err := parseConcurrencySettings(payload)
	if err != nil {
		t.Fatalf("100M hard ceiling must parse: %v", err)
	}
	if settings.ChatTokenBudget != 100_000_000 {
		t.Fatalf("chat token budget = %d, want 100000000", settings.ChatTokenBudget)
	}

	payload = []byte(`{
		"chat_request_budget": 8192,
		"chat_token_budget": 100000001,
		"embedding_per_ticket_concurrency": 8,
		"embedding_per_validator_concurrency": 24,
		"embedding_global_concurrency": 32
	}`)
	if _, err := parseConcurrencySettings(payload); err == nil {
		t.Fatal("over-ceiling chat token budget must be rejected")
	}
}
