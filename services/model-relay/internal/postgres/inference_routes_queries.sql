-- Provider routes and routing policies. Route rows sit OUTSIDE the
-- ticket->grant->request lock chain and are only ever locked singly, after
-- the hot three, inside the chat settle transaction
-- (record_route_observation). The relay never writes
-- inference_routing_policies (platform admin owns them) and never touches
-- routes on the embedding lane.

-- name: GetInferenceProviderRouteForUpdate :one
-- Lock the route row a chat settle observes: PK is
-- (model, provider, profile_revision) where model = grant.allowed_models[0],
-- provider = grant.route_provider, profile_revision = grant.route_profile.
-- Missing row => the observation is a no-op.
SELECT * FROM inference_provider_routes
WHERE model = sqlc.arg(model)::text
  AND provider = sqlc.arg(provider)::text
  AND profile_revision = sqlc.arg(profile_revision)::text
FOR UPDATE;

-- name: GetInferenceRoutingPolicy :one
-- Unlocked policy read (EWMA alpha, cooldown). Missing row => observation
-- no-op.
SELECT * FROM inference_routing_policies
WHERE model = sqlc.arg(model)::text;

-- name: UpdateInferenceProviderRouteObservation :exec
-- Write back one chat-settle observation. Every value is computed by the
-- caller from the LOCKED route row + policy (EWMA folds, healthy/degraded
-- status, cooldown_until = now + policy.cooldown_seconds on failure and NULL
-- on success), so this is a plain absolute-value write.
UPDATE inference_provider_routes
SET sample_count = sqlc.arg(sample_count)::bigint,
    ewma_latency_ms = sqlc.narg(ewma_latency_ms)::double precision,
    ewma_tokens_per_second = sqlc.narg(ewma_tokens_per_second)::double precision,
    ewma_error_rate = sqlc.arg(ewma_error_rate)::double precision,
    ewma_timeout_rate = sqlc.arg(ewma_timeout_rate)::double precision,
    ewma_cost_microusd = sqlc.narg(ewma_cost_microusd)::double precision,
    status = sqlc.arg(status)::text,
    cooldown_until = sqlc.narg(cooldown_until)::timestamptz,
    last_observed_at = sqlc.arg(last_observed_at)::timestamptz,
    updated_at = sqlc.arg(now)::timestamptz
WHERE model = sqlc.arg(model)::text
  AND provider = sqlc.arg(provider)::text
  AND profile_revision = sqlc.arg(profile_revision)::text;
