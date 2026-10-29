# Gamma transfers and manual GM funding

The authenticated Backroom MCP at `https://backroom.dittobench.ai/mcp` exposes
the same manual collector transfer as the Gamma page:

1. `get_treasury_manual_transfers` reads custody readiness and exact requests.
2. `preview_treasury_manual_transfer` takes a bucket, amount, retained stake and
   audit reason. It returns an immutable envelope, digest and confirmation.
3. `queue_treasury_manual_transfer` echoes those exact values. It queues a real
   on-chain transfer. Track that UUID; never create another while it is unresolved.

A collector transfer delivers SN118 alpha (DITTO) to the approved holding
wallet. A DITTO sale, a TAO payment to GM, and a completed GM purchase are separate
events. Custody finality is not evidence that a GM purchase completed.

## Historical accounting

`get_treasury_funding_history` reads private operator-reconciled history.
`preview_treasury_funding_reconciliation` takes `batchJson`, a JSON-encoded
Platform `FundingBatch`. `record_treasury_funding_reconciliation` takes that
same JSON, the preview's `confirmation_digest`, and its exact `confirmation`.
Both preview and record require `backroom:write` and a live write-level account.
The server supplies the authenticated operator's audit identity.

A batch contains `holding_wallet`, an audit `reason` of at least eight characters, and 1–100
`records`. Each record contains:

- `purchase`: canonical GM `purchase_id` UUID, timezone-aware `occurred_at`,
  integer `credited_usd_cents`, `method: x402-relayed`, `status: completed`.
- `payment`: `reference` in block-index form, `kind: payment`, `occurred_at`,
  `input_asset: TAO`, positive decimal-string `input_amount_atomic`, and the
  exact GM `recipient`.
- Optional `preceding_sale`: `kind: sale`, alpha input and TAO output amounts.
- Optional `earlier_conversions`: historical SN118-alpha to SN28-alpha swaps.

Every movement has an explicit input asset and amount. Conversions also have
an output asset and amount. Assets are `TAO`, `SN118_ALPHA`, or `SN28_ALPHA`;
all atomic amounts are integer strings, bounded to u64. One asset unit has
1,000,000,000 atomic units. Known fields alone determine the confirmation hash;
unknown fields are ignored and confer no authority.

Payment must precede its uniquely matched completed purchase by at most five
minutes. A preceding sale is an observed temporal association, not a claim that
all of that payment came from that sale. Existing TAO balances, fees, shared
sales and earlier conversions must remain explicit. Pending provider purchases
and failed chain attempts do not belong in this history.

This history is append-only and idempotent per purchase. Conflicting purchases,
reused payments and changed chain observations refuse. Shared conversions are
counted once in totals. Records and actor/reason metadata remain private; these
tools do not mutate public receipt proof, historical payee policy, balances,
spending limits or recurring jobs. Operator time/recipient correlation is not
provider transaction attestation. Historical credits added are not GM's current
spendable balance. Automatic top-ups remain off.

## Custody runtime verification

Verify the running process's `PYTHONPATH`, not just its script path. A systemd
`EnvironmentFile` can override `Environment=` from a later drop-in. The manual
worker launcher must pass its immutable source and protocol paths through
`/usr/bin/env PYTHONPATH=...` on `ExecStart` when an environment file supplies
an older path. Do not print the rest of the process environment.

Unsigned expiry recovery closes only the exact expired request after finalized
expiry and proof that it never reserved signed bytes. It neither resets the
journal nor retries a payment. A new manual transfer needs its own authorization
and UUID. Keep recurring collector units masked during manual-only operation.
