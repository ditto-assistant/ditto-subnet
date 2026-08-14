// Real-Postgres tests for the relay's query layer, following the backend's
// *_pg_test.go pattern: external test package, generated queries under an
// alias, effects asserted with raw SQL against the pool. Tests skip (never
// fail) when the monorepo test Postgres is unavailable.
package postgres_test

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgtype"
	"github.com/jackc/pgx/v5/pgxpool"

	dbpg "github.com/ditto-assistant/model-relay/internal/postgres"
	"github.com/ditto-assistant/model-relay/internal/testutil"
)

const (
	testHotkey       = "5FTestValidatorHotkeyAAAAAAAAAAAAAAAAAAAAAAAAAAA"
	testBenchVersion = 9
)

// fixture is one agent + issued ticket + active grant, the minimum state the
// relay's request path operates on.
type fixture struct {
	queries  *dbpg.Queries
	pool     *pgxpool.Pool
	agentID  uuid.UUID
	grantID  uuid.UUID
	deadline time.Time
}

func pgUUID(id uuid.UUID) pgtype.UUID {
	return pgtype.UUID{Bytes: id, Valid: true}
}

func pgTime(ts time.Time) pgtype.Timestamptz {
	return pgtype.Timestamptz{Time: ts, Valid: true}
}

func newFixture(t *testing.T) *fixture {
	t.Helper()
	queries, pool := testutil.NewTestPGQueries(t)

	agentID := uuid.New()
	grantID := uuid.New()
	deadline := time.Now().UTC().Add(30 * time.Minute).Truncate(time.Microsecond)

	testutil.SeedSQL(t, pool,
		`INSERT INTO agents (agent_id, miner_hotkey, name, sha256)
		 VALUES ($1, 'miner-hotkey', 'test-agent', repeat('a', 64))`, agentID)
	testutil.SeedSQL(t, pool,
		`INSERT INTO validator_tickets (agent_id, validator_hotkey, slot_id, status, deadline, bench_version, attempt_count)
		 VALUES ($1, $2, 'slot-0', 'issued', $3, $4, 1)`,
		agentID, testHotkey, deadline, testBenchVersion)
	testutil.SeedSQL(t, pool,
		`INSERT INTO inference_grants (
		    grant_id, agent_id, bench_version, validator_hotkey, slot_id,
		    ticket_deadline, status, bearer_digest, broker_public_key,
		    generation, allowed_models, request_budget, token_budget,
		    expires_at, usage_accounting_version)
		 VALUES ($1, $2, $3, $4, 'slot-0', $5, 'active',
		         'digest', 'broker-key', 1,
		         '["openai/gpt-oss-20b"]'::jsonb, 8192, 25000000, $5, 2)`,
		grantID, agentID, testBenchVersion, testHotkey, deadline)

	return &fixture{queries: queries, pool: pool, agentID: agentID, grantID: grantID, deadline: deadline}
}

// inTx runs fn inside a transaction with the ticket -> grant lock order
// already taken, mirroring the relay's transaction discipline.
func (f *fixture) inTx(t *testing.T, fn func(ctx context.Context, tx pgx.Tx, q *dbpg.Queries)) {
	t.Helper()
	ctx := t.Context()
	tx, err := f.pool.Begin(ctx)
	if err != nil {
		t.Fatalf("begin: %v", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	q := f.queries.WithTx(tx)

	// Lock rank 1: the owning ticket. Lock rank 2: the grant.
	if _, err := q.GetValidatorTicketForUpdate(ctx, dbpg.GetValidatorTicketForUpdateParams{
		AgentID:         pgUUID(f.agentID),
		BenchVersion:    testBenchVersion,
		ValidatorHotkey: testHotkey,
	}); err != nil {
		t.Fatalf("lock ticket: %v", err)
	}
	if _, err := q.GetInferenceGrantForUpdate(ctx, pgUUID(f.grantID)); err != nil {
		t.Fatalf("lock grant: %v", err)
	}

	fn(ctx, tx, q)
	if err := tx.Commit(ctx); err != nil {
		t.Fatalf("commit: %v", err)
	}
}

func isUniqueViolation(err error) bool {
	var pgErr *pgconn.PgError
	return errors.As(err, &pgErr) && pgErr.Code == "23505"
}

func TestConsumeValidatorNonceReplayIsSavepointContained(t *testing.T) {
	f := newFixture(t)
	ctx := t.Context()
	nonce := uuid.New()
	now := time.Now().UTC()

	tx, err := f.pool.Begin(ctx)
	if err != nil {
		t.Fatalf("begin: %v", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()

	consume := func(outer pgx.Tx) error {
		// Savepoint (pgx nested transaction): a replay must not poison the
		// outer transaction.
		sp, err := outer.Begin(ctx)
		if err != nil {
			t.Fatalf("savepoint: %v", err)
		}
		q := f.queries.WithTx(sp)
		if err := q.ConsumeValidatorNonce(ctx, dbpg.ConsumeValidatorNonceParams{
			Nonce:           pgUUID(nonce),
			ValidatorHotkey: testHotkey,
			UsedAt:          pgTime(now),
			ExpiresAt:       pgTime(now.Add(2 * time.Minute)),
		}); err != nil {
			_ = sp.Rollback(ctx)
			return err
		}
		return sp.Commit(ctx)
	}

	if err := consume(tx); err != nil {
		t.Fatalf("first consume: %v", err)
	}
	err = consume(tx)
	if !isUniqueViolation(err) {
		t.Fatalf("second consume: want unique violation, got %v", err)
	}

	// The outer transaction must still be usable after the savepoint rollback.
	var one int
	if err := tx.QueryRow(ctx, "SELECT 1").Scan(&one); err != nil {
		t.Fatalf("outer transaction poisoned after replay: %v", err)
	}
	if err := tx.Commit(ctx); err != nil {
		t.Fatalf("commit: %v", err)
	}

	var count int
	if err := f.pool.QueryRow(ctx,
		"SELECT count(*) FROM validator_request_nonces WHERE nonce = $1", nonce).Scan(&count); err != nil {
		t.Fatalf("count nonces: %v", err)
	}
	if count != 1 {
		t.Fatalf("want exactly 1 nonce row, got %d", count)
	}
}

func TestActivateInferenceGrantRotation(t *testing.T) {
	f := newFixture(t)
	now := time.Now().UTC().Truncate(time.Microsecond)

	f.inTx(t, func(ctx context.Context, _ pgx.Tx, q *dbpg.Queries) {
		if err := q.ActivateInferenceGrant(ctx, dbpg.ActivateInferenceGrantParams{
			BearerDigest:    "new-digest",
			BrokerPublicKey: "new-broker-key",
			SlotID:          "slot-0",
			ExpiresAt:       pgTime(f.deadline),
			Now:             pgTime(now),
			GrantID:         pgUUID(f.grantID),
		}); err != nil {
			t.Fatalf("activate: %v", err)
		}
	})

	var (
		digest, broker, status string
		generation             int
		active, embActive      int
	)
	if err := f.pool.QueryRow(t.Context(),
		`SELECT bearer_digest, broker_public_key, status, generation, active_requests, embedding_active_requests
		 FROM inference_grants WHERE grant_id = $1`, f.grantID,
	).Scan(&digest, &broker, &status, &generation, &active, &embActive); err != nil {
		t.Fatalf("read grant: %v", err)
	}
	if digest != "new-digest" || broker != "new-broker-key" || status != "active" {
		t.Fatalf("rotation write wrong: digest=%q broker=%q status=%q", digest, broker, status)
	}
	if generation != 2 {
		t.Fatalf("generation: want 2 (seeded 1, incremented), got %d", generation)
	}
	if active != 0 || embActive != 0 {
		t.Fatalf("active counters not zeroed: %d/%d", active, embActive)
	}
}

func TestInsertInferenceRequestNonceReplay(t *testing.T) {
	f := newFixture(t)
	nonce := uuid.New()
	now := time.Now().UTC().Truncate(time.Microsecond)

	insert := func(ctx context.Context, outer pgx.Tx) error {
		sp, err := outer.Begin(ctx)
		if err != nil {
			t.Fatalf("savepoint: %v", err)
		}
		q := f.queries.WithTx(sp)
		if _, err := q.InsertInferenceRequest(ctx, dbpg.InsertInferenceRequestParams{
			GrantID:             pgUUID(f.grantID),
			Nonce:               pgUUID(nonce),
			Generation:          1,
			RequestKind:         "chat",
			Model:               "openai/gpt-oss-20b",
			ReservedTokens:      1000,
			MaxChargeableTokens: 2000,
			StartedAt:           pgTime(now),
		}); err != nil {
			_ = sp.Rollback(ctx)
			return err
		}
		return sp.Commit(ctx)
	}

	f.inTx(t, func(ctx context.Context, tx pgx.Tx, q *dbpg.Queries) {
		if err := insert(ctx, tx); err != nil {
			t.Fatalf("first insert: %v", err)
		}
		if err := q.IncrementGrantChatAdmission(ctx, dbpg.IncrementGrantChatAdmissionParams{
			Now:     pgTime(now),
			GrantID: pgUUID(f.grantID),
		}); err != nil {
			t.Fatalf("increment admission: %v", err)
		}

		// Replay: the (grant_id, nonce) PK is the distributed replay guard.
		err := insert(ctx, tx)
		if !isUniqueViolation(err) {
			t.Fatalf("replay: want unique violation, got %v", err)
		}
	})

	var requestCount, activeRequests int
	if err := f.pool.QueryRow(t.Context(),
		"SELECT request_count, active_requests FROM inference_grants WHERE grant_id = $1", f.grantID,
	).Scan(&requestCount, &activeRequests); err != nil {
		t.Fatalf("read grant: %v", err)
	}
	if requestCount != 1 || activeRequests != 1 {
		t.Fatalf("admission counters: want 1/1, got %d/%d", requestCount, activeRequests)
	}
}

func TestStaleReclamationChargesReservationAndRecounts(t *testing.T) {
	f := newFixture(t)
	now := time.Now().UTC().Truncate(time.Microsecond)
	staleNonce := uuid.New()
	freshNonce := uuid.New()

	// One stale started request (older than the 2*timeout window) and one
	// fresh one.
	testutil.SeedSQL(t, f.pool,
		`INSERT INTO inference_requests (grant_id, nonce, generation, status, request_kind, model,
		    reserved_tokens, max_chargeable_tokens, prompt_tokens, completion_tokens, cost_microusd, started_at)
		 VALUES ($1, $2, 1, 'started', 'chat', 'openai/gpt-oss-20b', 500, 500, 0, 0, 0, $3)`,
		f.grantID, staleNonce, now.Add(-10*time.Minute))
	testutil.SeedSQL(t, f.pool,
		`INSERT INTO inference_requests (grant_id, nonce, generation, status, request_kind, model,
		    reserved_tokens, max_chargeable_tokens, prompt_tokens, completion_tokens, cost_microusd, started_at)
		 VALUES ($1, $2, 1, 'started', 'chat', 'openai/gpt-oss-20b', 700, 700, 0, 0, 0, $3)`,
		f.grantID, freshNonce, now)

	cutoff := now.Add(-3 * time.Minute) // 2 * 90s

	f.inTx(t, func(ctx context.Context, _ pgx.Tx, q *dbpg.Queries) {
		stale, err := q.ListStaleStartedInferenceRequestsForUpdate(ctx, dbpg.ListStaleStartedInferenceRequestsForUpdateParams{
			GrantID:     pgUUID(f.grantID),
			RequestKind: "chat",
			Cutoff:      pgTime(cutoff),
		})
		if err != nil {
			t.Fatalf("list stale: %v", err)
		}
		if len(stale) != 1 || stale[0].Nonce != pgUUID(staleNonce) {
			t.Fatalf("want exactly the stale request, got %d rows", len(stale))
		}

		for _, req := range stale {
			if err := q.CancelInferenceRequestChargingReservation(ctx, dbpg.CancelInferenceRequestChargingReservationParams{
				Now:     pgTime(now),
				GrantID: req.GrantID,
				Nonce:   req.Nonce,
			}); err != nil {
				t.Fatalf("cancel: %v", err)
			}
			if err := q.AddReclaimedChatTokens(ctx, dbpg.AddReclaimedChatTokensParams{
				ReservedTokens: req.ReservedTokens,
				Now:            pgTime(now),
				GrantID:        req.GrantID,
			}); err != nil {
				t.Fatalf("charge grant: %v", err)
			}
		}

		// Recount, not decrement.
		remaining, err := q.CountStartedInferenceRequests(ctx, dbpg.CountStartedInferenceRequestsParams{
			GrantID:     pgUUID(f.grantID),
			RequestKind: "chat",
		})
		if err != nil {
			t.Fatalf("recount: %v", err)
		}
		if remaining != 1 {
			t.Fatalf("recount: want 1 started row remaining, got %d", remaining)
		}
		if err := q.SetGrantChatActiveRequests(ctx, dbpg.SetGrantChatActiveRequestsParams{
			ActiveRequests: int32(remaining),
			Now:            pgTime(now),
			GrantID:        pgUUID(f.grantID),
		}); err != nil {
			t.Fatalf("write recount: %v", err)
		}
	})

	var reqStatus string
	var reqPromptTokens int64
	if err := f.pool.QueryRow(t.Context(),
		"SELECT status, prompt_tokens FROM inference_requests WHERE grant_id = $1 AND nonce = $2",
		f.grantID, staleNonce).Scan(&reqStatus, &reqPromptTokens); err != nil {
		t.Fatalf("read request: %v", err)
	}
	if reqStatus != "canceled" || reqPromptTokens != 500 {
		t.Fatalf("stale request: want canceled/500 (the ESTIMATE, not bytes), got %s/%d", reqStatus, reqPromptTokens)
	}

	var grantPromptTokens int64
	var grantActive int
	if err := f.pool.QueryRow(t.Context(),
		"SELECT prompt_tokens, active_requests FROM inference_grants WHERE grant_id = $1", f.grantID,
	).Scan(&grantPromptTokens, &grantActive); err != nil {
		t.Fatalf("read grant: %v", err)
	}
	if grantPromptTokens != 500 {
		t.Fatalf("grant charge: want 500 reclaimed prompt tokens, got %d", grantPromptTokens)
	}
	if grantActive != 1 {
		t.Fatalf("grant active recount: want 1, got %d", grantActive)
	}
}

func TestSettleInferenceRequestAndGrantAccounting(t *testing.T) {
	f := newFixture(t)
	now := time.Now().UTC().Truncate(time.Microsecond)
	nonce := uuid.New()

	testutil.SeedSQL(t, f.pool,
		`INSERT INTO inference_requests (grant_id, nonce, generation, status, request_kind, model,
		    reserved_tokens, max_chargeable_tokens, prompt_tokens, completion_tokens, cost_microusd, started_at)
		 VALUES ($1, $2, 1, 'started', 'chat', 'openai/gpt-oss-20b', 1000, 2000, 0, 0, 0, $3)`,
		f.grantID, nonce, now)
	testutil.SeedSQL(t, f.pool,
		"UPDATE inference_grants SET active_requests = 1 WHERE grant_id = $1", f.grantID)

	f.inTx(t, func(ctx context.Context, _ pgx.Tx, q *dbpg.Queries) {
		// Lock rank 3 after ticket + grant.
		req, err := q.GetInferenceRequestForUpdate(ctx, dbpg.GetInferenceRequestForUpdateParams{
			GrantID: pgUUID(f.grantID),
			Nonce:   pgUUID(nonce),
		})
		if err != nil {
			t.Fatalf("lock request: %v", err)
		}
		if req.Status != "started" {
			t.Fatalf("fixture: want started, got %s", req.Status)
		}

		if err := q.SettleInferenceRequest(ctx, dbpg.SettleInferenceRequestParams{
			Status:             "completed",
			PromptTokens:       800,
			CompletionTokens:   150,
			CostMicrousd:       4200,
			UpstreamProvider:   pgtype.Text{String: "deepinfra", Valid: true},
			UpstreamAttempts:   1,
			OpenrouterAttempts: 1,
			FallbackPhase:      0,
			TimedOut:           false,
			LatencyMs:          pgtype.Int4{Int32: 1234, Valid: true},
			CompletedAt:        pgTime(now),
			GrantID:            pgUUID(f.grantID),
			Nonce:              pgUUID(nonce),
		}); err != nil {
			t.Fatalf("settle request: %v", err)
		}

		grant, err := q.ApplyGrantChatSettlement(ctx, dbpg.ApplyGrantChatSettlementParams{
			ActiveRequests:   0, // caller-computed max(0, 1-1)
			PromptTokens:     800,
			CompletionTokens: 150,
			CostMicrousd:     4200,
			Now:              pgTime(now),
			GrantID:          pgUUID(f.grantID),
		})
		if err != nil {
			t.Fatalf("settle grant: %v", err)
		}
		if grant.PromptTokens != 800 || grant.CompletionTokens != 150 || grant.ActiveRequests != 0 {
			t.Fatalf("grant accounting wrong: %+v", grant)
		}
		// The returned row is what the caller applies the exhaustion rule
		// to (prompt+completion >= token_budget -> exhausted); nowhere near
		// the budget here.
		if grant.PromptTokens+grant.CompletionTokens >= grant.TokenBudget {
			t.Fatalf("unexpected exhaustion")
		}
	})

	var status string
	var completedAtValid bool
	if err := f.pool.QueryRow(t.Context(),
		"SELECT status, completed_at IS NOT NULL FROM inference_requests WHERE grant_id = $1 AND nonce = $2",
		f.grantID, nonce).Scan(&status, &completedAtValid); err != nil {
		t.Fatalf("read request: %v", err)
	}
	if status != "completed" || !completedAtValid {
		t.Fatalf("settle write: want completed with completed_at, got %s/%v", status, completedAtValid)
	}
}

func TestRateWindowCountsSettledRows(t *testing.T) {
	f := newFixture(t)
	now := time.Now().UTC().Truncate(time.Microsecond)

	// A completed request still counts toward the minute window (the rail
	// bounds request RATE, not concurrency).
	testutil.SeedSQL(t, f.pool,
		`INSERT INTO inference_requests (grant_id, nonce, generation, status, request_kind, model,
		    reserved_tokens, max_chargeable_tokens, prompt_tokens, completion_tokens, cost_microusd, started_at, completed_at)
		 VALUES ($1, $2, 1, 'completed', 'chat', 'openai/gpt-oss-20b', 100, 100, 90, 10, 1, $3, $3)`,
		f.grantID, uuid.New(), now.Add(-10*time.Second))

	ctx := t.Context()
	since := now.Add(-1 * time.Minute)

	grantCount, err := f.queries.CountRecentGrantRequests(ctx, dbpg.CountRecentGrantRequestsParams{
		GrantID:     pgUUID(f.grantID),
		RequestKind: "chat",
		Since:       pgTime(since),
	})
	if err != nil {
		t.Fatalf("grant window: %v", err)
	}
	validatorCount, err := f.queries.CountRecentValidatorRequests(ctx, dbpg.CountRecentValidatorRequestsParams{
		ValidatorHotkey: testHotkey,
		RequestKind:     "chat",
		Since:           pgTime(since),
	})
	if err != nil {
		t.Fatalf("validator window: %v", err)
	}
	globalCount, err := f.queries.CountRecentGlobalRequests(ctx, dbpg.CountRecentGlobalRequestsParams{
		RequestKind: "chat",
		Since:       pgTime(since),
	})
	if err != nil {
		t.Fatalf("global window: %v", err)
	}
	if grantCount != 1 || validatorCount != 1 || globalCount != 1 {
		t.Fatalf("windows must count settled rows: got %d/%d/%d", grantCount, validatorCount, globalCount)
	}

	chatEmb, err := f.queries.SumValidatorActiveLaneRequests(ctx, testHotkey)
	if err != nil {
		t.Fatalf("validator active: %v", err)
	}
	if chatEmb.ChatActive != 0 || chatEmb.EmbeddingActive != 0 {
		t.Fatalf("active lane sums: settled rows must NOT count, got %+v", chatEmb)
	}
}

func TestGetLatestInferenceConcurrencySettings(t *testing.T) {
	f := newFixture(t)
	ctx := t.Context()

	_, err := f.queries.GetLatestInferenceConcurrencySettings(ctx)
	if !errors.Is(err, pgx.ErrNoRows) {
		t.Fatalf("empty board: want pgx.ErrNoRows (serve shipped defaults), got %v", err)
	}

	checksum := "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
	testutil.SeedSQL(t, f.pool,
		`INSERT INTO inference_concurrency_settings_revisions
		    (parent_revision, scope, settings, checksum, reason, actor)
		 VALUES (0, '*', '{"embedding_per_ticket_concurrency": 12}'::jsonb, $1, 'initial defaults', 'test-operator')`,
		checksum)
	testutil.SeedSQL(t, f.pool,
		`INSERT INTO inference_concurrency_settings_revisions
		    (parent_revision, scope, settings, checksum, reason, actor)
		 SELECT max(revision), '*', '{"embedding_per_ticket_concurrency": 24}'::jsonb, $1, 'raise embeddings', 'test-operator'
		 FROM inference_concurrency_settings_revisions`,
		checksum)

	latest, err := f.queries.GetLatestInferenceConcurrencySettings(ctx)
	if err != nil {
		t.Fatalf("latest: %v", err)
	}
	if latest.Reason != "raise embeddings" {
		t.Fatalf("want the NEWEST revision, got %+v", latest)
	}
}
