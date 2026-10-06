# Finney v473 collector compatibility review, 2026-10-05

The official [v473 release](https://github.com/RaoFoundation/subtensor/releases/tag/v473)
from source `f87cada631f81d11683e715a9f059f693992e64a` publishes compressed WASM
SHA-256 `cbd9f5b4c72edd86af5c956ae21c66448818e227f4085ecb021acd89e533adfc`.
Its exact Blake2-256 `0x7773f5c0a6d6e9ea9ff347edcc491246eec08a5cf441d964ee96f40d7fa65a08`
matches finalized public Finney `:code`. The release's srtool digest binds this
artifact to that source. This review did **not** independently rebuild v473;
this is an official artifact/source binding with an independent scoped source
review and public contract check, not a reproducible-build claim.

The last v472 state is block 9217263; the first v473 state is 9217264.
Metadata SHA-256 is `054f253ec4e57a441ef79cddb7a9c0d9cd98efffbb8384add69b31315013632a`.
[manifest.json](manifest.json) records the pinned finality, native scopes and
source fingerprints. Ten critical collector source files are byte-identical
to the reviewed v472 contract: native allowlists and proxy dispatch,
registration/burn cap, same-hotkey stake transfer, lock/account protection,
coinbase liquid credit and StakeInfo event/API contracts.

## New storage migration and bounded authority

v473 schedules AlphaV2 format conversion and dust settlement in metered idle
batches. Retired Alpha/TotalHotkeyShares aliases remain readable through the
transitional share-pool getters, with legacy taking precedence until conversion.
The collector does not query retired Alpha shares. It uses StakeInfo's amount
and the independent aggregate available-to-unstake/collateral checks. The
migration preserves protected positions; unknown quotes are retained; settlement
and deletion are transactional. It may change stake or liquid TAO through dust
refund/burn. Such changes are **not earnings**. Only matching SN118 initialization
`IncentiveAlphaEmittedToMiners` and self-directed `AutoStakeAdded` for the exact
collector UID/owner/hotkey authorize the credited amount; balances do not.

Native Registration/Transfer filters and their call indices were reread from
the finalized runtime. Application preparation remains restricted to bounded
`register_limit` and same-SN118/same-hotkey `transfer_stake`, with unchanged
fee, sponsor, identity, collateral, finality and paired effect checks.

## Historical journal continuity and fresh cold approval

New preparation/broadcast requires the exact v473 hash and a matching new cold
policy signature. v472 policies do not authorize this runtime. Read-only receipt
interpretation accepts exact audited v472 and v473 states. The only permitted
mixed parent/post pair is the reviewed forward v472-to-v473 upgrade. That block
executes under its parent's v472 runtime, whose collector event schemas are
unchanged; the proof reports the execution hash. Reverse, unknown, or later
runtime changes refuse. Identity and payout route are still proved on both
sides, and migration refunds cannot stand in for liquid incentive events.

The separately signed journal migration permits only revision+1 and this exact
runtime hash change in the same isolated project with the exact role service
accounts. No delegates, destinations, caps, spent budget, cursor or history may
change. Old snapshots remain immutable; one signed migration event is appended.
This code review grants no emissions activation, secret/key access or signature.
The original v472 offline transaction was stopped before submission when the
chain changed; refreshing its payload requires a new cold signature.
