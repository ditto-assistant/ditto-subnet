# SN118 — Production Handoff

**As of 2026-07-06.** A current snapshot of where Subnet 118 is and the
ordered path to a finney launch.

---

## TL;DR

The full pipeline — miner → platform → validator → **real DittoBench scoring** →
signed ledger → KOTH weights → chain — **works end to end and is proven live** on
the dev localnet (netuid 3, validator uid 4). A real (non-mock) agent scored
**composite 0.587**, its signed score landed in the ledger, and the validator set
weights on chain unattended. Emission already flows on the dev localnet
(`SubnetTaoInEmission[3]` is non-zero — winners accrue alpha). What stands between
us and finney is **not new architecture** — it's moving to a real network **under
the subnet owner's UID** (no separate validator registration/burn needed),
decentralizing to multiple validators, and productionizing (cost egress,
plagiarism tuning, ops).

---

## What works today (proven + live)

- **Miner CLI** — upload / status / pre-flight; signs + pays the eval fee on chain,
  streams agent + payment proof. Proven against the live API.
- **Platform API** — on-chain payment verification (replay-protected), object
  storage, the validator queue, the **self-verifying signed score ledger**, the
  anti-copy gate (now **two-channel content fingerprint** — see below),
  banned-hotkeys. Deployed, **auto-deploys from `dev`** to
  `platform-api-dev.heyditto.ai` (Caddy TLS; `main` → prod, not yet cut over).
- **DittoBench scoring engine** — real `docker build` of a submitted harness in a
  sandbox, seeded tool + memory cases, LLM judge → `ScoreReport`. Co-located on the
  validator VM. **Cost cap on** (`max_tokens` + per-run token budget).
- **Validator worker** — queue → score → **sign** → submit → KOTH+ATH weight fold →
  `put_weights`, live (uid 4, netuid 3, mock **off**). Weights are recomputed from
  the durable ledger **every epoch**, so a scored agent keeps its emission.
- **Incentive mechanism** — KOTH / winner-take-all + ATH gate (90/10 champion/tail,
  1 % relative dethrone margin, first-seen tie-break), deterministic + validator-side.
  Merged; Yuma consensus confirmed winner-take-all (incentive = 1.0) on the dev chain.
- **First real end-to-end scoring run** — **DONE 2026-07-03.** Root-caused the
  auto-submit gap to a validator/platform **signing-version skew** (validator signed
  a 2-field payload, platform verified 5); fixed by redeploying `dev`. Real signed
  composite persists in the ledger and drove an on-chain weight.
- **Content-level plagiarism — two-channel fingerprint (shipped 2026-07-05, live on dev):**
  the anti-copy gate now runs a **content fingerprint** in addition to the old
  sha256 + size/score heuristics. Two channels, each a bottom-k MinHash (KMV)
  sketch compared by one estimator (Jaccard + conditioned-KMV containment):
  - **Lexical** (platform, at upload) — per-file k-line shingles with all
    intra-line whitespace stripped → survives reindent/reformat/file-rename and,
    via containment, junk-file padding.
  - **Structural / AST** (dittobench scorer, at score) — tree-sitter-rust
    named-node-type k-node shingles → additionally survives identifier renaming;
    travels on `ScoreReport` as **unsigned advisory** metadata.
  A cross-miner near-duplicate is **held in `ath_pending_review` for human
  review — never autobanned.** Merged across all three repos (dittobench #12,
  subnet #29/#30, platform #16). **Left:** thresholds are conservative
  placeholders wanting tuning against a real corpus, and the review queue is
  drained by hand (no reviewer UI/automation yet).
- **Public transparency (shipped this week, live on dev):**
  - `GET /api/v1/public/leaderboard` + `GET /api/v1/public/health` — no-auth,
    aggregate-only, cached.
  - **Dashboard** served same-origin by the platform at
    `https://platform-api-dev.heyditto.ai/`.
  - **W&B telemetry** — **LIVE on the dev validator (2026-07-06)**: publishing
    aggregate sweep stats to `heyditto/ditto-sn118`, and the dashboard's "full
    telemetry" link resolves to it. (Still opt-in / off by default per host.)

---

## Position on the critical path

The spine from `NEXT-STEPS.md §2`, marked to today:

| # | Step | Status |
| --- | --- | --- |
| 1 | First real E2E scoring run | ✅ **done** (2026-07-03) |
| 2 | OpenRouter cost cap + **egress allowlist** | cost cap ✅ · egress allowlist ❌ |
| 3 | **Screener worker** (automate `uploaded → evaluating`) | ❌ (manual today) |
| 4 | **Emissions** + **testnet migration** | emission ✅ live on localnet · testnet migration ❌ |
| 5 | **Multi-validator** consensus (k=3 + median) | ❌ (single validator; localnet test keys recipe below) |
| 6 | **Content-level plagiarism** detection | ✅ lexical + structural/AST fingerprint channels · tuning ⏳ |
| 7 | Observability + autoupdater + HA | ⚠️ transparency ✅ · W&B telemetry ✅ live (dev) · autoupdater/HA ❌ |
| 8 | **Mainnet (finney) cutover** | ❌ |

We are through **step 1**; emission is already live on localnet, so the near-term
focus is steps 2–3 (egress allowlist, screener worker) then the testnet hop.

---

## Road to production (do in order)

1. **Sandbox egress allowlist** *(dittobench-api / infra)* — the eval container still
   runs on the default bridge with full egress. Needs a host-allowlist egress proxy
   (OpenRouter + package registries only). The cost cap bounds *spend* today, not
   *destinations*. **Do before running real scoring at volume.**
2. **Screener worker** *(ditto-subnet, new Rust daemon)* — poll `GET /screener/queue`,
   pull the artifact, run lint/compile/build, post the signed verdict to flip
   `uploaded → evaluating`. Removes the human from the loop. Platform endpoints already
   exist and are signed/row-locked.
3. **Testnet migration** *(chain + infra)* — emission already flows on localnet
   (`SubnetTaoInEmission[3]` is non-zero; winners accrue alpha), so this is a *move*,
   not a *turn-on*. Validation runs under the **subnet owner's UID** — no separate
   validator hotkey registration or registration burn. Confirm/tune the alpha-pool /
   `TaoWeight` on the target network, point the deploy at it (`SUBTENSOR_NETWORK`/
   `NETUID`), flip `enable_validator`, and re-run a real E2E there.
4. **Multi-validator (k=3 + median-of-3)** *(ditto-subnet + platform)* — shard the queue
   across validators, finalize on the median. Endpoints still use stub names. Needed for
   a trustless subnet; a single owner-validator is a centralization + liveness risk.
   **To test on localnet:** create 2 new hotkeys under the *existing* localnet validator
   coldkey and register them on netuid 3 (fallback if that misbehaves: generate a fresh
   coldkey/hotkey pair and transfer localnet TAO to it from the current validator key).
5. **Content-level plagiarism** *(platform + dittobench ✅ — tuning ⏳)* — first-seen +
   margin defeat verbatim copies; two fingerprint channels now catch tweaked ones and
   feed a human-reviewed hold:
   - **Lexical** *(platform)* — a shingle MinHash sketch of the tarball text, computed
     at upload; survives re-indent / rename / reformat / localized edits (Jaccard) and
     junk-file padding (containment). `ditto/api_server/fingerprint.py`, `scoring_gate.py`.
   - **Structural / AST** *(dittobench → validator → platform)* — a shingle MinHash of
     the Rust **parse-tree shape** (tree-sitter, identifiers/literals discarded),
     computed where the crate is unpacked; additionally survives identifier renaming.
     Forwarded UNSIGNED on the score report (no signing skew).
     `dittobench-api/internal/astfp`, gate structural channel.
   **Remaining:** (a) **tune thresholds** (`_DEFAULT_*_TOL` in `scoring_gate.py`) against
   a real score/similarity corpus — conservative defaults today, holds human-reviewed not
   auto-banned (ties to B3); (b) **drain the review queue** — `ath_pending_review` is
   worked by hand, needs a reviewer UI/workflow; (c) optional token-level channel for
   reorder-within-window evasion. For a downloadable-artifact subnet this remains the
   existential risk at scale.
6. **Ops** *(infra)* — git-watching autoupdater (manual systemd updates today),
   alerting, HA. Re-enable **commit-reveal** for production (the worker gains a reveal
   step) and decide the **Pylon identity write-path** vs. the bittensor-SDK weight path
   (only a *read* token is provisioned today; SDK is the dev fallback).
7. **Finney cutover** *(everything)* — repeat the testnet hop against finney SN118 (again
   under the **subnet owner's UID**, no validator registration burn), run the deploy
   runbook, verify each hop, run a real E2E on mainnet.

---

## Hard blockers (external, not code)

These gate production regardless of engineering; secure them in parallel:

- **Subnet owner UID / hotkey** on the target network — validation runs under it, so
  there is **no separate validator hotkey registration or registration burn**. (We hold
  it on localnet netuid 3 today.) Stake it enough to meet the `validator_permit`
  threshold on the target network.
- **Pylon identity (write) credentials** for the production `put_weights` path.
- ~~Non-zero netuid emission~~ — **done on localnet** (`SubnetTaoInEmission[3]` non-zero);
  confirm/tune the pool on the target network.
- ~~W&B account~~ — **provisioned + wired live** (see below).

---

## W&B telemetry — LIVE on dev (2026-07-06)

The dev validator publishes aggregate-only sweep stats to **`heyditto/ditto-sn118`**
and the dashboard's "full telemetry" link resolves to it. All wiring done:

1. **Account:** ✅ key at `docs/wandb-keys.yml` (gitignored); project
   `heyditto/ditto-sn118` created (`access: USER_READ` — verify world-public in the
   W&B UI if fully open viewing is wanted).
2. **Infra:** ✅ Secret Manager `validator-wandb-key` (+ runtime-SA accessor, captured
   in Terraform, infra PR #8); `validator_worker` role wires `WANDB_MODE/PROJECT/ENTITY`
   + `uv sync --extra telemetry` (infra PR #7); enabled per host via
   `validator_wandb_enabled` (dev on). Converged 2026-07-06 → run `validator-5EexQS8U`
   syncing.
3. **Platform:** ✅ GH Actions var `DITTO_DASHBOARD_WANDB_URL =
   https://wandb.ai/heyditto/ditto-sn118`; deployed to `ditto-platform-dev` (platform
   PR #17 also added `workflow_dispatch` for manual redeploys).

**To enable on another validator host:** set `validator_wandb_enabled: true` in its
host_vars and converge (the `validator-wandb-key` secret + accessor already exist).

---

## Repos & where things live

| Repo | Role |
| --- | --- |
| `ditto-platform` (Python/FastAPI) | API: upload, screener, validator/scoring endpoints, ledger, anti-copy gate, public API + dashboard. **The OpenAPI contract.** |
| `ditto-subnet` (Python) | Validator daemon + miner CLI. **Owns weight-setting / KOTH+ATH fold / signing / chain I/O.** |
| `dittobench-api` (Go) | Scoring engine: sandbox build + seeded cases + LLM judge → `ScoreReport`. |
| `ditto-harness` (Rust) | Reference memory-harness library (a pinned build dep of submissions). |
| infra (Terraform + Ansible) | GCP deploy, Secret Manager, systemd, `enable_validator` gate. |

**Boundaries:** weight/mechanism logic lives **only** in `ditto-subnet`; the platform
never computes champions/weights (Yuma determinism). `api_models/validator.py` is
**copied** into both repos and kept in sync by the contract test. Migrations own the
schema. Detailed map + acceptance criteria: [`NEXT-STEPS.md`](NEXT-STEPS.md).
