# Relative efficiency bonus — platform-layer specification (v7+)

Status: IMPLEMENTED in `ditto-platform` behind operator-controlled shadow and
enforcement settings. Nothing in this document runs in the validator. Under
the v7+ quality-only contract the validator's composite is a
pure function of (dataset, transcript) — deterministic and time-invariant —
and audited token usage is recorded, never scored. Efficiency incentives live
here, in the platform layer, computed over frozen cohorts.

## Why platform-layer, not validator-layer

A deterministic validator must produce the same score for the same artifact
regardless of WHEN it runs. Any relative-efficiency term inside the validator
would break that (the comparison set changes over time). Absolute starter-kit
budgets and their 60-run calibration workflow are retired. The platform already
owns time-indexed state (the KOTH ledger) and can freeze cohorts by epoch.

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

## Bonus definition

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

## Rollout sequence

1. Validators report quality-only scores plus complete trusted usage.
2. The platform observes the model-use verdict and relative bonus in shadow.
3. Backroom enables enforcement and bonus assignment only after the live cohort
   is healthy. No scorer deploy or calibration campaign is part of this step.
