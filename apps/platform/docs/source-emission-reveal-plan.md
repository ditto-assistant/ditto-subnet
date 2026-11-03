# Reveal-anchored range planning for the source-emission collector

The collector proves a king earned completed winner emissions by replaying
finalized blocks. Catch-up plans each range of up to 720 blocks and fully reads
only the blocks that can matter. Two planners exist:

| | Event planner (`plan_source_emission_range`) | Reveal planner (`plan_source_emission_range_by_reveals`) |
|---|---|---|
| How it finds blocks | reads every block's `System.Events` | payout hops + reveal-queue bisection |
| Calls per 720 blocks | ≥ 720 heavy reads | ~80–140 small reads |
| Extra full reads | every structural event on *any* subnet | queue shrinks without a reveal (rare) |
| Status | fallback only (`shadow` mode) | **drives the cursor** (`_REVEAL_PLAN_MODE = "primary"`, since 2026-10-18) |

Both share `_range_endpoints`: equal runtime code, mode controls and Yuma
consensus at the range ends, or the collector falls back to the per-block scan.
A change of a UID's hotkey inside the range no longer forces that fallback.
Each one is found and read in full (the reader resets provenance there):

- **Hotkey swaps.** Every swap on SN118, on its own or on all subnets, sets
  `LastHotkeySwapOnNetuid(118, coldkey)` to its block. Only a dissolution or
  the prune of records older than `HotkeySwapOnSubnetInterval` (7200 blocks)
  removes one (subtensor v448–v481). The cooldown is per coldkey, and a
  coldkey swap carries none over. So a UID's hotkey *can* go A → B → A inside
  one range, under two coldkeys, and read the same at both ends. While the
  interval is longer than the range, a coldkey cannot swap twice in it and
  none of its records is pruned. The records at the range end therefore name
  every swap inside it. A shorter interval, or none, falls back.
- **Registrations.** Every registration sets `BlockAtRegistration` to the
  current block, so per UID it only increases and never repeats. That catches
  A → B → A through a registration (SN118's 300-block immunity period is
  shorter than a range). A UID counts as changed when its `Keys` or
  `BlockAtRegistration` differs at the ends. Its `BlockAtRegistration(118,
  uid)` storage hash is bisected to the exact registrations, then `Keys(118,
  uid)` is bisected in each stretch between them. A registration can restore
  the hotkey a swap moved away, and a swap can restore the one a
  registration replaced.

Up to 16 changed UIDs, and 16 swaps, are located per range; more fall back.
Before this, any registration in a 720-block range sent the whole range to the
per-block scan, which was most of the backlog on 2026-10-18.

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

Controls are read at the ends and at every payout, not at every block. A
subnet owner cannot switch commit-reveal or the mechanism count off and back
on between those points: each owner change is rate-limited to one per
`Tempo × OwnerHyperparamRateLimit` blocks (360 × 2 = 720 on Finney), longer
than a range spans. The planner requires that at both ends, or falls back.
The one case no read rules out is **root** toggling it twice inside a range.

Measured on Finney (public archive, 2026-10-18): seven 720-block ranges
across v472, v473, v475, v477 and v479 planned every payout and SN118 write
block that the per-block events showed, with zero misses, in 81–140 calls per
range.
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

The shadow runs only after a sweep has finished, payout resolution included,
in a fresh window of its own (90 s plus a 15 s reserve) on the same archive
connection. It never fails the sweep and delays the next one by at most that
window. (On v0.372.0 it ran inside whatever the replay left of the sweep
budget, which on Dwellir was never enough, so no comparison was recorded.)

**Promoted on 2026-10-18** at the owner's call, after 27 shadowed ranges
(19,440 blocks on v475), all agreed, no misses,
fallbacks or errors. The original bar was 100; the catch-up was going to end
before it could be reached. In primary mode the shadow no longer runs, and
Backroom keeps showing the frozen summary with `mode: "primary"`. The sparse
self-audit still re-reads two skipped blocks per range with the full reader,
and any disagreement stops the sweep. To demote, set the mode back to
`shadow`; the event planner and the comparison are unchanged.

## Where a range's time goes

On Dwellir, a 720-block range took about 8 minutes. Planning was 1 to
1.5 minutes of that. Most of the rest was the full reads of the ~4 planned
blocks (payouts and reveals) and their payout receipts, at 1 to 1.7 minutes
each, run one after another. The replay now starts every planned read, and
the two audit reads, at once, `_SPARSE_READ_CONCURRENCY` (4) at a time. It
still commits each block in cursor order. The audits must pass before
anything is committed, and a failure cancels every read still running.

After that change (v0.374.0) a range's replay took under two minutes, but each
sweep still took about ten. Payout resolution after the replay had been
running until the 600 s budget's reserve, re-checking every pending row
(~190 rows, ~6 s each). During catch-up it now gets at most
`_CATCHUP_RESOLUTION_SECONDS` (90 s) per sweep. Rows are picked
oldest-checked first, so a newly recorded payout is still checked in the
sweep that recorded it, and older rows each come round every dozen or so
sweeps. Steady state is unchanged.
