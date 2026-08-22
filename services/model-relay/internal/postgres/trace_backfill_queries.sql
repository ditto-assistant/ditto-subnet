-- Trace backfill reads: keyset-paginated walks of the inference ledgers joined
-- to their grants, in (started_at, grant_id, nonce) order, used by
-- `model-relay trace-backfill` to export historical rows (metadata only; the
-- relay never stored bodies before the trace capture existed) to the trace
-- buckets. The optional delete is bounded to the exact key range an exported
-- batch covered and to rows no live grant can still touch.
--
-- Lock order: reads only; the delete touches inference_requests alone
-- (rank 3) and never a grant row.

-- name: ListInferenceRequestsForBackfill :many
SELECT r.grant_id, r.nonce, r.generation, r.status, r.model, r.reserved_tokens,
       r.max_chargeable_tokens, r.prompt_tokens, r.completion_tokens,
       r.cost_microusd, r.started_at, r.completed_at, r.upstream_provider,
       r.timed_out, r.latency_ms, r.request_kind, r.upstream_attempts,
       r.openrouter_attempts, r.fallback_phase, r.terminal_error_code,
       g.agent_id, g.bench_version, g.validator_hotkey, g.slot_id,
       g.ticket_deadline, g.status AS grant_status,
       g.generation AS grant_generation, g.allowed_models, g.route_provider,
       g.route_profile, g.route_quantization, g.expires_at AS grant_expires_at
  FROM inference_requests r
  JOIN inference_grants g ON g.grant_id = r.grant_id
 WHERE (r.started_at, r.grant_id, r.nonce) > (sqlc.arg(after_started_at)::timestamptz, sqlc.arg(after_grant_id)::uuid, sqlc.arg(after_nonce)::uuid)
   AND r.started_at < sqlc.arg(until)::timestamptz
 ORDER BY r.started_at, r.grant_id, r.nonce
 LIMIT sqlc.arg(batch_limit);

-- name: ListConfirmationInferenceRequestsForBackfill :many
SELECT r.grant_id, r.nonce, r.generation, r.status, r.model, r.reserved_tokens,
       r.max_chargeable_tokens, r.prompt_tokens, r.completion_tokens,
       r.cost_microusd, r.upstream_provider, r.started_at, r.completed_at,
       g.ticket_id, g.bundle_id, g.validator_hotkey, g.lane,
       g.status AS grant_status, g.generation AS grant_generation,
       g.model AS grant_model, g.provider, g.route_provider,
       g.receipt_provider, g.profile_revision,
       g.expires_at AS grant_expires_at
  FROM confirmation_inference_requests r
  JOIN confirmation_inference_grants g ON g.grant_id = r.grant_id
 WHERE (r.started_at, r.grant_id, r.nonce) > (sqlc.arg(after_started_at)::timestamptz, sqlc.arg(after_grant_id)::uuid, sqlc.arg(after_nonce)::uuid)
   AND r.started_at < sqlc.arg(until)::timestamptz
 ORDER BY r.started_at, r.grant_id, r.nonce
 LIMIT sqlc.arg(batch_limit);

-- name: DeleteBackfilledInferenceRequests :execrows
DELETE FROM inference_requests r
 USING inference_grants g
 WHERE g.grant_id = r.grant_id
   AND (r.started_at, r.grant_id, r.nonce) >= (sqlc.arg(from_started_at)::timestamptz, sqlc.arg(from_grant_id)::uuid, sqlc.arg(from_nonce)::uuid)
   AND (r.started_at, r.grant_id, r.nonce) <= (sqlc.arg(to_started_at)::timestamptz, sqlc.arg(to_grant_id)::uuid, sqlc.arg(to_nonce)::uuid)
   AND r.status <> 'started'
   AND r.started_at < sqlc.arg(retain_before)::timestamptz
   AND g.expires_at < sqlc.arg(retain_before)::timestamptz;

-- name: DeleteBackfilledConfirmationInferenceRequests :execrows
DELETE FROM confirmation_inference_requests r
 USING confirmation_inference_grants g
 WHERE g.grant_id = r.grant_id
   AND (r.started_at, r.grant_id, r.nonce) >= (sqlc.arg(from_started_at)::timestamptz, sqlc.arg(from_grant_id)::uuid, sqlc.arg(from_nonce)::uuid)
   AND (r.started_at, r.grant_id, r.nonce) <= (sqlc.arg(to_started_at)::timestamptz, sqlc.arg(to_grant_id)::uuid, sqlc.arg(to_nonce)::uuid)
   AND r.status <> 'started'
   AND r.started_at < sqlc.arg(retain_before)::timestamptz
   AND g.expires_at < sqlc.arg(retain_before)::timestamptz;
