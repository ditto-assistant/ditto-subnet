-- name: GetLatestSubmissionSettings :one
SELECT revision, cooldown_seconds, fee_amount_rao
FROM submission_settings_revisions
ORDER BY revision DESC
LIMIT 1;

-- name: GetLatestSubmissionDepositAddress :one
SELECT payment_address
FROM submission_deposit_address_revisions
ORDER BY revision DESC
LIMIT 1;

-- name: IsHotkeyBanned :one
SELECT EXISTS (
    SELECT 1 FROM banned_hotkeys WHERE hotkey = $1
) AS banned;

-- name: GetSameHotkeyAgentBySHA :one
SELECT a.agent_id, a.status::text AS status
FROM agents AS a
JOIN evaluation_payments AS p ON p.agent_id = a.agent_id
WHERE a.miner_hotkey = $1 AND a.sha256 = $2
ORDER BY a.created_at ASC, a.agent_id ASC
LIMIT 1;

-- name: GetSubmissionLatestPaidCreatedAt :one
SELECT max(a.created_at)::timestamptz
FROM agents AS a
JOIN evaluation_payments AS p ON p.agent_id = a.agent_id
WHERE p.miner_coldkey = $1;

-- name: LockUploadAdmissionColdkey :exec
SELECT pg_advisory_xact_lock(hashtextextended(sqlc.arg(miner_coldkey), 0));

-- name: LockClaPolicy :exec
-- Matches Platform's policy publication and revision lock.
SELECT pg_advisory_xact_lock(hashtextextended('cla-policy', 0));

-- name: GetUploadAdmissionForColdkey :one
SELECT miner_coldkey, token, miner_hotkey, sha256, settings_revision,
       cooldown_seconds, fee_amount_rao, payment_send_address,
       legacy_payment_cutoff_at, created_at, expires_at
FROM upload_admission_reservations
WHERE miner_coldkey = $1
FOR UPDATE;

-- name: DeleteUploadAdmission :exec
DELETE FROM upload_admission_reservations WHERE miner_coldkey = $1;

-- name: InsertUploadAdmission :one
INSERT INTO upload_admission_reservations (
    miner_coldkey, token, miner_hotkey, sha256, settings_revision,
    cooldown_seconds, fee_amount_rao, payment_send_address,
    legacy_payment_cutoff_at, created_at, expires_at
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NULL, $9, $10)
RETURNING miner_coldkey, token, miner_hotkey, sha256, settings_revision,
          cooldown_seconds, fee_amount_rao, payment_send_address,
          legacy_payment_cutoff_at, created_at, expires_at;

-- name: GetClaUploadGate :one
-- The migration seeds audited mode=off. Missing policy is invalid state and
-- must fail closed, matching Python, rather than silently disabling consent.
WITH policy AS (
    SELECT mode FROM cla_settings_revisions ORDER BY revision DESC LIMIT 1
), current_version AS (
    SELECT version, sha256 FROM cla_versions
    WHERE effective_at <= sqlc.arg(observed_at)::timestamptz
    ORDER BY version DESC LIMIT 1
)
SELECT policy.mode, current_version.version,
    EXISTS (
        SELECT 1 FROM cla_signatures AS signature
        WHERE signature.netuid = sqlc.arg(netuid)
          AND signature.hotkey = sqlc.arg(hotkey)
          AND signature.cla_version = current_version.version
          AND signature.cla_sha256 = current_version.sha256
    ) AS has_signature
FROM policy LEFT JOIN current_version ON true;
