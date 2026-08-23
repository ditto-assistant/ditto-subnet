# Shadow coding inference grant authority

Platform owns a coding-specific inference capability in
`coding_inference_grants`. It is deliberately separate from ordinary
`inference_grants`: ordinary grants are foreign-keyed to normal validator
tickets and permit benchmark-version routing that cannot prove the locked Luna
model, medium reasoning, Azure EU route, no fallback, private account posture,
plugin policy, cache policy, or coding-ticket identity.

## Authority and lifecycle

One grant is uniquely bound to one `coding_shadow_tickets` row. Grant creation
occurs only after Platform reconstructs the private task lease and rechecks the
ticket, run, screened artifact, active coding certification, authoring phase,
case, profile capability, and canonical inference-policy digest.

The persisted authority fixes:

- ticket, run, validator, case, and profile capability;
- the canonical `inference_grant_sha256` and locked Luna route fields;
- the ticket deadline;
- request budget `min(workspace_tool_calls + 16, 256)`;
- prompt and completion budgets bounded by both task and policy;
- the locked policy cost ceiling;
- zeroed accounting counters and permanent `weight_eligible=false`.

States are `pending`, `active`, `revoked`, and reserved future `exhausted`.
Exchange rotates a fresh inference bearer plus a separate revocation-only
bearer and increments `generation`. Platform stores only their SHA-256 digests
and the normalized broker public key. A new signed
exchange may safely rotate after a lost response; the missing prior bearer then
becomes invalid. Revocation binds the exact observed generation and is durable
and idempotent. Platform also revokes any pending or active grant inside the
same transaction that accepts an authoring freeze or terminal result, so a
forgotten validator cleanup cannot keep authoring inference live.

## Validator API

The three validator-authority requests bind validator hotkey, fresh nonce, timestamp, and a
domain-separated sr25519 signature:

- `POST /api/v1/validator/coding-shadow/inference-grant`
- `POST /api/v1/validator/coding-shadow/inference-exchange`
- `POST /api/v1/validator/coding-shadow/inference-revoke`

The trusted Go gateway can additionally call
`POST /api/v1/validator/coding-shadow/inference-revoke-capability` with the
revocation-only bearer and exact grant/ticket/generation body. That bearer
cannot dispatch inference. Its digest is retained after revocation so a lost
successful response can be retried and receive an authenticated idempotent
acknowledgement.
An active grant created during a rolling upgrade before the revocation digest
column is populated remains valid but cannot use this narrow endpoint; the
validator's existing signed revocation remains the fail-closed fallback.

Responses are `Cache-Control: no-store`. The offer contains no bearer. The
exchange response contains the validator-scoped inference and revocation-only
bearers, but never a
provider API key, provider credential, private prompt, receipt, or settlement.
The validator client refuses redirects, bounds response bytes, verifies the
complete authority projection, and never follows the response URL as an
arbitrary destination.

## Current activation boundary

Grant offer and exchange are mounted but fail with `503` unless the application
receives an explicit `CodingInferenceGrantTransport`. Revocation remains
available for already-persisted grants if that transport is removed. The normal
factory does not create a transport. The separate request ledger can reserve
and settle an already-authorized generation, but no provider or model-relay
route invokes it. Consequently the current stack cannot send a Luna request,
produce model evidence, run a coding task, affect a score, or affect weights.

See `coding-inference-request-ledger.md` for the durable dispatch boundary. A
later PR must add the dedicated provider adapter and gateway without exposing
OpenRouter credentials or accepting miner-authored settlement evidence.

Validation:

```bash
cd apps/platform
python scripts/check_migration_order.py origin/main
make lint lint-copy typecheck test

cd ../..
make lint typecheck test
```
