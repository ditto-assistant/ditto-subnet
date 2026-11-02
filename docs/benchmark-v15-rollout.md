# Bench v15 qualification and rollout

Status: **contract shipped and deployed (v0.430.0), decision lane on, canary passed; not activated.** Supporting v15 does not change
Platform's active version (14). Activation is a separate, guarded operator
rollout (`start_benchmark_rollout` desired 15) after the steps below.

## What v15 is

V15 keeps the v14 generation surface, grader and gates (plus the v15
atomic-value grading floor, `grade.PolicyFloorV15`) and changes two things a
harness sees. Full contract: `research/dittobench-datagen/docs/bench-versions.md`
("V15 production-scale tool surface and decision lane").

1. **Production-scale tool catalog.** Each seed's catalog is the v14 surface
   plus 200 connector tools the workspace has not linked (~235 tools, ~62 KB).
   Calling a connector returns "not connected" and does nothing; it is an
   ordinary extra call. Tool selection out of a production-sized menu is the
   new difficulty axis.
2. **Scored decision lane.** The sandbox gets `DITTOBENCH_DECISIONS_URL`. A
   harness may POST a System One body there; the relay forces the
   operator-pinned TypeSafe Jev model, meters it as ledger cost and applies the
   lane's walls. Use is unrestricted. Only v15+ grants carry the lane pin, so
   every v14 lease is byte-identical to before.

The generator pack policy 15 ships with it (tandem upgrade). V15 rounds accept
packs declaring 14 or 15; v13 packs keep working in v14 rounds.

## Layers

| Layer | Change |
| --- | --- |
| Datagen | `BenchVersionV15`, epoch `2027-07-01`, `catalog/v15.go`, connector refusal in `toolexec`, pack policy 15, known vector `33232bf3…` |
| Scorer | advertises 8–15, `v15_scored_runtime_env`, `/decisions` broker route (`decision_lane.go`), locked `DITTOBENCH_DECISIONS_URL` |
| Shared protocol | `V9EvidenceBenchVersion` gains 15 |
| Platform | `benchmark_contract` 15, v15 packet gate in `verified_scorer_for_version`, decision grants only at v15+, v15 rollout start blocked while the `decision_lane` board is off |
| Validator | parses `v15_scored_runtime_env`; `GENERATOR_ROUND_BENCH_VERSIONS` gains 15 |
| Backroom / dashboard | regenerated client, Zod and dashboard unions |
| Miner kit | accepts 15 on `/run`; rehearsal ceiling 15 (live fallback stays 14 until activation) |
| Release | identity gate expects scorer versions 8–15 |

## Activation steps

1. Merge, release and deploy the contract. Confirm the scorer image on every
   validator advertises 15 and a `v15_scored_runtime_env` packet
   (`get_benchmark_rollout_control` → v15 `capable_validator_count`).
2. Turn on the decision lane with an exact pin:
   `set_inference_concurrency_settings` `decision_lane: {enabled: true,
   model: "typesafe/jev-1.14"}` (price and walls default). The v15 start
   blocker clears only once this revision exists.
3. Canary: `issue_benchmark_canary` at v15 on one validator against a
   released top agent; confirm a completed v15 score, connector refusals in
   the trace, and decision-lane rows in Backroom traces if the harness used it.
4. Announce in #118 with at least 24 hours' notice (the draft lives in the
   rollout PR), then `start_benchmark_rollout` desired 15. The top cohort
   retests into v15 and activation follows the ordinary quorum rule.
5. After activation: move `LIVE_SCORING_BENCH_VERSION` and the `mine` skill's
   live bench to 15, and record the activation here.

Rollback before activation is cancelling the rollout; v14 stays active
throughout.

## Activation record (in progress)

- **Contract.** #3318 merged as `a131ed26`, released and deployed in v0.430.0
  (2026-11-02). Backroom `get_benchmark_rollout_control` lists v15 as the only
  available target. Tempo-aligned wave pacing (#3319) shipped in the next
  release with `wave_cadence_tempos` 0 (off).
- **Fleet.** By 03:15 UTC four of five online validators ran scorer 0.430.0
  advertising 8–15. One host advertises v15 but has zero serviceable slots
  (memory below the 8 GiB sandbox limit).
- **Decision lane.** Inference board revision 38 (2026-11-02 02:4x UTC)
  enabled `decision_lane` with `typesafe/jev-1.14` at the default price and
  walls. The v15 start blocker cleared.
- **Canary.** `6f15c0de-7a15-4e15-8a15-000000000001`: a released-era DrPresident
  build (`872028c6`, accepted v14 scores 0.919 / 0.947 / 0.910) on one
  0.430.0 validator, full profile, seed `5125359883995889733`, dataset
  `a5455429…`. It completed at 03:18 UTC: composite **0.9476** (tool 0.9467,
  memory 0.962), within its v14 band. The harness predates v15, so the
  connector menu was present and the decision lane went unused.
- **Next.** 24-hour #118 notice, then `start_benchmark_rollout` desired 15;
  set `wave_cadence_tempos: 4`, `wave_seeds_per_window: 3` with the notice.
