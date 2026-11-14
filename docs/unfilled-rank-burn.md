# Burn the rank shares no agent fills (protocol 36)

Phase 3 of option D in `docs/bench-bump-readmission.md` (#3443). The code ships
**inert**: nothing changes until an operator sets `burn_unfilled_rank_shares`
after every recently-live weight setter reports protocol 36.

## Why

Today a short board renormalises. With four agents the validator folds
`rank_shares[:4]` and `apply_miner_emission_cap` divides by their sum, so the
four split the whole memory pool 0.677/0.146/0.104/0.073. With one agent that
agent takes the whole pool, while with zero agents everything burns. Two
consequences: after a bench reset a lone early resubmitter holds the full pool
(first-mover hoarding), and a thin board pays the same total as a full one.

The rule: every rank slot keeps its **fixed fraction of the miner pool**. A slot
no eligible agent fills burns its fraction.

| Agents paid | Burned share of the memory pool | Of total emission at memory 56% |
|---:|---:|---:|
| 5 | 0% | 0% |
| 4 | 4% | 2.2% |
| 3 | 11% | 6.2% |
| 2 | 21% | 11.8% |
| 1 | 35% | 19.6% |
| 0 | 100% (unchanged) | 56% |

(Default curve 65/14/10/7/4. The 56% memory share is from the task brief, not
re-read from production.)

## Semantics, exactly

- **Which curve.** The memory track's `rank_shares`: the frozen default or the
  operator curve served since protocol 31. The generator and router tracks keep
  their own curves and are untouched.
- **Occupied slots.** Champion plus tail by the existing fold. Tie pooling still
  averages **only the occupied slots**, so four exactly tied agents each hold
  `(0.65+0.14+0.10+0.07)/4 = 0.24` and rank 5's `0.04` burns; it is never
  spread over a tied group.
- **Joint crown.** The score-ceiling joint crown pays the whole pool to its
  cohort. It has no unfilled slot, so the burn changes nothing there.
- **Zero scored agents.** Unchanged: the fold returns `{}` and the cap routes
  100% to the burn hotkey. One agent now takes `0.65`, not `1.0`, so the
  zero→one discontinuity is gone.
- **`burn_share`.** Burns first, off the whole pool. The unfilled fraction then
  burns out of what is left, multiplicatively. With `burn_share = 0.1` and four
  agents, miners hold `0.9 × (0.65, 0.14, 0.10, 0.07)` and the burn hotkey holds
  `0.1 + 0.9 × 0.04`.
- **Track split.** The memory vector is normalised to its track share, so rank
  5's `0.04` is `0.04 × memory_bps/10000` of total emission. Memory 70% and
  generator 30%: 2.8% burns.
- **Provisional incumbent (protocol 28).** Both burn through one key.
  `compute_weights` adds the unfilled tail to `UNPAID_SHARE_KEY` (summing, never
  overwriting). `split_unpaid_share` then measures the paid fraction over the
  curve total, so nothing counts twice. A lone unpaid incumbent leaves 100%
  burning.
- **Miner emission cap.** Untouched. `worker.py` already scales `miner_share` by
  `allocated × paid_fraction` and passes it to `apply_miner_emission_cap` (or
  the treasury fold); the unfilled share rides that path.

## What changed

| Layer | Change |
|---|---|
| Validator fold | `compute_weights(burn_unfilled=…)`, `MemoryFoldParams.burn_unfilled`, `worker._ledger_unfilled_burn` |
| Platform fold | `emission_allocation(burn_unfilled=…)` returns `unfilled_share`; the pin keeps the curve total in the denominator, so `pin_expected_burn` prescribes the burn |
| Wire | `LedgerResponse.unfilled_share_mode: "burn" \| None`, absent while off, in both copies of the wire model and the contract goldens |
| Pin | frozen under `served.unfilled_share_mode` only when on, so every other pin keeps its digest |
| Setting | `burn_unfilled_rank_shares` in the versioned, audited burn settings (same table and checksum as `burn_share`, excluded from the stored JSON while false) |
| Gate | served only when the setting is on **and** `live_weight_setter_fleet_supports_protocol(36)`; fails closed on an empty fleet |
| Replay | pin replay to a sub-36 requester: 428; stale last-known-good snapshot carrying the marker: 503 |
| Visibility | `get_burn_settings` / `set_burn_settings` MCP tools and the Burn panel show the field and `effective.unfilled_burn_served`; the public board reports `unfilled_burn_active` and `unfilled_share_burned` |

Writes that do not mention the field keep its current value (the console dial
posts `burn_share` alone), and so do emission-allocation writes. An unreadable
latest revision reads, and therefore carries, as off.

## Parity

A fold mistake fails every weight receipt with `ledger_pin_mismatch`.

- `ditto/tests/fixtures/unfilled_burn_cases.json` is run by the validator
  (`ditto/tests/validator/test_unfilled_burn.py`) and by Platform
  (`apps/platform/ditto/tests/api_server/test_unfilled_burn.py`). The
  expectations are exact fractions of the curve, not output of either fold.
- Off is byte-identical to protocol 35: the fold returns the same dict with no
  extra key, the ledger JSON, pin digest and settings checksum are unchanged.
- Negative controls: a fold that ignores the marker fails the fixture; a v35
  vector against a v36 pin differs in burn by more than the 0.002 tolerance.
- The burn is not visible to `classify_vector_against_pins`, which removes the
  burn hotkey and renormalises. It is caught where it already was for protocol
  28: `pin_expected_burn` in the emission collector, and the exact projection in
  `treasury_vector_matches_pin`. That is why the marker must not reach part of
  the fleet.

## Rollout

Same shape as protocol 35 (`docs/title-duels.md` §6).

1. **Release.** Merge, let the release graph publish the validator image and
   Platform. Platform is inert: the setting defaults off.
2. **Deploy Platform.** Confirm `get_burn_settings` shows
   `unfilled_burn_served: false` and `settings` without the field.
3. **Validators on 36.** Every recently-live weight setter must report protocol
   36 (`get_validator_fleet`). Until then the gate withholds the marker even if
   the setting is on.
4. **Operator payload** (Peyton applies; the build never does). Written from the
   current revision, changing only the new field. `burn_share` is resent as
   served because an emission allocation owns it once one exists:

```json
{
  "scope": "*",
  "expectedRevision": "<current burn revision>",
  "settings": {"burn_share": "<current burn_share, 0 today>", "burn_unfilled_rank_shares": true},
  "reason": "burn rank slots no agent fills; fleet reports protocol 36",
  "confirmation": "APPLY BURN SETTINGS"
}
```

   Pass it to `set_burn_settings`. Omit `rank_shares` to keep the current curve.
5. **Verify.** `effective.unfilled_burn_served` is `true`; a ledger read carries
   `unfilled_share_mode: "burn"`; the next pin freezes it; weight diagnostics
   show `matches_pin` across one epoch; the public board shows the burn on any
   board with fewer than five paid agents.

**Rollback.** The same payload with `"burn_unfilled_rank_shares": false`. A
pinned epoch finishes under the marker it froze, so allow one epoch.

## Sybil and slot-filling risk (flag for Peyton; not decided here)

Today an empty slot is not worth filling by itself: the miners present absorb it.
Once empty slots burn, a slot has a price. A cheap agent that scores above zero
and outranks nobody still claims `rank_shares[i]` of the pool, and the share
comes out of the burn, which benefits no one except the filler.

- **What a filler earns.** Rank 5 is 4% of the memory pool; at memory 56% that is
  about 2.2% of total emission per slot, 4.1% for ranks 4 and 5 together. The
  entry cost is one upload fee plus a score above the full-benchmark case floor
  (`MIN_ELIGIBLE_CASES`), and any positive composite pays.
- **One hotkey per slot.** The ledger already keeps one best generation per
  payment-time coldkey, and `_tail` takes distinct hotkeys under tie pooling, so
  fillers need separate coldkeys, each paying the fee. That is a real cost but
  not a barrier at 2% of emission.
- **Who is hurt.** With the renormalising fold a filler takes from the other
  miners; with the burn it takes from the burn. Honest miners are unaffected in
  share, but the burn saved by the thin board shrinks to nothing, so the policy
  intent (a thin board earns less) is defeated by the cheapest agents.
- **Does it need a minimum paid composite?** Probably, if the burn is meant to
  bind: a floor relative to the champion (for example a slot is paid only at
  `composite ≥ r × champion`, otherwise it burns) would price the fillers out,
  at the cost of one more consensus parameter in both folds and a new fixture
  axis. It is not required for the burn to function and is **not built here**.
  Decision for Peyton: whether to require it, `r`, and whether it applies to
  rank 2 as well or only the tail. Measure first: how many tail agents today sit
  far below the champion, and what they earn.

## Not verified

Observed in code at `737f5d497` (v0.467.0) and by tests on a Boat builder; not
run: a live fleet, a weight receipt under the marker, the public board with a
real short board, or any production setting. The 56/24/20 split and
`burn_share = 0` are taken from the task brief.
