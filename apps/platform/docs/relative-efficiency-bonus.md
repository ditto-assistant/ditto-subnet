# Relative token-efficiency bonus (bench_version >= 7)

Platform-side implementation of the v7 efficiency contract. The other half of
the contract already shipped validator-side: for `bench_version >= 7` the
deterministic scorer is **quality-only** — it reports audited token usage
(`details.token_usage`, relay-metered through the trusted broker) and a neutral
`token_efficiency` record (`formula_version: "v7-quality-only-v1"`, multiplier
1) as in-band proof that usage never moved the composite. Efficiency
incentives live here, as a bounded, epoch-frozen, strictly-upside bonus over
frozen cohorts. The validator scorer is never touched: same artifact, same
validator score, forever.

Code map:

* `ditto/api_server/efficiency.py` — the pure math (cohort build, dedupe,
  robust reference, bonus curve) plus the DB-backed materializer.
* `ditto/db/models.py` — `EfficiencyCohortSnapshot` + `EfficiencyBonus`
  (migration `2026_07_24_add_efficiency_bonus_tables.py`).
* `ditto/db/queries/efficiency.py` — append-only reads/writes.
* `ditto/api_server/endpoints/public.py` — leaderboard exposure +
  `GET /public/efficiency/snapshots/{snapshot_id}`.
* `ditto/api_server/endpoints/scoring.py` — validator-ledger exposure behind
  the default-off fold flag.

## Algorithm

One **efficiency epoch** is a fixed UTC window (`epoch_hours`, default 24 h,
windows counted from the Unix epoch). Once per epoch, per
`(bench_version, run_size)` board — ranked boards are always `run_size =
"full"`, since smaller profiles never rank — the platform freezes a cohort:

1. **Candidates.** Every finalized (k=3 quorum), ranked
   (`n >= MIN_ELIGIBLE_CASES`, composite > 0) entry of the authoritative
   ledger (`list_eligible_ledger`, one entry per payment-time coldkey).
2. **Audited cost.** Per agent: the median of `details.token_usage.total_tokens`
   over its quorum score rows whose accounting is `status == "complete"` with
   `usage_unavailable == 0`. Chat tokens only — embedding load is
   validator-fixed per dataset, not a harness skill. Miner-reported numbers
   never reach this blob; the validator writes it from the trusted relay. No
   complete audited row → the submission is unqualified (bonus 0, never a
   penalty).
3. **Quality gate.** `composite >= Q_min` AND `memory_mean >= M_min`. The
   memory floor exists so a harness cannot buy efficiency by gutting the
   memory half. First epoch: the static config floors. Later epochs ratchet
   from the previous **active** cohort: `Q_min = max(config, median composite
   of previous cohort)`, `M_min = max(config, 0.8 x median memory_mean)`.
4. **Lineage dedupe.** Qualified submissions collapse to one entry per
   lineage key — `normalized_source_hash` (canonicalized source; survives
   repack/reformat) when present, else the artifact `sha256`. The
   best-scoring entry survives (composite desc, first_seen asc, agent_id
   asc); the collapsed ids are recorded in the snapshot. Grounded in a real
   observation: 3 of the top-5 harnesses ship byte-identical model fixtures
   under different hotkeys — one lineage must not define the frontier.
5. **Cohort.** The top-`cohort_size` (default 25) deduped entries by
   composite.
6. **Activation gate.** If the deduped cohort is smaller than `min_cohort`
   (`N_min`, default 8 — the spec's value), the snapshot is frozen **inactive**:
   the observation is recorded, no bonus is awarded, the API reports
   `active: false`, and no bonus rows are written (so activation in a later
   epoch can still freeze those agents at their first active epoch).
7. **Robust reference** (never the mean, never the single minimum):
   * `P25` = nearest-rank 25th percentile of cohort token totals — the
     efficient-quartile full-bonus frontier;
   * `median` = cohort median — the zero-bonus point.
8. **Bonus curve** (two tiers, `curve_version = 2`). Let
   `deep_frontier = deep_frontier_ratio x P25` (default `0.5 x P25`). For a
   qualified submission with audited cost `C`:

   ```
   bonus = deep_cap                                                 if C <= deep_frontier   (tier 2: SATURATED)
   bonus = cap + (deep_cap - cap) * (P25 - C) / (P25 - deep_frontier)  if deep_frontier < C <= P25   (tier 2: ramp)
   bonus = cap * (median - C) / (median - P25)                      if P25 < C < median     (tier 1: unchanged)
   bonus = 0                                                        if C >= median
   ```

   `cap` = tier-1 `B_max`, default 5%; `deep_cap` default 10%; both inside
   the agreed 5–10% envelope: boot checks enforce `0 < cap <= deep_cap <=
   0.10` and `0 < deep_frontier_ratio < 1`, and the DB CHECKs mirror them
   (the `efficiency_bonuses.bonus <= 0.1` CHECK already admits the 10% deep
   cap). The curve is **continuous at P25** (both tiers evaluate to `cap`
   there) and **monotone non-increasing** in usage. Every anchor is a pure
   cohort statistic — P25, median, and a fixed fraction of P25 — never the
   single cheapest submission. Degenerate cohorts (`median == P25`) collapse
   to a step at the frontier; a degenerate `P25 == 0` collapses tier 2 to a
   step at zero.

   **Saturation rationale:** below the deep frontier the bonus is flat at
   `deep_cap` on purpose. An asymptotically increasing reward for ever-fewer
   tokens would incentivize gutting real work (skipping retrieval passes,
   truncating memory writes) in ways the quality gate cannot fully observe
   on a finite case sample. Flat saturation means once a harness is already
   twice as lean as the efficient quartile, further starvation buys nothing.

   *Reconciliation note:* the validator-side spec draft tapered to zero at
   `4 x P25`. The operator decision (implemented here) anchors the zero point
   at the cohort **median** instead: both are robust, but the median ties the
   taper to the cohort's actual dispersion, so a uniformly lean cohort does
   not hand near-full bonuses to its own laggards, and an outlier-heavy tail
   cannot stretch the paying range. Full-bonus frontier (P25) and the 5–10%
   cap envelope are identical to the spec.

   **Curve/policy versioning:** every snapshot freezes its `curve_version`
   (`1` = original single-tier, `2` = two-tier) plus the tier-2 knobs.
   `reference_from_snapshot` replays a snapshot under **its stored policy**,
   so a pre-tier snapshot reproduces its single-tier bonuses exactly forever,
   regardless of the running build's defaults.
9. **Strictly upside.** `bonus >= 0` always. Unqualified, unaudited, or
   expensive runs keep their unmodified composite. There is no path where
   fewer tokens raise a score that quality did not already earn.
10. **Effective score.** `effective_composite = composite * (1 + bonus)` —
    multiplicative, matching the spec's `bonus_multiplier` form; a scale
    factor preserves the fold's composite-comparison semantics and a zero
    composite can never buy weight from cheapness. The validator's signed
    composite is never modified; the effective value is derived at read time
    from the frozen bonus.

### Freezing semantics

* The **snapshot** (membership, floors, `P25`/`median`, cap) is computed once
  per epoch and persisted append-only. Recomputing in a later epoch inserts a
  new row; historical rows never change.
* A submission's **bonus row** (`efficiency_bonuses`) is inserted exactly
  once per `(agent_id, bench_version)`, against the frozen snapshot of the
  epoch in which it is first seen finalized while a snapshot is ACTIVE —
  including explicit zero rows, so "no bonus" is as frozen as "5%". Published
  effective scores never drift when later submissions arrive.
* A submission finalizing mid-epoch is scored against the epoch's **frozen**
  reference (it does not join the cohort until the next epoch's freeze).
* Materialization is lazy and idempotent: the first authoritative-board
  leaderboard read of an epoch freezes the snapshot and assigns missing
  rows (the dashboard polls continuously, so in practice this is prompt).
  Concurrent materializers race safely — unique keys make the loser retry and
  adopt the winner's frozen rows.

## Storage schema

`efficiency_cohort_snapshots` (append-only, immutable):

| column | meaning |
|---|---|
| `snapshot_id` (PK, UUID) | provenance pointer used by bonus rows and the API |
| `bench_version`, `run_size`, `epoch_index` | unique cohort key |
| `active` | whether `n_min` was met after dedupe |
| `cohort_limit`, `n_min`, `bonus_cap`, `quality_floor`, `memory_floor` | frozen policy |
| `curve_version`, `deep_bonus_cap`, `deep_frontier_ratio` | frozen bonus-curve policy (1 = single-tier legacy, 2 = two-tier; tier-2 knobs null under v1) |
| `reference_p25_tokens`, `reference_median_tokens` | frozen robust reference (null while inactive) |
| `members` (JSON) | `[{agent_id, miner_hotkey, lineage_key, composite, memory_mean, token_total, collapsed_agent_ids}]` |
| `computed_at` | freeze time (UTC) |

`efficiency_bonuses` (insert-once per `(agent_id, bench_version)`):

| column | meaning |
|---|---|
| `agent_id`, `bench_version` (PK) | the bonused submission at its board version |
| `snapshot_id` (FK) | the frozen cohort the bonus was computed against |
| `token_total` | audited cost used (median of complete quorum totals; null → bonus 0) |
| `bonus` | frozen fraction in `[0, 0.1]` (DB CHECK) |
| `created_at` | assignment time (UTC) |

Every bonus is reproducible from stored data alone:
`reference_from_snapshot(snapshot)` + the row's `token_total` re-derive it.

## API changes

Public (`GET /api/v1/public/leaderboard`):

* Per finalized entry (v7+, feature enabled): `efficiency_bonus`,
  `effective_composite`, `efficiency_snapshot_id` — base composite, bonus,
  and effective score are exposed **distinctly** so the UI can show
  provenance. Null below v7, while disabled/inactive, or before assignment.
* Response-level `efficiency` status: `active`, `epoch_index`,
  `snapshot_id`, `cohort_size`, `n_min`, `bonus_cap`,
  `reference_p25_tokens`, `reference_median_tokens`. Before activation the
  API says the bonus is inactive (`active: false`); below v7 or disabled it
  is null.
* `GET /api/v1/public/efficiency/snapshots/{snapshot_id}`: the full immutable
  audit record. Lineage digests are moderation-adjacent and never exposed —
  members carry opaque `lineage_group` ordinals plus `collapsed_agent_ids`.
* Leaderboard ranking always follows `official_composite`. While the fold is
  active that field is the continual aggregate multiplied by the frozen bonus;
  `pre_efficiency_composite`, `efficiency_bonus`, and `effective_composite`
  expose the same arithmetic as separate provenance fields for the dashboard.

Validator ledger (`GET /api/v1/scoring/scores`): `LedgerEntry` gains
additive-optional `efficiency_bonus` and `effective_composite`, populated
**only** when `DITTO_EFFICIENCY_BONUS_FOLD_ENABLED=true`. With the flag off
(default) the ledger is byte-identical to the pre-bonus wire shape. The
weight fold itself lives in ditto-subnet and independently applies the bonus
after continual aggregation, including shared-seed paired comparisons. A
validator must never fold these fields unless the Platform operator activates
the fold globally. (The `LedgerEntry` contract golden was regenerated —
`ditto/tests/contract/validator_contract.json` — and the ditto-subnet copy
must be synced when that repo picks up the fields.)

## Config knobs (env)

| env var | default | meaning |
|---|---|---|
| `DITTO_EFFICIENCY_BONUS_ENABLED` | `false` | master switch: snapshots, bonus assignment, public exposure |
| `DITTO_EFFICIENCY_BONUS_FOLD_ENABLED` | `false` | expose effective_composite on the validator ledger (requires enabled) |
| `DITTO_EFFICIENCY_BONUS_CAP` | `0.05` | tier-1 `B_max` (the curve's value at P25); boot-validated to `(0, 0.10]` |
| `DITTO_EFFICIENCY_BONUS_DEEP_CAP` | `0.10` | tier-2 saturation cap; boot-validated to `cap <= deep_cap <= 0.10` |
| `DITTO_EFFICIENCY_BONUS_DEEP_FRONTIER_RATIO` | `0.5` | deep frontier as a fraction of P25; boot-validated to `(0, 1)`; the bonus saturates flat below `ratio x P25` |
| `DITTO_EFFICIENCY_BONUS_COHORT_SIZE` | `25` | top-N cohort membership cap |
| `DITTO_EFFICIENCY_BONUS_MIN_COHORT` | `8` | `N_min` activation gate (after dedupe) |
| `DITTO_EFFICIENCY_BONUS_EPOCH_HOURS` | `24` | efficiency epoch length (fixed UTC windows) |
| `DITTO_EFFICIENCY_BONUS_QUALITY_FLOOR` | `0` | static `Q_min` fallback (first epoch / lower bound) |
| `DITTO_EFFICIENCY_BONUS_MEMORY_FLOOR` | `0` | static `M_min` fallback (first epoch / lower bound) |

All are validated at boot (`check_config`); the process refuses to start with
an out-of-range cap, `cohort_size < min_cohort`, `min_cohort < 2`, or fold
exposure without the master switch.

## Hot-swappable settings (no redeploy)

The env knobs above are the **seed default**. All of them — both booleans and
all eight numeric knobs — are also runtime-settable from backroom via an
append-only revision table (`efficiency_bonus_settings_revisions`) behind
`admin/efficiency-bonus-settings`, so an operator can enable / disable / fold
the bonus and retune every knob **live, with no redeploy** (enable → watch the
shadow → flip the fold → maybe roll back). Modeled on
`admin_screener_review_settings` / `admin_benchmark_rollout`: optimistic
concurrency (`expected_revision`), a typed confirmation string, and an
actor/reason audit trail; the table is append-only, so every change is
recoverable history.

* **Read path.** The three compute points — `ensure_efficiency_state` and
  `read_efficiency_board` (materialize + board read) and the validator fold in
  `scoring.py` — resolve the *latest* revision at compute time through
  `EfficiencyBonusSettingsResolver` (a short TTL cache,
  `DITTO_EFFICIENCY_BONUS_SETTINGS_TTL_SECONDS`, default 5 s; the admin write
  also invalidates the cache immediately). A backroom change therefore lands on
  the next leaderboard / ledger read with no restart. The resolver reads on an
  independent session, so it never disturbs the request transaction.
* **Seed = byte-identical default.** With no revision written, the env seed
  governs, so an untouched deployment is exactly the pre-change behavior
  (default off).
* **`fold requires enabled` is enforced at read time**, not only at boot: a
  persisted `fold_enabled=true` with `enabled=false` folds nothing.
* **Reproducibility is preserved.** The knobs that reproduce a published bonus
  are frozen **in the epoch snapshot** (`efficiency_cohort_snapshots`:
  `bonus_cap`, `deep_bonus_cap`, `deep_frontier_ratio`, `curve_version`,
  `cohort_limit`, `n_min`, `quality_floor`, `memory_floor`, the reference
  statistics, and the membership) at freeze time — so changing a setting mid
  epoch never mutates an already-frozen snapshot or its insert-once bonuses,
  exactly as changing an env knob never did. Only **future** epochs pick up the
  new values.

Backroom drives it through a dedicated MCP tool that wraps this endpoint
(`set_efficiency_bonus_settings` → `POST /admin/efficiency-bonus-settings`),
distinct from the product's `set_feature_flag_override` (which governs per-user
consumer entitlements in `backend`, a different service). To enable the bonus:
apply a revision with `enabled: true` (confirmation `APPLY EFFICIENCY BONUS
ENABLED`). To fold it into validator weights (separately, later): apply a
revision with `enabled: true, fold_enabled: true`.

## Activation lifecycle

1. **Dark (today).** `DITTO_EFFICIENCY_BONUS_ENABLED=false`. Nothing is
   computed, written, or exposed. All boards byte-identical to today.
2. **Observing.** Enable the master switch once v7 is the authoritative
   board. Each epoch freezes a snapshot; while the deduped qualified cohort
   is below `N_min` the snapshots are inactive, every bonus is 0, and the API
   reports `active: false`.
3. **Active.** The first epoch whose deduped cohort reaches `N_min` freezes
   an active snapshot; every finalized qualified agent gets its insert-once
   bonus row; the leaderboard shows base / bonus / effective distinctly.
4. **Fold (coordinated, cross-repo).** After the bonus-aware ditto-subnet
   release is live across the participating fleet, flip the fold on
   (`fold_enabled: true` via the settings endpoint, or
   `DITTO_EFFICIENCY_BONUS_FOLD_ENABLED=true` as the seed) so the ledger
   carries the fields. Validators derive the combined score from the continual
   evidence plus `efficiency_bonus`; the dashboard exposes the pre-bonus score,
   awarded percentage, and folded score so the change is never silent.

Each of these transitions can be a live backroom settings change (no restart);
the env vars remain the seed default for a fresh deployment.

### Operational runbook: enabling on v7

1. Deploy this build; run `make migrate` (adds the two tables; no existing
   table is touched).
2. Confirm v7 is the authoritative board (`/public/leaderboard` →
   `active_bench_version >= 7`) and that scores carry
   `token_efficiency.formula_version == "v7-quality-only-v1"` with complete
   `token_usage` blocks.
3. Pick the epoch's policy: leave defaults (`cap 5%`, `deep cap 10%`, `deep
   frontier 0.5 x P25`, `N=25`, `N_min 8`, 24 h epochs) or set the knobs (env
   seed, or a settings revision). Set static floors if the first epoch should
   already gate quality (e.g. `quality_floor 0.3`). To run tier 1 only, set
   `deep_cap` equal to `cap` (the ramp collapses to flat `cap` below P25 —
   bonuses never drop from disabling tier 2 mid-flight because old snapshots
   keep their frozen policy).
4. Turn the master switch on — apply a settings revision with `enabled: true`
   from backroom (no restart), or set `DITTO_EFFICIENCY_BONUS_ENABLED=true` as
   the seed. The next leaderboard read freezes the first snapshot.
5. Verify: `/public/leaderboard` → `efficiency.active`; when true, spot-check
   one entry's bonus against its snapshot via
   `/public/efficiency/snapshots/{id}` (P25/median + the entry's
   `token_total`).
6. Leave the fold off (`fold_enabled: false`) until the subnet-side fold change
   is reviewed, shipped, and coordinated.

Changing `cap` / floors / `N` mid-flight only affects **future** epochs'
snapshots; frozen snapshots and assigned bonuses never move.

## Anti-gaming properties and residual risks

Blocked by construction:

* **Sandbagging** (cheap-but-bad runs): the quality gate — composite floor
  AND memory floor — plus the ratchet from the previous cohort's medians.
  Returning nothing or gutting memory can only *lose* the bonus.
* **Outlier / lowball bombing:** the reference is nearest-rank P25 and
  median — one adversarially cheap run cannot move everyone's frontier, and
  the single minimum is never used.
* **Sybil / lineage stuffing:** near-identical submissions collapse to one
  cohort entry before the reference is computed (normalized-source hash,
  else artifact sha256), so cloning a lean harness across hotkeys does not
  widen its influence on the frontier; the coldkey-deduped ledger already
  limits one entry per paying owner.
* **Blast radius:** the cap envelope (tier-1 5%, tier-2 saturation 10%, hard
  max 10% everywhere) keeps the bonus a tiebreaker among comparable-quality
  agents; quality dominates by construction.
* **Token-starvation racing:** tier 2 SATURATES flat below the deep frontier
  — there is no marginal reward for pushing usage toward zero, so the curve
  never incentivizes gutting real work (skipped retrieval, truncated memory
  writes) that the quality gate cannot fully observe on a finite sample.
* **Retroactive drift:** epoch-frozen snapshots + insert-once bonus rows —
  published scores never move when new submissions arrive.
* **Cheap-model substitution / miner-reported usage:** only relay-metered
  usage from the validator's trusted broker is read; runs without complete
  audited accounting are simply unqualified.

Residual risks (honest list):

* **Lineage detection is best-effort.** The keys are the exact artifact
  digest and the canonicalized-source hash; a refactored or re-generated
  copy of the same harness evades both and occupies its own cohort slot. The
  shadow anti-copy signals (AST fingerprints, code embeddings) exist but are
  not yet fused into this dedupe; when they graduate from shadow mode the
  lineage key should adopt them.
* **Route-identity pinning is validator-side.** The platform trusts the
  scorer's `status == "complete"` accounting and does not re-verify the
  model/route identity block against the locked contract; a scorer bug that
  marked a partial or off-route run complete would leak into the frontier.
  (The broker enforces the allowlist upstream, so this requires a validator
  fault, not a miner action.)
* **Cohort timing:** the snapshot reflects the board at the first
  leaderboard read of the epoch; a lean harness finalizing minutes later
  waits one epoch (<= 24 h) to influence the frontier. Deliberate — the
  alternative is a moving reference.
* **Insert-once vs. re-scores:** a submission re-scored later (retest lane)
  keeps its originally frozen bonus even if its composite moved; the frozen
  contract was chosen over recomputation. The base composite (which
  dominates) always reflects the latest accepted scores.
* **Median-zero curve is cohort-relative:** if the whole cohort converges to
  near-identical usage, bonuses degenerate toward a step at P25 — bounded by
  the cap, and exactly the "field has converged" signal.

## Test coverage

* `ditto/tests/api_server/test_efficiency.py` — audited-total parsing,
  lineage keys/dedupe, nearest-rank + median reference, interpolation
  boundaries (at/below P25, at/above median, midpoint, degenerate), quality
  gate, N_min inactivity, strictly-upside, floor ratchet, epoch arithmetic;
  two-tier curve: continuity at P25, monotonicity across the whole curve,
  saturation at/below the deep frontier, tier-1 half unchanged, legacy
  single-tier when the knobs are absent, envelope bound, and
  curve_version-1 references reproducing single-tier bonuses.
* `ditto/tests/db/queries/test_efficiency.py` — snapshot roundtrip
  (members JSON), unique epoch key, old-snapshot immutability under new
  epochs, bonus insert-once.
* `ditto/tests/api_server/endpoints/test_public_efficiency.py` — end-to-end:
  active cohort awards frozen bonuses; within-epoch freezing (newcomer scored
  against the frozen frontier, published rows unmoved); N_min inactive board;
  lineage dedupe visible via the snapshot endpoint (no raw digests); v<7 and
  disabled boards write and expose nothing; new-epoch snapshot without
  mutating the old; floor ratchet; validator-ledger fold flag off/on.
* `ditto/tests/api_server/test_config.py` — env parsing + boot validation.
* `ditto/tests/contract/` — regenerated `LedgerEntry` golden (additive
  fields).
