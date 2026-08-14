-- Inference concurrency settings: an append-only revision board written by
-- platform admin endpoints. The relay only ever reads the latest revision,
-- on its OWN pooled connection, NEVER inside the admission transaction
-- (doing so would extend the grant+ticket critical section), and caches it
-- for 5 seconds in process. A DB error serves shipped defaults WITHOUT
-- caching them; corrupt JSON payloads also fail open to shipped defaults.

-- name: GetLatestInferenceConcurrencySettings :one
-- Latest whole-policy revision for the only scope ('*'). pgx.ErrNoRows means
-- "no operator override yet": serve shipped defaults.
SELECT * FROM inference_concurrency_settings_revisions
WHERE scope = '*'
ORDER BY revision DESC
LIMIT 1;
