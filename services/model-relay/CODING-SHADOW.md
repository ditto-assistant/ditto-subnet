# Shadow coding Luna handler

The model relay mounts
`POST /api/v1/inference/coding/chat/completions` as the trusted producer for
the validator-side `codingplatform` client. The route is independently
disabled by default and returns 404 unless all of these are true:

```text
DITTO_CODING_INFERENCE_ENABLED=true
OPENROUTER_API_KEY=<server-only credential>
DITTO_CODING_INFERENCE_ACCOUNT_GUARDRAIL=openrouter_private_account_v1
```

The ordinary inference proxy does not enable coding implicitly. Coding uses
separate per-validator/global concurrency caps, defaulting to 4/16 and bounded
at 32/64.

## Admission and persistence

The handler accepts only the proof-bound
`dittobench-coding-inference-dispatch-v1` envelope. Before provider activity it:

1. bounds and duplicate-checks the JSON document;
2. verifies the bearer digest and Ed25519 DPoP over the exact body;
3. binds ticket, case, profile, grant generation and deadline to the locked DB
   grant;
4. independently validates the fixed system prompt, tool schema, Luna model,
   medium/excluded reasoning, serial tools, provider route, ZDR and no-fallback
   request;
5. locks the grant and latest request row;
6. uses PostgreSQL `clock_timestamp()` for lease and durable event time;
7. enforces logical request/retry ordering, task budgets and concurrency;
8. commits a `started` coding request before calling OpenRouter.

Receipt-free retry keeps the request UUID, logical sequence and locked request
digest while advancing only attempt/global sequence. It never consumes a
second logical request slot.

## Provider boundary

Only the locked request object reaches OpenRouter. The relay replaces every
inbound header with its own provider authorization and fixed metadata/cache
headers. It performs one HTTP attempt—provider retries remain explicit,
journaled coding-relay attempts rather than hidden transport retries.

A trusted settlement requires JSON router metadata for the exact Luna model,
one Azure endpoint, no fallback and an empty transformation pipeline. Complete
responses additionally require coherent generation identity, tool-call shape,
token usage and cost. The relay constructs and hashes the canonical normalized
response itself.

Explicit pre-provider metadata produces `receipt_free_retry`. Selected HTTP or
invalid-response failures produce canonical private failure projections only
when their provider identity and accounting are available. Missing metadata,
transport loss, timeout, oversized bodies or unverifiable accounting become a
durable `unsettled` request and revoke the grant; they never receive a clean
retry.

The request and grant settlement commit before the client response is written.
Provider generation IDs and settlement digests remain globally unique through
the Platform schema. No prompt, locked request, raw provider response, bearer
or provider credential is stored in the Platform ledger.

## Activation boundary

No deployment file sets the coding gate or guardrail. No validator gateway
constructs the coding relay/client, and no scheduler calls this route. The
handler therefore cannot make a production coding provider request in the
current stack. A later deployment must also raise the model-relay process kill
timeout above the locked 300-second coding drain before enabling the gate.

Coding contract v1 remains permanently `weight_eligible=false`. This handler
does not write ordinary scores, rank miners or change emissions.

Validation:

```bash
cd services/model-relay
make fmt-check vet test sqlc-check release-build
```
