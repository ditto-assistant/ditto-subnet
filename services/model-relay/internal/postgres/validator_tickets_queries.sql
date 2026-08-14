-- Validator tickets: LOCK RANK 1 of the repo-wide hot-table lock order
-- validator_tickets -> inference_grants -> inference_requests. Every relay
-- transaction that will lock a grant row MUST lock the owning ticket row
-- first (activate_inference_grant, begin_inference_request,
-- finish_inference_request all do). The relay never updates ticket data
-- columns — it only locks the row and reads its liveness fields.

-- name: GetValidatorTicketForUpdate :one
-- Lock and read the ticket that owns a grant (composite PK). Zero rows is a
-- legal outcome (ticket deleted): callers treat pgx.ErrNoRows as
-- "ticket missing" and fail closed, they do not error out.
SELECT * FROM validator_tickets
WHERE agent_id = sqlc.arg(agent_id)::uuid
  AND bench_version = sqlc.arg(bench_version)::integer
  AND validator_hotkey = sqlc.arg(validator_hotkey)::text
FOR UPDATE;
