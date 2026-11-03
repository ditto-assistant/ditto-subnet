# Collector Finney v478 compatibility review — 2026-10-14

Production custody stopped correctly: the signed v473 runtime fingerprint
`0x7773f5c0a6d6e9ea9ff347edcc491246eec08a5cf441d964ee96f40d7fa65a08`
differs from finalized v478
`0x7e722b3aa5c9192c98665a5d65fa91364bf9055e21fc90efce6d59bf4ef414e1`.
This is separate from Gamma allocation authority and validator weights.

## Artifact and source binding

[manifest.json](manifest.json) records independently downloaded and SHA256 /
BLAKE2-256 hashed official compressed v475–v478 WASM, the matching published
srtool digest/source commit, pinned critical source hashes, and the actual
finalized native proxy metadata. The v478 source is
[850f2755bb05a3851f948489f0ab27b0645cebce](https://github.com/RaoFoundation/subtensor/tree/850f2755bb05a3851f948489f0ab27b0645cebce).
This is official artifact verification plus scoped source review, not an
independent reproducible WASM build. No v474 fingerprint is inferred.

## Scoped compatibility findings

Compared v473 to the exact v478 source and the intervening official tags:

- `common/src/proxy.rs`, `pallets/proxy/src/lib.rs`, stake move/transfer,
  StakeInfo, account and lock implementations retain their critical contract.
  Transfer still has exactly the five previously reviewed native calls; the
  application still prepares only bounded same-hotkey/SN118 `transfer_stake`.
- Registration's native union additionally admits `pow_register` at call152.
  The existing `register_limit` call134 retains its bounded burn path. The
  adapter recognizes this exact native inventory for the new hashes; it does
  not implement PoW registration or allow arbitrary/nested calls.
- New Null consensus changes reward computation and introduces a sole-owner
  share-pool credit fast path. This review admits **explicit Yuma only**, at
  each exact current/historical observation hash. Unknown/Null states refuse.
  In Yuma, `credit_epoch_owner_emission` uses the ordinary deposit path.
- Liquid earnings remain the exact Initialization miner incentive and matching
  `AutoStakeAdded` for collector owner/hotkey/route. Stake deltas, dust refunds,
  nominal incentives, and the staking-association restoration migration are
  not substituted for liquid credits. The v478 migration restores indexes;
  it does not authorize any payment or wallet action.
- Receipt execution uses the parent runtime at upgrade blocks. Only known
  fingerprints and forward transitions are admitted; reverse/unknown states
  refuse. Current signing requires exact v478 and a newly signed policy.

## Journal and activation boundary

A v473-to-v478 migration changes only revision+1 and runtime fingerprint.
It requires the existing primary coldkey's new policy approval and a separate
signature over the exact quiesced snapshot/history manifest. The migration
retains all original operations, signed bytes, spent budgets, cursor, earnings,
canary/replacement/manual intents and terminal settlements. It appends a
public signed lineage record, permitting old policy digests only on intents
that predate that migration. Historical intents never authorize a new claim.

The emission policy must also be signed again because it binds the collector
policy digest. Preserve GM bounds0–10000/default7500, the same destinations,
100-alpha custody ceiling, fee limits and recurrence off. Deploy code and
Platform report support before starting the new worker. An old runtime policy
must remain blocked until these signatures and a guarded migration/rollout
are complete. Never rewrite the journal pin directly, reset the scan cursor,
unmask recurring timers, or submit a transfer to test readiness.

The worker now reports bounded runtime/observation blockers through its existing
keyless mailbox. Platform's existing public Backroom manual-state read surfaces
the fixed reason instead of an unexplained freshness wait. Such a failure report
cannot carry readiness, request identity, settlement or spending authority.
