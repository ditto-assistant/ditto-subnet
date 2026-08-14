-- Inference grants: LOCK RANK 2. The grant row FOR UPDATE is THE
-- serialization point for every per-grant budget invariant; all
-- money-critical checks must be evaluated against the values returned by the
-- locked read (GetInferenceGrantForUpdate), NEVER against an earlier
-- unlocked snapshot (the Python populate_existing re-read is load-bearing —
-- see test_reservations_on_one_grant_serialize_and_respect_the_budget).
--
-- updated_at is application-maintained (no DB trigger); every UPDATE here
-- sets it explicitly from the caller's clock, matching the ORM onupdate.

-- name: GetInferenceGrant :one
-- Unlocked snapshot by grant id. Used only to learn the owning ticket PK (so
-- the ticket can be locked FIRST, at rank 1) and for post-settle reads.
-- Never evaluate budget gates against this row.
SELECT * FROM inference_grants
WHERE grant_id = sqlc.arg(grant_id)::uuid;

-- name: GetInferenceGrantForUpdate :one
-- Locked re-read (rank 2, after the ticket lock). All gate evaluation uses
-- ONLY the values this returns.
SELECT * FROM inference_grants
WHERE grant_id = sqlc.arg(grant_id)::uuid
FOR UPDATE;

-- name: RevokeInferenceGrant :exec
-- Mid-lease revocation writer. Called by activate_inference_grant's validity
-- gate (COMMITS via /exchange) and by begin_inference_request's
-- ticket-liveness gate (rolls back on the relay endpoints, where declines
-- abort the admission transaction). The platform reads status='revoked'
-- deadline-scoped via ticket_inference_revoked_mid_lease.
UPDATE inference_grants
SET status = 'revoked',
    updated_at = sqlc.arg(now)::timestamptz
WHERE grant_id = sqlc.arg(grant_id)::uuid;

-- name: MarkInferenceGrantExhausted :exec
-- Terminal budget exhaustion. The committed writer in relay practice is the
-- settle transaction (finish_inference_request).
UPDATE inference_grants
SET status = 'exhausted',
    updated_at = sqlc.arg(now)::timestamptz
WHERE grant_id = sqlc.arg(grant_id)::uuid;

-- name: ActivateInferenceGrant :exec
-- The /exchange rotation write: store the bearer digest (hex sha256 of the
-- opaque bearer, which is returned once and never stored in clear), the
-- broker public key (base64url with trailing '=' stripped by the caller),
-- bump the generation, adopt the ticket's slot and deadline, and zero both
-- lane active counters. Runs only after the caller validated the grant
-- against the LOCKED ticket and grant rows.
UPDATE inference_grants
SET bearer_digest = sqlc.arg(bearer_digest)::text,
    broker_public_key = sqlc.arg(broker_public_key)::text,
    generation = generation + 1,
    status = 'active',
    slot_id = sqlc.arg(slot_id)::text,
    expires_at = sqlc.arg(expires_at)::timestamptz,
    active_requests = 0,
    embedding_active_requests = 0,
    updated_at = sqlc.arg(now)::timestamptz
WHERE grant_id = sqlc.arg(grant_id)::uuid;

-- name: AddReclaimedChatTokens :exec
-- Stale-reclamation charge: a canceled in-flight chat request is charged its
-- full reservation into the grant's prompt_tokens. Grant row is locked by
-- the caller.
UPDATE inference_grants
SET prompt_tokens = prompt_tokens + sqlc.arg(reserved_tokens)::bigint,
    updated_at = sqlc.arg(now)::timestamptz
WHERE grant_id = sqlc.arg(grant_id)::uuid;

-- name: AddReclaimedEmbeddingTokens :exec
-- Embedding-lane twin of AddReclaimedChatTokens.
UPDATE inference_grants
SET embedding_tokens = embedding_tokens + sqlc.arg(reserved_tokens)::bigint,
    updated_at = sqlc.arg(now)::timestamptz
WHERE grant_id = sqlc.arg(grant_id)::uuid;

-- name: SetGrantChatActiveRequests :exec
-- Post-reclamation lane recount (COUNT of still-started rows, computed by
-- the caller via CountStartedInferenceRequests — a recount, not a
-- decrement).
UPDATE inference_grants
SET active_requests = sqlc.arg(active_requests)::integer,
    updated_at = sqlc.arg(now)::timestamptz
WHERE grant_id = sqlc.arg(grant_id)::uuid;

-- name: SetGrantEmbeddingActiveRequests :exec
-- Embedding-lane twin of SetGrantChatActiveRequests.
UPDATE inference_grants
SET embedding_active_requests = sqlc.arg(embedding_active_requests)::integer,
    updated_at = sqlc.arg(now)::timestamptz
WHERE grant_id = sqlc.arg(grant_id)::uuid;

-- name: IncrementGrantChatAdmission :exec
-- Admission bookkeeping after the request row INSERT: one more chat request
-- admitted and in flight. Grant row is locked by the caller.
UPDATE inference_grants
SET request_count = request_count + 1,
    active_requests = active_requests + 1,
    updated_at = sqlc.arg(now)::timestamptz
WHERE grant_id = sqlc.arg(grant_id)::uuid;

-- name: IncrementGrantEmbeddingAdmission :exec
-- Embedding-lane twin of IncrementGrantChatAdmission.
UPDATE inference_grants
SET embedding_request_count = embedding_request_count + 1,
    embedding_active_requests = embedding_active_requests + 1,
    updated_at = sqlc.arg(now)::timestamptz
WHERE grant_id = sqlc.arg(grant_id)::uuid;

-- name: ApplyGrantChatSettlement :one
-- Settle-transaction grant accounting for the chat lane. active_requests is
-- an ABSOLUTE value: the caller computes max(0, active_requests-1) from the
-- locked row (the clamp lives in the application, matching Python, so the
-- CHECK constraint never aborts the transaction). Spend columns are deltas.
-- Returns the updated row so the caller can apply the chat token-budget
-- exhaustion rule (prompt+completion >= token_budget -> exhausted) without
-- re-reading.
UPDATE inference_grants
SET active_requests = sqlc.arg(active_requests)::integer,
    prompt_tokens = prompt_tokens + sqlc.arg(prompt_tokens)::bigint,
    completion_tokens = completion_tokens + sqlc.arg(completion_tokens)::bigint,
    cost_microusd = cost_microusd + sqlc.arg(cost_microusd)::bigint,
    updated_at = sqlc.arg(now)::timestamptz
WHERE grant_id = sqlc.arg(grant_id)::uuid
RETURNING *;

-- name: ApplyGrantEmbeddingSettlement :one
-- Embedding-lane settle accounting. No exhaustion rule on this lane (killing
-- the grant would take the chat lane down with it).
UPDATE inference_grants
SET embedding_active_requests = sqlc.arg(embedding_active_requests)::integer,
    embedding_tokens = embedding_tokens + sqlc.arg(embedding_tokens)::bigint,
    embedding_cost_microusd = embedding_cost_microusd + sqlc.arg(embedding_cost_microusd)::bigint,
    updated_at = sqlc.arg(now)::timestamptz
WHERE grant_id = sqlc.arg(grant_id)::uuid
RETURNING *;

-- name: CountFreshValidatorActiveRequests :one
-- Cross-grant per-validator concurrency rail (PR #735). Aggregates the
-- authoritative request rows, not the denormalized grant counters: a
-- validator ticket can expire before its scorer finishes the request, and
-- older cleanup paths left the corresponding *_active_requests value behind
-- on an otherwise-active grant — one such ghost permanently consumed a fleet
-- concurrency slot. Only fresh 'started' rows (started_at >= the same
-- 2*timeout recovery cutoff the stale sweep uses) on active grants count.
-- DELIBERATELY unlocked and best-effort: a burst may overshoot by at most the
-- number of racers. Do NOT add locks (a global advisory lock was removed
-- because it capped horizontal scaling).
SELECT COUNT(*)::bigint
FROM inference_requests r
JOIN inference_grants g ON g.grant_id = r.grant_id
WHERE g.status = 'active'
  AND r.status = 'started'
  AND r.request_kind = sqlc.arg(request_kind)::text
  AND r.started_at >= sqlc.arg(stale_cutoff)::timestamptz
  AND g.validator_hotkey = sqlc.arg(validator_hotkey)::text;

-- name: CountFreshGlobalActiveRequests :one
-- Cross-grant global concurrency rail; same fresh-row, best-effort semantics
-- as CountFreshValidatorActiveRequests.
SELECT COUNT(*)::bigint
FROM inference_requests r
JOIN inference_grants g ON g.grant_id = r.grant_id
WHERE g.status = 'active'
  AND r.status = 'started'
  AND r.request_kind = sqlc.arg(request_kind)::text
  AND r.started_at >= sqlc.arg(stale_cutoff)::timestamptz;
