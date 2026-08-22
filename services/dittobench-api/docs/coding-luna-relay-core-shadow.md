# Ticket-bound Luna relay core

`internal/codingrelay` is the unwired, validator-local execution core for the
locked Luna contract. It accepts miner-visible Chat Completions requests,
projects them through `codingcontract.LockInferenceRequest`, dispatches only a
deep-owned typed request to a trusted upstream, and returns only the miner-safe
response projection.

This package does not open a listener or read a provider key. It has no
Platform grant exchange, worker, score, or weight path. A future local gateway
must construct one relay per ticket, mount its handler behind an unguessable
source-bound capability, and supply both required ports.

## Immutable binding

Construction binds:

- shadow attempt/certification, agent artifact, and harness instance;
- canonical ticket UUID, case, and profile capability;
- live grant UUID/generation and the canonical policy digest;
- immutable issuance time and an authoring deadline no more than two hours
  after issuance;
- lower task request, prompt-token, and completion-token ceilings; cost remains
  bound by the v1 policy ceiling because v1 has no separate signed task-cost
  field.

The relay recomputes the policy digest and rejects aliases, nil identities,
invalid lifetimes, over-policy budgets, and typed-nil dependencies. An expired
relay may be reconstructed to revoke and publish evidence, but cannot admit a
new request. Evidence finalization separately requires the caller to repeat
the attempt, artifact,
harness, task, policy, deadline, and effective token/request budgets. Grant ID
and generation never come from the evidence caller.

## Durable journal port

The injected `Journal` must durably commit `Begin` before provider activity.
Each dispatch fixes global sequence, logical request sequence, attempt,
validator-generated request UUID, miner-request digest, locked-request digest,
the deep-owned parsed miner request, and the exact effective locked request.
Recovery recomputes the miner digest and re-applies the remaining completion
budget before accepting the locked request. `Complete` atomically replaces
that marker with the trusted settlement, derived receipt, normalized private
response, and stripped miner response before the HTTP success is written.

Journal methods are exact-byte idempotent and conflict rejecting. `Load`
supports response-loss replay and settled restart recovery. Every nonempty or
revoked snapshot repeats the complete immutable relay binding, so even an empty
`not_invoked` record cannot move between tickets. A dispatch without
a matching completion, or a final receipt-free retry without its terminal
attempt, is deliberately non-rerunnable. The core returns
`ErrAmbiguousDispatch`; it never grants a clean model retry after activity that
may have reached the provider.

The core defines this persistence port but no filesystem or database adapter.
The next gateway PR must provide the single-owner durable implementation and
bind its reservation/capacity and retention to the evidence outbox lifecycle.
`Load` must enforce the policy receipt-count and per-object byte ceilings before
allocating decoded records; the core rejects an oversized entry count before
making its defensive clone.
After a durable completion, the live relay drops locked requests and private
provider projections from RAM; it retains compact receipts/settlements and only
the latest miner response needed for response-loss replay.

## Trusted upstream port

The injected `Upstream` receives no HTTP headers, miner bearer, URL, routing
override, or provider credential. It receives only the locked request,
validator request identity/sequence, and deadline. It must return:

1. the canonical Platform provider-settlement projection for every admitted
   attempt; and
2. normalized provider-response bytes only when the outcome is `complete`, or
   the canonical private failure-response projection when a failure settlement
   carries a `canonical_json_v1` response digest.

The core verifies ticket/case/profile, grant generation, request/attempt,
locked-request digest, model/route/profile, no fallback, router/pipeline/cache
claims, independent usage and cost settlement, and response identity/digest.
The response completion count may not exceed the effective locked request.
Failure projections are re-hashed before journaling, so a response digest is
never accepted as an unauditable label.

An upstream error without a settlement leaves the pre-dispatch marker intact
and becomes validator infrastructure. Its error text is not propagated. A
receipt-free pre-provider outcome retries the same request UUID and locked
bytes, up to the policy attempt bound. A trusted provider failure is terminal
and remains distinct from an unreceipted infrastructure failure.

## Admission, replay, and revocation

Only one logical model request executes at a time. A concurrent different body
fails; a concurrent identical body waits for the owner and receives its cached
result. Admission exposes only two bounded parser slots—the owner and one
possible exact-replay waiter—so concurrent bodies cannot multiply the 4 MiB
request envelope. An exact replay of the last completed body never calls the
provider again. New requests are rejected after a terminal provider failure, budget
exhaustion, deadline expiry, clock rollback, or revocation.

Before locking, the relay caps `max_completion_tokens` to the remaining signed
task budget. Provider-reported prompt, completion, request, retry, and cost
totals are checked again before any successful response. An over-budget trusted
settlement is retained but cannot become evidence or a miner success.

Client cancellation is honored before durable admission. After `Begin`, the
provider and journal work use task/policy deadlines rather than the client
connection, so a lost HTTP response cannot erase accounting. `Revoke` closes
admission immediately, waits for the admitted attempt to settle, and durably
marks the journal. `Evidence` is unavailable until that revocation is durable.

## HTTP boundary

`Handler` serves only `POST /chat/completions` with unencoded
`application/json`. It rejects query strings, alternate paths/methods, body
overflow, malformed contract input, and unsupported encodings. Authorization,
referer, and OpenRouter title headers are never forwarded because they are not
represented in the typed upstream request.

Successful responses omit provider identity and cost. Errors use bounded
generic OpenAI-compatible envelopes and do not include task text, provider
details, upstream errors, ticket IDs, or credentials. The gateway remains
responsible for the outer source-bound route and network policy.

## Current activation boundary

The package has an evidence-only adapter for the existing certification port,
but nothing constructs or mounts it in production. No current model-relay
traffic may claim this evidence. The remaining gateway work includes durable
journal storage, Platform coding-grant acquisition and settlement, capability
mount/revocation, harness orchestration, and terminal publication.

Validation:

```bash
cd services/dittobench-api
go test -race ./internal/codingrelay ./internal/codingcertifier
go vet ./internal/codingrelay ./internal/codingcertifier
```
