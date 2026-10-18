# Reveal-anchored range planning for the source-emission collector

The collector proves a king earned completed winner emissions by replaying
finalized blocks. Catch-up plans each range of up to 720 blocks and fully reads
only the blocks that can matter. Two planners exist:

| | Event planner (`plan_source_emission_range`) | Reveal planner (`plan_source_emission_range_by_reveals`) |
|---|---|---|
| How it finds blocks | reads every block's `System.Events` | payout hops + reveal-queue bisection |
| Calls per 720 blocks | ≥ 720 heavy reads | ~80–140 small reads |
| Extra full reads | every structural event on *any* subnet | queue shrinks without a reveal (rare) |
| Status | drives the cursor | **shadow** (`_REVEAL_PLAN_MODE`) |

Both share `_range_endpoints`: equal runtime code, mode controls, Yuma
consensus and `Keys(118)` at the range ends, or the collector falls back to the
per-block scan.

## Why the reveal planner is enough

Within those endpoint checks, the full reader acts only on payouts and SN118
weight writes. In the audited runtime:

- **Direct `set_weights`** (and the mechanism and batch variants) is rejected
  while `CommitRevealWeightsEnabled` is true (`macros/dispatches.rs`).
- **Commit-reveal v3** reveals happen in `block_step` → `reveal_crv3_commits`
  on every block. Each reveal pops an entry from
  `TimelockedWeightCommits(118, cur_epoch − reveal_period)`. Pulses that
  aren't available yet are retried in later blocks of the same epoch.
- **Commit-reveal v2** `reveal_weights` also writes `Weights`. The planner
  refuses any range where SN118's v2 store (`WeightCommits`) is non-empty at
  any probed block.
- **Other `Weights` writers** (UID replace/swap/trim, mechanism count change,
  dissolution) change `Keys` or the controls, which the endpoint checks catch.

So:

1. **Payouts:** read `LastMechansimStepBlock` at the range end, then at each
   payout's parent, until it falls before the range. That's one read per
   payout.
2. **Reveals:** between payouts the epoch is fixed, so the reveal key
   `TimelockedWeightCommits(118, e − reveal_period)` only shrinks. Commits go
   to the current epoch's key, and a taken entry never comes back. A
   bisection on its `state_getStorageHash` therefore finds every block where
   it changed, exactly. Each segment starts at the block before its payout,
   so initialization reveals in the payout block are included.
3. Anything else (epoch index changing without a payout marker, a changed
   reveal period, controls or v2 commits at a probed block) returns the
   full-scan plan.

The one assumption no read can rule out: commit-reveal being turned off and
back on *between* probed blocks inside one range. That is an owner/sudo
action, and it is checked at both ends and at every payout.

Measured on Finney (public archive, 2026-10-18): six 720-block ranges across
v472, v473, v475, v477 and v479 planned every payout and SN118 write block
that the per-block events showed, with zero misses, in 81–140 calls per range.
Two ranges added three blocks in total where the queue shrank with no reveal
event (a dropped commit). That costs reads but never correctness.

## Shadow and promotion

In `shadow` mode, after each event-planned range is replayed, the collector
runs the reveal planner on the same range. It checks that every block the
full reader acted on (payout, vector update, reset or invalidation) is in that
plan. The running summary is on Backroom `get_source_release_policy` →
`release_gate.reveal_plan_shadow`:

- `agreed`: the plan covered every acted block.
- `missed`: it didn't; `recent_misses` lists the blocks.
- `fallback` / `error`: it asked for a full scan, or couldn't be planned.
- `calls` / `blocks`: its cost per block.

The shadow runs under its own 90 s budget and never blocks the sweep.

**Promote** (`_REVEAL_PLAN_MODE = "primary"`) only after at least 100 shadowed
ranges with `missed == 0` and errors explained. In primary mode the sparse
self-audit still re-reads two skipped blocks per range with the full reader,
and any disagreement stops the sweep.
