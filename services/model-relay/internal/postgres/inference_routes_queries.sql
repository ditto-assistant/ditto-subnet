-- Provider routes and routing policies. Route rows sit OUTSIDE the
-- ticket->grant->request lock chain and are only ever locked singly, after
-- the hot three, inside the chat settle transaction
-- (record_route_observation). The relay never writes
-- inference_routing_policies (platform admin owns them) and never touches
-- routes on the embedding lane.

-- name: GetInferenceRoutingPolicy :one
-- Unlocked policy read (EWMA alpha, cooldown). Missing row => observation
-- no-op.
SELECT * FROM inference_routing_policies
WHERE model = sqlc.arg(model)::text;

-- name: ObserveInferenceProviderRoute :execrows
-- Fold one chat-settle observation into the route row in a single statement.
-- Before 2026-09-08 this was SELECT ... FOR UPDATE, a policy read, EWMA math
-- in Go, then an UPDATE: every chat settle on the subnet took the same one of
-- ~22 route rows exclusively across three round trips while still holding
-- its ticket->grant->request locks, so route contention (1.2 s average
-- waits) inflated every rail behind it. The EWMA folds, healthy/degraded
-- status, and cooldown are computed here from the current row and the
-- model's routing policy under the UPDATE's own brief row lock. A missing
-- route or policy row updates nothing (0 rows), matching the old no-op.
--   ewma(prev, x) = alpha*x + (1-alpha)*prev, seeded with x when prev is NULL.
UPDATE inference_provider_routes AS r
SET sample_count = r.sample_count + 1,
    ewma_latency_ms = CASE
        WHEN r.ewma_latency_ms IS NULL THEN sqlc.arg(latency_ms)::double precision
        ELSE p.ewma_alpha * sqlc.arg(latency_ms)::double precision
             + (1 - p.ewma_alpha) * r.ewma_latency_ms
    END,
    ewma_tokens_per_second = CASE
        WHEN NOT sqlc.arg(tokens_per_second_observed)::boolean THEN r.ewma_tokens_per_second
        WHEN r.ewma_tokens_per_second IS NULL THEN sqlc.arg(tokens_per_second)::double precision
        ELSE p.ewma_alpha * sqlc.arg(tokens_per_second)::double precision
             + (1 - p.ewma_alpha) * r.ewma_tokens_per_second
    END,
    ewma_error_rate = p.ewma_alpha * sqlc.arg(error_observed)::double precision
        + (1 - p.ewma_alpha) * r.ewma_error_rate,
    ewma_timeout_rate = p.ewma_alpha * sqlc.arg(timeout_observed)::double precision
        + (1 - p.ewma_alpha) * r.ewma_timeout_rate,
    ewma_cost_microusd = CASE
        WHEN NOT sqlc.arg(cost_observed)::boolean THEN r.ewma_cost_microusd
        WHEN r.ewma_cost_microusd IS NULL THEN sqlc.arg(cost_microusd)::double precision
        ELSE p.ewma_alpha * sqlc.arg(cost_microusd)::double precision
             + (1 - p.ewma_alpha) * r.ewma_cost_microusd
    END,
    status = CASE WHEN sqlc.arg(success)::boolean THEN 'healthy' ELSE 'degraded' END,
    cooldown_until = CASE
        WHEN sqlc.arg(success)::boolean THEN NULL
        ELSE sqlc.arg(now)::timestamptz + make_interval(secs => p.cooldown_seconds)
    END,
    last_observed_at = sqlc.arg(now)::timestamptz,
    updated_at = sqlc.arg(now)::timestamptz
FROM inference_routing_policies AS p
WHERE r.model = sqlc.arg(model)::text
  AND r.provider = sqlc.arg(provider)::text
  AND r.profile_revision = sqlc.arg(profile_revision)::text
  AND p.model = r.model;
