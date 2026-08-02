# DittoBench v7 difficulty contract (~10x)

Benchmark version 7 is the difficulty release: the datagen suite gets ~10x
harder (more cases, denser haystacks, more adversarial paraphrase — tracked in
`dittobench-datagen`) and this validator's scoring gets strictly harder. This
document is the validator-side half: what changed, why each change is safe for
v2–v6, and the measured difficulty identity.

Everything is gated on `bench_version >= 7` (`protocol.BenchVersionV7`). No
wire type changed; the only wire difference v7 harnesses see is the additive
`bench_version` field on `RunRequest` that has shipped since the v6
byte-compatibility work (#68).

## Difficulty target

Naive/pattern-matching strategies must score roughly an order of magnitude
lower under v7 scoring than under v6, while a correct oracle response set
still scores full marks.

Measured identity, pinned by `TestV7DifficultyNaiveVsOracle`
(`internal/scorer/v7_test.go`) through the same scoring pipeline
`runSizeJob` drives:

| strategy | v6 practice composite | v7 practice composite | ratio |
| --- | --- | --- | --- |
| naive parser (self-reported trajectories, decoy-grepped answers) | 0.475 | 0.0375 | **12.7x lower** |
| oracle (observed execution, correct trajectories, needles incorporated) | 1.000 | 1.000 | 1.0x |

The naive fixture is the deterministic-parser archetype the anti-cheat work
targets: six observable tool cases answered with a plausible self-report and
no `tool_endpoint` execution, plus two result-usage cases where a number was
grepped out of the wrong tool's payload (the served decoy).

End-to-end companion on REAL generated suites
(`TestV7DifficultyOnGeneratedToolSuites`, `cmd/dittobench-api`), medium tool
suites from the hardened datagen (seed 123456789), measured at datagen
`fe1835e` (`v0.11.3-0.20260724115621-fe1835e69bff`, the pinned iteration-2
deepened head — the profilesV7 full suite is now Tools 84 / Mem 185, ~282
cases; earlier drafts measured 2.8x at `e1dfbbb` and 3.4x at `a91fc9b`):

| strategy | v6 suite + v6 scoring | v7-hard suite + v7 scoring | ratio |
| --- | --- | --- | --- |
| naive parser (best-case self-reports, fails the negation trap) | 0.600 | 0.190 | **3.2x lower** |
| oracle | 1.000 | 1.000 | 1.0x |

The suite-level ratio is smaller than the lever-level 12.7x because
abstention/no-tool and memory-routing cases legitimately reward inaction in
both contracts (the exact ratio drifts with the datagen mix as the suite is
deepened); on the observable slice — where the parser's free credit lived —
the collapse is the full 10x (0.5 → 0.05 per case, traps at 0). The
memory-axis hardening is measured in the datagen module's own v7 difficulty
tests.

The v2–v6 benchmark evidence in
`docs/third-party-benchmark-timeline/` (#76, native Hermes and OpenClaw
harnesses on OpenRouter/Nebius) is the calibration backdrop: competent honest
harnesses execute through `tool_endpoint`, self-report faithfully, and
incorporate served needle values, so none of the v7 levers below move an
honest run that was already scoring on observed execution.

## The v7 levers (validator side)

Per-case (`internal/scorer/v7.go`, wired in `cmd/dittobench-api/main.go`):

| lever | v2–v6 | v7 |
| --- | --- | --- |
| observable-but-unobserved ceiling (practice) | 0.5 | **0.05** (scored scope stays 0) |
| result-usage composition | `0.4·traj + 0.6·usage` | **`traj × gate`**: needle 1.0 / miss 0.1 / decoy 0.0 (whole case) |
| self-report vs observed trajectory mismatch | unscored (observed replaces self-report) | **×0.5** on the case (non-empty disagreeing self-report) |
| forbidden argument on an expected tool | dents arg precision | **case scores 0** |
| hop order on ordered multi-hop | scales the 0.2 trajectory term | **multiplies the whole score** |
| extra-call / over-budget penalty | `extras/expected` | **doubled** |

Composite gates (`compositeGateV7`):

| gate | v3–v6 | v7 |
| --- | --- | --- |
| tool-efficiency free overshoot / saturation / max | 1 / +5 / 15% | **0 / +3 / 40%** (gate score 0.5 → 0.6) |
| memory over-call max | 10% | **25%** |
| metamorphic split max | 15% | **40%** |
| bounded-product floor | 0.75 | **0.40** |
| conversational-sanity floor | 0.5 | **0.25** |
| canary leak multiplier | 0.5 | **0.25** |
| transform audit | observational (env-gated), 15% | **enforced by contract, 40%** |

The transform audit keeps its directional key (base-only minus transform-only
splits ≥ the 4-pair minimum), so symmetric honest noise still gates 1.0 — the
2026-07-18 calibration separation argument is unchanged; v7 simply stops
shipping the gate dark.

Token contract: v7 is QUALITY-ONLY. Token usage is audited and reported
first-class (`details.token_usage`, broker accounting, and a neutral
`token_efficiency` record with formula `v7-quality-only-v1`) but never moves
the v7 composite — a deterministic validator scores the same artifact
identically regardless of when it runs. v5/v6 keep their absolute 10%-max
transform byte-for-byte. Efficiency incentives live in the platform layer as
an epoch-frozen relative bonus (`docs/relative-efficiency-bonus-spec.md`);
the reviewed aggregate GPT-OSS manifest remains the v7 identity/readiness
anchor and the audited reference evidence.

Admission and activation: the admission gate for v7 is model + route/profile +
trusted accounting, NOT a reviewed token baseline (none is needed when usage
cannot move the composite); `scoring_enabled` stays false permanently.
`scoring_enabled=false` is the quality-only contract, and the embedded manifest
is production-ready in that state. There is no validator-side activation flag:
the validator ADVERTISES v7 iff it is technically ready
(`efficiency.ReadyForV7QualityOnly`), exactly as v5/v6 advertise on their
reviewed manifests. Whether v7 is dispatched/scored is the platform's normal
benchmark rollout (active bench, controlled via backroom); the validator scores
whatever bench the platform sends in the run request, and rolling the active
bench back to 6 rolls v7 back with no validator change. Full contract and gate
trace: `docs/token-efficiency-v7.md`.

## Operational envelopes for the ~10x datasets

Client-side only; no wire bytes change:

- per-case `/run` deadline: 120s → 5m for v7 (`DITTOBENCH_V7_CASE_TIMEOUT`).
- `/seed` deadline: ≥ 15m for v7 (`DITTOBENCH_V7_SEED_TIMEOUT`; a larger
  `DITTOBENCH_SEED_TIMEOUT` still wins).
- sandbox caps stay 3g/512m by default (frozen v2–v6 envelope) and are
  overridable via `DITTOBENCH_SANDBOX_MEMORY_LIMIT` /
  `DITTOBENCH_SANDBOX_TMPFS_LIMIT` for v7 rollout (suggested 6g / 2g).

## Backwards-compatibility evidence

- Wire: `TestRunCaseSendsBenchVersionOnlyForV7Plus` (runner) pins that v2–v6
  `RunRequest` bytes are unchanged; `TestSeedForVersionWireIdentity` pins that
  the v7 seed path marshals byte-identical `/seed` requests.
- Scoring: `TestScoreToolCaseObservedForVersionPreV7Identity`,
  `TestComposeResultUsageForVersionPreV7Identity`,
  `TestCapUnobservedForVersion`, `TestApplyForVersionPreV7Identity`
  (efficiency) pin that every versioned entry point delegates byte-identically
  for versions ≤ 6; the pre-existing v2 (`TestAggregateForVersionV2IsUngated`),
  v3/v4 gate, v5 ship-gate, and replay suites pass unchanged.
- The v5/v6 gate stack in `CompositeGateForVersion` is untouched for
  `benchVersion < 7`; v7 routes to `compositeGateV7`.

## Datagen v7-hard integration status (2026-07-23, integrated)

The hardened datagen suite (branch `v7-difficulty` of `dittobench-datagen`,
sibling worktree `../datagen-v7-hard`) went green and this repo is now built
against it via the `go.mod` replace directive marked
`// TEMPORARY: swap for tagged release before merge`. Integration findings:

- Full suite passes with the replace active; `gofmt`/`go vet` clean.
- Only allowed drift observed: the v7 known vector moved to
  `1aa1ad26d6b5285258128f5e4a222a150e6d3781411ffdc982f0e336f8e1ee94`
  (historically pinned by the now-retired token baseline tooling). The v2/v3 vectors pinned by
  `TestBenchVersionDatasetVectors` and every v5/v6 identity test are
  unchanged.
- New v7 tool categories (`negation_no_tool`, `stale_context_web`,
  `link_chain_result_usage`, `job_chain_recovery_result_usage`) route
  correctly with no API change: `datagen.IsResultUsage` is suffix-based and
  `toolexec.Observable`/the fixture serving gate handle the dependent
  link-chain (search_web serves a per-case page URL; read_links reveals the
  needle only when called with it) inside the datagen module the validator
  already drives. New memory modules (deepchain, deepjoin, near-miss
  abstention, tempcalc, composedinj) grade through the public
  `dittobench-datagen/grade` path the scorer already uses. The MemorySuite
  telemetry counters are additive Go fields only.
- `internal/efficiency` remains internally consistent: the embedded reviewed
  manifest and `v7DatasetKnownVector` still pin the pre-hardening vector
  `1cfc6e3b…f58fcf`, so a manifest refreshed under the hardened datagen is
  correctly rejected by `ReadyForV7Production` until the real recalibration
  campaign produces a reviewed replacement (v7 scored rollout stays gated on
  that review, as designed).

Remaining pre-merge steps:

1. Tag the datagen `v7-difficulty` release and swap the replace for the
   tagged `require` version (`go mod edit -dropreplace …` + version bump).
2. Update `v7DatasetKnownVector` and the capability identity to the
   tagged release's v7 vector (the identity anchor for readiness and
   metering trust; no token budget depends on it under the quality-only
   contract).

## Rollout sequence (token contract)

The 60-run calibration workflow described by the original plan is retired.
V7+ ships with the scorer token penalty disabled and trusted usage recorded;
the platform observes and then enables its model-use-gated, epoch-frozen
relative efficiency bonus (`docs/relative-efficiency-bonus-spec.md`).

## Reviewer watch-items

- The v7 per-case fixtures here measure the validator-side levers; the memory
  half of the ~10x target rides the datagen v7-hard suite and should be
  re-measured end-to-end once that module is tagged.
- The v7 strict order gate is harsh (a fully reversed multi-hop scores 0);
  if honest-model calibration shows excessive false positives, soften to a
  floor.
- The transform audit is contract-enforced for v7 without the
  champion-population false-positive calibration the v5 plan wanted before
  enabling the env flag — deliberate (new contract), worth a calibration
  pass.
- The former calibration-transfer tooling was never wired into scoring and has
  been removed. Do not rebuild it for v8; relative live cohorts are the
  successor contract.
