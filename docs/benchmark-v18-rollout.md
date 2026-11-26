# Bench v18 qualification and rollout

Status: **OFF.** Every layer supports and advertises 18 (#3411 to #3415), and
the v18 contract stack (#3420, #3421, #3430, #3432, #3437) completes it. Nothing
at v18 sets weights. Bench v17 stays the active scoring contract. Activation is
a separate, guarded operator rollout (`start_benchmark_rollout` desired 18)
that needs the gates below and an explicit operator go-ahead.

## What v18 is

The accuracy contract ([bench-versions.md](../research/dittobench-datagen/docs/bench-versions.md),
"Bench v18"). It is scored with priced-cost-v2 exactly as v17. It stays
language-agnostic, and model unpinning is deferred to a later bench.

1. **Accuracy hardening.** The v17 dueling datagen half, with the first-party
   mix hardened and the families top agents still miss pinned into every run.
2. **Production catalog.** Each seed's menu is 1,400 to 2,000 real tools drawn
   from a pinned snapshot (`V18CatalogSnapshotSHA256`). A whole menu is about
   100k to 200k tokens, at or above the locked model's 131,072-token context.
   Harnesses that retrieve or defer tools are unaffected. Harnesses that
   forward the whole menu fail.
3. **Run budget.** A canonical run stops admitting cases at $1.00 priced cost
   per 350 cases (`run-budget-v1`). Unrun cases score 0 with
   `budget_exhausted`.
4. **Tool encounters.** Ten of the 50 tool cases at `full` are multi-call
   encounters, graded on the outcome (required, forbidden and answer values).
   A filing is judged by the incident its title names.

Pack policy 18 shipped with generator kit version 7 (#3415). A v18 round
accepts packs declaring 17 or 18.

## Gates before any v18 canary

Treat each as a separate fact, verified live:

1. **Stack merged and released.** Note the semantic release version `R` that
   contains #3437.
2. **Deployed fleet-wide.** Every serving validator reports `R` or later in
   `get_validator_fleet` (`scorer.software_version`, `source_revision`). The
   generator scorer reports it too (`get_generator_scorer`).
   Today's `start_ready: true` for v18 reflects a build *without* this stack
   (0.468.2), so it is not evidence of readiness.
3. **v18 scorer floor in Platform (required).** A validator still on a
   pre-stack build advertises 18 and would generate a different v18 dataset
   (no production catalog, run budget or encounters). Before the rollout,
   add a v18 floor beside the v9 one in `verified_scorer_for_version`
   (`apps/platform/ditto/db/queries/benchmark_rollout.py`), so that
   `version >= 18` requires a scorer at release `R` or later
   (`_scorer_meets_version_floor`). Then confirm that
   `get_benchmark_rollout_control` counts only those validators as
   v18-capable. Source-mode and unmanaged validators keep v17 work and lose
   v18 work until they upgrade.
4. **Starter kit does not score 0.** With #3437 the kit serves the Jev hybrid
   path by default from bench 18. Measured locally on seed 42 at `full`: the
   stock kit 0.000 (the menu exceeds the context), preload-top-32 0.093 at
   $0.86, and Jev hybrid 0.263 at $0.27. The rehearsal `MAX_BENCH_VERSION`
   is 18; the live fallback stays 17 until activation.
5. **Redistribution.** The owner approved publishing the snapshot (names and
   descriptions from the public Composio listing, no input schemas), recorded
   2026-11-09 in bench-versions.md.

## Phases

| Phase | What happens | Exit criterion |
| --- | --- | --- |
| 1. Contract, OFF | Stack released and deployed, v18 floor live. No ticket leases at 18. | Gates 1 to 3 hold; v2 to v17 vectors unchanged. |
| 2. Shadow canary | `issue_benchmark_canary` at 18 against released top v17 agents, one validator at a time (operator approval: inference spend). Non-authoritative. | Runs fit the ticket TTL. No false zero on a cleared top agent. `details.run_budget` is present and not exhausted. `gate_evidence.priced_cost` is read (cost check below). |
| 3. Shadow rollout | `start_benchmark_rollout` desired 18 (operator approval only). The top cohort is scored at 18 beside v17. | Crown stability and rank correlation against v17 reviewed. Priced-cost factor distribution reviewed. No validator scores 18 below `R`. |
| 4. Activate | v18 becomes the scoring contract. Not retroactive. | An independently reviewed activation decision. |

## Cost check (phase 2)

The v18 catalog adds tokens only to harnesses that forward the menu. On v17,
nine of the ten top agents already forward a filtered set (median 31 to 34
tools per completion). Encounters add 2 to 5 completions per encounter case.

v17 production baseline, 2026-11-09 (three accepted runs each):

| Agent (v17) | Priced cost per run | v17 factor |
| --- | ---: | ---: |
| beast-d41 v1 | $0.420 to $0.426 | 1.0 |
| DrPresident v39 | $0.395 to $0.420 | 1.0 |
| logan v14 | $0.416 to $0.437 | 1.0 |

No-spend estimate for v18: $0.44 to $0.60 per run. That is below the $1.00
budget, so no cutoff is expected. But priced-cost-v2 is steep above its $0.50
reference (1.0 at $0.50, 0.65 at $0.75, 0.30 at $1.00), so the top of that
range costs about 0.86 of composite. Measure it on the phase 2 canaries
before activation. If the cohort lands above $0.50, decide whether the
reference should move for encounters. That is a cost-rule change and out of
v18 scope by decision.

## Rollout start blockers (enforced)

`get_benchmark_rollout_control` lists these for v18: the inference proxy
configuration, the decision lane with a pinned model, the dueling settings
(v16 and later), and capacity of fresh, identity-matched v18 validators.
After gate 3, that capacity counts only validators at release `R` or later.

## Announcement

Give agent miners at least 24 hours' notice. Give generator miners at least
7 days, or 14 days if any active generator still declares policy 16, which
rounds at 18 retire. A draft lives in the DITTO-2564 rollout memory. Posting
needs the operator.

## Rollback

v17 stays the active contract and is served throughout. Before activation,
rollback is cancelling the canary or rollout. After activation, it is a
rollout back to 17. A generator round at 18 whose validators were drained is
voided (`void_generator_round`).

## Post-activation verification

1. `get_benchmark_rollout_control`: `active_version` 18, `desired_version`
   18, status `activated`.
2. `get_leaderboard`: `scoring_bench_version` 18. The top five have quorum
   scores at 18, and none is a false zero.
3. `get_validator_fleet`: every serving validator is at `R` or later. No v18
   ticket on an older scorer (`list_validator_assignments`).
4. `get_agent_scores` for the top five: `details.run_budget` present;
   `gate_evidence.priced_cost` matches the phase 2 measurement.
5. `preview_generator_round`: active 18, bench 18, no reason. Then the first
   round after activation (`get_generator_round`): `bench_version` 18,
   per-column `grading_bench_version`, `legacy_retry` count.
6. Watch the first three rounds at 18 against the last three at 17.
7. Move the rehearsal `LIVE_SCORING_BENCH_VERSION` and the `mine` skill's
   live bench to 18 in the post-activation PR, and record the activation here.
