-- Inference requests: LOCK RANK 3 (locked only after ticket and grant). The
-- (grant_id, nonce) composite PK is the distributed nonce-replay guard
-- across relay replicas; InsertInferenceRequest MUST run inside a savepoint
-- so a unique violation converts to NONCE_REPLAYED without poisoning the
-- admission transaction.

-- name: GetInferenceRequest :one
-- Fast (unlocked) replay check during admission.
SELECT * FROM inference_requests
WHERE grant_id = sqlc.arg(grant_id)::uuid
  AND nonce = sqlc.arg(nonce)::uuid;

-- name: GetInferenceRequestForUpdate :one
-- Settle-path locked read (rank 3, after ticket and grant locks).
SELECT * FROM inference_requests
WHERE grant_id = sqlc.arg(grant_id)::uuid
  AND nonce = sqlc.arg(nonce)::uuid
FOR UPDATE;

-- name: ListStartedInferenceRequestsForUpdate :many
-- /exchange rotation gate: every started request for the grant, locked.
-- Ordered by nonce for a deterministic lock order within rank 3 (mirrors
-- revoke_ticket_inference's ORDER BY grant_id, nonce).
SELECT * FROM inference_requests
WHERE grant_id = sqlc.arg(grant_id)::uuid
  AND status = 'started'
ORDER BY nonce
FOR UPDATE;

-- name: ListStaleStartedInferenceRequestsForUpdate :many
-- Admission stale reclamation: started requests of this grant+lane older
-- than the recovery window (started_at < now - 2*timeout_seconds; the caller
-- computes the cutoff). Locked at rank 3.
SELECT * FROM inference_requests
WHERE grant_id = sqlc.arg(grant_id)::uuid
  AND status = 'started'
  AND request_kind = sqlc.arg(request_kind)::text
  AND started_at < sqlc.arg(cutoff)::timestamptz
ORDER BY nonce
FOR UPDATE;

-- name: CancelInferenceRequestChargingReservation :exec
-- Reclamation write: cancel a (locked) started request without booking the
-- reservation estimate. Token spend is receipted provider usage only; a stale
-- or rotated call that never settled charges zero. The request still counts
-- against the request-count budget.
UPDATE inference_requests
SET status = 'canceled',
    prompt_tokens = 0,
    completed_at = sqlc.arg(now)::timestamptz
WHERE grant_id = sqlc.arg(grant_id)::uuid
  AND nonce = sqlc.arg(nonce)::uuid;

-- name: CountStartedInferenceRequests :one
-- Post-reclamation lane recount input (written back via
-- SetGrant*ActiveRequests).
SELECT COUNT(*)::bigint FROM inference_requests
WHERE grant_id = sqlc.arg(grant_id)::uuid
  AND status = 'started'
  AND request_kind = sqlc.arg(request_kind)::text;

-- name: SumStartedReservedTokens :one
-- Token-headroom gate input: unlocked aggregate of in-flight reservations
-- for this grant+lane.
SELECT COALESCE(SUM(reserved_tokens), 0)::bigint FROM inference_requests
WHERE grant_id = sqlc.arg(grant_id)::uuid
  AND status = 'started'
  AND request_kind = sqlc.arg(request_kind)::text;

-- name: InsertInferenceRequest :one
-- The admission INSERT (run inside a savepoint; PK violation =>
-- NONCE_REPLAYED). generation comes from the LOCKED grant row;
-- max_chargeable_tokens is max(token_reservation, byte-derived ceiling),
-- computed by the caller.
INSERT INTO inference_requests (
    grant_id,
    nonce,
    generation,
    status,
    request_kind,
    model,
    reserved_tokens,
    max_chargeable_tokens,
    prompt_tokens,
    completion_tokens,
    cost_microusd,
    started_at
) VALUES (
    sqlc.arg(grant_id)::uuid,
    sqlc.arg(nonce)::uuid,
    sqlc.arg(generation)::integer,
    'started',
    sqlc.arg(request_kind)::text,
    sqlc.arg(model)::text,
    sqlc.arg(reserved_tokens)::bigint,
    sqlc.arg(max_chargeable_tokens)::bigint,
    0,
    0,
    0,
    sqlc.arg(started_at)::timestamptz
)
RETURNING *;

-- name: CountRecentGrantRequests :one
-- Per-ticket RPM rail: rows of ANY status count toward the minute window
-- (finished requests still count — see
-- test_ticket_request_rate_is_bounded_after_requests_finish). Unlocked,
-- best-effort by design.
SELECT COUNT(*)::bigint FROM inference_requests
WHERE grant_id = sqlc.arg(grant_id)::uuid
  AND request_kind = sqlc.arg(request_kind)::text
  AND started_at >= sqlc.arg(since)::timestamptz;

-- name: CountRecentValidatorRequests :one
-- Per-validator RPM rail; same any-status window semantics.
SELECT COUNT(*)::bigint
FROM inference_requests r
JOIN inference_grants g ON g.grant_id = r.grant_id
WHERE g.validator_hotkey = sqlc.arg(validator_hotkey)::text
  AND r.request_kind = sqlc.arg(request_kind)::text
  AND r.started_at >= sqlc.arg(since)::timestamptz;

-- name: CountRecentGlobalRequests :one
-- Global RPM rail; same any-status window semantics.
SELECT COUNT(*)::bigint FROM inference_requests
WHERE request_kind = sqlc.arg(request_kind)::text
  AND started_at >= sqlc.arg(since)::timestamptz;

-- name: SettleInferenceRequest :exec
-- The settle write for a (locked) request row. All clamps (non-negative
-- attempts, fallback_phase in {0,1}, token/ceiling clamping, the
-- completed-but-not-deliverable -> 'canceled' status rule) are applied by
-- the caller before this runs, so the DB CHECK constraints never abort the
-- settle transaction.
UPDATE inference_requests
SET status = sqlc.arg(status)::text,
    prompt_tokens = sqlc.arg(prompt_tokens)::bigint,
    completion_tokens = sqlc.arg(completion_tokens)::bigint,
    cost_microusd = sqlc.arg(cost_microusd)::bigint,
    upstream_provider = sqlc.narg(upstream_provider)::text,
    upstream_attempts = sqlc.arg(upstream_attempts)::integer,
    openrouter_attempts = sqlc.arg(openrouter_attempts)::integer,
    fallback_phase = sqlc.arg(fallback_phase)::integer,
    terminal_error_code = sqlc.narg(terminal_error_code)::text,
    timed_out = sqlc.arg(timed_out)::boolean,
    latency_ms = sqlc.narg(latency_ms)::integer,
    completed_at = sqlc.arg(completed_at)::timestamptz
WHERE grant_id = sqlc.arg(grant_id)::uuid
  AND nonce = sqlc.arg(nonce)::uuid;
