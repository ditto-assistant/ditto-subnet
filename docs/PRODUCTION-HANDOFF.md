# SN118 — Production Handoff

**As of 2026-07-04.** A concise, current snapshot of where Subnet 118 is and the
ordered path to a finney launch. This supersedes the *status* in
[`STATE-OF-THE-SUBNET.md`](STATE-OF-THE-SUBNET.md) (2026-06-30, pre-DittoBench).
For the deep, task-level roadmap see [`NEXT-STEPS.md`](NEXT-STEPS.md); for the
credential/network "hops" see [`CREDENTIALED-HANDOFF.md`](CREDENTIALED-HANDOFF.md).

> **We own the entire subnet end to end** — miner CLI, platform API, screener,
> validator + weight fold, the dittobench scorer, chain params, and emissions.
> The only real dependencies are external *services* (a registered hotkey with
> stake, Pylon write creds, OpenRouter, W&B), not other teams.

---

## TL;DR

The full pipeline — miner → platform → validator → **real DittoBench scoring** →
signed ledger → KOTH weights → chain — **works end to end and is proven live** on
the dev localnet (netuid 3, validator uid 4). A real (non-mock) agent scored
**composite 0.587**, its signed score landed in the ledger, and the validator set
weights on chain unattended. What stands between us and finney is **not new
architecture** — it's turning on emissions, moving to a real network with a
registered/staked hotkey, decentralizing to multiple validators, and productionizing
(cost egress, plagiarism, ops).

---

## What works today (proven + live)

- **Miner CLI** — upload / status / pre-flight; signs + pays the eval fee on chain,
  streams agent + payment proof. Proven against the live API.
- **Platform API** — on-chain payment verification (replay-protected), object
  storage, the validator queue, the **self-verifying signed score ledger**, the
  anti-copy gate, banned-hotkeys. Deployed, **auto-deploys from `dev`** to
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
- **Public transparency (shipped this week, live on dev):**
  - `GET /api/v1/public/leaderboard` + `GET /api/v1/public/health` — no-auth,
    aggregate-only, cached.
  - **Dashboard** served same-origin by the platform at
    `https://platform-api-dev.heyditto.ai/`.
  - **W&B telemetry** module in the validator — opt-in, off by default; **merged,
    awaiting infra enablement** (see below).

---

## Position on the critical path

The spine from `NEXT-STEPS.md §2`, marked to today:

| # | Step | Status |
| --- | --- | --- |
| 1 | First real E2E scoring run | ✅ **done** (2026-07-03) |
| 2 | OpenRouter cost cap + **egress allowlist** | cost cap ✅ · egress allowlist ❌ |
| 3 | **Screener worker** (automate `uploaded → evaluating`) | ❌ (manual today) |
| 4 | **Emissions on** + **testnet migration** | ❌ (blocker — see below) |
| 5 | **Multi-validator** consensus (k=3 + median) | ❌ (single validator) |
| 6 | **Content-level plagiarism** detection | ⚠️ platform content-fingerprint ✅ · semantic/AST ❌ |
| 7 | Observability + autoupdater + HA | ⚠️ transparency ✅ · autoupdater/HA ❌ |
| 8 | **Mainnet (finney) cutover** | ❌ |

We are through **step 1**; steps 2–4 are the near-term focus.

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
3. **Emissions on + testnet migration** *(chain + infra)* — **the gating blocker.**
   `SubnetTaoInEmission[3] = 0`, so winners accrue no alpha. Tune the alpha-pool /
   `TaoWeight`; then register the validator hotkey (with `validator_permit` + stake) on
   **testnet**, point the deploy at it (`SUBTENSOR_NETWORK`/`NETUID`), flip
   `enable_validator`, and re-run a real E2E there. Requires **TAO for the registration
   burn** and a funding coldkey.
4. **Multi-validator (k=3 + median-of-3)** *(ditto-subnet + platform)* — shard the queue
   across validators, finalize on the median. Endpoints still use stub names. Needed for
   a trustless subnet; a single owner-validator is a centralization + liveness risk.
5. **Content-level plagiarism** *(platform ✅ / screener+dittobench ⏳)* — first-seen +
   margin defeat verbatim copies. The platform now also fingerprints each upload
   (normalized per-file content-hash set, indentation/rename-insensitive) and the
   anti-copy gate holds a cross-miner near-dup when score proximity **and** high
   content-Jaccard both hold — so a re-indented/renamed copy no longer slips past the
   old size heuristic (`ditto/api_server/fingerprint.py`, `scoring_gate.py`). What
   remains is **semantic/AST** near-dup (identifier-renaming, logic reordering),
   computed where the tree is already unpacked (screener/dittobench). For a
   downloadable-artifact subnet this is the existential risk at scale.
6. **Ops** *(infra)* — git-watching autoupdater (manual systemd updates today),
   alerting, HA. Re-enable **commit-reveal** for production (the worker gains a reveal
   step) and decide the **Pylon identity write-path** vs. the bittensor-SDK weight path
   (only a *read* token is provisioned today; SDK is the dev fallback).
7. **Finney cutover** *(everything)* — repeat the testnet hop against finney SN118, run
   the deploy runbook, verify each hop, run a real E2E on mainnet.

---

## Hard blockers (external, not code)

These gate production regardless of engineering; secure them in parallel:

- **Registered SN118 validator hotkey** with `validator_permit` + stake on the target
  network (testnet, then finney). Have it on **localnet netuid 3 only**.
- **TAO** to fund the registration burn + stake.
- **Non-zero netuid emission** configured (step 3).
- **Pylon identity (write) credentials** for the production `put_weights` path.
- **W&B account** — a team/entity + **public** `ditto-sn118` project + API key (see next).

---

## Enabling W&B telemetry (module merged; wiring left)

The validator publishes aggregate-only stats when enabled; off by default. To light up:

1. **Account:** create a W&B team + **public** project `ditto-sn118` + an API key.
2. **Infra (validator systemd env + Secret Manager):** add `WANDB_API_KEY` to Secret
   Manager; set `WANDB_MODE=online`, `WANDB_PROJECT=ditto-sn118`, `WANDB_ENTITY=<team>`;
   and make the validator install run **`uv sync --extra telemetry`** — `wandb` is an
   opt-in extra, so without it the sink no-ops even when `online`.
3. **Platform:** set the GitHub Actions **variable** `DITTO_DASHBOARD_WANDB_URL =
   https://wandb.ai/<entity>/ditto-sn118`; the next `dev` deploy upserts it into the VM
   `.env` and the dashboard's "full telemetry" link resolves.

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
