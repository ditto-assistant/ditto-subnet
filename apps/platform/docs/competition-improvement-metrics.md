# Competition improvement metrics

Status: **measurement only.** Nothing here changes a score, status, hold,
release policy or weight. It exists so a source-release embargo decision can
be made from data.

Code: `ditto/api_server/competition_improvement.py` (pure arithmetic),
`ditto/db/queries/competition_improvement.py` (inputs and storage),
`ditto/api_server/competition_improvement_job.py` (hourly loop, platform
role), `ditto/api_server/endpoints/admin_competition_improvement.py`
(operator read). Backroom: `get_competition_improvement_metrics`.

## Tables

| table | one row per | rewritten |
|---|---|---|
| `competition_improvement_daily` | bench version x UTC day | open days only |
| `competition_source_releases` | exact-SHA public handoff | every run |
| `competition_release_forks` | (release, detected fork) | every run |

A day is **closed** once it is two days old. A closed row is never rewritten
under the same `metrics_version`, so it records what the frontier was rather
than what a later re-score made it. The first run backfills every day from the
first bench v13 quorum; those rows carry `backfilled = true`. Bumping
`METRICS_VERSION` recomputes every day.

## Definitions

- **Canonical composite**: median of the agent's k=3 quorum composites on one
  bench version. The continual mean is not reconstructed.
- **Frontier(v, d)**: highest canonical composite among `scored`/`live` agents
  on bench `v` whose quorum finalized (third score row) before the end of `d`.
- **Top 5**: the frontier set with one agent per owner (hotkey and payment
  coldkey are one owner).
- **Champion**: the last epoch-pinned champion of the day
  (`ledger_epoch_snapshots`).
- **New high**: an agent whose composite strictly exceeds the frontier when it
  finalized. Driver: `released_fork` when it is a detected fork of a release,
  `same_owner_iteration` when its owner uploaded anything earlier, else
  `original`.
- **Source release**: first unauthenticated public artifact fetch of the exact
  SHA. Its bench is the newest it had finalized on by then; its embargo is the
  release-policy revision in force at the handoff.
- **Fork**: an other-owner upload after the handoff holding at least 0.5 of
  the release's distinctive items (`agent_distinctive_items`, #3303), or
  declaring the release as its screening `released_base`. **Novel fraction**
  is the share of the fork's own items the base lacks; under 0.10 is **thin**.
  Delta compares canonical composites on the base's bench; the shared-seed
  delta pairs the two on every seed both were scored on (quorum and
  confirmation ledger, voided samples excluded).
- **Noise floor**: for each bench's top 10, the SD of per-seed composites
  (validator median per seed, three seeds or more) and the quorum SEM. A step
  is beyond noise when it clears 2 x sqrt(2) x the median seed SD.
- **Embargo windows**: the release-policy history split at each revision.
  Releases belong to the window of their handoff and carry their forks; new
  highs belong to the window they finalized in.

## Limits

- Score rows are upserted in place, so backfilled days use today's composites
  and statuses; only rows captured live are true point-in-time snapshots.
- Fork detection depends on the distinctive-item index; `coverage` reports how
  much of the upload history it covers. A fork that rewrote every literal is
  invisible unless it declared its base.
- Owners do not include attested hotkey links.
