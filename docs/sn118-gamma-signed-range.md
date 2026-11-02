# Gamma signed allocation range

The collector coldkey signs a **range** for each service bucket rather than one
fixed percentage. While Gamma is enforcing, the Backroom Gamma % moves inside
that range without a new offline signature. It behaves like burn: each epoch
pin freezes the value saved at materialization time. Leaving the range, or
changing any wallet, still needs a new coldkey signature.

## Signed policy shape

`TreasuryEmissionBucket` accepts two optional bounds:

```json
{
  "bucket_id": "gm_credits",
  "allocation_bps": 2500,
  "holding_coldkey": "<public SS58>",
  "min_allocation_bps": 0,
  "max_allocation_bps": 7500
}
```

- Set both bounds or neither. `allocation_bps` is the signed default and must
  lie inside the range.
- The sum of every bucket's `max_allocation_bps` must be at most 10,000. This
  makes every in-range choice a valid emission split, not just the default.
- A bucket without bounds is fixed. It serializes exactly as before ranges
  existed, so every historical digest, signature and stored epoch pin stays
  byte-identical.
- The bounds are part of the canonical digest. The approval message is
  unchanged: `ditto-treasury-emission-policy-v1:<policy digest>`.

To sign on the offline machine, build the policy with
`ditto_screening_protocol.treasury.TreasuryEmissionPolicy` and take its
`.digest`. Sign `ditto-treasury-emission-policy-v1:<digest>` with the collector
coldkey, then carry the public `{policy, signature}` envelope back. Never move
seeds.

## What moves without the coldkey

| Change | Ranged policy, enforcing | Fixed policy, enforcing |
| --- | --- | --- |
| Gamma % inside the signed range | Saved directly; next epoch folds it | Pause + new signature |
| Gamma % outside the signed range | Refused (409) | Pause + new signature |
| Collector, holding wallet, payee rules, purpose | Pause first | Pause first |

Platform enforces this at three points:

1. **Save.** `POST /admin/treasury-settings` takes the runtime advisory lock. It
   accepts an enforcing-time revision only when the bucket allocations are the
   only change and every bucket stays inside its signed range.
   `GET /admin/treasury-settings` reports `weight_effect: "next_epoch"` while
   this applies.
2. **Epoch pin.** The enforcing producer reads the latest checksummed settings
   revision and writes `allocations` into `EnforcingTreasuryPin`. It refuses
   when the revision is outside the range. Under the same lock as the insert,
   it refuses the epoch if the allocation changed during materialization. Fixed
   policies pin no `allocations`.
3. **Every consumer.** The pin model rejects a ranged policy without
   allocations, a fixed policy with them, and any value outside the signed
   bounds. The validator fold, the Pylon queued and normalized vector guards,
   the weight receipt and Platform's expected vector all use
   `EnforcingTreasuryPin.service_bps`, the pinned value. Receipt ingress splits
   distributions by the pin's `effective_buckets`. A manual transfer may target
   a ranged bucket whenever its signed ceiling is positive.

## Trust tradeoff

A Platform or Backroom admin can now move Gamma anywhere inside the signed
range. That is the same trust that admin already holds over burn. The coldkey
still bounds the range, the wallets and the collector. A compromised admin
cannot route more than `max_allocation_bps` or pay a wallet the coldkey did not
sign.

## Rollout

1. Release this change to Platform, every managed validator, Pylon and Backroom
   first. Older consumers ignore the bound fields, compute a different digest
   and refuse a ranged approval. That fails closed, but it would also block
   activation.
2. Sign the ranged policy offline once. Pause Gamma, install the new approval
   and `DITTO_TREASURY_APPROVED_POLICY_DIGEST` on Platform and the managed
   validators/Pylon (see [gamma-managed-guard-rollout.md](gamma-managed-guard-rollout.md)).
   Then observe and enforce through
   [sn118-gamma-runtime-control.md](sn118-gamma-runtime-control.md).
3. From then on, adjust the Gamma % on the Backroom **Gamma & transfers** page.
   The next epoch pin folds it. Verify finalized weights as usual.
