package inference

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"log/slog"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/ditto-assistant/model-relay/internal/postgres"
	"github.com/ditto-assistant/model-relay/internal/testutil"
)

func TestSourceReviewProviderEventOwnsCircuitLifecycle(t *testing.T) {
	pool := testutil.NewTestPGPool(t)
	agentID := uuid.New()
	attemptID := uuid.New()
	reviewID := uuid.New()
	token := "source-review-provider-event-test-token"
	digest := sha256.Sum256([]byte(token))
	tokenHash := hex.EncodeToString(digest[:])
	now := time.Now().UTC().Truncate(time.Microsecond)

	testutil.SeedSQL(t, pool,
		`INSERT INTO agents (agent_id, miner_hotkey, name, sha256)
		 VALUES ($1, 'miner-hotkey', 'source-review-agent', repeat('a', 64))`, agentID)
	testutil.SeedSQL(t, pool,
		`INSERT INTO screening_attempts (
		    attempt_id, agent_id, screener_hotkey, policy_version, status,
		    started_at, deadline)
		 VALUES ($1, $2, 'screener-hotkey', 1, 'running', $3, $4)`,
		attemptID, agentID, now.Add(-time.Minute), now.Add(time.Hour))
	testutil.SeedSQL(t, pool,
		`INSERT INTO submission_source_reviews (
		    review_id, agent_id, attempt_id, environment, artifact_sha256,
		    status, job_token_hash, job_token_expires_at)
		 VALUES ($1, $2, $3, 'prod', repeat('a', 64), 'running', $4, $5)`,
		reviewID, agentID, attemptID, tokenHash, now.Add(time.Hour))

	logger := slog.New(slog.NewTextHandler(nullWriter{}, nil))
	current := now
	deps := &Deps{
		Logger:  logger,
		Pool:    pool,
		Queries: postgres.New(pool),
		Now:     func() time.Time { return current },
		Sleep:   func(context.Context, time.Duration) {},
	}

	post := func(status int, startedAt time.Time) int {
		t.Helper()
		body, err := json.Marshal(sourceReviewProviderEvent{
			ReviewID: reviewID.String(),
			Status:   status,
			Started:  startedAt.Format(time.RFC3339Nano),
		})
		if err != nil {
			t.Fatalf("marshal event: %v", err)
		}
		req := proxyRequest(
			"/api/v1/inference/source-review/provider-event",
			string(body),
			map[string]string{"Authorization": "Bearer " + token},
		)
		return serve(deps, req).Code
	}

	requestStarted := now.Add(-10 * time.Second)
	if status := post(http.StatusTooManyRequests, requestStarted); status != http.StatusNoContent {
		t.Fatalf("open status=%d, want %d", status, http.StatusNoContent)
	}
	var state string
	var lastFailureAt, retryAt time.Time
	if err := pool.QueryRow(t.Context(),
		`SELECT state, last_failure_at, retry_at
		 FROM provider_outage_circuits WHERE provider = 'openrouter'`,
	).Scan(&state, &lastFailureAt, &retryAt); err != nil {
		t.Fatalf("read open circuit: %v", err)
	}
	if state != "open" || !lastFailureAt.Equal(now) || !retryAt.Equal(now.Add(2*time.Minute)) {
		t.Fatalf("open circuit state=%s failure=%s retry=%s", state, lastFailureAt, retryAt)
	}

	// A success from work that started before the observed failure cannot heal
	// the shared circuit after arriving late.
	if status := post(http.StatusOK, requestStarted); status != http.StatusNoContent {
		t.Fatalf("stale close status=%d, want %d", status, http.StatusNoContent)
	}
	if err := pool.QueryRow(t.Context(),
		`SELECT state FROM provider_outage_circuits WHERE provider = 'openrouter'`,
	).Scan(&state); err != nil {
		t.Fatalf("read stale close: %v", err)
	}
	if state != "open" {
		t.Fatalf("stale success closed circuit: state=%s", state)
	}

	current = now.Add(3 * time.Minute)
	if status := post(http.StatusOK, current.Add(-time.Minute)); status != http.StatusNoContent {
		t.Fatalf("probe close status=%d, want %d", status, http.StatusNoContent)
	}
	if err := pool.QueryRow(t.Context(),
		`SELECT state FROM provider_outage_circuits WHERE provider = 'openrouter'`,
	).Scan(&state); err != nil {
		t.Fatalf("read closed circuit: %v", err)
	}
	if state != "closed" {
		t.Fatalf("fresh success did not close circuit: state=%s", state)
	}
}

func TestSourceReviewProviderEventRejectsInvalidInputBeforeDatabase(t *testing.T) {
	deps := &Deps{Logger: slog.New(slog.NewTextHandler(nullWriter{}, nil))}
	req := proxyRequest(
		"/api/v1/inference/source-review/provider-event",
		`{"review_id":"not-a-uuid","status":429,"started_at":"now","extra":true}`,
		map[string]string{"Authorization": "Bearer invalid"},
	)
	w := serve(deps, req)
	if w.Code != http.StatusBadRequest || !strings.Contains(w.Body.String(), "invalid provider event") {
		t.Fatalf("invalid event response=%d %s", w.Code, w.Body.String())
	}
}
