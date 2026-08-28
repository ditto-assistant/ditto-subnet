# SN118 submission contract — platform & validator enforcement

What a miner submits, and which invariants the **platform**, **screener**, and
**validator** actually enforce — including, transparently, what is *not* enforced
yet.

The canonical *miner-facing* contract (what's in the tarball, the fixed HTTP
interface vs. the free-to-edit surface, the wire shapes) lives with the harness:
the [dittobench-starter-kit](https://github.com/ditto-assistant/dittobench-starter-kit)
`README.md` + `PROTOCOL.md`. This doc is the **enforcement side** — what we check,
where, and what's deferred.

## The artifact

A single **gzipped tarball of the miner's whole harness crate** — the entire
buildable project with a `Dockerfile` at the tarball root. Not a single source
file, and not `ditto-harness` (that's a pinned git dependency the crate builds on
top of). The platform stores the tarball in object storage keyed by agent id and
**never unpacks it**; the screener and validator do.

## What's enforced today

| Stage | Where | Check |
| --- | --- | --- |
| **Upload** | `endpoints/upload.py` (`/api/v1/upload/*`) | On-chain eval-fee payment verified (replay-protected); tarball ≤ **20 MiB** by default (`MAX_TARBALL_SIZE_BYTES`, overridable with `DITTO_MAX_TARBALL_SIZE_BYTES`) enforced from the *actual streamed bytes*; **SHA-256 re-verified** against the miner's claim; one payment per upload; **hotkey-level ban enforced** — `/upload/agent` returns a hard 403 right after the signature proves hotkey ownership and before any chain/payment/storage work, `/upload/check` reports it in the dry run so a banned miner learns it before spending TAO, and `/retrieval/agent-by-hotkey` surfaces it as `banned`. Active bans live in the `banned_hotkeys` table. Backroom can inspect the complete active list and remove one exact ban with a timestamp guard, operator reason, exact confirmation, and append-only audit; agent-level bans remain unchanged. |
| **Screen** | `endpoints/screener.py` (`/api/v1/screener/*`) + public `ditto-screener` source | The screening core — `SCREENING_POLICY_VERSION` (currently **10**, in `ditto-screening-protocol`; the platform advertises the required version and screeners refuse to claim work at any other) — verifies the bounded archive and SHA-256, enforces the root Rust package contract, builds the image, and requires `/health` with no default `/run` assertion. Policy v10 additionally binds one I1-I7 invariant sweep into every source-review finding. Its lease-bound signed verdict maps pass → `evaluating`, deterministic fail → `rejected`, and retryable infrastructure failure → `screening_failed`. Private timing, random-control, fingerprint, and behavioral signals can only pass or route to quarantine/inconclusive review; no model finding automatically rejects. The screener is **platform-operated** and authenticates with a dedicated allowlisted hotkey plus bearer token, not a validator permit. |
| **Evaluate** | `dittobench-api` (mode B) | Fetch the presigned tarball; safe-extract with zip-slip + gzip-bomb guards; require a `Dockerfile` at the tarball root (or a single top-level dir); `docker build` + run the container; drive `GET /health`, `POST /seed`, `POST /run`; score. |
| **Anti-overfit** | `dittobench-api` datagen | A **fresh seed per run** (stratified categories); the miner cannot see or pin the dataset. Difficulty variance is calibrated to a between-seed stddev ≤ 0.03. |

So the effective bar today is: **the hotkey is not banned, payment is valid, the
tarball is within limits, the crate builds, and the running container speaks the
`/run` protocol well enough to be scored.**

## What is deferred — NOT enforced yet

Per `CLAUDE.md`, several `/upload/*`-adjacent validations are intentionally
deferred pending the harness-interface spec and supporting tables. Stated plainly
so miners and reviewers aren't misled:

- **tar manifest** format validation (a declared file/entrypoint manifest);
- **import / dependency allowlist** (what the crate may pull in);
- **schema diff** — verifying the crate still implements the required harness
  interface rather than just building.

The screener's build gate is the first real guard. The manifest + allowlist +
schema checks are the planned next layer; until they land, "it builds and serves
the protocol" is the whole bar, and a submission is trusted to be a good-faith
harness crate.

## Lifecycle

```
uploaded ──screener claims lease──▶ screening ──pass──▶ evaluating
                                        │                   │ validator score
                                        │                   ▼
                                        │                 scored ──▶ live
                                        │                   │
                                        │                   └──ATH / anti-copy
                                        │                        signal──▶ ath_pending_review
                                        ├──deterministic fail──▶ rejected (terminal)
                                        ├──infra fail / lease expiry──▶ screening_failed
                                        │                               (retryable: a new
                                        │                                claim returns it
                                        │                                to screening)
                                        └──private-signal hit──▶ quarantined

(banned is terminal and is per-agent — distinct from the hotkey-level ban in
`banned_hotkeys`, which blocks all of that miner's future uploads.)
```

Operator review resolves `quarantined` and `ath_pending_review` forward to a
cleared state or back to `rejected`; neither is a dead end, and both are held
out of the public leaderboard and source release while open. The enum also
carries `screening_passed`, which the platform never writes on this path — a
pass goes straight to `evaluating`; it is only tolerated on the read side.

The status column is the shared `AgentStatus` enum — canonically defined in
`ditto-screening-protocol` (`ditto_screening_protocol/models.py`) and
re-exported by `api_models/agent_status.py`, so platform, screener, and
validator cannot drift. Transitions are owned by `endpoints/upload.py`,
`endpoints/screener.py`, `endpoints/validator.py`, and the operator-review
endpoints (`admin_quarantine.py`, `admin_copy_review.py`).

## Scoring

`composite = (0.5 * tool_mean + 0.5 * memory_mean) * gate` when both kinds are
present (the `0.5 / 0.5` split dates to bench_version 2 / DittoBench v2 —
rebalanced from v1's `0.6 / 0.4` because memory is the core product value and
the raw-pairs seeding tier makes `memory_mean` the harder axis).

**The gate is not optional and is why your arithmetic will not match your
score.** Since v3 the mean is multiplied by a bounded composite gate
(`CompositeGateForVersion`, dittobench-api `internal/scorer/`): tool-efficiency,
metamorphic-consistency, and memory-over-call factors multiplied together and
floored, then a canary-integrity factor, with v5 adding conversational-sanity
and transform-audit tiers and v7 re-tuning every one of them. An honest harness
scores a gate of 1.0 and sees the plain mean; the gate only bites on wasted
calls, inconsistency under transform, over-calling on pure-memory questions, or
a leaked canary. Only a single kind present collapses the composite to that
kind's mean, still gated.

The platform **records what the validator reports and never recomputes it**
(`api_models/validator.py`, `db/queries/scores.py`). Grading is **fully
deterministic and judge-free** — a pure function of the dataset and the
transcript, reproducible by anyone from the public dittobench-datagen module.
There is no LLM judge. The on-chain profile is `run_size=full`; the starter
kit's local `practice` loop uses the *same* deterministic grader, so a miner's
real score differs because of the **dataset**, not the scoring: `practice`
re-rolls from a small fixed template pool and never exercises the seeding
tiers/waves or the real question mix, while the validator generates a fresh
full-size anti-cheat dataset per submission.

Benchmark scoring changes are versioned: the validator stamps `bench_version`
in the score `details`, and the weight fold only compares scores of the max
version present (see the DittoBench v2 design). A version bump triggers a
re-score sweep before old scores are compared to new.

## Pointers

- **Miner-facing contract + wire protocol** — dittobench-starter-kit `README.md`
  ("Submit") + `PROTOCOL.md`.
- **Upload** — `ditto/api_server/endpoints/upload.py`.
- **Screener protocol and state transitions** — `ditto/api_server/endpoints/screener.py`.
- **Platform-operated build/run worker (public source)** —
  `ditto-assistant/ditto-subnet/workers/screener`.
- **Tarball ingest + Docker sandbox (mode B)** — `dittobench-api`
  `internal/sandbox/` (`Dockerfile`-at-root build-context rule, safe extractor).
- **Validator deploy** — infra `docs/validator-deploy.md`.
- **End-to-end diagram** — `docs/validator/subnet-architecture.mmd`.
