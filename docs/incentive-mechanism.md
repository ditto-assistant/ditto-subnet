# Incentive mechanism (SN118)

How SN118 distributes emissions across miners based on their benchmark scores.

## What makes this subnet different

SN118 is a best-artifact competition, not a live-inference subnet. Miners submit
a memory-harness implementation, validators bench it, and the artifact is
downloadable. That shapes the incentive design in two ways:

1. Copying is the central threat. Anyone can download the current best harness and
   resubmit it verbatim or lightly tweaked, so the mechanism must not pay copiers.
2. Improvement is discrete, not continuous. Scores jump when someone ships a
   genuinely better harness, then plateau, so the mechanism rewards beating the
   state of the art rather than occupying a rank.

## The mechanism: king-of-the-hill with an all-time-high gate

Emissions concentrate on one champion, the highest-scoring non-duplicate
submission, with a small participation tail.

- Champion (king-of-the-hill). The current champion holds about 90% of emissions
  (`VALIDATOR_KOTH_CHAMPION_SHARE`) until another submission dethrones it.
- Dethroning gate (all-time-high). A challenger takes the crown only by beating
  the champion by more than an indifference band,
  `max(flat relative margin, z·√(se_c² + se_champ²))`. The flat margin is 5%
  (`VALIDATOR_KOTH_MARGIN`); the statistical term (`VALIDATOR_KOTH_DETHRONE_Z`,
  default 1.64) widens the band by the measurement noise of both scores when the
  ledger carries a per-score standard error, so a challenger inside the noise
  cannot flip the crown on a lucky seed. A verbatim copy ties the champion and
  never clears the band, so it earns nothing; first-seen timestamps protect the
  original author on ties.
- Participation tail. The rest is spread over the next few distinct,
  non-duplicate submissions (`VALIDATOR_KOTH_TAIL_SIZE`, default 4), so the field
  does not hollow out to a single earner.

Two supports are non-negotiable:

- First-seen timestamps plus plagiarism and near-duplicate detection, so a copy
  cannot displace the original.
- A deterministic scoring fold (`ditto/validator/weights.py`), so every validator
  computes the identical weight vector from the same public ledger and Yuma
  consensus converges.

## Alternatives considered

Each was judged on anti-copy, drive to improve, participation, and complexity.
King-of-the-hill wins because anti-copy is the existential risk for a
downloadable-artifact subnet, and it is the only shape where a copy structurally
cannot earn.

| Mechanism | Anti-copy | Drive to improve | Participation | Complexity | Why not |
| --- | --- | --- | --- | --- | --- |
| Top-K equal split | weak | weak | high | low | A copy that lands in the top K collects a full share. |
| Top-3 weighted (70:20:10) | weak | medium | medium | low | A copy at #2 or #3 earns for no new work. |
| Pareto frontier (multi-objective) | strong | strong | medium | high | Matches a multi-objective product, but too complex and too jittery to keep deterministic for consensus. A candidate direction once the benchmark scores several objectives worth trading off. |
| Score-proportional / softmax | weak | medium | high | medium | A copy earns the same proportional share as the original, and near-duplicate spam farms the tail. |

Terms: king-of-the-hill (KOTH) is the reigning-champion model; all-time-high
(ATH) is the dethroning gate a challenger must clear; sybil spam is many
near-duplicate submissions from one operator.
