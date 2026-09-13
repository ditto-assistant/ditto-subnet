-- Miner ↔ Ditto account links (apps/platform migration 7a2c91d4e5f0). Read-only
-- here: the Platform owns linking; the relay only asks "does this agent's
-- miner have a consenting Ditto account?" to attribute a Ditto Router call.
-- No lock: the row is advisory attribution, never a budget gate.

-- name: GetActiveMinerDittoLinkForAgent :one
SELECT l.miner_hotkey, l.ditto_user_id, l.ditto_email, l.linked_via
FROM agents a
JOIN miner_ditto_links l ON l.miner_hotkey = a.miner_hotkey
WHERE a.agent_id = sqlc.arg(agent_id)::uuid
  AND l.revoked_at IS NULL;
