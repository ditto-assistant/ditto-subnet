# Benchmark rollout activation

Every benchmark transition uses a durable five-agent collection cohort. A median never mixes
score rows from different versions, and the ledger is on exactly one version at
a time.

> **Threshold-gated authority.** The desired version takes over ledger
> authority for the whole pool only once at least
> `MIN_DESIRED_AUTHORITY_AGENTS` (= 5, the KOTH champion + `KOTH_TAIL_SIZE`)
> agents hold a complete, ranked desired-version quorum
> (`ditto/db/queries/benchmark_rollout.py`, applied in
> `list_eligible_ledger`). Below that count the active version stays
> authoritative for every agent — for the public leaderboard, the validator
> weight fold, and KOTH — while desired-version quorums are collected and shown
> as per-row rollout progress. The threshold exists because the flip drops
> agents without a desired-version quorum: crossing it guarantees the emission
> set still has its full complement of recipients. Authority is never mixed
> per-agent, because composites from different benchmark versions are not on a
> comparable scale within one KOTH fold.

## Compatibility

- Heartbeat protocols before v8 cannot advertise benchmark capabilities.
- Protocol v8 and newer sign `capabilities.scorer_benchmarks`. A validator is
  capable of a version only while its `fresh_verified` observation is at most
  five minutes old and its scorer source revision and software version match the
  signed stack identity.
- Job and artifact responses carry `bench_version`; old validators ignore the
  additive field.
- A legacy v2 score may omit `bench_version` and keeps the legacy signature
  bytes. Newer scores must explicitly report and sign their version.

## Operator-controlled start

Shipping a contract makes it available but never opens a rollout. Validator
heartbeats and job polls may refresh an existing rollout, but cannot create one.
Backroom discovers the shipped choices from the read-only control endpoint:

```text
GET /api/v1/admin/benchmark-rollout
Authorization: Bearer ...
```

Starting a selected target is an authenticated, audited UI action. It requires
a reason, an expected active-version compare-and-swap value, and the exact typed
confirmation `START BENCHMARK V{target}`. Superseding an unactivated rollout
similarly requires `SUPERSEDE BENCHMARK V{target}` and a reason. These controls
are intentionally not exposed over MCP.

The start freezes the current top five eligible agents and miners and renders a
target-version dataset for every member. The frozen membership and positions
are never silently reshuffled. At least one fresh, identity-matched validator
must advertise the selected target before it can start; additional compatible
validators can join asynchronously.

## Operator-configurable rescore cohort size

How many inherited agents a transition re-scores is subnet policy, not a
constant. It defaults to the historical **ten** and an operator may set it
anywhere in `[5, 25]` from backroom as the subnet scales, with no redeploy:

```text
GET  /api/v1/admin/queue-policy-settings
POST /api/v1/admin/queue-policy-settings
```

The POST is an append-only revision carrying the complete policy, an
`expected_revision` compare-and-swap, an actor + reason, and the exact typed
confirmation `APPLY QUEUE POLICY SETTINGS` (a mismatch is a 409). The table
(`queue_policy_settings_revisions`) is never updated or deleted, so every
change is recoverable history. Unlike rollout *activation*, this control **is**
exposed over MCP — it is policy for a future transition, not an action on a live
one.

Three properties make the semantics unsurprising:

* **Bounds, enforced at every layer.** `5 <= rescore_cohort_size <= 25` in the
  backroom tool schema, the request model, `historical_rescore_cohort`,
  `create_rollout_snapshot`, and a `CHECK` constraint. Five is
  `PRIORITY_COHORT_SIZE`, the KOTH emission set, below which the authority
  switch could not be satisfied. Twenty-five is the persisted storage ceiling
  (`benchmark_rollout_bounded_members`). Out-of-range input is refused; nothing
  is clamped.
* **The value is frozen at START, not read continuously.** A rollout records the
  policy in force when it was started as `benchmark_rollouts.rescore_cohort_target`
  and every downstream consumer — the qualification backfill, the blocker
  report, quarantine impact analysis — reads *that* column, never the live
  setting. So raising the policy from ten to twenty-five **while a rollout is
  open does nothing to that rollout**: it keeps filling to ten, and the new
  value applies to the next transition. Membership being append-only would make
  growing an in-flight cohort safe, but not predictable — it would silently move
  the completion bar that gates the authority switch under validators already
  scoring the cohort. `GET /admin/queue-policy-settings` reports the open
  rollout's frozen target beside the configured one so the difference is visible
  rather than inferred.
* **Historical rollouts stay explainable.** The frozen target is on the rollout
  row *and* in the immutable `cohort_frozen` audit payload, so "why did the v6→v7
  transition rescore twelve agents?" is answerable from the record alone.
  `cohort_size` (how many members actually qualified) and
  `rescore_cohort_target` (how many it was built to) are deliberately separate:
  the target is a ceiling, not a quota, and a transition inheriting only twelve
  eligible agents freezes twelve under a target of twenty-five.

Widening the cohort does **not** widen the historical search. The backfill still
consults exactly the two most recent scored benchmark eras; a larger target can
only admit more of those same two, never reach into a third. A rollout started
at twenty-five rescores a strict superset of the one it would have started at
ten, in the same order.

Only capable validators receive target-version cohort tickets. Scores, tickets,
retry budgets, datasets, leases, and uniqueness are keyed by benchmark version.
At one or two distinct target scores, the active version remains authoritative
for the whole pool. This lets work begin with one compatible validator while
the canonical ranking remains stable. Once the full five-agent emission set has
target-version quorum, the desired version takes authority for the whole pool;
the same locked transaction activates the target and appends an audit event.
Canonical reads then exclude source-only agents; no median or weight fold mixes
versions.

The authenticated ledger adds optional `bench_version` provenance. Old
validators ignore that additive field and fold the full platform-selected pool;
new validators retain it for audit and re-score scheduling but deliberately fold
the same pool. This keeps on-chain weights identical during asynchronous
validator upgrades.

If a frozen member becomes banned, rejected, quarantined, or screening-failed,
it stays on the append-only snapshot so the freeze remains explainable, but it
is suppressed from remaining rollout work and from the authority numerator.
`blocked_ineligible` is reserved for an incomplete frozen membership, not for
source-review enforcement. Desired-version authority still requires the
inherited priority prefix to have raw 3/3 (skipping those permanently
ineligible members) and at least `MIN_DESIRED_AUTHORITY_AGENTS` ranked owner
families anywhere on the desired ledger, so banning an inherited cheat cannot
revert weights while honest desired-version families already fill the emission
set. An operator may still explicitly supersede the unactivated rollout with a
recorded reason.

## Observation and recovery

`GET /api/v1/public/bench/rollout` reports desired and active versions, rollout
status, the conservative count of currently capable validators, each qualified
agent and position, and its target-version score count. State is database-backed
and idempotent across API restarts.

Before activation, the public leaderboard defaults to the active-version pool
and retains explicit historical version views that never project current
emissions. After activation, the target is canonical and source-only results
are excluded.
