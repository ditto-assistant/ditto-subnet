# Bench v15 qualification and rollout

Status: **contract shipped, not activated.** Supporting v15 does not change
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

## Previous-era scored work after activation

Activation admits only the frozen rescore cohort (top 15 owners by distinct
hotkey), new-era uploads, work first queued after the rollout opened,
carryover rows (`prev_gen_carryover`, off by default) and audited contract
refreshes. A submission that finished its v14 quorum outside the cohort gets
no v15 dataset and no v15 ticket, and is missing from the v15 board. This
includes a newer sibling of a cohort member and work that reached quorum
while the rollout was open. The v13→v14 bump behaved the same way (#3237).
Backroom `list_stranded_previous_era_submissions` lists this population. Any
backfill policy is an operator decision; the read does not admit anything.
