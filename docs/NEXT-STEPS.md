# Ditto SN118 — Production-Readiness Roadmap

**As of 2026-07-02.** Audience: the next engineer/agent picking up SN118. This is
the **authoritative roadmap** and supersedes the "what's left" sections of
[`STATE-OF-THE-SUBNET.md`](STATE-OF-THE-SUBNET.md) and any earlier `NEXT-STEPS`
draft. Goal: take Subnet 118 from a working dev-chain walking skeleton to a
**production-ready Bittensor subnet on finney**.

> **Ownership: we own the entire subnet, end to end.** Every mechanic is ours to
> build, tune, and change — the miner CLI, the platform API, the screener, the
> validator + weight fold, the dittobench scoring engine, the chain parameters,
> and the emission economics. There is **no external team to hand pieces to** and
> **no upstream constraint we can't change**. Where earlier docs deferred a knob
> to "the team" or treated emissions / the scorer / chain config as givens, those
> are now **direct levers we control**. Plan accordingly: the only real
> dependencies are external *services* (the subnet owner UID staked to the permit
> threshold, Pylon write creds, an OpenRouter key) — not other people.

> **TL;DR — where we are.** The end-to-end pipeline
> (miner → platform → screener → validator → dittobench → chain) is plumbed and
> the validator is **live on the dev localnet** (uid 4, netuid 3). The incentive
> mechanism (**KOTH / winner-take-all + ATH gate**) and the critical
> weight-ingestion fix just **merged** (platform PR #10, subnet PR #22): weights
> now come from a persistent best-score ledger, so a scored agent keeps its
> emission instead of being zeroed after one epoch. What stands between us and
> production is not net-new architecture — it's (1) a **first real end-to-end
> scoring run** with the non-mock scorer ✅ done, (2) **multi-validator consensus**
> (k=3 + median), (3) **hardening** (cost caps ✅, sandbox egress, plagiarism at the
> content level ✅), (4) **observability + ops**, and (5) the **testnet → finney
> migration** (emission already flows on localnet; runs under the owner UID).

---

## 0. How to work in this codebase

### The repos (four + infra)

| Repo | Role | Language |
| --- | --- | --- |
| **`ditto-platform`** | The API server: upload, screener, validator & scoring endpoints, the score ledger, anti-copy gate, Postgres. The **contract** (OpenAPI). | Python / FastAPI |
| **`ditto-subnet`** | The **validator daemon** (`python -m ditto.validator`) + the **miner CLI**. Owns weight-setting, KOTH+ATH fold, signing, chain I/O. | Python |
| **`dittobench-api`** | The **scoring engine**: `docker build`s a submitted harness in a sandbox, runs seeded tool+memory cases, LLM-judges, returns a `ScoreReport`. | Go |
| **`ditto-harness`** | Reference memory-harness **library** (a pinned build dep of miner submissions). Not a service. | Rust |
| **infra** | Terraform + Ansible for the GCP dev deploy (`enable_validator` gate, Secret Manager, systemd units). | HCL / YAML |

### Boundaries you must respect (see each repo's `CLAUDE.md`)

- **Weight/mechanism logic lives ONLY in `ditto-subnet`.** The platform exposes
  the raw score ledger (`GET /scoring/scores`); it must never compute champions
  or weights. This is load-bearing for Yuma consensus (every validator recomputes
  the identical deterministic fold — see [`docs/incentive-mechanism.md`](incentive-mechanism.md)
  and `PROJECT.md` D3). A second platform-side copy of the fold is a
  determinism-divergence hazard; do not add one.
- **The validator worker does not live in `ditto-platform`.** No
  `ditto/validator/` package there; no dittobench-scoring code there.
- **No shared package between repos.** `ditto/api_models/validator.py` is
  **copied** into both `ditto-platform` and `ditto-subnet` and kept in sync by a
  contract test (`ditto-subnet/ditto/tests/contract/`, `SHARED_MODELS` in
  `_schema.py`). If you add/change a shared wire field or model, regenerate the
  golden `validator_contract.json` **from the platform models** (see that
  script's header) or the contract test fails.
- **Migrations own the schema.** `ditto/db/models.py` mirrors it in Python but
  Alembic (`alembic/versions/`) is the source of truth. Add a migration for any
  schema change and keep both in sync.
- **Pydantic only in `ditto/api_models`;** everything else is
  `@dataclass(frozen=True)`. Config is env-driven with `parse_*_from_env()` +
  fail-fast typed `*ConfigError`.

### Testing & CI (learn from prior pain)

- Every PR: run **`make lint typecheck test`** (platform) or the equivalent
  (`uv run ruff format --check . && uv run ruff check . && uv run mypy ditto/ && uv run pytest`).
  CI runs on **Python 3.11 and 3.12** and blocks merge.
- **GOTCHA:** CI runs **`mypy ditto/` over the whole repo including tests** —
  running `mypy ditto/<subpkg>/` locally will miss type errors in test files and
  green-light a PR that then fails CI. Always run the full-repo mypy before pushing.
- Unit tests use a SQLite fallback (`aiosqlite`); markers `slow`/`integration`/
  `localnet`/`e2e` are excluded by default. A real Postgres (dev: `:15432`) is
  used to sanity-check migrations + window-function queries the SQLite fallback
  can't fully vouch for.
- Branching: `main` (release) ← `dev` (integration) ← `name/topic`. **PRs into
  `dev`.** Never commit to `main`.

### Where things live (fast map)

| Concern | Path |
| --- | --- |
| Validator epoch loop | `ditto-subnet/ditto/validator/worker.py` |
| KOTH+ATH weight fold | `ditto-subnet/ditto/validator/weights.py` |
| Score signing | `ditto-subnet/ditto/validator/signing.py` |
| Platform HTTP client | `ditto-subnet/ditto/validator/platform.py` |
| Validator/scoring endpoints | `ditto-platform/ditto/api_server/endpoints/{validator,scoring}.py` |
| Screener endpoints | `ditto-platform/ditto/api_server/endpoints/screener.py` |
| Anti-copy gate | `ditto-platform/ditto/api_server/scoring_gate.py` |
| Ledger query | `ditto-platform/ditto/db/queries/scores.py::list_eligible_ledger` |
| Review exit (admin) | `ditto-platform/scripts/resolve_review.py` |
| Wire models (both copies) | `ditto/api_models/validator.py` |
| Scoring engine + sandbox | `dittobench-api/internal/{sandbox,datagen,gen}/` |
| Deploy | infra `terraform/envs/gcp-platform/validator.tf`, `ansible/roles/{dittobench,validator_worker}` |

---

## 1. Current state — "you are here"

Verdicts: **DONE / PARTIAL / MISSING**.

| Stage | Status | Notes |
| --- | --- | --- |
| Miner upload (payment, size/sha cap, S3) | **DONE** | `/upload/*`; deferred tar-manifest/import-allowlist checks remain (by design). |
| Submission contract | **DONE** | Whole buildable crate as one gzipped tarball; documented + enforced at upload. |
| Screener **endpoints** (`uploaded → evaluating`) | **DONE** | Signed verdict (binds `passed`), idempotent, 409 on conflict, row-locked. |
| Screener **worker** (lint/compile/build gate) | **DONE + live** | `python -m ditto.screener` — docker-build + `/health` gate, signed verdict (subnet #32). Deployed on `ditto-validator-dev` (infra #9); two live bugs fixed (4→20 MiB cap #34; dummy LLM key for the serve smoke #35). Proven: real agent built (private ditto-harness dep) + served /health → `evaluating`. |
| dittobench scoring engine | **DONE + deployed** | Full `run_size` pipeline; mode-B tarball ingest; co-located on the validator VM. |
| Validator worker (queue → score → sign → weights) | **DONE + live** | uid 4, netuid 3 dev localnet, mock **off**, polling the platform. |
| Best-score ledger (`/scoring/scores`) | **DONE** | Persistent, self-verifying (stores signatures), whole-row consistent. |
| **Incentive mechanism (KOTH + ATH gate)** | **DONE** | 90/10 split, 1% relative margin, first-seen wins; deterministic fold, validator-side. **Merged (PR #10/#22).** |
| Weight-ingestion (one-epoch-weight bug) | **FIXED** | Weights recomputed from the durable ledger every epoch; bounded `put_weights` retry. |
| Trust-boundary hardening (sig binding, row locks) | **DONE** | Score/verdict signatures bind the full payload; both status txns row-locked. |
| Anti-copy (exact-hash + size/score + content fingerprint) | **DONE** (tuning left) | Cross-miner exact-sha256 + size/score + **two-channel content fingerprint** (lexical + AST) → `ath_pending_review`. Merged 2026-07-05 (dittobench #12, subnet #29/#30, platform #16). Thresholds untuned; review manual. |
| First **real** end-to-end scoring run | **DONE (small) 2026-07-07** | Agent `2b52b610` flowed the full non-mock path (screener→sweep→build→seed→run→LLM judge): real **composite=0.522**, signed, in the `scores` ledger, weights folded + `set_weights` **accepted on-chain**. Run at `small`; **full** proof pending a miner rebuilt from `dittobench-starter-kit#9` (the default 2 MB `/seed` body limit blocks full-size haystacks). |
| Multi-validator (k=3 + median-of-3) | **MISSING** | Single validator; one score row per agent. Endpoints still use stub names. |
| OpenRouter cost cap (`max_tokens` + per-run token budget) | **DONE** | Per-call `max_tokens` + per-run token budget on the dittobench LLM client (`LLM_MAX_TOKENS` / `LLM_RUN_TOKEN_BUDGET`); a looping harness fails the run instead of burning unbounded spend. |
| OpenRouter/sandbox **egress allowlist** | **MISSING** | Sandbox container still runs on the default bridge (full egress); a host-allowlist needs an egress proxy. Cost cap above bounds spend in the meantime. |
| Sandbox hardening (seccomp/gVisor/egress) | **DEFERRED** | `docker build --memory 2g`, no-new-privileges, private net; deeper isolation deferred in code comments. |
| Plagiarism / first-seen at content level | **DONE** (tuning left) | First-seen (`created_at`) + margin defeat verbatim copies; lexical + AST content fingerprint now catches reindent/reformat/rename/pad near-dups. Thresholds want tuning against a real corpus. |
| Emission economics (non-zero netuid emission) | **DONE on localnet** | `SubnetTaoInEmission[3]` non-zero → winners accrue alpha on netuid 3. Re-tune per network on migration. |
| Commit-reveal (production reveal step) | **MISSING** | Off on dev netuid 3; production needs a first-class reveal. |
| Chain-conformance hardening (version_key, permit self-check, tempo cadence) | **DONE (code)** | `version_key` pinned to `ditto.__spec_version__` on the SDK path; pre-submit `validator_permit` self-check (fail-open, both sinks); scoring sweep decoupled from the weight-set cadence (`VALIDATOR_SWEEP_SECONDS` vs `VALIDATOR_EPOCH_SECONDS`) so a submission is scored within ~one sweep instead of waiting a full epoch. Pylon path still delegates version_key/commit-reveal internally — verify on testnet (E1). |
| Observability (W&B, dashboard, metrics, alerts) | **PARTIAL** | W&B telemetry + public dashboard LIVE on dev (2026-07-06); richer metrics/alerts still TODO. |
| Deploy automation + autoupdater | **PARTIAL** | Terraform/Ansible dev deploy done (gated); no git-watching autoupdater. |
| Testnet → finney migration | **MISSING** | Everything runs on the dev localnet (netuid 3). |
| Pylon **identity (write)** creds | **MISSING** | Only a read token provisioned; the SDK weight path is the dev fallback. |

---

## 2. Critical path to mainnet (do these in order)

Everything else is parallelizable, but this is the spine — each gates the next:

1. **First real E2E scoring run** (§A1). Prove the non-mock path works with one
   agent, real composite lands, weight persists across epochs. *Nothing else
   matters if scoring doesn't actually run.*
2. **OpenRouter cost cap + egress allowlist** (§C3). Before running real scoring
   at any volume — unbounded LLM spend is a live financial risk.
3. **Screener worker** (§A2). ✅ Built + merged (subnet #32); **deploy it** (infra
   role + converge) to automate `uploaded → evaluating` so the pipeline flows
   without a human.
4. **Testnet migration** (§E). Emission already flows on localnet (§B1); move off
   the dev localnet to a real network **under the subnet owner's UID** (no separate
   validator registration/burn) and re-tune the pool there.
5. **Multi-validator consensus (k=3 + median)** (§A3). Decentralize scoring;
   move from one owner validator to the set.
6. **Content-level plagiarism detection** (§C1) — **done** (lexical + structural/AST
   fingerprint channels); remaining work is threshold tuning against a real corpus +
   automating the review queue. The existential risk for a downloadable-artifact
   subnet at scale is now covered.
7. **Observability + autoupdater + HA** (§D). Operate it like production.
8. **Mainnet (finney) cutover** (§E4).

---

## 3. Workstreams (the comprehensive roadmap)

Each item: **goal · status · tasks · files · acceptance**. Check tasks off as
you land them.

### A. Functional completeness

#### A1 — First real end-to-end scoring run  ·  DONE (small) 2026-07-07 · full pending
**Goal:** one agent flows the entire non-mock path and its composite lands +
persists on-chain.
- [x] Promoted agent `2b52b610` through the live path: **auto-screener** →
      validator queue → `get_artifact` → dittobench `tarball_url` →
      `docker build` (pulled `ditto-harness` via the GH token) → seeded
      datagen → tool + memory cases → LLM judge → `ScoreReport`.
- [x] Signed score accepted, row in `scores` (**composite 0.522**, tool 0.758 /
      mem 0.167, 128-char sr25519 sig, run 89f6060c), agent → `scored`.
- [x] Worker folded KOTH weights from the ledger and `set_weights` was
      **accepted on-chain** (`msg=Success`). *Cross-epoch persistence + champion
      weight actually landing needs the scored miner hotkey registered on the
      localnet — today `5CLUBKGj…` is unregistered so its 0.9 mapped to no UID and
      was skipped (localnet setup gap, not a code bug).*
- [ ] **Full-size proof (Phase 2):** rerun at `run_size=full` with a miner built
      from `dittobench-starter-kit#9`. The first full run exposed a
      production-blocking bug: the reference harness router used axum's **2 MB
      default body limit**, so a full seed haystack (842 pairs / 2258 subjects)
      POSTed to `/seed` 413'd at the **seeding** stage — for every starter-kit
      miner. Fixed in #9 (`DefaultBodyLimit::max(256 MiB)`); already-submitted
      agents bake in the old limit, so full needs a rebuilt miner.
- **Files:** `ditto-subnet/ditto/validator/{worker,dittobench,platform}.py`;
  `dittobench-starter-kit/src/bin/dittobench-miner.rs`; runbook `dev-e2e-handoff.md`.
- **Acceptance:** a real (non-mock) composite for a real harness is visible in
  the ledger and drives an on-chain weight — **met at `small`**; full-scale
  seeding proven once #9 ships in a submitted miner.

#### A2 — Screener worker (build gate)  ·  BUILT (deploy pending)
**Goal:** automate `uploaded → evaluating` (today it's manual).
- [x] **Daemon built + merged (subnet #32):** `python -m ditto.screener` (Python,
      mirrors the validator worker) polls `GET /screener/queue`, pulls the artifact,
      verifies sha256, and runs the gate — `docker build` with the tarball as the
      build context on **stdin** (Docker unpacks it in its own sandbox), BuildKit +
      optional `gh_token` secret for the private ditto-harness dep, then runs the
      image with a memory/pids cap and polls `/health`. Posts a **signed** verdict
      (`{screener_hotkey}:{agent_id}:{passed}`) to `POST /screener/agent/{id}/result`.
      Pass = builds AND serves; no LLM key needed. `ditto/screener/`, 24 tests.
- [ ] **Deploy it:** an infra `screener_worker` Ansible role (mirror
      `validator_worker`; reuse the validator hotkey — it holds the permit on netuid
      3 — so no new secret) + converge. Until then promotion is still manual in practice.
- [ ] Deeper gate: `POST /seed`/`/run` smoke, a failure-reason persist, a stale-claim
      reset sweep; a distinct `screener_permit` vs the validator permit.
- [ ] A contract-test guard for the screener wire models (mirrored by hand today).
- **Files:** `ditto-subnet/ditto/screener/`, `ditto/api_models/screener.py`; platform
  side done (`endpoints/screener.py`). **Acceptance:** submissions flow to
  `evaluating` with no human — met once the role is converged.

#### A3 — Multi-validator: k=3 sharded queue + median-of-3  ·  MISSING
**Goal:** decentralize scoring per `PROJECT.md` D2/D3.
- [ ] Lease-based work assignment: `GET /validator/request-evaluation` hands each
      agent to **3 distinct** validators (records `(agent_id, validator_hotkey,
      expires_at)`, won't re-hand, reassigns expired leases).
- [ ] Finalize a score as the **median of the 3** signed raw scores (robust to
      one liar/outlier).
- [ ] Migrate the current **stub** endpoint names (`/validator/queue`,
      `/agent/{id}/artifact`, `/agent/{id}/score`) to the **target** names
      (`request-evaluation`, `submit-score`) in a lockstep cross-repo change
      (both `api_models/validator.py` copies + golden + subnet client).
- [ ] Onboard >1 validator and confirm Yuma converges on the KOTH champion.
      **Localnet test keys:** create **2 new hotkeys under the existing localnet
      validator coldkey** and register them on netuid 3 (fallback if that misbehaves:
      generate a fresh coldkey/hotkey pair and transfer localnet TAO to it from the
      current validator key). Gives 3 distinct validator hotkeys to exercise the
      median fold without any new funding.
- **Files:** `ditto-platform/ditto/api_server/endpoints/validator.py`, the
  `scores` schema (add lease table/columns), `ditto-subnet/ditto/validator/`.
  **Acceptance:** 3 validators independently score, the ledger finalizes a
  median, and all validators fold identical weights.

#### A4 — Miner CLI completion  ·  PARTIAL
- [ ] Deferred upload validations: tar manifest, import allowlist, schema diff,
      banned-hotkey (pending the harness interface + `banned_hotkeys` table).
- [ ] Miner UX: clearer submit errors, a `logs`/status command, practice-endpoint
      docs.
- **Files:** miner CLI in `ditto-subnet`; enforcement side
  `ditto-platform/docs/submission-contract.md`.

### B. Incentive & economics

#### B1 — Emissions on (non-zero netuid share)  ·  DONE on localnet  ·  tune per-network
**Goal:** winners actually accrue alpha.
- [x] **`SubnetTaoInEmission[3]` is non-zero on the dev localnet** — alpha flows to
      the winner (consensus already picks it, `Incentive[3] = 65535`). The earlier
      "= 0" reading was stale.
- [ ] Confirm the winning miner's `TotalHotkeyAlpha` increases on each real run, and
      **re-tune the alpha pool / `TaoWeight`** when migrating to testnet/finney (the
      split is ours to set directly on each network).
- **Ref:** `STATE-OF-THE-SUBNET.md` §"For Ethan" (exact on-chain values — now
  ours to set directly).

#### B2 — Commit-reveal in production  ·  MISSING
- [ ] Add a first-class reveal step to the worker (dev netuid 3 has commit-reveal
      **off**; production needs it). SDK path sets weights directly today; the
      Pylon path delegates reveal to Pylon.
- [x] **`version_key` pinned** on the SDK path (`set_weights(version_key=...)`,
      default `ditto.__spec_version__`, env `VALIDATOR_WEIGHT_VERSION_KEY`) so the
      chain groups our weights by mechanism version instead of averaging against a
      validator on a different version. The Pylon path derives its own version_key
      from subnet hyperparams — **verify that matches** on testnet.
- **Files:** `ditto-subnet/ditto/validator/{worker,sdk_weights}.py`, `ditto/chain`.

#### B2a — Weight-setting robustness (conformance self-checks)  ·  DONE (code) · verify on testnet
- [x] **`validator_permit` self-check** before submitting: read the metagraph
      through the active sink (`ChainClient.has_validator_permit` /
      `SdkWeightSetter.has_validator_permit`) and skip (loudly) when the hotkey
      lacks a permit, rather than burning an epoch on a guaranteed rejection.
      **Fail-open** on a flaky read so it never wedges weight-setting.
- [x] **Tempo-aware cadence:** scoring sweeps (`VALIDATOR_SWEEP_SECONDS`, default
      120s) are decoupled from on-chain weight submission (`VALIDATOR_EPOCH_SECONDS`,
      default 3600s ≈ tempo), so the queue drains promptly without pushing weights
      faster than the chain's `weights_rate_limit` window. First sweep still sets
      weights so a fresh start doesn't wait a full epoch.
- [ ] **Still open:** read the on-chain tempo / `weights_rate_limit` directly
      (today `epoch_seconds` is a hand-set proxy); exponential backoff on a
      rate-limit rejection; a min-stake check alongside the permit. Verify the
      Pylon path's normalization / u16 / `max_weight_limit` on testnet (E1) — the
      validator delegates all of it and does not verify it in-repo.
- **Files:** `ditto-subnet/ditto/validator/{worker,sdk_weights,config}.py`,
  `ditto/chain/client.py`.

#### B3 — Validate KOTH+ATH parameters against real score distributions  ·  NEW
- [ ] Once real composites exist (A1), sanity-check the **1% margin** and
      **90/10 split** against the observed score spread and between-seed variance
      (a margin below the scorer's noise floor lets noise flip the crown; a
      margin far above it makes the ATH un-dethronable). Tune via
      `VALIDATOR_KOTH_MARGIN` / `_CHAMPION_SHARE` / `_TAIL_SIZE`.
- [ ] Decide the participation-tail economics (how many miners, min-score floor).
- **Files:** `ditto-subnet/ditto/validator/{weights,config}.py`;
  dittobench `cmd/calibrate` for the noise floor.

#### B4 — Registration & immunity economics  ·  NEW
- [ ] Set registration burn cost, immunity period, and recycling so spam
      registration is deterred without pricing out honest miners (ours to set).

### C. Anti-gaming & trust

#### C1 — Content-level plagiarism / near-dup detection  ·  DONE (tuning left)  ·  🔴 at scale
**Goal:** upgrade the heuristic gate to catch lightly-tweaked copies. Two
fingerprint channels now do, each feeding the human-reviewed `ath_pending_review`
hold (`scripts/resolve_review.py` clears/bans). Both are computed once, stored, and
compared cross-miner with one Jaccard/containment estimator.
- [x] **Lexical channel (platform).** `/upload/agent` fingerprints each tarball into a
      shingle MinHash sketch: per-file k-line shingles with all intra-line whitespace
      removed, so re-indent / tabs / line-endings / operator-spacing reformat and
      localized edits wash out; bottom-k (KMV) sketch `{v,k,card,m}` in
      `agents.content_fingerprint`. The gate holds on high **Jaccard** (edited copy) or
      **containment** (copy padded with junk files). Bomb/DoS-capped (incl. member
      flood), CPU offloaded off the event loop, fail-open.
      **Files:** `ditto-platform/ditto/api_server/fingerprint.py`, `scoring_gate.py`,
      migration `c4e8b1a06d72`.
- [x] **Structural / AST channel (dittobench → validator → platform).** dittobench parses
      the built crate with tree-sitter-rust and emits a shingle MinHash of the
      **named node-type stream** (identifiers/literals discarded), so it additionally
      survives **identifier renaming + reformatting** (proven: a renamed+reformatted
      crate yields an identical sketch). Forwarded UNSIGNED on `ScoreReport`
      (`structural_fingerprint`) — no signing-version skew — persisted at score time in
      `agents.structural_fingerprint`; the gate adds a structural arm at higher
      thresholds (0.85 / 0.98) since unrelated crates share more parse-tree shape than
      text. **Files:** `dittobench-api/internal/astfp`, `pkg/protocol`,
      `ditto-subnet/ditto/api_models/validator.py`, platform gate, migration `d5f2a3b91e64`.
- [ ] **Threshold tuning** against a real score/similarity corpus (ties to B3): the
      lexical (0.75 / 0.95) and structural (0.85 / 0.98) tolerances are conservative
      guesses. Holds are human-reviewed (not auto-bans), so the risk is a few false
      holds, but the tolerances want validation once real submissions exist.
- [ ] **Automate the review queue** — `ath_pending_review` is drained by hand
      (`scripts/resolve_review.py`); a reviewer UI/workflow removes the manual step.
- [x] `first_seen` provenance: assessed — `agents.created_at` has no `onupdate`, so it
      is already an immutable first-seen; the KOTH tie-break reads it directly. No
      dedicated column needed unless a backfill/re-import path is added later.
- **Acceptance:** a renamed/reindented/reformatted copy of the current champion is
  flagged, not paid — **met** across both channels (lexical for text edits + padding,
  structural for identifier renaming). Logic-reordering *within* a shingle window and
  deep semantic equivalence remain out of scope.

#### C2 — Signature replay-cache / nonce enforcement  ·  PARTIAL
- [ ] Signatures now bind the full payload (agent + composite + seed), closing
      cross-agent replay + tamper. Add a **nonce/expiry + server-side replay
      cache** so a captured-and-replayed signed message (even same-agent) is
      rejected, not just idempotently re-applied.
- **Files:** `ditto-platform/ditto/api_server/endpoints/{validator,screener}.py`,
  `ditto-subnet/ditto/validator/signing.py`, the wire models (contract regen).

#### C3 — dittobench-api hardening  ·  PARTIAL  ·  🔴 cost + isolation
- [x] **Per-run cost cap** — per-call `max_tokens` + a per-run token budget on the
      OpenRouter client (`internal/llm/llm.go`; env `LLM_MAX_TOKENS` /
      `LLM_RUN_TOKEN_BUDGET`). The client is created per submission, so the budget
      is per-run; a looping harness fails the run instead of burning unbounded
      spend. Covers critical-path #2's cost half.
- [ ] **OpenRouter/sandbox egress allowlist** — still MISSING. The sandbox
      container runs on the default bridge (full egress); a real host-allowlist
      needs an egress proxy. Deferred (bigger than a code change).
- [ ] **Sandbox isolation:** add seccomp/gVisor + egress restriction (the sandbox
      comment defers these); the build host unpacks attacker-controlled tarballs.
- [x] **Redact the tarball error path** (`internal/sandbox/tarball.go`) — the
      transport-level fetch error now runs through `redactURL`, stripping the
      presigned signature query before it can reach job status / logs (parity with
      the git path's `redact`). Covered by `TestRedactURL`.
- [x] **Extractor CPU-DoS + tag:** skipped/non-regular tar-entry bodies are now
      charged against the 64 MiB cap (`drainCounted`) so a large-bodied symlink
      can't force unbounded gzip inflate; the extract loop honors `ctx`. The docker
      tag already pins `tarball_sha256[:12]` when present — the validator now
      **forwards** `tarball_sha256` (see below), so the tag is content-pinned and
      the scorer re-verifies the fetched bytes. Covered by `TestDrainCounted`,
      `TestExtractTarGz_CtxCanceled`.
- **Repo:** `dittobench-api` (Go). **Paired subnet change:** the validator now
  forwards `tarball_sha256` to `/v1/submit` and cross-checks the queue-vs-artifact
  digest before scoring (`ditto/validator/{worker,dittobench}.py`) — this also
  closes the "no sha256 verification anywhere in the validator path" cross-cutting
  gap.

#### C4 — Banned-hotkeys table + enforcement  ·  DONE
- [x] `banned_hotkeys` table (migration `a3f1c9d27b40`, model `BannedHotkey`,
      queries `ditto/db/queries/bans.py`), enforced at upload (`/upload/agent`
      hard 403; `/upload/check` reports code `1103` pre-payment) and surfaced on
      `/retrieval/agent-by-hotkey` (a hotkey-level ban shows `banned` regardless of
      the latest agent's own status). Owner-only writes via `scripts/ban_hotkey.py`.
- **Files:** `ditto-platform/ditto/db/{models.py,queries/bans.py}`,
  `alembic/versions/2026_07_02_add_banned_hotkeys.py`, `endpoints/upload.py`,
  `endpoints/retrieval.py`, `scripts/ban_hotkey.py`. Migration verified up+down on
  real Postgres.

#### C5 — Scoring integrity / verifiable scoring  ·  NEW (design)
- [ ] Today scoring integrity is trusted to the dittobench-api operator
      (centralized scorer, distributed weight-setting). Design a path toward
      verifiable/replicable scoring (reproducible seeds are already in the
      ledger) so a validator can't be forced to trust a single scorer.
- **Note:** because we run the scorer today, verifiable/replicable scoring is a
      deliberate design choice we make on our own timeline — not a constraint
      imposed on us. Sequence it against C1 (plagiarism) and A3 (multi-validator).

#### C6 — API abuse controls  ·  NEW
- [ ] Global + per-hotkey rate limits, request-size limits, and auth throttling on
      the public platform endpoints; today auth is permit-check + signatures only.

### D. Reliability & operations

#### D1 — Observability  ·  PARTIAL  ·  telemetry + dashboard live
- [x] **W&B run logging LIVE (dev, 2026-07-06):** validator publishes aggregate
      sweep stats (per-agent composite + tool/memory + per-category means,
      leaderboard, weight vector, sweep health) to `heyditto/ditto-sn118`. Opt-in
      per host (`validator_wandb_enabled`); infra PRs #7/#8, platform PR #17.
- [x] **Public winner/leaderboard dashboard** live (reads `/public/leaderboard`);
      "full telemetry" link resolves to the W&B project.
- [ ] Validator: richer structured logging + metrics (sweep duration, put_weights
      success, ledger size) beyond the W&B tables.
- [ ] Platform: request metrics, error rates, DB health; wire to the existing
      **Datadog** MCP if used for alerting.

#### D2 — Deployment lifecycle / autoupdater  ·  PARTIAL
- [ ] Git-watching autoupdater for the validator (today systemd, manual updates).
- [ ] Zero-downtime restart / drain handling (the worker already drains on
      SIGTERM; verify weight-set safety across restarts).
- **Files:** infra `ansible/roles/validator_worker`.

#### D3 — Third-party validator onboarding  ·  MISSING
- [ ] Reproducible "run a validator" package: docs, hardware reqs, config, the
      stateless worker (it already talks only HTTP + chain, no DB), key custody.
- **Files:** infra + `ditto-subnet/docs/`.

#### D4 — Database operations  ·  MISSING
- [ ] Prod-grade Postgres: automated backups, PITR, migration runbook, connection
      pooling, retention/archival for `scores`/`agents`, read replica for the
      public ledger read.
- **Files:** `ditto-platform/alembic/`, infra.

#### D5 — HA / DR / cost controls  ·  MISSING
- [ ] Platform API redundancy; dittobench-api scaling; queue durability.
- [ ] Disaster recovery: state reconstruction, chain re-sync.
- [ ] Cost ceilings: LLM judge spend (ties to C3), VM/storage budgets + alerts.

#### D6 — Secrets management & rotation  ·  PARTIAL
- [ ] Rotation policy + procedure for the hotkey mnemonic, OpenRouter key, and GH
      token (all in Secret Manager). Document the rotation runbook (the validator
      hotkey has already been rotated once — capture the steps).

### E. Network migration (testnet → finney)

#### E1 — Pylon identity (write) credentials  ·  MISSING  ·  blocker
- [ ] Provision `PYLON_IDENTITY_*` (write) so the Pylon `put_weights` path works
      in production (today only a read token exists; the SDK path is the dev
      fallback). **Files:** infra `platform.env.j2`, `ditto-subnet/ditto/validator/config.py`.

#### E2 — Testnet permit + stake (subnet owner UID)  ·  MISSING
- [ ] Validation runs under the **subnet owner's UID** — **no separate validator
      hotkey registration or registration burn.** Ensure the owner hotkey holds
      enough stake to clear the `validator_permit` threshold on testnet (then
      finney). (Owner UID already validates on localnet netuid 3.)

#### E3 — Chain parameters  ·  MISSING
- [ ] Set tempo, immunity period, weights-rate-limit, validator-permit threshold,
      and **enable commit-reveal** (B2) for the target network.
- [ ] Once the network's tempo / weights-rate-limit are set, align
      `VALIDATOR_EPOCH_SECONDS` to that window (the validator now honors a
      configurable weight-set cadence, B2a) and set `VALIDATOR_WEIGHT_VERSION_KEY`
      to the mechanism version every validator agrees on (defaults to the package
      spec version; the Pylon path derives its own — confirm they match).

#### E4 — Mainnet (finney) cutover  ·  MISSING
- [ ] Point the platform + validator at finney SN118, flip `enable_validator`,
      run the deploy runbook, verify each hop, and run a real E2E (A1) on mainnet.
- **Ref:** `STATE-OF-THE-SUBNET.md` §"For Ethan" (revert-to-finney backup notes).

### F. Documentation & ecosystem

- [ ] **F1 — Miner onboarding:** build-a-harness guide, submission contract,
      practice endpoint, scoring rubric (0.6 tool / 0.4 memory), KOTH rules.
- [ ] **F2 — Validator onboarding:** run-a-validator guide (D3).
- [ ] **F3 — Public dashboard:** leaderboard, current champion, emissions (D1).
- [ ] **F4 — Subnet landing / lightpaper:** what SN118 rewards and why
      (best-artifact competition + KOTH+ATH anti-copy rationale).

### G. Testing & QA

- [ ] **G1 — E2E integration suite in CI (localnet):** exercise the full pipeline
      (upload → screen → evaluate → score → weights) behind the `e2e`/`localnet`
      markers, gated in CI.
- [ ] **G2 — Load & chaos testing:** many miners/validators; inject chain
      outages, dittobench failures, partial writes; confirm no lost-update / no
      zeroed-chain / graceful degradation.

---

## 4. Open decisions needing a human

- **Trust model:** owner-centralized scorer (today) vs permissionless-distributed
  verifiable scoring (C5). We run the scorer, so this is our call — it gates how
  much of A3/C5 to build now.
- **Participation-tail economics** (B3): tail size, min-score floor, or pure WTA
  at mainnet.
- **Registration/immunity economics** (B4).
- **Emission split / alpha-pool tuning** target values (B1).
- **Endpoint-name migration timing** (A3): when to move stub → target names.

## 5. Hard external blockers to go-live (not code)

1. **Subnet owner UID staked to the `validator_permit` threshold** on the target
   network (testnet, then finney) — validation runs under the owner UID, so **no
   separate validator hotkey registration or registration burn.**
2. **Pylon identity (write) credentials** (E1).
3. An **OpenRouter key** for the dittobench `run_size` pipeline **plus a cost cap**
   (C3) before running at volume.
4. ~~Non-zero netuid emission~~ — **done on localnet** (B1); re-tune per network.

## 6. References

- `PROJECT.md` — locked decisions (D1–D4), phases, target endpoint names.
- `docs/STATE-OF-THE-SUBNET.md` — the 6/30 dev-chain proof + on-chain specifics
  (§"For Ethan").
- `docs/incentive-mechanism.md` — KOTH+ATH rationale (Option A) + alternatives.
- `docs/dev-e2e-handoff.md` — step-by-step dev runbook.
- `docs/ditto-architecture-v2.mmd` — architecture diagram.
- Merged incentive/ledger work: ditto-platform **PR #10**, ditto-subnet **PR #22**.

---

*Ownership: we own the entire subnet end to end — platform API, screener,
validator, miner CLI, dittobench scorer, chain/emissions config, and every
mechanic and economic knob. There are no external owners to hand a workstream
to; the sequencing above (§2) is how one team drives the whole stack to
production. `PROJECT.md`'s per-person owner table is historical and superseded
by this.*
