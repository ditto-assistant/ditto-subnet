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
SELECT pg_advisory_xact_lock(hashtextextended($1, 0));

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
