-- Confirmation inference: the purpose-bound ledger for one Bench v9
-- confirmation ticket (ditto/db/queries/confirmation_inference.py).
-- Confirmation deliberately has its own tables instead of weakening the hot
-- ordinary inference_grants table with nullable foreign keys.
--
-- Lock order mirrors the Python module exactly:
--   begin:  confirmation_bundle_tickets (rank 1, FOR UPDATE) ->
--           confirmation_inference_grants (rank 2, FOR UPDATE) ->
--           confirmation_inference_requests (rank 3, via the INSERT).
--   finish: confirmation_inference_grants (FOR UPDATE) ->
--           confirmation_inference_requests (FOR UPDATE). No ticket lock.

-- name: GetConfirmationInferenceGrant :one
-- Unlocked snapshot by grant id: used to learn the owning ticket PK (so the
-- ticket can be locked FIRST) and by the endpoints' pre-admission lane/proof
-- gates. Never evaluate budget gates against this row.
SELECT * FROM confirmation_inference_grants
WHERE grant_id = sqlc.arg(grant_id)::uuid;

-- name: GetConfirmationInferenceGrantForUpdate :one
-- Locked re-read (rank 2, after the ticket lock in begin; the first lock in
-- finish). All gate evaluation uses ONLY the values this returns.
SELECT * FROM confirmation_inference_grants
WHERE grant_id = sqlc.arg(grant_id)::uuid
FOR UPDATE;

-- name: GetConfirmationBundleTicketForUpdate :one
-- Rank-1 lock for confirmation admission. A missing ticket is legal; the
-- liveness gate fails closed with LEASE_EXPIRED.
SELECT * FROM confirmation_bundle_tickets
WHERE ticket_id = sqlc.arg(ticket_id)::uuid
FOR UPDATE;

-- name: RevokeConfirmationInferenceGrant :exec
-- Liveness-gate revocation write. On the relay endpoints a decline rolls the
-- admission transaction back, so this write is discarded there — emitted for
-- statement parity with the Python module.
UPDATE confirmation_inference_grants
SET status = 'revoked',
    updated_at = sqlc.arg(now)::timestamptz
WHERE grant_id = sqlc.arg(grant_id)::uuid;

-- name: MarkConfirmationInferenceGrantExhausted :exec
-- Terminal budget exhaustion. The committed writer in relay practice is the
-- settle transaction (finish_confirmation_inference_request).
UPDATE confirmation_inference_grants
SET status = 'exhausted',
    updated_at = sqlc.arg(now)::timestamptz
WHERE grant_id = sqlc.arg(grant_id)::uuid;

-- name: InsertConfirmationInferenceRequest :one
-- Replay-safe admission INSERT (PR #712 semantics): ON CONFLICT DO NOTHING on
-- the (grant_id, nonce) PK collapses replay detection into the insert itself —
-- zero rows back (pgx.ErrNoRows under :one) means NONCE_REPLAYED, and the
-- admission transaction is never poisoned, so no savepoint is needed.
-- reserved_tokens/max_chargeable_tokens are pre-clamped by the caller
-- (max(1, reservation) / max(1, reservation, ceiling)) so the provisional row
-- stays constraint-valid and replay detection stays ahead of
-- malformed-reservation classification; a malformed fresh request deletes the
-- row again (DeleteConfirmationInferenceRequest).
INSERT INTO confirmation_inference_requests (
    grant_id,
    nonce,
    generation,
    status,
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
    sqlc.arg(model)::text,
    sqlc.arg(reserved_tokens)::bigint,
    sqlc.arg(max_chargeable_tokens)::bigint,
    0,
    0,
    0,
    sqlc.arg(started_at)::timestamptz
)
ON CONFLICT (grant_id, nonce) DO NOTHING
RETURNING *;

-- name: DeleteConfirmationInferenceRequest :exec
-- Decline cleanup for a just-inserted provisional row (budget/shape declines
-- after the replay-guarding insert). Discarded by the endpoint's decline
-- rollback; emitted for statement parity.
DELETE FROM confirmation_inference_requests
WHERE grant_id = sqlc.arg(grant_id)::uuid
  AND nonce = sqlc.arg(nonce)::uuid;

-- name: IncrementConfirmationGrantAdmission :exec
-- Admission bookkeeping after the request row INSERT. Grant row is locked by
-- the caller.
UPDATE confirmation_inference_grants
SET request_count = request_count + 1,
    active_requests = active_requests + 1,
    updated_at = sqlc.arg(now)::timestamptz
WHERE grant_id = sqlc.arg(grant_id)::uuid;

-- name: GetConfirmationInferenceRequestForUpdate :one
-- Settle-path locked read (after the grant lock).
SELECT * FROM confirmation_inference_requests
WHERE grant_id = sqlc.arg(grant_id)::uuid
  AND nonce = sqlc.arg(nonce)::uuid
FOR UPDATE;

-- name: SettleConfirmationInferenceRequest :exec
-- The settle write for a (locked) confirmation request row. The
-- non-deliverable -> reservation-charged/failed rewrite is applied by the
-- caller before this runs.
UPDATE confirmation_inference_requests
SET status = sqlc.arg(status)::text,
    prompt_tokens = sqlc.arg(prompt_tokens)::bigint,
    completion_tokens = sqlc.arg(completion_tokens)::bigint,
    cost_microusd = sqlc.arg(cost_microusd)::bigint,
    upstream_provider = sqlc.narg(upstream_provider)::text,
    completed_at = sqlc.arg(completed_at)::timestamptz
WHERE grant_id = sqlc.arg(grant_id)::uuid
  AND nonce = sqlc.arg(nonce)::uuid;

-- name: ApplyConfirmationGrantSettlement :one
-- Settle-transaction grant accounting. active_requests is an ABSOLUTE value:
-- the caller computes max(0, active_requests-1) from the locked row so the
-- CHECK constraint never aborts the transaction. Spend columns are deltas.
-- Returns the updated row so the caller can apply the exhaustion rule
-- (request/token/cost budgets) without re-reading.
UPDATE confirmation_inference_grants
SET active_requests = sqlc.arg(active_requests)::integer,
    prompt_tokens = prompt_tokens + sqlc.arg(prompt_tokens)::bigint,
    completion_tokens = completion_tokens + sqlc.arg(completion_tokens)::bigint,
    cost_microusd = cost_microusd + sqlc.arg(cost_microusd)::bigint,
    updated_at = sqlc.arg(now)::timestamptz
WHERE grant_id = sqlc.arg(grant_id)::uuid
RETURNING *;
