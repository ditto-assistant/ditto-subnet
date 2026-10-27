---
name: ditto-subnet-bench-version-bump
description: Ship a new DittoBench contract version (bench_version bump) across every layer without stranding it. Use whenever adding, activating, or auditing a bench version — new contract, version pins, scorer/validator/platform/screener/frontend wiring, generated artifacts, gate postures, and rollout. Also use to audit an EXISTING version for pins that silently exclude it, and for the matching generator pack version (tandem upgrade) that every activated bench version needs.
---

# Bench version bump

A `bench_version` is an **immutable generation + scoring contract**. Adding one is not a
feature flag flip: it is a cross-layer contract change. This repository has stranded a new
version on **every** bump so far, always the same way — one layer kept an old pin.

## The failure mode (read this first)

| Bump | What was missed | Effect |
|---|---|---|
| v10 | `== 9` equality pins across layers | v10 ran **ungated** with legacy fold |
| v11 | validator `SUPPORTED_BENCH_VERSIONS` lagged the scorer | Platform counted **zero** v11-capable validators |
| v12 | `longmemeval/profile.go` exclusion whitelist `!=9 && !=10 && !=11` | every v12 longmem op **failed closed** |
| v12 | confirmation wire `bench_version: Literal[9]` | v12 bundle selected, then **rejected at transport** |
| v9→ | DB CHECK `bench_version = 9` on curve-v3 efficiency | v10+ efficiency rows **500** |

Every one was a *silent exclusion*: the new version is accepted somewhere, refused somewhere
else, and nothing fails loudly until a real run hits the gap.

## Rule: floors and shared constants, never enumerations

```
BAD   if v == 9 ...                    BAD   if v != 9 && v != 10 && v != 11 { reject }
BAD   v in (9, 10, 11)                 BAD   Literal[9]        BAD  z.literal(9)
BAD   bench_version = 9 (DB CHECK)     BAD   [8, 9, 10, 11]    BAD  v <= BenchVersionV11

GOOD  if v >= BenchVersionV9 ...       GOOD  bench_version >= 9 (DB CHECK)
GOOD  supports_confirmation(v)         GOOD  V9EvidenceBenchVersion  (one Literal, derived)
GOOD  SUPPORTED_BENCH_VERSIONS         GOOD  a single exported constant every layer imports
```

When a set genuinely must be enumerated, derive it from **one** source of truth
(`get_args(SomeLiteral)`, an exported Go slice) and import it everywhere — never retype it.

## Orient

```bash
python3 .agents/skills/ditto-subnet-context/scripts/lookup-context.py "bench version bump"
rg -n 'BenchVersionV|SUPPORTED_BENCH_VERSIONS|CONFIRMATION_BENCH_VERSIONS|CurrentBenchVersion'
```

Read `.agents/skills/ditto-subnet-benchmark` for scoring semantics, and
`research/dittobench-datagen/docs/bench-versions.md` for what each version *is*.
A bump is two competitions moving together: read
[`tandem-upgrades.md`](../../../research/dittobench-datagen/docs/tandem-upgrades.md)
before step 10 or any activation.

## The bump checklist

Work top-down. Everything is **additive** and gated `bench_version >= N`; versions below N must
regenerate and re-grade **byte-identically**.

### 1. Contract + generator (`research/dittobench-datagen`)
- `protocol/epoch.go` — `BenchVersionVN`, `datasetEpochVN`, `SupportedBenchVersion`,
  `DatasetEpochForVersion`, `RotateSeedForVersion`, and the error strings listing versions.
- `universe/vN_contract.go`, `gen/vN_surface.go` — the new contract, gated `>= N`.
- Wire into the shared assembly: `gen/artifact.go` (surface pass dispatch), `gen/memory_v2.go`
  (suite/mix + budget carve-outs), `gen/gen.go` (`ProfileForVersion` run-size envelope),
  `datagen/datagen.go`.
- `grade/grade.go` — if grading changes, add a `>= N` policy branch; **never** alter an
  existing version's grading (immutable re-grade contract).
- `docs/bench-versions.md` — document the contract.
- Known-vectors: pin the **new** vector; if any v2..v(N-1) vector moves, your gating is wrong.

### 1b. LongMemEval / confirmation — a PERMANENT DIMENSION of every bench

Not an opt-in per-version feature: every new `bench_version` is expected to run it. Two rules:

- **The execution profile is an INSTRUMENT, not a per-epoch contract.** A new version is confirmed
  by the *already-shipped* profile — no new release asset is required to bump. The bundle's
  `bench_version` is the subject score's epoch; the profile's is the instrument's epoch. Modeling
  it as a per-epoch contract is what has repeatedly made a bump look blocked on a new asset.
  (`test_confirmation_follows_the_live_benchmark` encodes this.) Installing a per-version
  instrument is an optional DEPTH upgrade: the v10+ floors (`V10MinCasesPerCapability=8`,
  `MinHistorySessions=55`, `MinHistoryBytes=400_000`) make it ~4x the cost of the v9 instrument.
- **Sweep the lane as ONE unit — it has failed at ~8 boundaries.** Platform select -> bundle ->
  validator claim -> Go execute -> Python wire converter -> signed evidence root -> ranking ->
  public/Backroom projections. Fixing only the boundary you were pointed at is the failure mode:
  the layer above starts accepting and the next one down still rejects. Trace the whole path and
  grep `bench_version`, `== 9`, `!= 9`, `Literal[9]` across `packages/`, `apps/platform/`,
  `ditto/`, `services/dittobench-api/` before editing.
- Watch the **bit-paired** literals (the signed evidence root on both the validator and Platform
  side) — move both atomically or neither — and any checksum Python computes that Go recomputes
  (`longmem_checksum`, `ablation_checksum`): a hardcoded epoch there causes profile identity drift.
- Bundles ship **OFF**; the ladder is OFF -> SHADOW -> ENFORCE, and ENFORCE is **fail-closed**
  (an unqualified confirmation-capable row is dropped from the authoritative ledger). Never enable
  ENFORCE before qualified bundles exist under SHADOW.

### 2. Scorer (`services/dittobench-api`)
- `internal/scoregates/scoregates.go` — `BenchVersionVN` + `SupportedBenchVersion` **upper bound**
  (a `<= V(N-1)` here silently rejects the new version).
- `internal/v9base/` — evidence assembly, contract thresholds, population.
- `internal/efficiency/efficiency.go` — `ProductionReadyForVersion` case list, `ApplyForVersion`.
- `internal/longmemeval/profile.go` — version acceptance (use a **floor**, not a whitelist).
- `internal/ablation/`, `internal/runner/`, `cmd/dittobench-api/main.go` (the advertised
  `supportedBenchVersions` slice), `relay_preflight.go`, `confirmation*.go`.

### 3. Shared protocol (`packages/ditto-screening-protocol`)
- `bench_v9.py` — `V9EvidenceBenchVersion` is the single Literal; `CONFIRMATION_BENCH_VERSIONS`
  and `supports_confirmation()` derive from it. Bump the **Literal**, not the derivations.
- `confirmation.py`, `confirmation_transport.py` — models must use the alias, not `Literal[9]`.

### 4. Platform (`apps/platform`)
- `db/models.py` CHECK constraints mentioning `bench_version` (efficiency curve, confirmation
  cost, retest/bundle/subject) **+ a matching Alembic migration** (single head — check
  `uv run alembic heads` and `scripts/check_migration_order.py`).
- `db/queries/` — `scores.py`, `score_ranking.py`, `queue_order.py`, `benchmark_rollout.py`.
- `api_server/` — `validator.py`, `efficiency.py`, `confirmation*.py`, `inference.py`.

### 5. Validator (`ditto/`)
- `validator/dittobench.py` `SUPPORTED_BENCH_VERSIONS` — **must** track the scorer's advertised
  set or Platform counts zero capable validators.
- Confirmation transport/signing: keep the v9-named wire labels **stable**; carry the version in
  a field. Renaming the labels is a coordinated validator+platform+scorer break.

### 6. Screener (`workers/screener`)
- Policy/version gates and any anti-emulation fingerprints that name versions.

### 7. Frontend + generated artifacts (the silent ones)
- **Regenerate, never hand-edit**: `apps/backroom/src/generated/platform-api.ts`
  (`apps/backroom/scripts/platform-contract/generate.sh`); both `validator_contract.json`
  copies — `scripts/gen_validator_contract.py` is the **only** generator, and one run
  from Platform writes `ditto/tests/contract/` plus the byte-identical
  `apps/platform/ditto/tests/contract/` mirror:
  `cd apps/platform && uv run python ../../scripts/gen_validator_contract.py`
  (run from the repo root it regenerates from the subnet's *copy* of the models and
  refuses the mirror, so the goldens will not match);
  `services/model-relay/db/schema.sql` (`scripts/gen-schema.sh` + `go tool sqlc generate`)
  whenever a migration lands.
- **Hand-written mirrors drift and CI often will not catch them** — grep and fix by hand:
  - `apps/backroom/src/lib/admin.schemas.ts` (Zod: `z.literal(9)`, review-kind unions)
  - `apps/platform/dashboard/src/types/` (hand-written unions mirrored by `expectTypeOf` tests)
  - any label/enum map keyed by version or by a new enum value.

### 8. Miner rehearsal (otherwise miners keep scoring v(N-1) locally)

- `miners/dittobench-starter-kit/scripts/local-rehearsal.py` `MAX_BENCH_VERSION` on support; the offline-fallback
  `LIVE_SCORING_BENCH_VERSION` on activation (the default itself reads the public rollout `active_version`;
  `DITTO_LIVE_CHECKS=1 python3 scripts/test_local_rehearsal.py` compares the fallback with live)
- `miners/dittobench-starter-kit/src/protocol.rs` `MAX_SUPPORTED_BENCH_VERSION` (accept the new version on `/run`; do not change `ACTIVE_BENCH_VERSION` unless cargo-evaluate should follow)
- `.agents/skills/mine/SKILL.md` live bench number and run-size envelope, plus the starter-kit `README.md`/`AGENTS.md`/`CLAUDE.md` and `docs/MINER.md` live-bench mentions

A kit that 400s the new `bench_version` on `/run` looks like "scoring is broken" to miners.

### 9. Release + CI
- `.github/workflows/release.yml` — the deploy identity gate asserting
  `supported_bench_versions | sort == [...]` **fails closed**; update it with the bump.
- `release/components.toml` — every new path needs a release owner or an `ignored_paths` entry
  (dev tooling included), or `test_release_plan.py` fails.
- Stale "unsupported version" placeholders in tests (`12` used as *the invalid version*) must
  move to `N+1`.

### 10. Generator pack version N (tandem upgrade — required, even with an unchanged grader)

One number for both competitions: bench version N is the agent contract, the generator-round
runtime, **and** pack grading policy N. Every bench version that will be activated ships a pack
policy N in the same contract release, even when `grade/` does not change (grading is by floor,
so policy N grades like N-1). Detail and rationale:
[`tandem-upgrades.md`](../../../research/dittobench-datagen/docs/tandem-upgrades.md).

- `research/dittobench-datagen/challenge/pack.go` — `GradingBenchVersionVN` appended to
  `supportedGradingBenchVersions`, plus any new pack validation rule gated to policy N (v14
  added `v14_rendered_text`).
- `GENERATOR_ROUND_BENCH_VERSIONS` in `ditto/api_models/generator_round_validator.py` (Platform
  and validator copies) gains N. `test_generator_round_contracts_match_pack_grading_and_scorer_mode`
  in `ditto/tests/test_bench_version_pins.py` fails if the tuple and the Go list disagree.
- dittobench-api `generator-round-v1` advertises `generator-round-bench-N`; Platform leases a
  round at N only to validators forwarding N in `scorer_benchmarks.generator_round_bench_versions`.
- Generator scorer (`challenge-scorer`) carries `bench_version` on round and qualification work.
  It must **advertise its supported policies explicitly** (being implemented; until then Platform
  infers from `scorer_version`). A scorer that advertises nothing is treated as **v13-only**.
- `miners/dittobench-generator-starter-kit` declares N when offered
  (`req.NewestAccepted(...)`, rehearse `-round-bench N-1` and `-round-bench N`); bump its
  version, keep the N-1 output byte-identical apart from the version string, add a README
  migration note, and re-register the public default generator after release.
- Do **not** move the request baseline (`challenge.GradingBenchVersion`) in the same release.
  Retiring N-1 is its own later release with 14 days' notice.

Standing decisions (operator, 2026-10-27):

| Decision | Rule |
|---|---|
| Pack version per bench version | Every activated bench version gets a matching pack policy, grader changed or not. |
| Support window | Two policies: the active version and the previous one. Today {13, 14}, baseline 13. |
| Who picks a column's policy | The generator, from the request. Platform never forces one. |
| Legacy retry | Keep the once-only retry on the baseline request (`legacy_retry`) for now. |
| Round switch | Rounds move to N only once a **validator quorum** can run N, symmetrical with the agent rollout quorum. The quorum gate is `min_round_validators` (being implemented); read the live value from Backroom `preview_generator_round` before assuming it. Until the quorum is met, rounds fall back to N-1 with a recorded reason. |
| During an open rollout | Rounds and qualification stay on the active version; they pay generators. |
| Qualification probes at N during rollout | **Open.** The agent side lets the top agents retest into the new version so a board exists at activation; the generator migration story is undecided. Do not build or assume one without an operator decision. |

## Validation ladder

```bash
cd research/dittobench-datagen && go build ./... && go test ./...   # v2..v(N-1) vectors MUST NOT move
cd services/dittobench-api    && go build ./... && go test ./...
cd apps/platform && uv sync --locked --group dev --reinstall-package ditto-screening-protocol
uv run pytest ditto/tests/validator ditto/tests/contract -q
cd apps/backroom && pnpm install --frozen-lockfile && pnpm run check && pnpm test
```

**Stale venv is a known trap**: `apps/platform`, the repo root, and the screener each hold their
own built copy of `ditto-screening-protocol`. Reinstall before testing or regenerating contracts,
or you will validate against old code and regenerate a bogus artifact.

## Calibrate before you enforce

A new gate multiplies into the composite: a false positive **zeroes an honest miner**.

- Run the new version against **real cleared top-of-board agents** before activation.
  Use `$ditto-subnet-preview` / `localstack/phase1/` (scored v12, gates firing).
  Sessionless `SCORED=0` is the v12 *dataset* only; it does not fire
  `model_dependence`. See
  `.agents/skills/ditto-subnet-preview/references/localstack.md`.
- v12 caught exactly this: the answer-stuffing gate at `enforce` **false-zeroed the genuine
  champion** while missing the real exploit. Fix was a **graduated, capped, never-zero penalty**.
- Prefer, in order: `review` (flag only) → `penalize` (graduated, capped) → `enforce` (zero).
  Reserve `enforce` for signals that are *provable*, not merely suspicious. Make the posture
  env-configurable and default to the safe end.
- A gate that cannot distinguish an honest pattern from a cheating one belongs in **source
  review**, not the scoring path.

## Ship it

Pre-activation first. `CurrentBenchVersion` stays put; the new contract is generated, scored, and
**observed** against the live fleet before it sets weights. Sequence: deploy scorer (advertises
the new set) → validators advertise → Platform shadow rollout target → calibration gate on
genuine champions → activate. Fail-closed gates (LongMem confirmation enforce) stay **off** until
their evidence is actually qualifying, or the board empties.

Never enforce retroactively (the bunny ruling): fix the gate forward.

## Activate (both competitions)

**Never call `start_benchmark_rollout` without explicit operator approval in this conversation**,
and only after the version's qualification gates (for v14,
[`benchmark-v14-rollout.md`](../../../docs/benchmark-v14-rollout.md) "Open activation blockers")
are closed or waived by the operator. The full ordered runbook is "Runbook: activating v14 for
both competitions" in
[`tandem-upgrades.md`](../../../research/dittobench-datagen/docs/tandem-upgrades.md); in short:

1. **Release is live, layer by layer** (`$ditto-subnet-release-ops`): Platform
   `preview_generator_round` lists N in `bench_contract.round_contracts`; Backroom
   `get_generator_scorer` shows the **deployed** generator scorer build supports N (and the
   governing `cases_per_seed`); `get_validator_fleet` shows validators forwarding N in
   `generator_round_bench_versions`.
2. **Default generator**: package the bumped generator starter kit and register it
   (`create_default_generator_upload`, `register_default_generator`).
3. **Announce** to generator miners at least 7 days before activation (template in the doc).
4. **Readiness**: `preview_generator_round` `capable_validators["N"]` meets the round quorum.
5. **Open the agent rollout** only on approval. Rounds stay on N-1 while it is open.
6. **After activation**, verify in order: `preview_generator_round` (active N, bench N, no
   reason); the first round opened after (`get_generator_round`: `bench_version` N, per-column
   `grading_bench_version`, `legacy_retry` count); round panel leases only to validators reporting
   N (`list_validator_assignments`); the next qualification run (`get_generator_run`).
7. **Watch** the first three rounds at N: column validity versus the last three at N-1, the
   `legacy_retry` count falling, and new-policy validation rejections at qualification.
8. **Rollback** sends the next round back to N-1; void an in-flight round at N whose validators
   were drained (`void_generator_round`).

Bump the miner rehearsal live fallback and the `mine` skill's live bench in the same activation
PR (checklist step 8; `test_miner_docs_quote_the_rehearsal_live_fallback` enforces it).

### Generator scorer bounds and reinstatement (lesson from 2026-10-27)

Before raising generator `cases_per_seed` (or activating a version whose rounds need more cases),
confirm the **deployed** generator scorer build supports it with Backroom `get_generator_scorer`
— not the merged source. A stale scorer still capped at 64 cases voided a round and wrongly
rejected six generators; the fix was redeploying the scorer and reinstating them. Reinstate a
rejection that was not the generator's fault with `reinstate_generator_submission` (confirmation
`REINSTATE GENERATOR SUBMISSION`, `expectedStatus: rejected`, the exact `expectedStatusReason`).
It is blocked when the hotkey has a newer upload or another active submission; check
`get_generator_submission` `reinstate_blockers` first.

## Starter kits

Agent and generator starter kits are improved continuously, not only at bumps. An improvement
derived from a miner's submission ships only after that submission is public under the Backroom
source-disclosure setting (`get_source_release_policy`, `get_agent_source_release_evidence`).
Never name the miner, hotkey, agent or generator in the kit or its commit.
