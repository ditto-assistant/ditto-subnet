# Native hosted provider adapter

Status: private Python adapter with real PostgreSQL accounting tests and a
synthetic HTTP provider. No factory wiring, HTTP listener, source route, provider
credential loading, worker activation or live model call is enabled by this PR.

## Authority and transport

`HostedProviderAdapter` belongs inside the trusted Platform worker, not the
validator or miner. It requires an existing native grant, policy, private provider
key and explicit trusted `BudgetEstimator`. Grant UUIDs are not authentication.
The [runtime budget profile](coding-hosted-budget-v2.md) supplies a concrete
conservative estimator, pinned by the native policy. Its first algorithm reserves
a reviewed billed-input cap instead of guessing local tokenization. The operator
must approve actual billing/route evidence before use. Synthetic ceilings are not
an approved live profile; generic estimators work only with the mock transport.

The adapter validates the locked request and sends its canonical known-field
projection only after a fresh PostgreSQL reservation commits. The returned
evaluation, attempt and policy identity come from that grant, not caller claims.
Replayed requests never dispatch again. Lost commit acknowledgements fail closed;
there is no automatic retry or new request identity to repair ambiguity.

The only production destination is the exact HTTPS OpenRouter Chat Completions
origin/path. The adapter owns its HTTP client: TLS verification, zero retries,
no redirects or ambient proxy configuration, fixed headers, no cookies, bounded
response streaming and a deadline capped by both policy and grant expiry. The
private `_test_transport` seam must never be populated from runtime/user input.
Provider I/O occurs outside database transactions. Python objects are redacted
from repr, but this is not a secure-memory zeroization guarantee.

## Provider evidence

Only HTTP 200 JSON with matching model/provider, a direct first attempt, exactly
one selected endpoint, no BYOK and an explicitly empty pipeline is accepted.
An optional attempt log must independently agree. Additive advisory metadata is
ignored, not included in miner-visible output. Missing authoritative fields fail
closed. Token totals and numeric billing must agree; decimal USD costs round up
to microdollars and actual usage must fit the committed reservation.

The normalized response uses `dittobench-coding-hosted-inference-response-v2` and
reuses only version-neutral chat/tool shapes. It strips router/debug metadata.
No response is released until native settlement commits and the ledger's active
assignment, task, grant and expiry checks pass again. Revocation during a call
can still settle billing, but prevents late output delivery.

The settlement's provider-receipt digest hashes the canonical pair
`{"provider_api":"openrouter","generation_id":"..."}`. This is a generation
identity commitment: changing a replayed response's text or usage cannot avoid
global receipt uniqueness. It is not a hash of all raw provider bytes.
`ProviderResult.provider_evidence` separately retains the bounded raw response for
the trusted sealed-evidence publisher. Never send this field to the miner or log
it. The publisher must bind the raw evidence and settlement together; this PR
does not implement publication or a signed terminal result.

OpenRouter documents [router metadata](https://openrouter.ai/docs/guides/features/router-metadata)
and [usage accounting](https://openrouter.ai/docs/cookbook/administration/usage-accounting).
These are provider assertions, not independent proof of deployment behavior.
In particular, metadata `region` identifies an edge region, not proof of Azure EU
execution. The locked `azure/eu` route, account/ZDR controls, caching behavior and
budget estimator still need a reviewed live capability profile. Documentation
and mock responses cannot certify those properties.

## Failure, cancellation and recovery

Any failed attempt closes that adapter's local admission. A timeout, socket error,
non-200 response, invalid receipt or failed settlement leaves the durable request
reserved with its full ceilings. It never becomes zero usage or automatically
`uncertain`. A reserved row blocks another dispatch and patch freeze.

The worker must close its source route, cancel/await active transport, call
`revoke`, and retain failure evidence. `revoke` reports ledger drain only; it does
not stop an active call. Closing a local socket is not proof that remote provider
execution or billing stopped. An operator-reviewed recovery path must establish
quiescence before `mark_uncertain`. If settlement committed but its acknowledgement
was lost, recovery uses durable state and must not dispatch again. No adapter
instance may be recreated to bypass failure or revive a used source capability.

## Next boundary

The [native source-bound relay](coding-hosted-relay-v2.md) now connects this
adapter through an authenticated private Unix socket, with miner request locking
and revocation/drain checks. Next integrate the durable evidence publisher and
full worker execution with an approved live budget/provider profile. There are no schema migrations, public API changes, embeddings,
public-practice changes, scoring, weights or emissions in this layer.
