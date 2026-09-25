# Payout-block initialization reveal attribution

Subtensor's audited `block_step` reveals matured commitments before
`run_coinbase`. Rejecting every payout block containing a weight write therefore
rejects ordinary successful CR reveals. The previous conservative gate remains
the default for unproven cases.

## Accepted sequence

For every target-subnet write, require exactly one initialization `WeightsSet`,
followed by exactly one initialization `TimelockedWeightsRevealed`, followed by
the single initialization `IncentiveAlphaEmittedToMiners`. Each reveal must have
an unambiguous singleton parent-state commitment. The runtime fingerprint and
UID mapping checks still apply. Duplicate, late, extrinsic, unknown and ambiguous
writes cannot use this path, even when the vector stays numerically identical.

The receipt reader independently checks this sequence, permits matrix and
LastUpdate changes only for the proven revealed validators, and consumes their
post-block vectors. Proven validators may have LastUpdate equal to the payout
block. Ownership changes, unexplained changes, unsupported runtime or missing
proof still fail closed.

The collector persists these proven reveals before snapshotting the payout.
The same-block binding is admissible only with its explicit initialization
marker and matching reveal/payout block hash. Database bindings are first read
after the upserts to avoid stale ORM identity-map values when consecutive
submissions use equal vectors. Signed commitment inclusion, immutable ledger
UUID/SHA, actual miner earnings and greater-than-two-thirds effective stake
remain necessary; the reveal sequence alone never authorizes disclosure.

## Evidence

Audited source: [v466 block_step at cdffbe2](https://github.com/RaoFoundation/subtensor/blob/cdffbe2f7ab0c37ab07884387bfbd6443dca178d/pallets/subtensor/src/coinbase/block_step.rs#L17).
The v464 and v466 critical reveal, weights and epoch files are byte-identical;
see the separate v466 audit record.

Public finalized SN118 payout block `9091069`, hash
`0xb008a547066dd34b56f97d80675c2bf2e02e0c4e0fa95fefeab78faccba629a8`:
UID34 write/reveal at event indices1/2, UID28 at3/4, and miner emission at128.
Both have unique parent commitments. The patched reader accepts this order and
reads its completed miner receipt. This public replay does not establish a live
Platform UUID/SHA resolution or authorize enabling disclosure.

Tests cover late and extrinsic writes, duplicate payouts, ambiguous commitments,
unexplained matrix/update changes, unknown runtime, actual consumed post-reveal
vectors, and two consecutive exact submissions on one hotkey. The second
submission stays private until its own reveal and completed payout. Existing
legacy no-backfill, late-claim recovery and strict stake-quorum controls remain.
Previously terminal ambiguous payouts are not automatically revisited.
