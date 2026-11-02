# Public administrative activity

The dashboard at `/activity` reads `GET /api/v1/public/admin-activity` without
credentials. Search (`q`, up to 120 characters), outcome (`status`), and bounded
cursor pagination (`before`, `limit`, maximum 100) run in Postgres. Newest intent
IDs come first. The response is publicly cached for five seconds.

## Coverage and durability

Every authenticated Platform admin POST, PUT, PATCH, and DELETE passes through
`require_admin`. This commits an `admin_activity` intent before the handler can
mutate state. A separate `admin_activity_outcomes` row records HTTP completion.
Neither record is updated or deleted by application code. Existing transactional
domain audit ledgers remain unchanged and authoritative for individual effects.

All 78 current mutation routes are covered, including Backroom UI and MCP calls,
reviewer/concurrency settings, canary issue/cancel, batch rulings, retry requests,
trusted builds, and fleet settings. A route-inventory regression fails if any
admin mutation omits this boundary. New endpoints using it inherit coverage.
Read-only polling and rejected authentication attempts do not create activity;
sensitive artifact reads keep their existing dedicated artifact audit ledger.

An unavailable initial audit database prevents the mutation. A crash or failed
completion insert leaves the durable intent with `unknown` outcome. A failed
completion insert does not change an already-applied request into a retryable
HTTP failure. `succeeded` means HTTP completion below 400, not fleet adoption,
canary completion, or success of every item inside a batch. `failed` means an
HTTP error; consult domain evidence before retrying a request with side effects.

## Public projection

The feed never returns or searches operator email, arbitrary request/response
bodies, authorization, reasons, error text, secrets, private seeds, or benchmark
source. The signed-in actor forwarded by Backroom is retained privately.
Action names come from matched route templates, not raw URLs. Public UUIDs and
bounded revisions are explicitly allowed. Settings use validated models and a
frozen field inventory: adding a top-level policy field does not expose it.
Unknown actions still get a visible event with their method and outcome.

Reviewed policy values include reviewer, inference concurrency, validator slot,
burn, efficiency, continual retest, queue, copy court, confirmation bundle, and
screener provider settings. Submission fees/cooldown, source embargo hours, and
conversation enablement have explicit scalar projections. Free-form internal
notes and unreviewed fields remain private.

The migration imports retained settings revisions, operator events from the
score audit chain (including canaries and retest requests), benchmark rollout
history, inference routing history, hotkey unban history, and retained review,
retry, release, retirement, and queue-control ledgers. Imports preserve
original timestamps and have outcome `recorded`, never an invented HTTP status.
Historical settings are re-projected through the same allowlist when read.
Older actions without retained records cannot be reconstructed. The migration
does not rewrite or remove the original ledgers.

## Activation and validation

Deploy the migration before the new API. Deploy the dashboard and generated
Backroom contract in the same monorepo release. Roll back application code if
needed; the schema downgrade refuses to discard newly recorded requests.

Regression tests use real migrated Postgres, cover authenticated success/failure,
private-field exclusion, search/pagination, missing outcomes, write failure when
the audit database is unavailable, and the complete mutation-route inventory.
Dashboard tests cover search, outcome filtering, cursor navigation, empty states,
and retry after an API failure.
