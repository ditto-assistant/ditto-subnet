-- Shadow coding inference: the coding grant row is the per-grant
-- serialization point. The handler always locks it before reading or writing
-- request history, so two relay replicas cannot admit sibling dispatches.
-- Cross-grant validator/global concurrency is then reserved with
-- pg_advisory_xact_lock(hashtext('coding_inference_admission')) in the same
-- transaction before COUNT + INSERT.

-- name: GetCodingInferenceGrant :one
SELECT * FROM coding_inference_grants
WHERE grant_id = sqlc.arg(grant_id)::uuid;

-- name: GetCodingDatabaseNow :one
SELECT clock_timestamp()::timestamptz;

-- name: GetCodingInferenceGrantForUpdate :one
SELECT * FROM coding_inference_grants
WHERE grant_id = sqlc.arg(grant_id)::uuid
FOR UPDATE;

-- name: GetLatestCodingInferenceRequestForUpdate :one
SELECT * FROM coding_inference_requests
WHERE grant_id = sqlc.arg(grant_id)::uuid
ORDER BY sequence DESC
LIMIT 1
FOR UPDATE;

-- name: GetCodingInferenceRequestForUpdate :one
SELECT * FROM coding_inference_requests
WHERE grant_id = sqlc.arg(grant_id)::uuid
  AND sequence = sqlc.arg(sequence)::integer
FOR UPDATE;

-- name: InsertCodingInferenceRequest :one
INSERT INTO coding_inference_requests (
    request_row_id,
    grant_id,
    ticket_id,
    generation,
    sequence,
    request_sequence,
    attempt,
    request_id,
    case_id,
    profile_capability_id,
    inference_grant_sha256,
    locked_request_sha256,
    status,
    provider_settlement_sha256,
    provider_generation_id,
    provider_settlement_json,
    unsettled_reason,
    started_at,
    settled_at,
    weight_eligible
) VALUES (
    sqlc.arg(request_row_id)::uuid,
    sqlc.arg(grant_id)::uuid,
    sqlc.arg(ticket_id)::uuid,
    sqlc.arg(generation)::integer,
    sqlc.arg(sequence)::integer,
    sqlc.arg(request_sequence)::integer,
    sqlc.arg(attempt)::integer,
    sqlc.arg(request_id)::uuid,
    sqlc.arg(case_id)::text,
    sqlc.arg(profile_capability_id)::text,
    sqlc.arg(inference_grant_sha256)::text,
    sqlc.arg(locked_request_sha256)::text,
    'started',
    NULL,
    NULL,
    NULL,
    NULL,
    sqlc.arg(started_at)::timestamptz,
    NULL,
    false
)
RETURNING *;

-- name: CountActiveCodingInferenceRequestsForValidator :one
SELECT COUNT(*)::bigint
FROM (
    SELECT r.request_row_id
    FROM coding_inference_requests r
    JOIN coding_inference_grants g ON g.grant_id = r.grant_id
    WHERE r.status = 'started'
      AND g.status = 'active'
      AND g.expires_at > sqlc.arg(now)::timestamptz
      AND g.validator_hotkey = sqlc.arg(validator_hotkey)::text
    UNION ALL
    SELECT r.request_row_id
    FROM coding_certification_inference_requests r
    JOIN coding_certification_inference_grants g ON g.grant_id = r.grant_id
    WHERE r.status = 'started'
      AND g.status = 'active'
      AND g.expires_at > sqlc.arg(now)::timestamptz
      AND g.validator_hotkey = sqlc.arg(validator_hotkey)::text
) active;

-- name: CountActiveCodingInferenceRequestsGlobal :one
SELECT COUNT(*)::bigint
FROM (
    SELECT r.request_row_id
    FROM coding_inference_requests r
    JOIN coding_inference_grants g ON g.grant_id = r.grant_id
    WHERE r.status = 'started'
      AND g.status = 'active'
      AND g.expires_at > sqlc.arg(now)::timestamptz
    UNION ALL
    SELECT r.request_row_id
    FROM coding_certification_inference_requests r
    JOIN coding_certification_inference_grants g ON g.grant_id = r.grant_id
    WHERE r.status = 'started'
      AND g.status = 'active'
      AND g.expires_at > sqlc.arg(now)::timestamptz
) active;

-- name: BeginCodingInferenceGrantRequest :exec
UPDATE coding_inference_grants
SET request_count = request_count + sqlc.arg(request_increment)::integer,
    active_requests = 1,
    updated_at = sqlc.arg(now)::timestamptz
WHERE grant_id = sqlc.arg(grant_id)::uuid;

-- name: FindCodingInferenceSettlementIdentity :one
SELECT request_row_id FROM (
    SELECT request_row_id FROM coding_inference_requests
    WHERE request_row_id <> sqlc.arg(request_row_id)::uuid
      AND (
        provider_settlement_sha256 = sqlc.arg(provider_settlement_sha256)::text
        OR (
          sqlc.narg(provider_generation_id)::text IS NOT NULL
          AND provider_generation_id = sqlc.narg(provider_generation_id)::text
        )
      )
    UNION ALL
    SELECT request_row_id FROM coding_certification_inference_requests
    WHERE request_row_id <> sqlc.arg(request_row_id)::uuid
      AND (
        provider_settlement_sha256 = sqlc.arg(provider_settlement_sha256)::text
        OR (
          sqlc.narg(provider_generation_id)::text IS NOT NULL
          AND provider_generation_id = sqlc.narg(provider_generation_id)::text
        )
      )
) identities
LIMIT 1;

-- name: SettleCodingInferenceRequest :exec
UPDATE coding_inference_requests
SET status = sqlc.arg(status)::text,
    provider_settlement_sha256 = sqlc.arg(provider_settlement_sha256)::text,
    provider_generation_id = sqlc.narg(provider_generation_id)::text,
    provider_settlement_json = sqlc.arg(provider_settlement_json)::text,
    unsettled_reason = NULL,
    settled_at = sqlc.arg(settled_at)::timestamptz
WHERE request_row_id = sqlc.arg(request_row_id)::uuid;

-- name: ApplyCodingInferenceGrantSettlement :exec
UPDATE coding_inference_grants
SET status = sqlc.arg(status)::text,
    bearer_digest = CASE WHEN sqlc.arg(status)::text = 'active' THEN bearer_digest ELSE NULL END,
    broker_public_key = CASE WHEN sqlc.arg(status)::text = 'active' THEN broker_public_key ELSE NULL END,
    prompt_tokens = prompt_tokens + sqlc.arg(prompt_tokens)::bigint,
    completion_tokens = completion_tokens + sqlc.arg(completion_tokens)::bigint,
    cost_usd_micros = cost_usd_micros + sqlc.arg(cost_usd_micros)::bigint,
    active_requests = 0,
    revoked_at = CASE
      WHEN sqlc.arg(status)::text = 'revoked' THEN COALESCE(revoked_at, sqlc.arg(now)::timestamptz)
      ELSE NULL
    END,
    updated_at = sqlc.arg(now)::timestamptz
WHERE grant_id = sqlc.arg(grant_id)::uuid;

-- name: MarkCodingInferenceRequestUnsettled :exec
UPDATE coding_inference_requests
SET status = 'unsettled',
    provider_settlement_sha256 = NULL,
    provider_generation_id = NULL,
    provider_settlement_json = NULL,
    unsettled_reason = sqlc.arg(unsettled_reason)::text,
    settled_at = sqlc.arg(settled_at)::timestamptz
WHERE request_row_id = sqlc.arg(request_row_id)::uuid;

-- name: RevokeCodingInferenceGrantUnsettled :exec
UPDATE coding_inference_grants
SET status = 'revoked',
    bearer_digest = NULL,
    broker_public_key = NULL,
    active_requests = 0,
    revoked_at = COALESCE(revoked_at, sqlc.arg(now)::timestamptz),
    updated_at = sqlc.arg(now)::timestamptz
WHERE grant_id = sqlc.arg(grant_id)::uuid;

-- Public-canary inference uses a separate claimed-lease grant and request
-- ledger. Ticket lookup is first; only ErrNoRows falls through here.
-- Shared capacity and settlement-identity uniqueness UNION both ledgers.

-- name: GetCodingCertificationInferenceGrantForUpdate :one
SELECT * FROM coding_certification_inference_grants
WHERE grant_id = sqlc.arg(grant_id)::uuid
FOR UPDATE;

-- name: GetLatestCodingCertificationInferenceRequestForUpdate :one
SELECT * FROM coding_certification_inference_requests
WHERE grant_id = sqlc.arg(grant_id)::uuid
ORDER BY sequence DESC
LIMIT 1
FOR UPDATE;

-- name: GetCodingCertificationInferenceRequestForUpdate :one
SELECT * FROM coding_certification_inference_requests
WHERE grant_id = sqlc.arg(grant_id)::uuid
  AND sequence = sqlc.arg(sequence)::integer
FOR UPDATE;

-- name: InsertCodingCertificationInferenceRequest :one
INSERT INTO coding_certification_inference_requests (
    request_row_id,
    grant_id,
    lease_id,
    generation,
    sequence,
    request_sequence,
    attempt,
    request_id,
    case_id,
    profile_capability_id,
    inference_grant_sha256,
    locked_request_sha256,
    status,
    provider_settlement_sha256,
    provider_generation_id,
    provider_settlement_json,
    unsettled_reason,
    started_at,
    settled_at,
    weight_eligible
) VALUES (
    sqlc.arg(request_row_id)::uuid,
    sqlc.arg(grant_id)::uuid,
    sqlc.arg(lease_id)::uuid,
    sqlc.arg(generation)::integer,
    sqlc.arg(sequence)::integer,
    sqlc.arg(request_sequence)::integer,
    sqlc.arg(attempt)::integer,
    sqlc.arg(request_id)::uuid,
    sqlc.arg(case_id)::text,
    sqlc.arg(profile_capability_id)::text,
    sqlc.arg(inference_grant_sha256)::text,
    sqlc.arg(locked_request_sha256)::text,
    'started',
    NULL,
    NULL,
    NULL,
    NULL,
    sqlc.arg(started_at)::timestamptz,
    NULL,
    false
)
RETURNING *;

-- name: BeginCodingCertificationInferenceGrantRequest :exec
UPDATE coding_certification_inference_grants
SET request_count = request_count + sqlc.arg(request_increment)::integer,
    active_requests = 1,
    updated_at = sqlc.arg(now)::timestamptz
WHERE grant_id = sqlc.arg(grant_id)::uuid;

-- name: SettleCodingCertificationInferenceRequest :exec
UPDATE coding_certification_inference_requests
SET status = sqlc.arg(status)::text,
    provider_settlement_sha256 = sqlc.arg(provider_settlement_sha256)::text,
    provider_generation_id = sqlc.narg(provider_generation_id)::text,
    provider_settlement_json = sqlc.arg(provider_settlement_json)::text,
    unsettled_reason = NULL,
    settled_at = sqlc.arg(settled_at)::timestamptz
WHERE request_row_id = sqlc.arg(request_row_id)::uuid;

-- name: ApplyCodingCertificationInferenceGrantSettlement :exec
UPDATE coding_certification_inference_grants
SET status = sqlc.arg(status)::text,
    bearer_digest = CASE WHEN sqlc.arg(status)::text = 'active' THEN bearer_digest ELSE NULL END,
    broker_public_key = CASE WHEN sqlc.arg(status)::text = 'active' THEN broker_public_key ELSE NULL END,
    prompt_tokens = prompt_tokens + sqlc.arg(prompt_tokens)::bigint,
    completion_tokens = completion_tokens + sqlc.arg(completion_tokens)::bigint,
    cost_usd_micros = cost_usd_micros + sqlc.arg(cost_usd_micros)::bigint,
    active_requests = 0,
    revoked_at = CASE
      WHEN sqlc.arg(status)::text = 'revoked' THEN COALESCE(revoked_at, sqlc.arg(now)::timestamptz)
      ELSE NULL
    END,
    updated_at = sqlc.arg(now)::timestamptz
WHERE grant_id = sqlc.arg(grant_id)::uuid;

-- name: MarkCodingCertificationInferenceRequestUnsettled :exec
UPDATE coding_certification_inference_requests
SET status = 'unsettled',
    provider_settlement_sha256 = NULL,
    provider_generation_id = NULL,
    provider_settlement_json = NULL,
    unsettled_reason = sqlc.arg(unsettled_reason)::text,
    settled_at = sqlc.arg(settled_at)::timestamptz
WHERE request_row_id = sqlc.arg(request_row_id)::uuid;

-- name: RevokeCodingCertificationInferenceGrantUnsettled :exec
UPDATE coding_certification_inference_grants
SET status = 'revoked',
    bearer_digest = NULL,
    broker_public_key = NULL,
    active_requests = 0,
    revoked_at = COALESCE(revoked_at, sqlc.arg(now)::timestamptz),
    updated_at = sqlc.arg(now)::timestamptz
WHERE grant_id = sqlc.arg(grant_id)::uuid;
