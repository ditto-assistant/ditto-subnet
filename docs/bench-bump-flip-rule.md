# Bench bump flip rule (option D, phase 1)

Status: built, **disabled by default**, Platform only. Design:
`docs/bench-bump-readmission.md` (PR #3443), option D and Phase 1.

## Why

A bench bump rescores a frozen cohort and the ledger flips to the new version only
when the priority cohort holds a 3/3 quorum on it **and** five ranked owner
families do (`_live_desired_authority_ready`,
`apps/platform/ditto/db/queries/benchmark_rollout.py`). If fewer agents get
there, the old board is paid indefinitely and a champion who stops
resubmitting profits from the delay. The flip rule bounds that wait. It changes
nothing about who is scored, so it saves nothing by itself; it is the piece that
lets later phases (an opt-in credit, a smaller or empty cohort) finish a bump
with a thin board.

## The setting

`flip_rule` is a block of the queue policy (`GET/POST /admin/queue-policy-settings`,
MCP `get_queue_policy_settings` / `set_queue_policy_settings`). It is stored
whole, versioned and audited like the rest of the policy (revision, reason,
actor, checksum).

| Field | Default | Bounds | Meaning |
|---|---|---|---|
| `enabled` | `false` | | Master switch. Off keeps the original switch byte for byte. |
| `min_agents_to_flip` | 5 | 3..5 | Ranked owner families that flip the ledger immediately. The priority cohort must still be at quorum. 5 is today's rule. |
| `flip_deadline_hours` | 36 | 1..168 | Hours after rollout start at which the deadline branch opens. |
| `min_agents_at_deadline` | 3 | 3..5, at most `min_agents_to_flip` | Ranked owner families that flip once the deadline has passed. Does not wait for the priority cohort. |

The floor of 3 is the scoring quorum. Below it the board is a lone-miner board,
which is only safe once the burn of unfilled rank shares exists (phase 3, not
built). The ceiling of 5 is `MIN_DESIRED_AUTHORITY_AGENTS`: the rule can only
relax the family count, never demand more than today. The bounds are enforced in
the wire model, in `create_rollout_snapshot`, and by three CHECK constraints on
`benchmark_rollouts` (`benchmark_rollout_bounded_flip_*`).

It is **next-rollout policy**, like `rescore_cohort_size`: read once when a rollout
starts and frozen onto the rollout row (`flip_rule_enabled`, `flip_min_agents`,
`flip_deadline_hours`, `flip_min_agents_at_deadline`). A revision never changes a
rollout already open; `effective.open_rollout_flip_rule` shows what the open
rollout froze and `open_rollout_overrides_setting` says when it differs.

## When a rollout flips

With the rule off (every existing rollout, and any rollout started while the
setting is off): unchanged. With it on, the first of:

1. **Immediate.** Priority cohort at 3/3 quorum **and** at least
   `min_agents_to_flip` ranked owner families. At 5 this is exactly today's rule;
   a lower value only relaxes the family count.
2. **Deadline.** `flip_deadline_hours` after `rollout.created_at`, at least
   `min_agents_at_deadline` ranked owner families, **without** waiting for the
   priority cohort. A cohort member that can never score is the stall this branch
   exists to end. The cost is that a frozen cohort member who has not finished by
   the deadline (including the previous champion) is not on the new board.

"Ranked owner family" is `count_ranked_quorum_agents`: an owner root with a
3-score median row that ran the full benchmark with a positive composite, inside
the current-or-previous dueling season window. Banned, quarantined and ATH-held
agents do not count.

The flip is recorded the way it always was, as a sticky `desired_authority_earned`
audit event, and the rollout then closes. When the rule is enabled the event also
carries `flip_path` (`priority_cohort_quorum` or `deadline`) and the frozen
parameters, so an after-the-fact reader can tell which branch fired. A rollout
without the rule writes the same payload it always did.

### Why the deadline is evaluated on the write path only

`active_bench_version` runs on every read and must give every reader the same
answer. A wall-clock condition inside it would let two readers disagree for the
second around the deadline. So the read path stays clock-free (immediate branch
only), and the deadline branch is evaluated in `maybe_record_desired_authority`,
which `refresh_rolling_qualification` reaches from two places: after every
accepted score (`validator.py`, best-effort), and from authenticated idle
validator polls, single-flight and throttled to once per 30 seconds per API
process (`_refresh_qualification_if_due`). Either seals the flip durably. After the
sealing write every reader sees one answer. The practical latency is up to about
30 seconds after the deadline while a validator is polling; with no validator
polling and no score arriving it does not run, but then nothing else in the
rollout is moving either.

### The two-season expiry window

A score counts only for the current and previous dueling season
(`dueling_season_window.py`), so a quorum row lasts roughly two seasons (5-10
hours, estimated, not measured on a real rollout). The rule handles that three
ways, and does not handle a fourth:

- **Eager evaluation.** The immediate branch is evaluated in the same write that
  creates the quorum, so a quorum cannot lapse unobserved between two
  evaluations. The deadline branch is evaluated on every refresh after the deadline
  (at most about 30 seconds apart while validators poll), and the first one that
  sees the floor met seals the flip.
- **Only live quorums count.** The floor is measured against rows inside the
  window, which is exactly what the ledger will serve after the flip. A quorum
  that has already expired cannot be on the board the flip would produce, so
  counting it would flip to a board shorter than the floor.
- **Visibility.** `rollout_state.flip_rule.expired_quorum_agents` is the number of
  owner families that reached a ranked quorum on the desired version and have
  since aged out of the window (null while the rule is off, because the count
  scans every score at the version). `ranked_quorum_agents` beside it is the live
  count. Together they answer "did the quorum I was waiting for expire".
- **Not handled: a rollout with no rotation.** The rotation retest renews the
  rows of the frozen rollout cohort. A later phase with an empty cohort would have
  nothing renewing an early resubmitter's rows, so with a 36 hour deadline the
  early rows will have expired by the time it arrives. Phase 2 must either keep
  the deadline inside the window, extend the rotation cohort to credited agents,
  or express the deadline in rounds (option E). This phase deliberately does not
  guess.

## Observability

- `get_queue_policy_settings`: the `flip_rule` block, the revision history, and
  `effective.open_rollout_flip_rule` for the open rollout.
- `get_benchmark_rollout_control` and `GET /public/bench/rollout`: a `flip_rule`
  object with the frozen values, `deadline_at`, `deadline_elapsed`,
  `ranked_quorum_agents`, `expired_quorum_agents`, `priority_complete`,
  `immediate_ready`, `deadline_ready`, `authority_earned`. The two reads behind
  `expired_quorum_agents` and `authority_earned` run only while the rule is on
  (both are null otherwise), so the endpoint issues the same statements as before
  when it is off. `promotion_requirement`
  gains a sentence naming the deadline when the rule is on, and is unchanged when
  it is off.
- The rollout audit trail: the `cohort_frozen` event (written at start) records
  the frozen rule and `desired_authority_earned` records `flip_path`.

## What a flip with fewer than five agents does to emissions

The validator fold and the ledger pin were read, not changed. At the revision this
was built from:

- `emission_set` / `emission_shares` (`apps/platform/ditto/api_server/koth.py`)
  take `KOTH_RANK_SHARES[:len(members)]`, and `weights.py` documents that "Pylon
  normalizes a short recipient prefix, so only these ratios matter when fewer than
  five eligible miners exist". So a 3-agent board pays the first three rank
  shares renormalised: the champion takes more than its share of a full board, and
  nothing is burned. That is the lone-miner windfall the design doc ties to the
  burn (phase 3).
- No symbol named `entries_shared` exists at this revision (searched the whole
  tree). The shared-seed term in the pin is the crown gate, `CrownGate`
  (`min_shared_seeds`, `standing_rank` 1..5, `duel_window`) in
  `ledger_pin.py`, a dethrone requirement between a challenger and the champion.
  Reading it, nothing requires five entries in the pool. `standing_rank` is a
  rank bound of 1..5, and I did not exercise how the gate behaves when the pool
  is shorter than that bound.
- So the read finds no validator or pin change needed for the flip rule, and the
  ledger digest and fold are unaffected while the rule is off (they read the
  served ledger, which is unchanged). Not exercised: a protocol-level replay of a
  3-agent served ledger through both folds and the pin.

## Rollout of the rule

1. Merge and let the release deploy (the migration adds four columns with
   constant defaults to `benchmark_rollouts`; nothing reads them while the rule is
   off).
2. Write one queue-policy revision with `flip_rule.enabled=true` (payload below).
   Nothing changes for the open rollout.
3. The next rollout start freezes the rule. Watch `flip_rule` in
   `get_benchmark_rollout_control`.
4. Rollback: write a revision with `enabled=false`; it applies to the next
   rollout. For a rollout already open with the rule frozen, supersede it. There
   is deliberately no in-flight override.

Operator payload (not applied). `set_queue_policy_settings` stores the whole
policy, so send the current revision with only `flip_rule` changed:

```json
{
  "flip_rule": {
    "enabled": true,
    "min_agents_to_flip": 5,
    "flip_deadline_hours": 36,
    "min_agents_at_deadline": 3
  }
}
```

with `expectedRevision` set to the revision `get_queue_policy_settings` reports
(16 as last reported to this task; read it again), the confirmation
`APPLY QUEUE POLICY SETTINGS`, and an auditable reason.
