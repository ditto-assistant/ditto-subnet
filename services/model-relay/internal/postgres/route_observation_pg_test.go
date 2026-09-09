package postgres_test

import (
	"math"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgtype"

	dbpg "github.com/ditto-assistant/model-relay/internal/postgres"
	"github.com/ditto-assistant/model-relay/internal/testutil"
)

const (
	routeModel    = "openai/gpt-oss-20b"
	routeProvider = "provider-a"
	routeProfile  = "profile-1"
	routeAlpha    = 0.25
	routeCooldown = 90
)

func seedRoute(t *testing.T, f *fixture, withPolicy bool) {
	t.Helper()
	testutil.SeedSQL(t, f.pool,
		`INSERT INTO inference_provider_routes (
		    model, provider, profile_revision, status, calibration_status, discovered_at)
		 VALUES ($1, $2, $3, 'healthy', 'eligible', now())`,
		routeModel, routeProvider, routeProfile)
	if withPolicy {
		testutil.SeedSQL(t, f.pool,
			`INSERT INTO inference_routing_policies (
			    model, enabled, speed_weight, cost_weight, exploration_weight,
			    exploration_ticket_budget, min_tool_accuracy, min_composite,
			    min_calibration_samples, max_error_rate, max_timeout_rate,
			    cooldown_seconds, ewma_alpha)
			 VALUES ($1, true, 1, 1, 0, 0, 0, 0, 1, 1, 1, $2, $3)`,
			routeModel, routeCooldown, routeAlpha)
	}
}

type routeRow struct {
	sampleCount     int64
	latency         pgtype.Float8
	tokensPerSecond pgtype.Float8
	errorRate       float64
	timeoutRate     float64
	cost            pgtype.Float8
	status          string
	cooldownUntil   pgtype.Timestamptz
	lastObservedAt  pgtype.Timestamptz
}

func readRoute(t *testing.T, f *fixture) routeRow {
	t.Helper()
	var r routeRow
	err := f.pool.QueryRow(t.Context(),
		`SELECT sample_count, ewma_latency_ms, ewma_tokens_per_second, ewma_error_rate,
		        ewma_timeout_rate, ewma_cost_microusd, status, cooldown_until, last_observed_at
		   FROM inference_provider_routes
		  WHERE model = $1 AND provider = $2 AND profile_revision = $3`,
		routeModel, routeProvider, routeProfile).Scan(
		&r.sampleCount, &r.latency, &r.tokensPerSecond, &r.errorRate,
		&r.timeoutRate, &r.cost, &r.status, &r.cooldownUntil, &r.lastObservedAt)
	if err != nil {
		t.Fatalf("read route: %v", err)
	}
	return r
}

func observe(t *testing.T, f *fixture, p dbpg.ObserveInferenceProviderRouteParams) int64 {
	t.Helper()
	p.Model, p.Provider, p.ProfileRevision = routeModel, routeProvider, routeProfile
	rows, err := f.queries.ObserveInferenceProviderRoute(t.Context(), p)
	if err != nil {
		t.Fatalf("observe: %v", err)
	}
	return rows
}

func near(a, b float64) bool { return math.Abs(a-b) < 1e-9 }

func TestObserveRouteSeedsThenFoldsWithPolicyAlpha(t *testing.T) {
	f := newFixture(t)
	seedRoute(t, f, true)
	now := time.Now().UTC().Truncate(time.Microsecond)

	// First observation seeds every NULL EWMA with the observed value.
	if rows := observe(t, f, dbpg.ObserveInferenceProviderRouteParams{
		LatencyMs: 800, TokensPerSecondObserved: true, TokensPerSecond: 40,
		ErrorObserved: 0, TimeoutObserved: 0, CostObserved: true, CostMicrousd: 1200,
		Success: true, Now: pgTime(now),
	}); rows != 1 {
		t.Fatalf("expected 1 row folded, got %d", rows)
	}
	r := readRoute(t, f)
	if r.sampleCount != 1 || !near(r.latency.Float64, 800) || !near(r.tokensPerSecond.Float64, 40) ||
		!near(r.cost.Float64, 1200) || !near(r.errorRate, 0) || r.status != "healthy" || r.cooldownUntil.Valid {
		t.Fatalf("seed fold wrong: %+v", r)
	}

	// Second observation: a failure folds with alpha, skips the unobserved
	// tokens/s and cost, flips status, and sets cooldown = now + policy.
	later := now.Add(time.Second)
	observe(t, f, dbpg.ObserveInferenceProviderRouteParams{
		LatencyMs: 400, TokensPerSecondObserved: false, CostObserved: false, CostMicrousd: 0,
		ErrorObserved: 1, TimeoutObserved: 1, Success: false, Now: pgTime(later),
	})
	r = readRoute(t, f)
	wantLatency := routeAlpha*400 + (1-routeAlpha)*800
	if r.sampleCount != 2 || !near(r.latency.Float64, wantLatency) {
		t.Fatalf("latency fold wrong: %+v (want %.3f)", r, wantLatency)
	}
	if !near(r.tokensPerSecond.Float64, 40) || !near(r.cost.Float64, 1200) {
		t.Fatalf("unobserved fields must keep their previous value: %+v", r)
	}
	if !near(r.errorRate, routeAlpha) || !near(r.timeoutRate, routeAlpha) {
		t.Fatalf("rate folds wrong: %+v", r)
	}
	if r.status != "degraded" || !r.cooldownUntil.Valid ||
		!r.cooldownUntil.Time.Equal(later.Add(routeCooldown*time.Second)) {
		t.Fatalf("failure must degrade with cooldown: %+v", r)
	}
	if !r.lastObservedAt.Time.Equal(later) {
		t.Fatalf("last_observed_at not advanced: %+v", r)
	}
}

func TestObserveRouteIsANoOpWithoutRouteOrPolicy(t *testing.T) {
	f := newFixture(t)
	now := time.Now().UTC()
	// No route row at all.
	if rows := observe(t, f, dbpg.ObserveInferenceProviderRouteParams{
		LatencyMs: 10, Success: true, Now: pgTime(now),
	}); rows != 0 {
		t.Fatalf("missing route must fold nothing, got %d rows", rows)
	}
	// Route without a routing policy.
	seedRoute(t, f, false)
	if rows := observe(t, f, dbpg.ObserveInferenceProviderRouteParams{
		LatencyMs: 10, Success: true, Now: pgTime(now),
	}); rows != 0 {
		t.Fatalf("route without policy must fold nothing, got %d rows", rows)
	}
	if r := readRoute(t, f); r.sampleCount != 0 || r.latency.Valid {
		t.Fatalf("route must be untouched: %+v", r)
	}
}
