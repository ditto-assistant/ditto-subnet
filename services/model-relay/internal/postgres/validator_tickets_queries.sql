-- Validator tickets: LOCK RANK 1 of the repo-wide hot-table lock order
-- validator_tickets -> inference_grants -> inference_requests. Every relay
-- transaction that will lock a grant row MUST take its ticket row lock
-- first (activate_inference_grant, begin_inference_request,
-- finish_inference_request all do). The relay never updates ticket data
-- columns — it only reads the ticket's liveness fields — so the lock is a
-- SHARE lock: relay transactions on the same ticket no longer queue behind
-- each other (the 2026-09-07 slow log showed 28,664 executions averaging
-- 916 ms on an 8,625-row table, all tuple-lock waits), while a Platform
-- UPDATE or DELETE of the ticket still waits for every in-flight relay
-- transaction and every later relay transaction sees the new row. Budget
-- accounting is serialized on the grant row (rank 2), not here.

-- name: GetValidatorTicketForShare :one
-- Share-lock and read the ticket that owns a grant (composite PK). Zero rows
-- is a legal outcome (ticket deleted): callers treat pgx.ErrNoRows as
-- "ticket missing" and fail closed, they do not error out.
SELECT * FROM validator_tickets
WHERE agent_id = sqlc.arg(agent_id)::uuid
  AND bench_version = sqlc.arg(bench_version)::integer
  AND validator_hotkey = sqlc.arg(validator_hotkey)::text
FOR SHARE;
