-- Relay-owned provider-wide overload circuit. Platform consumes this row to
-- park scoring and screening leases; no Platform writer opens or closes it.

-- name: OpenProviderOutageCircuit :one
INSERT INTO provider_outage_circuits (
    provider, state, epoch, opened_at, retry_at, last_failure_at,
    failure_count, last_status, last_error_code, updated_at
) VALUES (
    sqlc.arg(provider)::text,
    'open',
    sqlc.arg(epoch)::uuid,
    sqlc.arg(now)::timestamptz,
    sqlc.arg(retry_at)::timestamptz,
    sqlc.arg(now)::timestamptz,
    1,
    sqlc.narg(last_status)::integer,
    sqlc.arg(last_error_code)::text,
    sqlc.arg(now)::timestamptz
)
ON CONFLICT (provider) DO UPDATE SET
    state = 'open',
    epoch = CASE
        WHEN provider_outage_circuits.state = 'closed' THEN EXCLUDED.epoch
        ELSE provider_outage_circuits.epoch
    END,
    opened_at = CASE
        WHEN provider_outage_circuits.state = 'closed' THEN EXCLUDED.opened_at
        ELSE provider_outage_circuits.opened_at
    END,
    retry_at = GREATEST(provider_outage_circuits.retry_at, EXCLUDED.retry_at),
    last_failure_at = GREATEST(
        provider_outage_circuits.last_failure_at,
        EXCLUDED.last_failure_at
    ),
    closed_at = NULL,
    failure_count = CASE
        WHEN provider_outage_circuits.state = 'closed' THEN 1
        ELSE provider_outage_circuits.failure_count + 1
    END,
    last_status = CASE
        WHEN EXCLUDED.last_failure_at >= provider_outage_circuits.last_failure_at
        THEN EXCLUDED.last_status
        ELSE provider_outage_circuits.last_status
    END,
    last_error_code = CASE
        WHEN EXCLUDED.last_failure_at >= provider_outage_circuits.last_failure_at
        THEN EXCLUDED.last_error_code
        ELSE provider_outage_circuits.last_error_code
    END,
    probe_kind = NULL,
    probe_key = NULL,
    probe_expires_at = NULL,
    updated_at = GREATEST(provider_outage_circuits.updated_at, EXCLUDED.updated_at)
RETURNING *;

-- name: CloseProviderOutageCircuit :execrows
UPDATE provider_outage_circuits
SET state = 'closed',
    closed_at = sqlc.arg(now)::timestamptz,
    probe_kind = NULL,
    probe_key = NULL,
    probe_expires_at = NULL,
    updated_at = sqlc.arg(now)::timestamptz
WHERE provider = sqlc.arg(provider)::text
  AND state = 'open'
  AND last_failure_at <= sqlc.arg(request_started_at)::timestamptz;
