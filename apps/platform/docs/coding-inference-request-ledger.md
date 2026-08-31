# Shadow coding inference request ledger

Platform persists each admitted coding-model dispatch before provider activity.
Private-ticket grants write `coding_inference_requests`. Claimed-lease public
canary grants write `coding_certification_inference_requests` and never reuse
the ticket table or its ticket foreign keys. Dispatch `ticket_id` remains the
wire identity: for canary it is the lease UUID and the durable column is
`lease_id`. Both ledgers consume the exact generation issued by their grant
table and stay separate from ordinary benchmark inference tables.

## Durable lifecycle

The request identity fixes the ticket, case, profile capability, policy digest,
grant generation, global sequence, logical request sequence, retry attempt,
request UUID, and locked provider-request digest. A grant permits one active
request.

Rows transition as follows:

```text
started -> receipt_free_retry -> started -> complete
started -> provider_failure
started -> unsettled
```

`receipt_free_retry` keeps the same request UUID, logical sequence, and locked
request digest. It increments only the attempt and global sequence, so a retry
does not spend a second logical request slot. `complete` permits the next
logical request while budget remains. `provider_failure` and `unsettled` are
terminal. An unsettled row records only a closed failure reason and revokes the
grant; possible provider activity never receives a clean retry.

Settlement stores the bounded canonical
`dittobench-coding-provider-settlement-v1` projection and its independently
recomputed digest. Platform checks the projection against the immutable grant,
dispatch identity, locked policy, route, provider metadata, response digest,
usage, and cost. Settlement digests and non-null upstream generation IDs are
globally unique across both ledgers, preventing provider evidence from being
transplanted across tickets or from a private ticket onto a canary lease.
Actual trusted usage and cost are booked even when the result crosses the
final budget boundary. Exact settlement replay is idempotent after grant
exhaustion, rotation, or later logical requests; changed replay is rejected.

The ledger contains no bearer, provider credential, prompt, locked request
body, raw response, repository content, memory content, test material, or
grader evidence. Every row remains `weight_eligible=false`.

## Current boundary

The disabled-by-default model-relay coding route now admits a ticket grant
first and, only on a missing ticket row, a claimed-lease certification grant.
It reserves and settles on the matching ledger, and shared concurrency plus
settlement-identity uniqueness count both started ledgers. No deployment
config enables the route and no validator gateway invokes it. Coding task
scheduling, scoring and weight activation remain absent. The next integration
must construct the local gateway and preserve the same reserve-before-dispatch,
exact-settlement and terminal-`unsettled` rules.

Validation:

```bash
cd apps/platform
python scripts/check_migration_order.py origin/main
uv run pytest -q ditto/tests/db/queries/test_coding_inference_requests.py \
  ditto/tests/db/queries/test_coding_certification_inference_requests.py
make lint lint-copy typecheck test

cd ../..
services/model-relay/scripts/gen-schema.sh
cd services/model-relay && go test ./... && go vet ./...
```
