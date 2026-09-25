package inference

import (
	"context"
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgtype"

	"github.com/ditto-assistant/model-relay/internal/config"
	"github.com/ditto-assistant/model-relay/internal/testutil"
)

func TestDittoRouterOnBehalfOfFollowsTheActiveLinkOnly(t *testing.T) {
	f := newPGFixture(t, chatTestConfig(t, "https://openrouter.ai/api/v1/chat/completions"))
	f.deps.Cfg.DittoRouter = config.DittoRouterConfig{Enabled: true, APIKey: "dk", Lanes: []string{config.DittoRouterLaneCompetition}}
	ctx := context.Background()
	agent := pgtype.UUID{Bytes: f.agentID, Valid: true}

	if user, ok := f.deps.dittoRouterOnBehalfOf(ctx, agent); ok || user != "" {
		t.Fatalf("no link yet: %q %v", user, ok)
	}
	testutil.SeedSQL(t, f.pool,
		`INSERT INTO miner_ditto_links (miner_hotkey, ditto_user_id, ditto_email, linked_via)
		 VALUES ('miner-hotkey', 'ditto-user-9', 'm@example.com', 'cli')`)
	user, ok := f.deps.dittoRouterOnBehalfOf(ctx, agent)
	if !ok || user != "ditto-user-9" {
		t.Fatalf("active link: %q %v", user, ok)
	}
	// Another miner's agent never inherits the link.
	other := uuid.New()
	testutil.SeedSQL(t, f.pool,
		`INSERT INTO agents (agent_id, miner_hotkey, name, sha256) VALUES ($1, 'other-hotkey', 'o', repeat('b', 64))`, other)
	if user, ok := f.deps.dittoRouterOnBehalfOf(ctx, pgtype.UUID{Bytes: other, Valid: true}); ok || user != "" {
		t.Fatalf("other miner must not be attributed: %q %v", user, ok)
	}
	testutil.SeedSQL(t, f.pool, `UPDATE miner_ditto_links SET revoked_at = now() WHERE miner_hotkey = 'miner-hotkey'`)
	if user, ok := f.deps.dittoRouterOnBehalfOf(ctx, agent); ok || user != "" {
		t.Fatalf("revoked link must not route: %q %v", user, ok)
	}
}
