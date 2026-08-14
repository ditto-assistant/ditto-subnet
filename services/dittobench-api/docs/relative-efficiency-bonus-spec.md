# Relative efficiency adjustment — platform-layer specification (v7+)

Status: IMPLEMENTED in `ditto-platform` behind operator-controlled shadow and
enforcement settings. Nothing here changes the deterministic benchmark scorer.
Under the v7+ quality-only contract, quality evidence is deterministic and
time-invariant; the platform separately computes relative efficiency over a
frozen cohort. Historical curves v1/v2 are upside-only. A newly created Bench
v9 snapshot selects curve v3, a bounded penalty-or-bonus factor.

## Why platform-layer, not validator-layer

A deterministic validator must produce the same score for the same artifact
regardless of WHEN it runs. Any relative-efficiency term inside the validator
would break that (the comparison set changes over time). Absolute starter-kit
budgets and their 60-run calibration workflow are retired. The platform already
owns time-indexed state (the KOTH ledger) and can freeze cohorts by epoch.

## Bench v9 policy (curve v3)

For a new active `(bench_version=9, run_size=full, epoch)` snapshot:

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

The defaults are `alpha=0.25`, `minimum_factor=0.85`, and
`maximum_factor=1.10`. The lower exponent deliberately softens cost ratios;
the clamps bound both the penalty and reward. Positive efficiency scales only
remaining quality headroom: quality `0.95` with factor `1.10` produces `0.955`,
not `1.0`. At equal authoritative quality, a lower-cost qualified agent ranks
higher before the existing `first_seen` tie break; genuinely equal results use
the existing deterministic tie break. Historical curve-v1/v2 bonus replay is
unchanged.

### Cost and integrity authority

`Agent Cost` is a durable token proxy, not a displayed dollar price. It is the
arithmetic mean over **seeds**, taken across everything comparable and accepted
at the epoch freeze: the pinned quorum seed plus every protocol-19 single-seed
continual retest.

A seed is the unit of observation at both levels:

```text
observation(seed) = median(audited cost across the validators that scored it)
Agent Cost        = mean(observation(pinned seed), observation(retest seeds…))
```

The three v9 quorum receipts all re-score the agent's single pinned dataset
seed, so they are validator replicates of one observation rather than three
independent samples — the same rule the confirmation ledger already applies to
per-seed quality. Averaging raw rows instead would weight a seed by how many
validators happened to draw it and would let a lone validator set that seed's
cost. The per-seed observations are then averaged, so every evaluated seed has
equal weight. With no retests this reduces exactly to the frozen
median-of-quorum value, so agents the fleet has never retested are unaffected.

Each observation is the typed
`v9_base.score_gates.model_use.prompt_tokens + completion_tokens`. Every root
used by the aggregate must:

* parse under the exact frozen v9 score-contract revision and manifest;
* carry the same exact threshold-profile identity;
* be in `enforce` mode;
* report passed model use with factor `10000` and clear curve-v3's frozen,
  factor-specific integrity floor: attributed inference on at least half of
  eligible cases, at least 200 prompt tokens per eligible case, and at least
  300 prompt tokens per successful request;
* carry a positive prompt-plus-completion total; and
* report semantic and applied gate factors of `10000`.

Fail closed if any canonical root or new cost-authoritative retest is absent,
malformed, shadow-mode, failed, probe-only, or zero-use: that submission
receives no v3 factor. Historical continual rows whose signatures predate the
protocol-19 base-evidence binding are ignored, never counted as zero. The
v9 base gate's one-basis-point threshold alone is intentionally not enough to
earn an efficiency adjustment. Do not read the
legacy `platform_model_use_reconciliation` annotation. Do not use
`average_run_cost_microusd`: that short-retention grant-ledger metric includes
provider pricing, embeddings, and possibly non-quorum work, so it is useful
telemetry but not durable replay authority. Dollar values such as `$0.09` and
`$0.16` are examples of agents' observed spend, never configured reference
costs.

### Dynamic reference and quality

Use the board's authoritative v9 composite as `Authoritative Quality Score`:
canonical / continual quality under the normal finalized-v9 policy, or the
independently verified `full_effective_micros / 1_000_000` while confirmation
enforcement is active. Apply the configured/ratcheted quality and memory
floors, collapse duplicate lineages, and retain the top `cohort_size` qualified
entries. If fewer than `N_min` remain, freeze an inactive observation and
assign no factor.

For an active cohort, sort the per-agent token costs ascending and select the
nearest-rank 25th percentile:

```text
Reference Cost = sorted_costs[ceil(0.25 × N) - 1]
```

This P25 is dynamic data from the qualified cohort—not the cheapest entry, a
mean, a median, a dollar target, or an operator-entered constant. It is neutral
(`factor=1.0`); lower cost moves toward the `1.10` cap and higher cost moves
toward the `0.85` floor. Consequently, roughly the efficient quartile is
neutral-or-better while the remainder of a non-degenerate cohort is on the
bounded penalty side.

Freeze membership, P25, alpha, and both clamps in the immutable epoch snapshot,
then store each assignment separately. New arrivals cannot move an existing
epoch. Historical curve-v1/v2 snapshots, including an already-frozen v9
snapshot, replay their stored policy exactly; v7/v8 behavior is unchanged.

## Inputs the validator exposes (all already produced today)

Per scored run, signed/content-addressed as usual:

1. `details.token_usage` (chat, relay-metered, trusted — never miner-reported):
   `prompt_tokens`, `completion_tokens`, `total_tokens`, `requests`,
   `prompt_bytes`, `status` (+ `successes`, `usage_available`,
   `usage_unavailable` for completeness checks), and the route identity
   `provider` / `profile_revision` / `model`.
2. The broker accounting record (per run id): embedding usage
   (`embedding.prompt_tokens`), request-kind counts (`chat` / `embedding`),
   status counts, and the full observed identity block (`allowed_models`,
   `observed_models`, `embedding_profile`, `ticket_route_profile`).
3. The quality result: `composite` (quality-only under v7),
   `composite_stderr`, `tool_mean`, `memory_mean`, `bench_version`,
   `run_size`, `seed`, `dataset_sha256`, `details.token_efficiency`
   (`formula_version = "v7-quality-only-v1"`, multiplier 1 — the in-band
   proof that usage did not move the composite).

The QUALITY GATE RESULT for the bonus is computable from (3) alone; the
platform must not re-derive quality from usage.

## Historical v1/v2 bonus definition

The remainder of this section is the replay contract for legacy snapshots. It
does not define new Bench-v9 cost authority or curve-v3 arithmetic.

For each cohort `(bench_version, run_size, epoch)`:

1. **Qualify.** A submission enters the cohort only if:
   - `token_usage.status == "complete"` and the accounting identity matches
     the locked route/model contract (no partial or mixed-identity runs);
   - its quality clears the threshold: `composite >= Q_min` AND
     `memory_mean >= M_min`. Both floors are platform policy per epoch;
     the memory floor exists so a harness cannot buy efficiency by
     sacrificing the memory half. Suggested starting point: `Q_min` = median
     composite of the previous epoch's cohort, `M_min` = 0.8 × the previous
     epoch's median memory_mean.
2. **Frontier.** Let `E` = the set of qualified submissions' audited
   `total_tokens` (chat; embedding tokens are reported but excluded from the
   frontier — embedding load is validator-fixed per dataset, not a harness
   skill). The reference cost is the EFFICIENT QUARTILE:
   `C_ref = 25th percentile of E` (nearest-rank). Never the single cheapest —
   one outlier (or one adversarial lowball) must not move everyone's bonus.
3. **Bonus.** For a qualified submission with audited cost `C`:

       bonus_multiplier = 1 + B_max × clamp((C_ref / C), 0, 1) × step
       where step = 1 if C <= C_ref × S, else scaled linearly to 0 at C_ref × S_hi

   Concretely, a simple two-piece form that satisfies the constraints:

       if C <= C_ref:            bonus = B_max
       if C_ref < C <= 4×C_ref:  bonus = B_max × (4×C_ref − C) / (3×C_ref)
       if C > 4×C_ref:           bonus = 0

   with `B_max` capped at **5–10%** (platform picks one value per epoch and
   freezes it). The bonus multiplies the platform-side ranking score, never
   the validator composite.
4. **Strictly upside.** `bonus >= 0` always. An unqualified submission gets
   bonus 0 — never a penalty. Returning nothing, failing cases, or gutting
   memory quality can only LOSE the bonus (via the quality gate), never gain
   from cheapness. There is no path where fewer tokens raise a score that
   quality did not already earn.
5. **Frozen cohorts.** The cohort — membership, `Q_min`/`M_min`, `C_ref`,
   `B_max` — is computed ONCE at epoch close and recorded. Historical scores
   never drift: a submission's bonus is a function of its own epoch's frozen
   cohort, not of any later submission. Re-scoring a historical run re-reads
   the frozen cohort record.
6. **Minimum cohort size.** If fewer than `N_min` (suggest 8) submissions
   qualify in a cohort, no bonus is awarded that epoch (a quartile over a
   handful of runs is not robust).

## Anti-gaming notes

- The efficient-quartile reference plus the quality gate means the only way
  to earn the bonus is to be BOTH good and lean relative to peers on the
  same frozen suite.
- Audited usage comes from the validator's relay/broker metering
  (`source == "model_proxy_provider_response"`); miner-reported token fields
  are ignored everywhere.
- Identity pinning (route/model/embedding profile) prevents "efficiency" via
  an unauthorized cheaper model or route.
- The cap (5–10%) keeps the bonus a tiebreaker among comparable-quality
  agents; quality dominates by construction.
- For v3, complete typed k=3 roots plus protocol-19 cost-bound continual roots
  make model bypass, zero-model output, or a cherry-picked cheap receipt
  unqualified. P25 limits one cheap outlier's
  effect, and `[0.85, 1.10]` bounds the fold even when cohort costs are widely
  dispersed. Quality/memory floors select the reference cohort and gate v3
  upside; valid-cost rows below a floor still receive bounded downside, so
  sandbagging below the threshold cannot avoid an expensive-run penalty.

## Rollout sequence

1. Validators report quality-only scores plus complete trusted usage and the
   signature-bound v9 roots used by curve v3.
2. Deploy factor-aware Platform and heartbeat-protocol-19 validator releases,
   leaving the efficiency fold off. Let Platform freeze snapshots and audit
   cohort health/P25.
3. Backroom enables assignment only after the live cohort is healthy. New v9
   snapshots use v3; legacy snapshots continue replaying v1/v2.
4. Enable validator-ledger fold exposure only after all participating
   validators report protocol 19+, prefer `efficiency_factor`, apply it after
   authoritative v9 quality with the same downside/headroom transform, and
   treat absent/malformed factors neutrally.
   Platform independently fails closed when the recently-live Bench-v9-capable
   fleet is mixed or empty, even when the fold flag is set. A fresh pre-19
   requester receives no v3 factors, while a validator that cannot serve Bench
   v9 does not indefinitely veto the capable fleet. This new readiness gate
   does not alter historical curve-v1/v2 bonus exposure.
