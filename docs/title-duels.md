# Title duels: replace continual retests with season duels

Status: design approved by Peyton on 2026-11-03. Phase A ships Platform
scheduling behind `retest_mode`. Phase B ships the sequential paired dethrone
in both folds behind `crown_duel_mode` (protocol 35). Neither phase changes
emissions or rank shares.

## Why

Two problems drive this change.

**Cost.** Continual retests walk every cohort member up to the shared-seed
cap. `TOP5_MAX_CONFIRMATION_SEEDS` is 10 (#3367). Each crown change re-anchors
the family and re-walks everyone. The cohort also grows past five
(`resolved_cohort_size` is 8 on the live board). Today that is about 180–200
retest runs a day at about $1.48 each, roughly 55% of scoring spend.

Bench v16 makes this worse. Every 4.8 h season restarts the walk on a fresh
family: seed planning reads only the current season (#3362). Every season also
queues three rotation re-scores per rotation-cohort member on top of the walk.
Unchanged, v16 would cost about 80 runs a season, or about 400 a day.

**Fairness.** The crown is mostly a coin flip. The paired dethrone rule takes a
lead of `max(0.007, min(1.64·se, 0.014)) × decay`. The statistical term is
capped at twice the margin, and the band decay roughly halves it near the top
of the board (×0.53 at a 0.915 champion). Per-seed paired differences between
top agents have a standard deviation of about 0.03. So a challenger that is
exactly as good as the champion clears the bar within one 10-seed walk about
half the time (below). The board shows it: crowns flip back and forth within
hours.

## Data

Source: production `confirmation_scores` (bench 12–15, 8,670 rows) and
`ledger_epoch_snapshots` (985 pins), read on 2026-11-04 through
`gcloud-ditto-readonly`. For each seed, the composite is the median across
validators, as both folds compute it. To reproduce the tables, run
`python3 docs/title-duels-analysis.py <dir>` on the two exports it describes.

| Bench | Pairs (≥10 shared seeds, top 30) | sd of per-seed paired difference | Cross-agent seed correlation |
|---|---:|---:|---:|
| 13 | 435 | 0.045 | 0.17 |
| 14 | 434 | 0.031 | 0.17 |
| 15 | 78 | 0.024 | −0.01 |

The cross-agent correlation is low, so most per-seed noise belongs to the run
(model nondeterminism), not to seed difficulty. Pairing helps less than the
design assumed. Only more paired samples reduce the noise.

**Crown history.** v13 had 46 crown changes in 14 days, v14 had 13 in 6 days,
and v15 had 3 in its first day. That is 2–3 a day. Six of v14's 13 changes are
round trips (A→B→A within hours), so at least three of them were wrong
whichever agent was really better. On all shared seeds accumulated to date (at
most 15), 34 of 92 changes from v12 to v15 have the new king behind the old
one, and 43 have it ahead by less than the margin. Some changes were forced (a
ban, a supersession, the standing rank), so this is an upper bound on noise.
Even so, only 11 of the 62 changes from v13 to v15 clear the sequential test
proposed below.

### Simulated false-dethrone rate

Challengers are exactly as good as the champion (δ = 0). Noise is resampled
from the empirical paired residuals above. Results are the same within ±0.02
for v13, v14 and v15 noise.

| Rule | P(an equal challenger takes the crown) | False crown changes a day |
|---|---|---:|
| Today: 10-seed walk, looks at n = 3…10, 6 challengers, ~2.6 walks a day | 0.50 per walk | 2.3 |
| v16 as merged: walk restarts every season, 5 seasons a day | ~0.5 per season | ~4 |
| Phase A alone (today's fold, 3 same-season pairs per season) | 0.33 per look | ~4 |
| **Phase B: sequential test, α′ = 0.01, 15-pair window, 4 challengers** | 0.03 per 15-pair horizon | **0.13** |

Simulated today's rate (2.3 a day) matches the observed crown-change rate
(2.2 a day on v14). Most crown changes today are noise.

### Power of the sequential test

Under the Phase B rule (α′ = 0.01, 3 pairs per season, v14 noise), this is the
chance that a better challenger takes the crown:

| True lead δ | Within 1 season | Within 1 day | Within 2 days | Today, within one walk |
|---:|---:|---:|---:|---:|
| 0.000 (equal) | 0.01 | 0.03 | 0.06 | 0.52 |
| 0.007 (margin) | 0.01 | 0.12 | 0.26 | 0.75 |
| 0.015 | 0.03 | 0.39 | 0.71 | 0.92 |
| 0.030 | 0.06 | 0.93 | 1.00 | 1.00 |
| 0.050 | 0.17 | 1.00 | 1.00 | 1.00 |

A clearly better agent (0.03 or more) still wins within a day. Close calls
(under about 0.015) keep the king, which is the intent. With α′ = 0.02 instead,
false changes rise to 0.22 a day and a 0.015 lead wins within a day 54% of the
time. We recommend 0.01.

## Design

### 1. Season shared seeds

At a dueling bench version (v16 and later), each season draws **K shared
seeds** (`season_duel_seeds`, default 3). They are the first K indices of the
champion-anchored CRN family (`crn_seed([anchor], version, k, block_hash)`) for
the season's anchor. The anchor is the first `confirmation_seed_anchors` row
pinned for `(bench_version, season)`, which is the champion that opened the
season. A crown change mid-season does not draw a new family. Validators
already re-derive and verify these seeds from the pin
(`_confirmation_pin_binding_mismatch`), so nothing changes on their side.

The seeds are bound to a finalized block after the season opens. They are
unpredictable before that, and public once the anchor is pinned. That is the
same exposure as today's continual family, but it rotates every season instead
of lasting a whole reign.

### 2. Fixed cohort, no catch-up walks

The duel cohort is the top five: the folded emission set (champion plus four)
and the public-board five. Those two views are usually the same agents. The
public-board five stay in because their shared coverage gates the fold's
participants intersection; leaving one out would strand a seed. Three settings
are ignored in `season_duel`:

- the tie band (`retest_eligibility_mode`);
- `retest_cohort_size` above five;
- owner successors. A stronger successor already reaches the public-board
  five through canonical owner dedupe.

Each cohort member runs each of the season's K seeds exactly once, all K opened
at once and off the reign-backoff cadence. There is no elastic walk, no growth
pacing and no re-walk on a crown change. An entrant mid-season owes the same K
seeds and nothing more.

`wave_cadence_tempos` and `wave_seeds_per_window` (#3319) do not apply: the
season is the window.

### 3. Sequential paired dethrone (Phase B)

Evidence accumulates across seasons as paired differences against the
champion:

- **Pairs.** A pair is `(season, seed)` where both agents have a record whose
  signed `details.dueling.season_id` is that season. Each record's composite is
  recomputed with `{owner(A), owner(B)}` masked, as #3351 does. Differences
  from different seasons are independent within-season comparisons. Season
  isolation protects score *levels*, which are not comparable across generator
  sets. It does not stop us accumulating *paired differences*, which are.
- **Window.** The newest `CROWN_DUEL_MAX_PAIRS = 15` pairs, ordered by
  `(season_index desc, seed asc)`. That is five seasons, about a day, and equal
  to the validator's compiled `TOP5_MAX_CONFIRMATION_SEEDS = 15` ceiling.
- **Boundary.** The challenger dethrones only if
  `n ≥ min_dethrone_confirmations` (3) and
  `mean_d > max(KOTH_MARGIN × decay(champion_ref), c(n) × se_d)`.
  `c(n)` is the frozen table below. It is the Student-t quantile at a constant
  per-look level α′ = 0.01 (Pocock-style). That level accounts for the fold
  looking after every new pair, and for the variance estimated from a handful
  of seeds. The statistical term is **not** capped or decayed. The 2×-margin
  cap is what made today's test a coin flip. The existing ceiling clamp
  (`dethrone_band_mode`) still applies on top, unchanged.
- **Close calls keep the king.** An equal challenger dethrones with
  probability about 0.03 over a full window, against 0.5 today.
- **No reset on a crown change.** Every cohort member ran the same season
  seeds, so a new king's pairs with every challenger already exist.

| n | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| c(n) | 6.965 | 4.541 | 3.747 | 3.365 | 3.143 | 2.998 | 2.896 | 2.821 | 2.764 | 2.718 | 2.681 | 2.650 | 2.624 |

Ranking, tail order, tie pooling and score levels are unchanged. They remain
same-season only. Only the champion/challenger dethrone comparison reads the
cross-season window.

**Per-case pairing.** We checked it and rejected it for now. Signed
`details.dueling` carries per-column `n`, `sum` and `sum_sq`, plus
`datagen_raw` and `datagen_gate`. It has no per-case vector. Without both
agents' per-case outcomes for the same cases, per-case paired differences
cannot be formed, so neither fold can compute them from the data it already
has. It would also buy less than hoped, because cross-agent correlation is low:
the noise is the run, not the case. If wanted later, it needs a signed per-case
vector (about 350 bits per run) in the scorer report, the ledger and both
folds.

### 4. Rotation re-scores are the duel runs (Phase B)

v16 needs a same-season quorum for each cohort member, or the member's row goes
provisional and then expires. Today that is the rotation re-score (#3362),
which re-runs each member's three canonical seeds. Phase B runs those three
rotation re-scores on the season's K = 3 shared seeds instead (quorum validator
`i` takes seed `i`), and records each accepted run as both the quorum `Score`
and the seed's confirmation record. One run is then both level evidence and
duel evidence. The continual lane is idle in `season_duel` once this is active.

Validators verify a season-seeded quorum lease the same way they verify a
confirmation pin (`seed_scope: "season"` plus the anchor binding fields).
Platform only issues these leases to validators that advertise protocol 35.

New uploads follow the same rule. An upload whose seed-binding block (its
job-ready block) precedes the season anchor block gets the season's shared
seeds for its three canonical runs. They double as its first three duel
pairs, with no catch-up runs. A later upload keeps the per-agent
`derive_validator_seed` seeds and joins the duel at the next season's
rotation. Either way, no scored seed is ever predictable at submission. This
is the one refinement to the approved target: putting every new upload on
already-public season seeds would let it hard-code that season's datagen
cases.

### 5. Run budget

| Mode | Runs per season | Runs a day | $ a day at $1.48 |
|---|---:|---:|---:|
| Today (v15 continual) | n/a | 180–200 | 266–296 |
| v16 as merged: rotation 5×3 + walk 8×8 | ~80 | ~400 | ~590 |
| **Phase A**: rotation 5×3 + duel 5×3 | 30 (+3 per mid-season entrant) | ~150 | ~222 |
| **Phase B**: rotation on duel seeds, 5×3 | 15 | **~75** | **~111** |

Phase B saves about $170 a day against today (−60%, about $62k a year) and
about $480 a day against v16 as merged. New uploads' canonical runs are
unchanged in count; in Phase B they also count as duel evidence.

### 6. Parity and rollout

Validator and Platform folds must crown the same champion, or every weight
receipt fails with `ledger_pin_mismatch`.

- **Phase A (Platform only).** `ContinualRetestSettings.retest_mode:
  "continual" | "season_duel"`, default `continual`, plus `season_duel_seeds`
  (2–5, default 3). It changes which confirmation seeds get leased, never how
  they are folded. It is inert below the dueling floor, so v15 is untouched.
  Backroom shows and sets it; the diagnostic reports the season's duel seeds,
  anchor and per-member coverage.
- **Phase B (both folds).**
  - Validator protocol 35 adds the sequential dethrone (`ditto/validator/weights.py`)
    and season-seeded quorum verification (`worker.py`).
  - Platform mirrors the fold (`apps/platform/ditto/api_server/koth.py`). Both
    folds run the shared `ditto/tests/fixtures/crown_gate_cases.json` cases,
    extended with sequential cases; #3351's pair-mask and season cases must
    keep passing unchanged.
  - Platform serves `crown_duel` only under `crown_duel_mode: fleet_ready`,
    after every recently-live weight setter reports protocol 35, exactly like
    `crown_gate_mode`. Season-seeded quorum leases go only to protocol-35
    validators.
  - Phase B is active only after compat-2 adoption: release, deploy, and every
    validator updated.

### 7. Phases

- **Phase A**: ship `retest_mode: season_duel` (Platform scheduling). Flip it
  after v16 activates and stabilises. It costs half of v16-as-merged and is no
  worse on fairness.
- **Phase B**: ship protocol 35 (sequential dethrone plus season-seeded quorum
  runs). Enable `crown_duel_mode: fleet_ready` once the fleet reports
  protocol 35. This is where the coin flip ends.

Operator payloads (Peyton applies; never flipped by the build):

Phase A, after v16 activation, written from the current revision with only
these fields changed:

```json
{"retest_mode": "season_duel", "season_duel_seeds": 3}
```

Phase B, after the fleet reports protocol 35:

```json
{"crown_duel_mode": "fleet_ready"}
```

### 8. Risks

- **Phase A without B keeps the coin flip.** Each season is a fresh 3-pair
  duel under today's rule, about 4 false changes a day. That is the same as v16
  as merged, but Phase A should not be read as the fairness fix.
- **Slower genuine dethrones.** A 0.015 lead takes about 1–2 days. That is the
  price of ending noise crowns.
- **Seed exposure inside a season.** This is unchanged from today's continual
  family and bounded to one season. Phase B's upload rule keeps canonical runs
  unpredictable.
- **Operator voids.** A voided pair drops out of the window like any missing
  record. A season with fewer than K pairs simply contributes fewer.
