-- Validator request nonces: the relay writes exactly one row per successful
-- /exchange; the platform-role janitor prunes expired rows. The nonce PK is
-- the single-use guard: a unique violation means replay, and the caller MUST
-- run this inside a savepoint (pgx nested transaction) so the replay does
-- not poison the outer /exchange transaction.

-- name: ConsumeValidatorNonce :exec
-- Consume a validator request nonce. used_at/expires_at are computed by the
-- application (now, now + exchange max age), matching the Python
-- consume_validator_nonce. A duplicate-key error on the nonce PK signals a
-- replayed exchange (mapped to HTTP 409 by the endpoint).
INSERT INTO validator_request_nonces (nonce, validator_hotkey, used_at, expires_at)
VALUES (
    sqlc.arg(nonce)::uuid,
    sqlc.arg(validator_hotkey)::text,
    sqlc.arg(used_at)::timestamptz,
    sqlc.arg(expires_at)::timestamptz
);
