# Relative token-efficiency adjustment (bench_version >= 7)

Platform-side implementation of the v7 efficiency contract. The other half of
the contract already shipped validator-side: for `bench_version >= 7` the
deterministic scorer is **quality-only** — it reports audited token usage
(`details.token_usage`, relay-metered through the trusted broker) and a neutral
`token_efficiency` record (`formula_version: "v7-quality-only-v1"`, multiplier
1) as in-band proof that usage never moved the composite. Efficiency
incentives live here as epoch-frozen cohort adjustments. Curves v1/v2 remain
the historical, strictly-upside bonus. New Bench v9 epochs use curve v3, a
bounded factor that can reward or penalize cost while leaving the signed
quality evidence unchanged. The validator scorer is never touched: same
artifact, same signed validator quality score, forever.

## Bench v9 bounded factor (curve v3)

Curve v3 implements:

```text
Efficiency Factor = clamp((Reference Cost / Agent Cost)^alpha,
                           minimum_factor,
                           maximum_factor)
if Efficiency Factor <= 1:
    Final Score = Authoritative Quality Score × Efficiency Factor
else:
    Final Score = Authoritative Quality Score
                  + (Efficiency Factor - 1) × (1 - Authoritative Quality Score)
```

The words "Reference Cost" do **not** name a fixed dollar amount. Values such
as `$0.09` and `$0.16` are examples of two observed agents, not policy
constants. For each new `(bench_version=9, run_size=full, epoch)` snapshot:

1. `Agent Cost` is the arithmetic mean over **seeds** of every comparable
   accepted evaluation available when the daily snapshot freezes: the pinned
   quorum seed plus protocol-19 single-seed continual retests. Each seed
   contributes one observation, the median audited chat-token total
   `v9_base.score_gates.model_use.prompt_tokens + completion_tokens` across the
   validators that scored it, and `Agent Cost` is the arithmetic mean of those
   seed observations. The three canonical receipts all re-score one pinned
   seed, so they collapse to a single observation — exactly how per-seed quality
   already treats them. With no retests this equals the frozen
   median-of-quorum value.
2. Every canonical and continual root used by the aggregate must parse as the
   exact frozen v9 score contract, be in
   `enforce` mode, and carry passed/`10000` model-use and semantic gate factors.
   Missing, malformed, shadow, failed, or zero-model evidence is unqualified
   and receives no factor. Historical retests whose signatures predate the
   protocol-19 cost binding are ignored rather than treated as zero-cost runs;
   an explicitly invalid protocol-19 retest fails the whole aggregate closed.
   The legacy `platform_model_use_reconciliation` annotation is never scoring
   authority.
3. Use the board's authoritative v9 composite as quality: canonical / continual
   quality under the normal finalized-v9 policy, or independently verified full
   quality while confirmation enforcement is active. Apply the existing
   quality/memory floors, collapse duplicate owners and lineages, and keep the
   top `cohort_size` qualified entries. These floors select the reference cohort
   and gate factor upside; every row with valid cost evidence still receives
   bounded downside, so sandbagging below a floor cannot escape a penalty.
4. `Reference Cost` is the **nearest-rank P25** of those per-agent costs:
   sort ascending and select rank `ceil(0.25 × N)`. At `N=8` it is the second
   observed cost; at `N=25` it is the seventh. It is never the cheapest single
   agent, a mean, an operator guess, or a moving live value.
5. Freeze membership, P25, `alpha`, `minimum_factor`, and `maximum_factor` in
   the immutable epoch snapshot. A later arrival is measured against that
   frozen reference and cannot move already-published scores.

At the reference, the factor is exactly `1.0`. A lower audited cost earns a
factor above one, capped at `1.10`; a higher audited cost receives a factor
below one, floored at `0.85`. Defaults use `alpha=0.25`, which softens raw cost
ratios. Positive efficiency scales only the quality score's remaining
headroom. For example, quality `0.95` and factor `1.10` produce `0.955`, not
`1.0`. An imperfect quality score therefore cannot become perfect through
efficiency, while a factor below one remains a bounded multiplicative penalty.
This never changes the signed quality evidence or the frozen factor.

Because the neutral point is P25, roughly the efficient quartile is
neutral-or-better and the rest of a non-degenerate cohort is on the bounded
penalty side. Thus equal Tool/Memory/Gates quality is ordered by audited
efficiency before the canonical `first_seen` tiebreak. `first_seen` remains
decisive only when the resulting effective scores are genuinely equal.

The public `average_run_cost_microusd` field remains informational. It reads a
short-retention grant ledger and includes embedding/provider-price effects and
possibly non-quorum leases, so it is neither permanent nor replayable enough
for consensus scoring. Under the locked v9 model/profile, signed chat tokens
are the durable comparable cost proxy. This PR must not claim that the factor
uses literal USD.

Historical v1/v2 snapshots—including any already-frozen v9 snapshot—replay
their stored curve exactly. Curve v3 is selected only when creating a new v9
snapshot; v7/v8 behavior and the deterministic scorer contract do not change.

Code map:

* `ditto/api_server/efficiency.py` — the pure math (cohort build, dedupe,
  robust reference, legacy bonus curves, bounded v3 factor) plus the DB-backed
  materializer.
* `ditto/db/models.py` — `EfficiencyCohortSnapshot` + `EfficiencyBonus`
  (base migration `2026_07_24_add_efficiency_bonus_tables.py`; curve-v3
  columns are additive and nullable for historical replay).
* `ditto/db/queries/efficiency.py` — append-only reads/writes.
* `ditto/api_server/endpoints/public.py` — leaderboard exposure +
  `GET /public/efficiency/snapshots/{snapshot_id}`.
* `ditto/api_server/endpoints/scoring.py` — validator-ledger exposure behind
  the default-off fold flag.

## Legacy v1/v2 algorithm (Bench v7/v8 and stored snapshots)

This section documents the historical upside-only policy. It remains the exact
replay rule for every snapshot already frozen with `curve_version` 1 or 2,
including an old v9 snapshot. It is **not** the cost authority or factor formula
for a newly created v9 snapshot; those are defined in the curve-v3 section
above.

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
   penalty). Curve v3 does not read this legacy blob.
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
   (`1` = original single-tier, `2` = two-tier, `3` = Bench-v9 bounded
   factor) plus the knobs belonging to that curve.
   `reference_from_snapshot` replays a snapshot under **its stored policy**,
   so a pre-tier snapshot reproduces its single-tier bonuses exactly forever
   and a v3 snapshot keeps its original exponent and clamps, regardless of the
   running build's defaults.
9. **Strictly upside (v1/v2 only).** `bonus >= 0` always. Unqualified,
   unaudited, or expensive runs keep their unmodified composite. There is no
   path where fewer tokens raise a score that quality did not already earn.
10. **Effective score (v1/v2 only).**
    `effective_composite = composite * (1 + bonus)` —
    multiplicative, matching the spec's `bonus_multiplier` form; a scale
    factor preserves the fold's composite-comparison semantics and a zero
    composite can never buy weight from cheapness. The validator's signed
    composite is never modified; the effective value is derived at read time
    from the frozen bonus.

### Freezing semantics

* The **snapshot** (membership, floors, reference statistics, curve version,
  and curve-specific knobs) is computed once per epoch and persisted
  append-only. Recomputing in a later epoch inserts a new row; historical rows
  never change.
* A submission's **assignment row** (`efficiency_bonuses`, retained as the
  compatibility table name) is inserted exactly once per
  `(agent_id, bench_version, epoch_index)` against that epoch's frozen active
  snapshot. Legacy rows store `bonus`; v3 rows keep `bonus = 0` for old readers
  and store nullable `factor`. Published scores for an epoch never drift when
  later submissions arrive; a later epoch gets a new immutable assignment.
* A submission finalizing or completing another retest mid-epoch is scored
  against the epoch's **frozen**
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
| `cohort_limit`, `n_min`, `bonus_cap`, `quality_floor`, `memory_floor` | frozen common/legacy policy |
| `curve_version`, `deep_bonus_cap`, `deep_frontier_ratio` | frozen legacy bonus policy (1 = single-tier, 2 = two-tier; legacy knobs are null when unused) |
| `factor_alpha`, `minimum_factor`, `maximum_factor` | frozen curve-v3 power and clamps; null for v1/v2 |
| `reference_p25_tokens`, `reference_median_tokens` | frozen robust statistics (P25 is v3 Reference Cost; median is retained but is not a v3 zero point; null while inactive) |
| `members` (JSON) | `[{agent_id, miner_hotkey, lineage_key, composite, memory_mean, token_total, first_seen, collapsed_agent_ids}]` (`first_seen` may be absent on legacy snapshots) |
| `computed_at` | freeze time (UTC) |

`efficiency_bonuses` (insert-once per
`(agent_id, bench_version, epoch_index)`):

| column | meaning |
|---|---|
| `agent_id`, `bench_version`, `epoch_index` (PK) | one immutable assignment per submission, board version, and efficiency epoch |
| `snapshot_id` (FK) | the frozen cohort the bonus was computed against |
| `token_total` | audited cost proxy used (legacy complete-usage median or v3 mean of signed per-seed quorum-plus-retest observations; null means unqualified) |
| `bonus` | frozen legacy fraction in `[0, 0.1]`; compatibility zero for v3 |
| `factor` | frozen curve-v3 multiplier in `[0.85, 1.10]`; null for v1/v2 (a v3 row is written only with valid cost evidence) |
| `created_at` | assignment time (UTC) |

Every adjustment is reproducible from stored data alone:
`reference_from_snapshot(snapshot)` + the row's `token_total` re-derive its
legacy bonus or v3 factor.

## API changes

Public (`GET /api/v1/public/leaderboard`):

* Per finalized entry (v7+, feature enabled): `efficiency_bonus` (legacy),
  `efficiency_factor` (v3), `effective_composite`, and
  `efficiency_snapshot_id`. The pre-adjustment quality, adjustment, result,
  and frozen-snapshot provenance are exposed **distinctly**. The factor
  supersedes the bonus when both appear; a v3 row's compatibility bonus is
  not exposed. Disabled previews use the separate
  `efficiency_bonus_preview` / `efficiency_factor_preview` fields and never
  enter `effective_composite`.
* Response-level `efficiency` status: `active`, `epoch_index`,
  `snapshot_id`, `cohort_size`, `n_min`, `curve_version`, legacy curve knobs,
  v3 `factor_alpha` / `minimum_factor` / `maximum_factor`,
  `reference_p25_tokens`, and `reference_median_tokens`. Before cohort
  activation the API reports `active: false`; while disabled it reports an
  explicitly non-persistent preview rather than an awarded adjustment. Preview
  status also exposes candidate, valid-cost, quality-qualified, owner-deduped,
  and lineage-deduped counts so a zero cohort identifies the failing stage.
* `GET /api/v1/public/efficiency/snapshots/{snapshot_id}`: the full immutable
  audit record. Lineage digests are moderation-adjacent and never exposed —
  members carry opaque `lineage_group` ordinals plus `collapsed_agent_ids`.
* Leaderboard ranking always follows `official_composite`. For a normal
  finalized-v9 entry, the pre-efficiency value is its canonical / continual
  quality. While confirmation enforcement is active, only full-confirmed rows
  rank and the pre-efficiency value is the verified
  `full_effective_micros / 1_000_000`. Curve v3 then computes
  a downside multiplication or an upside remaining-headroom adjustment,
  independently of the model-use rollout gate. That projection is the
  exact-quality secondary key when the coordinated fold is active and remains
  visible as audit telemetry otherwise. Curves v1/v2 retain their historical
  `pre_efficiency_composite * (1 + efficiency_bonus)` fold. The separate fields
  expose the same arithmetic as provenance for the dashboard.

Validator ledger (`GET /api/v1/scoring/scores`): `LedgerEntry` gains
additive-optional `efficiency_factor` alongside legacy `efficiency_bonus` and
`effective_composite`, populated **only** when
`DITTO_EFFICIENCY_BONUS_FOLD_ENABLED=true`. With the flag off (default), the
new fields are absent/null and validators retain their pre-adjustment behavior.
The validator independently derives the fold, preferring `efficiency_factor`
when present and applying the same curve-v3 downside/headroom transform after
authoritative v9 quality. A validator
must never fold these fields unless the Platform operator activates the fold
globally. The Platform and root validator contract goldens must be regenerated
and byte-identical whenever this wire shape changes.

## Config knobs (env)

| env var | default | meaning |
|---|---|---|
| `DITTO_EFFICIENCY_BONUS_ENABLED` | `false` | master switch: snapshots, bonus assignment, public exposure |
| `DITTO_EFFICIENCY_BONUS_FOLD_ENABLED` | `false` | expose effective_composite on the validator ledger (requires enabled) |
| `DITTO_EFFICIENCY_BONUS_CAP` | `0.05` | tier-1 `B_max` (the curve's value at P25); boot-validated to `(0, 0.10]` |
| `DITTO_EFFICIENCY_BONUS_DEEP_CAP` | `0.10` | tier-2 saturation cap; boot-validated to `cap <= deep_cap <= 0.10` |
| `DITTO_EFFICIENCY_BONUS_DEEP_FRONTIER_RATIO` | `0.5` | deep frontier as a fraction of P25; boot-validated to `(0, 1)`; the bonus saturates flat below `ratio x P25` |
| `DITTO_EFFICIENCY_BONUS_FACTOR_ALPHA` | `0.25` | curve-v3 exponent in `(0, 1]`; softens the reference/agent cost ratio |
| `DITTO_EFFICIENCY_BONUS_MINIMUM_FACTOR` | `0.85` | curve-v3 penalty floor in `[0.85, 1]` |
| `DITTO_EFFICIENCY_BONUS_MAXIMUM_FACTOR` | `1.10` | curve-v3 reward cap in `[1, 1.10]` |
| `DITTO_EFFICIENCY_BONUS_COHORT_SIZE` | `25` | top-N cohort membership cap |
| `DITTO_EFFICIENCY_BONUS_MIN_COHORT` | `8` | `N_min` activation gate (after dedupe) |
| `DITTO_EFFICIENCY_BONUS_EPOCH_HOURS` | `24` | efficiency epoch length / namespace (fixed UTC windows; immutable once policy history or snapshots exist) |
| `DITTO_EFFICIENCY_BONUS_QUALITY_FLOOR` | `0` | static `Q_min` fallback (first epoch / lower bound) |
| `DITTO_EFFICIENCY_BONUS_MEMORY_FLOOR` | `0` | static `M_min` fallback (first epoch / lower bound) |

All are validated at boot (`check_config`); the process refuses to start with
an out-of-range cap/factor, an inverted factor range,
`cohort_size < min_cohort`, `min_cohort < 2`, or fold exposure without the
master switch.

## Hot-swappable settings (no redeploy)

The env knobs above are the **seed default**. Both booleans and every numeric
knob except `epoch_hours` are runtime-settable from backroom via an
append-only revision table (`efficiency_bonus_settings_revisions`) behind
`admin/efficiency-bonus-settings`, so an operator can enable / disable / fold
the bonus and retune every knob **live, with no redeploy** (enable → watch the
shadow → flip the fold → maybe roll back). Modeled on
`admin_screener_review_settings` / `admin_benchmark_rollout`: optimistic
concurrency (`expected_revision`), a typed confirmation string, and an
actor/reason audit trail; the table is append-only, so every change is
recoverable history. `epoch_hours` remains a required full-policy field, but the
first revision must match the deployment seed and later revisions must preserve
it: changing the divisor would reuse/remap persisted epoch ordinals and break
snapshot replay. Treat the env value as deployment-immutable once efficiency
state exists as well.

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
* **Reproducibility is preserved.** The knobs that reproduce a published adjustment
  are frozen **in the epoch snapshot** (`efficiency_cohort_snapshots`:
  `bonus_cap`, `deep_bonus_cap`, `deep_frontier_ratio`, `curve_version`,
  `factor_alpha`, `minimum_factor`, `maximum_factor`,
  `cohort_limit`, `n_min`, `quality_floor`, `memory_floor`, the reference
  statistics, and the membership) at freeze time — so changing a setting mid
  epoch never mutates an already-frozen snapshot or its insert-once assignments,
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

1. **Dark (default).** `DITTO_EFFICIENCY_BONUS_ENABLED=false`. No snapshot or
   assignment is persisted. Public status may show an explicitly unapplied
   preview; ranking and validator weights are unchanged.
2. **Observing.** Enable the master switch. Each epoch freezes one snapshot;
   while the deduped qualified cohort is below `N_min`, it remains inactive
   and no assignment rows are written. For a new v9 epoch, inspect that all
   cohort members have complete signed k=3 v9 roots and that the P25 reference
   is plausible for the locked model/profile.
3. **Platform active.** The first epoch meeting `N_min` freezes an active
   snapshot and immutable per-agent assignments. New v9 snapshots select curve
   v3; v7/v8 and pre-existing v1/v2 snapshots keep their historical curves.
   The public leaderboard exposes quality / factor-or-bonus / effective score
   separately.
4. **Fleet-ready, then fold.** Deploy heartbeat-protocol-19 validators across
   the participating fleet and verify that they prefer `efficiency_factor`,
   apply the downside/headroom transform after authoritative v9 quality, ignore
   legacy bonus for that v9 path, and treat absent/malformed factors neutrally.
   Only then flip
   `fold_enabled: true` (or seed
   `DITTO_EFFICIENCY_BONUS_FOLD_ENABLED=true`) so the validator ledger carries
   the adjustment. Platform also fails closed at read time: it suppresses all
   v3 factors from both the validator ledger and public official/KOTH
   projections until every recently-live validator capable of serving Bench v9
   reports protocol 19 or newer. A fresh pre-19 requester receives a neutral
   factor projection, while a validator that cannot serve Bench v9 does not
   indefinitely veto the capable fleet. This protocol gate applies only to
   bounded factors; historical curve-v1/v2 bonus exposure is unchanged.

Each of these transitions can be a live backroom settings change (no restart);
the env vars remain the seed default for a fresh deployment.

### Operational runbook: enabling legacy v7/v8

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

### Operational runbook: enabling curve v3 on Bench v9

1. Deploy and migrate Platform, then deploy the matching heartbeat-protocol-19
   validator release everywhere that participates in the fold. Keep
   `fold_enabled=false`.
2. Confirm the authoritative board is Bench v9 and entries have full
   confirmation evidence. Do not substitute displayed USD cost: spot-check the
   three accepted roots' signed model-use prompt + completion token totals and
   curve-v3 integrity floors (attributed inference on at least half of eligible
   cases, 200 prompt tokens per eligible case, and 300 prompt tokens per
   successful request).
3. Choose the future-epoch v3 policy (defaults: `alpha=0.25`, factor range
   `[0.85, 1.10]`, `N=25`, `N_min=8`) and enable materialization. The next new
   v9 epoch freezes its own nearest-rank P25; operators do not enter a reference
   cost.
4. Verify the snapshot reports `curve_version=3`, the intended clamps and
   exponent, a dynamic `reference_p25_tokens`, and complete qualified members.
   Check at least one lower-cost and one higher-cost agent against the stored
   formula. Leave inactive or missing-integrity entries neutral; below-floor
   entries cannot receive upside but remain subject to bounded downside.
5. Activate the fold only after every participating validator is on protocol
   19+. Confirm the factor appears only after the live capable-fleet check
   succeeds. A mixed or empty capable fleet remains neutral even if the config
   flag is on. Watch public and validator-ledger projections for one epoch;
   rollback first sets `enabled=false` (which also makes the fold ineffective),
   then rewinds the binary. The upgraded database deliberately rejects a
   previous binary's attempt to freeze a new legacy v9 snapshot; existing
   historical v1/v2 snapshots remain replayable. This fails closed across an
   epoch boundary instead of corrupting the immutable v3 authority.

Changing alpha, clamps, floors, or cohort sizes affects only future snapshots.
An existing snapshot—including one frozen under v1/v2—always replays its stored
curve and parameters.

## Anti-gaming properties and residual risks

Blocked by construction:

* **Sandbagging** (cheap-but-bad runs): the quality gate — composite floor
  AND memory floor — plus the ratchet from the previous cohort's medians.
  Under v3, a below-floor row cannot receive upside but still receives bounded
  downside, so it cannot step just below a floor to escape an expensive-run
  penalty. Curve-v3 additionally rejects one-call, one-case, and cheap-probe
  shapes even when they clear the v9 base contract's deliberately minimal
  one-basis-point gate; missing/zero-model evidence is unqualified rather than
  cheap.
* **Outlier / lowball bombing:** the reference is nearest-rank P25 and
  median — one adversarially cheap run cannot move everyone's frontier, and
  the single minimum is never used.
* **Sybil / lineage stuffing:** near-identical submissions collapse to one
  cohort entry before the reference is computed (normalized-source hash,
  else artifact sha256), so cloning a lean harness across hotkeys does not
  widen its influence on the frontier; the coldkey-deduped ledger already
  limits one entry per paying owner.
* **Blast radius:** legacy curves remain capped at 10% upside; curve v3 is
  clamped to `[0.85, 1.10]`. The exponent softens raw ratios. Downside remains
  multiplicative, while upside closes only a bounded fraction of authoritative
  quality's remaining headroom. Historical v1/v2 replay is unchanged.
* **Token-starvation racing:** tier 2 SATURATES flat below the deep frontier
  — there is no marginal reward for pushing usage toward zero, so the curve
  never incentivizes gutting real work (skipped retrieval, truncated memory
  writes) that the quality gate cannot fully observe on a finite sample.
* **Retroactive drift:** epoch-frozen snapshots + insert-once bonus rows —
  published scores never move when new submissions arrive.
* **Cheap-model substitution / miner-reported usage:** legacy curves read only
  relay-metered usage. Curve v3 additionally requires every accepted quorum
  root to match the exact v9 score contract and threshold profile in enforce
  mode with passed model-use and semantic gates. Miner-reported usage and the
  unsigned reconciliation annotation are ignored.

Residual risks (honest list):

* **Lineage detection is best-effort.** The keys are the exact artifact
  digest and the canonicalized-source hash; a refactored or re-generated
  copy of the same harness evades both and occupies its own cohort slot. The
  shadow anti-copy signals (AST fingerprints, code embeddings) exist but are
  not yet fused into this dedupe; when they graduate from shadow mode the
  lineage key should adopt them.
* **Legacy route-identity pinning is validator-side.** Curves v1/v2 trust the
  scorer's `status == "complete"` token accounting. Curve v3 closes that
  particular gap by parsing the typed, signature-bound v9 roots and matching
  their frozen contract/profile identities, but still relies on the validator
  and broker to have produced those signed facts correctly.
* **Cohort timing:** the snapshot reflects the board at the first
  leaderboard read of the epoch; a lean harness finalizing minutes later
  waits one epoch (<= 24 h) to influence the frontier. Deliberate — the
  alternative is a moving reference.
* **Insert-once vs. re-scores:** a submission re-scored later (retest lane)
  keeps its originally frozen bonus even if its composite moved; the frozen
  contract was chosen over recomputation. The base composite (which
  dominates) always reflects the latest accepted scores.
* **Degenerate legacy curve:** if a v1/v2 cohort converges to near-identical
  usage, bonuses degenerate toward a step at P25. Curve v3 instead remains
  centered at factor `1.0` on P25 and bounded by its frozen clamps.

## Test coverage

* `ditto/tests/api_server/test_efficiency.py` — legacy audited-total parsing;
  fail-closed typed v9-root parsing; lineage keys/dedupe; dynamic nearest-rank
  P25 reference; bounded-factor formula, monotonicity, and clamps; interpolation
  boundaries (at/below P25, at/above median, midpoint, degenerate), quality
  gate, N_min inactivity, strictly-upside, floor ratchet, epoch arithmetic;
  two-tier curve: continuity at P25, monotonicity across the whole curve,
  saturation at/below the deep frontier, tier-1 half unchanged, legacy
  single-tier when the knobs are absent, envelope bound, and
  curve_version-1 references reproducing single-tier bonuses.
* `ditto/tests/db/queries/test_efficiency.py` — snapshot roundtrip
  (members JSON and v3 knobs), unique epoch key, old-snapshot immutability under
  new epochs, per-epoch assignment insert-once, and factor persistence.
* `ditto/tests/api_server/endpoints/test_public_efficiency.py` — end-to-end:
  active cohort awards frozen bonuses; within-epoch freezing (newcomer scored
  against the frozen frontier, published rows unmoved); N_min inactive board;
  lineage dedupe visible via the snapshot endpoint (no raw digests); v<7 and
  disabled boards write and expose nothing; new-epoch snapshot without
  mutating the old; floor ratchet; validator-ledger fold flag off/on.
* `ditto/tests/api_server/test_config.py` — env parsing + boot validation.
* Platform and root `ditto/tests/contract/` — regenerated, byte-identical
  `LedgerEntry` goldens for the additive factor field.
