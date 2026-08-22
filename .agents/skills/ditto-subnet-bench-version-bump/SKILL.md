---
name: ditto-subnet-bench-version-bump
description: Ship a new DittoBench contract version (bench_version bump) across every layer without stranding it. Use whenever adding, activating, or auditing a bench version — new contract, version pins, scorer/validator/platform/screener/frontend wiring, generated artifacts, gate postures, and rollout. Also use to audit an EXISTING version for pins that silently exclude it.
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

- `miners/dittobench-starter-kit/scripts/local-rehearsal.py` `LIVE_SCORING_BENCH_VERSION` and `MAX_BENCH_VERSION`
- `miners/dittobench-starter-kit/src/protocol.rs` `MAX_SUPPORTED_BENCH_VERSION` (accept the new version on `/run`; do not change `ACTIVE_BENCH_VERSION` unless cargo-evaluate should follow)
- `.agents/skills/mine/SKILL.md` live bench number

A kit that 400s the new `bench_version` on `/run` looks like "scoring is broken" to miners.

### 9. Release + CI
- `.github/workflows/release.yml` — the deploy identity gate asserting
  `supported_bench_versions | sort == [...]` **fails closed**; update it with the bump.
- `release/components.toml` — every new path needs a release owner or an `ignored_paths` entry
  (dev tooling included), or `test_release_plan.py` fails.
- Stale "unsupported version" placeholders in tests (`12` used as *the invalid version*) must
  move to `N+1`.

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
