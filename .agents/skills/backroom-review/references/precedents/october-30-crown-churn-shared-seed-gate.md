---
id: october-30-crown-churn-shared-seed-gate
resolution: policy
tags: [koth, crown, dethrone, shared-seed, confirmations, protocol-34, dan0l, horizon, drpresident, crown-v194, miner-communication]
holding: A challenger with fewer shared-seed confirmations than the minimum (default 3) cannot take the crown from a King that holds more; the crown moves only on the paired shared-seed comparison, and the King must stay inside the top official places (default 3). An unretested canonical lead is not a dethrone.
---

## What happened

On 2026-10-29/30 the KOTH crown changed hands five times in about a day.
dan0l v30 (0.954 canonical, 12 to 15 confirmations) lost to horizon v5
(0.942, 0 confirmations), then to crown-v194 (raw rank 8, 0.911, 7 shared
seeds), then to DrPresident v20 (0.947, 0 confirmations), then back to dan0l
v30. Each dethrone passed the existing band because the fold compared the
incumbent's retest-folded official composite with a newcomer's unretested
three-seed canonical on the unpaired path, and walked challengers in
`first_seen` order with no standing requirement.

Operator decision (Peyton, 2026-10-30): "it would super be unfair for less
confs to take over an agent with more confs". The UI must make the King and
the challengers dueling it clear.

## Ruling

- Crown moves require the paired comparison on at least
  `min_dethrone_confirmations` shared seeds (protocol 34, `crown_gate`).
- A King outside the top `champion_standing_rank` official places hands the
  crown to the best-qualified standing challenger.
- Board `rank` orders by `official_composite` (the continual mean), not the
  canonical `composite`; the canonical median is the recipients' `raw_rank`.
  A challenger "ahead" on canonical with no shared seeds is not ahead for the
  crown.

## Using this precedent

For a "why didn't I take the crown" question, read `get_leaderboard`
`emissions.challengers[]` for the exact agent and quote its status,
`shared_seed_confirmations` of `required_confirmations`, and `lead_needed`.
Do not promise a crown or a date; the lane adds seeds per round.
