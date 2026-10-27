# Tandem upgrades: agent bench versions and generator pack versions

Status: design and runbook. Nothing here activates a version or changes a live
setting. The v14 pack contract it builds on shipped in
[generator-competition.md](generator-competition.md) ("Bench contract").

SN118 runs two competitions on one benchmark:

- **Agents** are scored on a `bench_version` (v13 active, v14 built). A version
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
| Identifier | **One version number for both competitions.** Bench version N is the agent contract, the generator-round runtime contract, and pack grading policy N. Every activated bench version gets a pack policy N, even when its grader is unchanged (grading is by floor, so policy 15 grades like 14 unless v15 changes `grade/`). |
| Compatibility | Derived, never a hand-kept matrix. A run or round at version N accepts packs at any **supported** policy at or below N (`challenge.GradingBenchVersionsThrough(N)`). Each column is graded at its pack's declared policy. |
| Support window | **Two policies: the active version and the one before it.** A policy retires only by a release that raises the request baseline (`challenge.GradingBenchVersion`), with notice (below). Today: {13, 14}, baseline 13. |
| Who chooses a column's policy | **The generator**, deterministically from the request (`req.NewestAccepted(supported)`). Platform never forces a policy. A generator that cannot or does not upgrade keeps emitting the baseline. |
| Failure on a newer request | **Retry once on the baseline request** (`legacy_retry`). A working generator never loses its column to an upgrade. |
| What a round freezes | Bench version (runtime), columns, panel, column size at open. Each column's declared policy is recorded at generation. |
| Rounds during an open rollout | **Stay on the active version.** Rounds pay generators, so they run on the authoritative contract. Rollout readiness on the generator side is measured, not paid (below). |
| At activation | **Rounds flip at their next opening**, once at least one validator can run a round at the new version; until then they fall back with a recorded reason. An in-flight round finishes on its frozen version. |
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
| Generator scorer (challenge-scorer) | `scorer_version`, case bounds on every poll (#3204) | `generator_scorer_heartbeats` | Not refused; it sends baseline requests |
| Generator | The policy it declares in each pack | `generation.columns[].grading_bench_version`, qualification seed outputs | Baseline (v13) |

Rules:

- Platform leases a round at version N only to a validator whose
  `generator_round_bench_versions` contains N and whose scorer is verified for
  N (`heartbeat_supports_version`). A v13 round keeps the V13 scorer-cohort pin.
- The generator scorer needs no version capability for correctness: an old one
  ignores `bench_version` on its work and sends baseline requests, which every
  newer round accepts. It reports `challenge-scorer/5` or later when it offers
  newer policies. Add a `grading_bench_versions` poll parameter when a second
  generator scorer build is ever live at once (open question 5).
- Generator readiness for version N is the share of counted columns that
  declare N. Platform does not gate on it.

## Notice windows

| Event | Notice to generator miners | Notice to agent miners | Channel |
|---|---|---|---|
| Pack policy N merged (rehearsable with `-round-bench N`) | Same day: what changed in grading, the new validation rules, "no action required" | None | Discord announcement, starter kit README |
| Rollout for N opens | Planned activation window | Existing rollout notice | Discord |
| Activation of N | At least 7 days after the policy merged | Existing activation notice | Discord, Backroom record |
| Retiring policy N-1 (baseline moves to N) | At least 14 days, naming the release | None | Discord, starter kit README |
| Changing `cases_per_seed` or `MaxCases` | Existing rule: announce before applying | None | Discord |

Retiring a policy is the only step that can strand a generator, which is why it
has the longest notice and its own release.

## Dual support and the upgrade sequence

The overlap period is the support window: from the release that adds policy
N until the release that retires N-1, both competitions accept N-1 and N.

```
            release adds N         rollout opens      activation          release retires N-1
agents      N-1 active             N-1 + N scored     N active            N active
            (N served, inactive)   (cohort on N)      N-1 kept 1 window   N-1 no longer served
rounds      N-1                    N-1                N (next opening)    N
packs       N-1 only requested     N-1 only           N-1 or N            N only
qualif.     N-1                    N-1                N-1 or N            N
```

What `start_benchmark_rollout` drives on the generator side:

1. **Rollout opens (desired N).** Nothing changes for rounds or
   qualification: both follow `round_contract_for(active)`. The operator
   watches readiness with `preview_generator_round` (`bench_contract`,
   `capable_validators` per contract) and generator rehearsals. A follow-up
   (open question 4) can run each qualification's probe seed at N as a
   non-authoritative readiness signal.
2. **Activation (active N).** The next round to open freezes N when a
   validator can run it, else N-1 with the reason. Qualification requests
   policies through N from then on. No setting is touched.
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
5. **Eligibility reads round columns at any accepted policy.** A column scored
   in a v14 round (v13 or v14 policy) is eligible for a v15 season, since
   policy 13 and 14 both satisfy `<= 15`. Its measured solvability is under
   its declared policy, which the season pins, so solvability carries over.
6. **Pack policy 15.** Add `GradingBenchVersionV15` to
   `supportedGradingBenchVersions` and 15 to `GENERATOR_ROUND_BENCH_VERSIONS`
   in the v15 contract release, even if `grade/` is unchanged. The pin test
   enforces the pair. When 15 activates, the window is {14, 15} and the
   baseline moves to 14 only in a later, announced release.
7. **Rollback from 15.** Seasons stop at the next season open; canonical runs
   return to the v14 datagen-only profile; rounds return to 14. Generators that
   declared 15 re-choose 14 from the v14 request. Accepted v15 rows stay
   versioned and are not reinterpreted.
8. **Scorer capability.** dittobench-api advertises `dueling-slice-v1` and a
   per-version feature (`dueling-slice-bench-<n>`), forwarded like
   `generator_round_bench_versions`, so Platform never leases a v15 run with
   slices to a scorer that cannot grade the declared policies.

## Runbook: activating v14 for both competitions

Preconditions: the agent-side v14 qualification gates in
[benchmark-v14-rollout.md](../../../docs/benchmark-v14-rollout.md) are closed
or waived by the operator. Steps 1 to 4 change no live setting.

1. **Release.** Confirm the release containing the v14 pack contract is
   deployed: Platform (`preview_generator_round` returns `bench_contract` with
   `round_contracts: [13, 14]`), the generator scorer (`get_generator_scorer`
   shows `challenge-scorer/5` or later), and validators (`get_validator_fleet`:
   v14-verified validators report `generator_round_bench_versions` containing
   14).
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
   validators that will run v14 rounds after activation. Do not activate with
   `capable_validators["14"] == 0` unless rounds falling back to v13 (with a
   staffed V13 cohort) is accepted.
5. **Open the agent rollout** (`start_benchmark_rollout`, desired 14,
   confirmation `START BENCHMARK V14`). Rounds and qualification stay on v13.
   Re-check step 4 while the cohort scores.
6. **Activation.** The rollout activates when it earns desired authority. Then
   verify, in order:
   - `preview_generator_round`: `active_version` 14, `bench_version` 14, no
     reason. A reason naming v14 means no v14 round validator yet; rounds keep
     running at v13 until one appears.
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
9. **Later: retire v13 packs.** Not part of activation. When the operator
   decides, ship a release that moves the baseline to 14, announced 14 days
   ahead. Until then, v13 packs stay valid.

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

Retirement (later): name the release, the date, and the one-line fix
(rebuild against the current package and declare v14).

## Open questions for the operator

1. **One number.** Confirm that every activated bench version gets a pack
   policy of the same number, even with an unchanged grader. (Recommended.)
2. **Window length.** Two policies (active and previous), or keep three for
   one extra version? (Recommended: two, with the retirement notice.)
3. **Round switch threshold.** Rounds switch to the new version with one
   capable validator today. Require more (for example, enough to run the whole
   panel within the round)? A `min_round_validators` setting would make this
   explicit. (Recommended: add it before v15, keep 1 for v14.)
4. **Rollout readiness probe.** While a rollout is open, run each
   qualification's probe seed at the desired version and record the declared
   policy and validity, non-authoritatively? (Recommended for v15, optional for
   v14.)
5. **Generator scorer policy advertisement.** Add `grading_bench_versions` to
   the generator scorer's poll so Backroom shows it per scorer, instead of
   inferring it from `scorer_version`? (Recommended when more than one scorer
   build can be live.)
6. **Public visibility.** Show each round column's declared policy on the
   public generator board (`GeneratorRoundColumnView`), or keep it in Backroom
   only? (Recommended: public; it is not sensitive and helps miners see the
   upgrade.)
7. **Legacy retry after the window.** Keep the baseline retry permanently, or
   drop it when a policy retires? (Recommended: keep; it only ever retries the
   current baseline.)
8. **v15 datagen base and season policy pinning.** Confirm sections 1 to 4 of
   the v15 list above amend dueling-bench.md (slices graded at the pack's
   declared policy, policy pinned per season, shadow seasons during the
   rollout).
