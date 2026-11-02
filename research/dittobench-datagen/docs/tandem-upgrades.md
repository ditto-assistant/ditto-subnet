# Tandem upgrades: agent bench versions and generator pack versions

Status: design and runbook, with the operator's decisions of 2026-10-27
recorded and enforced in code (see "Operator decisions"). Nothing here
activates a version or changes a live setting. The v14 pack contract it builds
on shipped in [generator-competition.md](generator-competition.md) ("Bench
contract").

Design principle (operator): **every component is versioned and does version
matching**, so upgrades are asynchronous across the board. Validators,
dittobench-api, the generator scorer and each generator say which versions
they run; Platform intersects. No component's version is inferred from
another's release string.

SN118 runs two competitions on one benchmark:

- **Agents** are scored on a `bench_version` (v14 active since 2026-10-28). A version
  is an immutable generation and scoring contract, moved by
  `start_benchmark_rollout` and activated when the rollout earns authority.
- **Generators** emit challenge packs. Each round, a panel of top agents plays
  every column, and generators earn credit for cases the panel splits on. A
  pack declares the grading policy (`grading_bench_version`) its cases are
  graded under.

Until v15 the two are loosely coupled: rounds run panel agents, so a round
needs a runtime version the panel's validators can run and a grading policy
the scorer supports. In v15 ([dueling-bench.md](dueling-bench.md)) generator
cases run inside canonical agent runs, so the couple is tight: an agent bench
version and the pack policies it consumes must move together. This document
fixes how they move, from v14 on.

## Decisions

| Question | Decision |
|---|---|
| Identifier | **One version number for both competitions** (operator-confirmed). Bench version N is the agent contract, the generator-round runtime contract, and pack grading policy N. Every activatable bench version gets a pack policy N, even when its grader is unchanged (grading is by floor, so policy 15 grades like 14 unless v15 changes `grade/`). Enforced: `test_every_activatable_bench_version_has_a_generator_pack_version` (`ditto/tests/test_bench_version_pins.py`) fails when any bench version from 13 on that a rollout can target (a shipped `benchmark_contract.py` entry or a validator-supported version) has no pack policy and round contract of the same number. |
| Compatibility | Derived, never a hand-kept matrix. A run or round at version N accepts packs at the **supported** policies in its window (`challenge.GradingBenchVersionsThrough(N)`, Platform `accepted_grading_versions(N)`). Each column is graded at its pack's declared policy. |
| Support window | **Two policies: the round's version and the one before it** (operator-confirmed). Enforced in the contract: `challenge.GradingPolicyWindow = 2` and Platform `GRADING_POLICY_WINDOW = 2`, pinned equal by `test_generator_grading_policy_window_is_two_versions`. A round at N accepts packs at N and N-1 only once a third policy exists; with {13, 14} a v14 round accepts both. A v15 round's request floor is 14 (`grading_bench_version: 14, grading_bench_versions: [14, 15]`), so v13 packs retire for v15 rounds without a separate baseline release. `challenge.GradingBenchVersion` (13) stays the floor a pre-v14 generator requires. |
| Who chooses a column's policy | **The generator**, deterministically from the request (`req.NewestAccepted(supported)`). Platform never forces a policy. A generator that cannot or does not upgrade keeps emitting the baseline. |
| Failure on a newer request | **Retry once on the baseline request** (`legacy_retry`). A working generator never loses its column to an upgrade. |
| What a round freezes | Bench version (runtime), columns, panel, column size at open. Each column's declared policy is recorded at generation. |
| Rounds during an open rollout | **Stay on the active version.** Rounds pay generators, so they run on the authoritative contract. Rollout readiness on the generator side is measured, not paid (below). |
| At activation | **Rounds flip at their next opening**, once `min_round_validators` validators (default 3, the agent rollout's quorum; see "Round switch quorum") can run a round at the new version and the live generator scorer advertises it; until then they stay on the previous round's version with a recorded reason. An in-flight round finishes on its frozen version. |
| Rollback | Deactivating N sends new rounds back to N-1 at the next opening. Generators re-choose from the N-1 request, so a generator that had declared N falls back without a code change. |

### Why one number

A compatibility matrix (agent vN accepts pack policies {a, b}) would be a
second source of truth that every layer must read and every bump must edit.
The repository has stranded a new version on every bump so far, always by one
layer keeping an old pin (see the bench-version-bump skill). One number with
a derived window keeps the rule a floor: `policy <= N` and `policy` supported.
The cost is a pack policy per bench version even when grading does not change;
that cost is one constant and one list entry in `challenge/pack.go`.

### Why per-column policy instead of a round-wide one

The alternatives were: exclude generators that cannot emit the round's
version, hold the round at the old version until a threshold of columns can
emit the new one, or grade old packs under the new grader.

- Excluding columns strands miners who did nothing wrong and empties the
  round at activation (all 9 active generators pinned v13 when v14 was built).
- A threshold keeps the panel's agents on the old runtime after activation,
  which needs a staffed old-version validator cohort indefinitely and couples
  the round's version to miner behavior.
- Grading a v13 pack under the v14 grader changes what the miner's cases
  measure without their consent, and some v13 golds fail the v14 rendered-text
  rule.

Per-column policy has none of these costs. Column credit is discrimination
among the same panel, computed per column, so two columns under different
policies never share a scale. The panel agents run one runtime per round.

## Capability advertisement

Each party says what it can run; Platform intersects.

| Party | Advertises | Where Platform reads it | Absent means |
|---|---|---|---|
| Validator stack (dittobench-api) | `supported_bench_versions`; `generator-round-v1`; `generator-round-max-cases-<n>`; `generator-round-bench-<n>` per round version | `scorer_benchmarks.generator_round_bench_versions` in the signed heartbeat | Original v13-only round mode |
| Validator (Python) | Its own `SUPPORTED_BENCH_VERSIONS` and `GENERATOR_ROUND_BENCH_VERSIONS`; caps what it forwards | Same heartbeat | n/a |
| Generator scorer (challenge-scorer) | `scorer_version`, case bounds (#3204) and `grading_bench_versions` (`13,14`, `challenge-scorer/6`) on every poll | `generator_scorer_heartbeats.grading_bench_versions` | v13 only (`LEGACY_SCORER_ROUND_BENCH_VERSIONS`), never inferred from `scorer_version` |
| Generator | The policy it declares in each pack | `generation.columns[].grading_bench_version`, qualification seed outputs | Baseline (v13) |

Rules:

- Platform leases a round at version N only to a validator whose
  `generator_round_bench_versions` contains N and whose scorer is verified for
  N (`heartbeat_supports_version`). A v13 round keeps the V13 scorer-cohort pin.
- The generator scorer advertises its grading policies explicitly, the way a
  validator advertises `generator-round-bench-<n>`. Platform opens a round at
  N only when the live scorer lists N, voids (`infra:`) a v-N round leased by
  a scorer that does not list it, and requests a qualification at the newest
  round contract the polling scorer lists. One that advertises nothing is
  v13-only: rounds stay at v13 until a scorer listing 14 is live. Backroom
  `get_generator_scorer` shows `grading_bench_versions`,
  `effective_grading_bench_versions` and `supports_round_contract`.
- A generator ignores policies its vendored package does not know and refuses
  only a request that accepts none it supports
  (`GenerateRequest.Validate`, from this release on). Generators built against
  the #3212 package (starter kit 4 and earlier forks) still require the
  request floor 13, so they must rebuild before v15 rounds, whose floor is 14.
- Generator readiness for version N is the share of counted columns that
  declare N. Platform does not gate on it.

## Notice windows

| Event | Notice to generator miners | Notice to agent miners | Channel |
|---|---|---|---|
| Pack policy N merged (rehearsable with `-round-bench N`) | Same day: what changed in grading, the new validation rules, "no action required" | None | Discord announcement, starter kit README |
| Rollout for N opens | Planned activation window | Existing rollout notice | Discord |
| Activation of N | At least 7 days after the policy merged | Existing activation notice | Discord, Backroom record |
| Activation of N+1 (rounds at N+1 stop accepting policy N-1, by the window) | At least 14 days before that activation, naming the version | None | Discord, starter kit README |
| Changing `cases_per_seed` or `MaxCases` | Existing rule: announce before applying | None | Discord |

Retiring a policy is the only step that can strand a generator, which is why it
has the longest notice. With the window in the contract it happens at the
activation of the version two above it, not by a separate release.

## Dual support and the upgrade sequence

The overlap period is the support window: from the release that adds policy
N until the activation of N+1, rounds at N accept packs at N-1 and N.

```
            release adds N         rollout opens      activation of N     activation of N+1
agents      N-1 active             N-1 + N scored     N active            N+1 active
            (N served, inactive)   (cohort on N)      N-1 kept 1 window   N-1 no longer served
rounds      N-1                    N-1                N (at quorum)       N+1 (at quorum)
packs       N-2 or N-1             N-2 or N-1         N-1 or N            N or N+1
qualif.     N-1                    N-1                N-1 or N            N or N+1
```

What `start_benchmark_rollout` drives on the generator side:

1. **Rollout opens (desired N).** Nothing changes for rounds or
   qualification: both follow `round_contract_for(active)`. The operator
   watches readiness with `preview_generator_round` (`bench_contract`,
   `capable_validators` per contract) and generator rehearsals. A follow-up
   (open question 1) can run each qualification's probe seed at N as a
   non-authoritative readiness signal.
2. **Activation (active N).** The next round to open freezes N once
   `min_round_validators` validators can run it and the generator scorer
   lists N, else it stays on the previous round's version with the reason.
   Qualification requests policies through N (capped at the polling scorer's
   list) from then on. No setting is touched.
3. **Rollback to N-1.** The next round opens at N-1. A round frozen at N
   finishes at N if validators for N remain; if the rollback drains them, void
   it with `void_generator_round` rather than let leases expire.

### Freezes

| Unit | Frozen when | Frozen fields |
|---|---|---|
| Round | Opening | `bench_version`, `cases_per_column`, columns, panel, `generation.bench_contract` |
| Round column | Generation | Declared `grading_bench_version`, `legacy_retry`, case fingerprints |
| Panel run (ticket) | Lease | The round's version, dataset digest |
| Epoch ledger | Epoch close | Fold digest, crown, recipients (existing) |
| v15 season | Season open | Bench version, pinned columns (submission and tree SHA), each column's declared policy |

## v15: generator cases inside agent runs across a version boundary

v15 puts five 35-case generator columns into every canonical run, pinned per
season. These are the changes needed so that works across an activation and
across a pack policy change. They extend the v15 checklist in
[dueling-bench.md](dueling-bench.md) section 5.

1. **Slices carry the pack's declared policy.** Grade each slice case at its
   column's `grading_bench_version`, never at the run's bench version or a
   fixed 13 (dueling-bench section 4 says 13; that becomes "the pack's
   declared policy"). `challengerun` already does this.
2. **The season freezes each column's policy.** The season qualification
   battery (32 seeds) records the declared policy. All 32 must agree, and the
   season manifest pins it. A slice whose pack declares another policy fails
   the slice, like a digest mismatch. Requests stay
   `NewGenerateRequestFor(seed, hotkey, 35, seasonBench)`, so the generator's
   choice is deterministic and matches the battery.
3. **A season belongs to one bench version.** It opens at the active version
   and never spans an activation. Activation takes effect at the next season
   open; the open season finishes on its version. `season_id` and
   `bench_version` both key scores, and crown comparisons need both equal.
4. **Rollout seasons.** While a rollout for 15 is open, Platform runs shadow
   seasons at 15 for the cohort's desired-version runs, pinned from the same
   eligible set. They are non-authoritative and never move generator pay.
   Without them the v15 cohort has no slices to run.
5. **Eligibility reads round columns in the season's window.** A column
   scored in a v14 round at policy 14 is eligible for a v15 season (14 is in
   {14, 15}); one at policy 13 is not, and its generator must declare 14 or 15
   in a v15 round first. Its measured solvability is under its declared
   policy, which the season pins, so solvability carries over.
6. **Pack policy 15.** Add `GradingBenchVersionV15` to
   `supportedGradingBenchVersions` and 15 to `GENERATOR_ROUND_BENCH_VERSIONS`
   in the v15 contract release, even if `grade/` is unchanged. The pin test
   fails on a shipped v15 benchmark contract without them. When 15 activates,
   the window is {14, 15} by `GradingPolicyWindow`; no baseline release is
   needed.
7. **Rollback from 15.** Seasons stop at the next season open; canonical runs
   return to the v14 datagen-only profile; rounds return to 14. Generators that
   declared 15 re-choose 14 from the v14 request. Accepted v15 rows stay
   versioned and are not reinterpreted.
8. **Scorer capability.** dittobench-api advertises `dueling-slice-v1` and a
   per-version feature (`dueling-slice-bench-<n>`), forwarded like
   `generator_round_bench_versions`, so Platform never leases a v15 run with
   slices to a scorer that cannot grade the declared policies.

## Runbook: activating v14 for both competitions

**Done: v14 activated at 2026-10-28 01:28:21 UTC.** Rounds now open at v14.
At 01:51 UTC `preview_generator_round` showed `active_version` 14,
`bench_version` 14, no reason, `capable_validators` {13: 4, 14: 5} against
`min_round_validators` 3, and `scorer_grading_versions` [13, 14]; the next
round was epoch 25988. Steps 6 and 7 below (first-round checks and the
three-round watch) are the remaining post-activation verification. The agent
side is recorded in
[benchmark-v14-rollout.md](../../../docs/benchmark-v14-rollout.md)
"Activation record".

Preconditions: the agent-side v14 qualification gates in
[benchmark-v14-rollout.md](../../../docs/benchmark-v14-rollout.md) are closed
or waived by the operator. Steps 1 to 4 change no live setting.

1. **Release.** Confirm the release containing the v14 pack contract is
   deployed: Platform (`preview_generator_round` returns `bench_contract` with
   `round_contracts: [13, 14]`), the generator scorer (`get_generator_scorer`
   shows `challenge-scorer/6` or later with `grading_bench_versions` [13, 14]
   and `supports_round_contract` true after activation), and validators
   (`get_validator_fleet`: v14-verified validators report
   `generator_round_bench_versions` containing 14).
2. **Default generator.** Package starter kit version 4
   (`uv run ditto generator package miners/dittobench-generator-starter-kit`)
   and register it (`create_default_generator_upload`, then
   `register_default_generator`, confirmation `REGISTER DEFAULT GENERATOR`).
   Wait for it to qualify. Its v13 output is unchanged, so this is safe
   before activation.
3. **Announce to generator miners** (template below), at least 7 days before
   the planned activation.
4. **Readiness check.** `preview_generator_round`: with v13 active,
   `bench_contract.bench_version` is 13 and `capable_validators["14"]` counts
   validators that will run v14 rounds after activation;
   `scorer_grading_versions` must contain 14. Do not activate with
   `capable_validators["14"]` below `min_round_validators` (3) unless rounds
   staying on v13 (with a staffed V13 cohort) is accepted.
5. **Open the agent rollout** (`start_benchmark_rollout`, desired 14,
   confirmation `START BENCHMARK V14`). Rounds and qualification stay on v13.
   Re-check step 4 while the cohort scores.
6. **Activation.** The rollout activates when it earns desired authority. Then
   verify, in order:
   - `preview_generator_round`: `active_version` 14, `bench_version` 14, no
     reason. A reason naming v14 means fewer than `min_round_validators` v14
     round validators (or a scorer that does not list 14); rounds keep running
     at v13 until the quorum appears.
   - The first round opened after activation (`get_generator_round`):
     `bench_version` 14; `generation.columns[].grading_bench_version` shows 13
     or 14 per column; count `legacy_retry` entries.
   - Panel jobs for that round lease only to validators reporting 14
     (`list_validator_assignments`).
   - The next qualification run (`get_generator_run`): seed outputs carry
     `grading_bench_version`.
   - The round scores and the generator board updates as before.
7. **Watch.** For the first three v14 rounds: column validity rate versus the
   last three v13 rounds, the `legacy_retry` count (it should fall as miners
   rebuild), and any `v14_rendered_text` rejections at qualification.
8. **Rollback**, if needed: follow "Ticket boundary and rollback" in
   benchmark-v14-rollout.md for the agent side. The next round opens at v13
   (if a V13 cohort validator is available). Void an in-flight v14 round whose
   validators were drained (`void_generator_round`).
9. **Later: v13 packs retire with v15.** Not part of v14 activation. By the
   two-version window, v15 rounds accept 14 and 15 only; announce it 14 days
   before v15 activates. v14 rounds keep accepting v13 packs.

## Miner communications

Generator miners, when the v14 pack policy is released:

> The generator competition now supports a v14 grading policy. You do not need
> to change anything: generators that emit v13 packs keep their columns in v14
> rounds and are graded exactly as today. To opt in, rebuild against the
> current `research/dittobench-datagen`, set your pack's `grading_bench_version`
> to `req.NewestAccepted(...)`, and rehearse with `-round-bench 13` and
> `-round-bench 14`. v14 reads agent replies as rendered text, and v14 packs
> must state answers, distractors and dump guards as plain text. The starter
> kit (version 4) shows the change. Rounds switch to v14 when the agent
> benchmark activates v14, planned no earlier than <date>.

Agent miners need no generator-specific notice in v14: round results do not
enter the agent ledger. In v15 they do, and the v15 activation notice must say
which pack policies the season's columns use.

Retirement (before v15 activates): name the version, the date, and the
one-line fix (rebuild against the current package and declare v14 or v15).

## Round switch quorum

Rounds switch to a newer bench version only once
`min_round_validators` validators can run a round at it (generator competition
setting, 1..32, **default 3**). Below quorum, rounds stay on the version the
previous round froze, and `preview_generator_round` reports why
(`bench_contract.reason`, `previous_version`, `min_round_validators`). A
version at or below the previous round's needs one capable validator, so a dip
below quorum after the switch does not flap rounds back.

It mirrors the agent competition. There, opening a rollout needs one capable
validator (`MINIMUM_ROLLOUT_START_VALIDATORS = 1`,
`apps/platform/ditto/api_server/endpoints/admin_benchmark_rollout.py:68`), and
the new version takes authority only once each priority-cohort position holds
`SCORING_QUORUM = 3` desired-version scores
(`apps/platform/ditto/db/queries/benchmark_rollout.py:101`, checked in
`rollout_cohort_score_complete` at line 1651), one score per distinct
validator (the `(agent_id, validator_hotkey)` key on `scores`,
`apps/platform/ditto/db/models.py:4686`), and `MIN_DESIRED_AUTHORITY_AGENTS =
5` agents hold such a quorum (`benchmark_rollout.py:138`). So "3 adopters" is
the authority gate, not the opening gate; rounds switching is the generator
side of authority moving, so they use the same 3.
`test_round_switch_quorum_mirrors_the_agent_rollout_quorum` pins the default to
`SCORING_QUORUM`. One asymmetry: the agent quorum is a constant, the round
quorum is an operator setting.

## Starter kits

Operator policy: **every starter kit (agent and generator) is continuously
improved.** An improvement derived from a miner's submission ships in a
starter kit only **after** that agent or generator has been made public under
the Backroom source-disclosure setting: `get_source_release_policy`
(`apps/backroom/src/server/mcp.server.ts:2536`, Platform
`GET /admin/artifact-release-settings`,
`apps/platform/ditto/api_server/endpoints/admin_artifact_release_settings.py:101`).
That policy is subnet-wide (`disclosure` `public` or `never`, plus
`embargo_hours`), and release is king-only: only a submission that held the
crown and earned winner emissions is ever published, after the embargo. Under
`disclosure = never`, nothing submission-derived ships in a kit. Improvements
the subnet writes itself ship whenever they are ready. The agent starter-kit README is provenance-pinned
(`starter-kit-provenance-v<N>.json`), so this note lands there with the next
kit release rather than as a standalone edit.

Generator source has no public release path yet (generator source is never
served publicly, [generator-competition.md](generator-competition.md)
"Privacy"; `generator_source_releases` is v15 work in
[dueling-bench.md](dueling-bench.md) section 5). Until it exists, no
improvement derived from a miner generator ships in the generator starter kit.

The v13 legacy retry stays for now (operator decision 7). With the window it
only fires while a round still accepts v13 (rounds at 13 or 14); a v15 round
has no baseline to retry on.

## Operator decisions (2026-10-27)

| # | Question | Decision | Where it lives |
|---|---|---|---|
| 1 | One number | **Yes.** Every activatable bench version gets a pack policy of the same number, even with an unchanged grader. | `test_every_activatable_bench_version_has_a_generator_pack_version` |
| 2 | Window length | **Two versions**: a round at N accepts N and N-1. | `challenge.GradingPolicyWindow`, `GRADING_POLICY_WINDOW`, `accepted_grading_versions`; pinned equal |
| 3 | Round switch threshold | **Quorum, symmetrical with the agent rollout: default 3.** | `min_round_validators`; "Round switch quorum" above |
| 4 | Rollout readiness probe | **Open** (below). | Not implemented |
| 5 | Generator scorer policy advertisement | **Explicit.** The scorer sends `grading_bench_versions`; none means v13-only. | `challenge-scorer/6`, `generator_scorer_heartbeats.grading_bench_versions` |
| 6 | Public visibility | **Public.** Each round column's declared policy shows as a quiet badge on the generator board's round results and a generator's round history. | `GeneratorRoundColumnView.grading_bench_version`, dashboard `GradingBadge` |
| 7 | Legacy retry | **Keep for now.** Starter-kit policy recorded above. | `genrun.Evaluate` |
| 8 | v15 amendments | **Accepted.** Sections 1 to 4 of the v15 list above amend [dueling-bench.md](dueling-bench.md). | dueling-bench.md "Accepted amendments" |

## Open questions for the operator

1. **Qualification probes at the new version during a rollout.** While a
   rollout for N is open, should each qualification's probe seed also run at
   N (recording the declared policy and validity, non-authoritatively)? The
   agent competition answers the analogous question by letting the top N
   agents retest into the new bench version (the rollout cohort,
   `rescore_cohort_target`), so a board exists the moment the version takes
   authority. The generator-competition migration story is **not decided**:
   today rounds and qualification stay on the active version until activation,
   so the first rounds at N are also the first evidence of who can emit N.
