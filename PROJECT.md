# Ditto Subnet (SN118) — Project Plan

Status: in progress, updated 2026-06-29. Owners: Dan, Nick, Ethan, Omar (½ time).

This is the working plan for getting SN118 to a testnet end-to-end loop and
then a real evaluation/incentive pipeline. Architecture diagram:
[`ditto-architecture-v2.mmd`](docs/ditto-architecture-v2.mmd).

**Where we are (2026-06-29):** Phase 0 done (repo split, miner CLI merged, dev
chain reachable). Phase 1 intake half proven end-to-end on the dev chain
(`ditto upload` → on-chain eval-fee payment → verifier → agent stored). Validator
worker relocated into `ditto-subnet` with a mock-bench hook; the validator →
weights → emissions tail is **not** closed yet — blocked on the dev localnet's
disabled staking (no vpermit). See `docs/dev-e2e-handoff.md`.

---

## 1. Locked decisions

### D1 — Repos & state
- **No shared package.** API/platform → `ditto-platform`; miner + validator →
  `ditto-subnet`; harness stays `ditto-harness`.
- **Platform keeps the DB** (job queue, payment replay-protection, submission
  status, score pool). The chain stores outcomes (weights/stake), not workflow.
- **Validator is stateless** — no DB. It talks to the API over HTTP and uses the
  bittensor SDK directly to set weights.
- **Contract = the API's OpenAPI schema.** Validator keeps a thin client checked
  against the live schema in CI. Revisit a shared package only if drift hurts.

### D2 — Work assignment: sharded queue, k=3 (pull)
- Each submission is handed to **3 distinct validators** (`k=3`) — not all ~10,
  to keep eval cost down. Lease-based: validator polls `request-evaluation`, API
  writes `(agent_id, validator_hotkey, expires_at)` and won't re-hand a slot.
- Expired leases reassign to the next validator (e.g. a 4th); finalize on the
  first 3 valid scores.
- Fairness across validators: least-recently-assigned rotation.

### D3 — Scoring & weights: signed ledger + replicated weights
The trust boundary is **data, not code**. The API code stays closed; the score
ledger is public and self-verifying.

- Each of the 3 assigned validators posts a **signed** raw score:
  `(agent_id, validator_hotkey, raw_score, seed, signature)`.
- The API exposes these **read-only** (the transparent ledger). Signatures mean
  no one has to trust the closed API — anyone (validators *or* miners) can verify
  a validator really posted a given score; the API can't fabricate one.
- **Finalize rule:** when the 3rd valid signed score lands, score =
  **median of the 3** (median, not mean — robust to one liar/outlier).
- **Every validator** (incl. the 7 that didn't run it) pulls the public ledger
  and runs the same **deterministic, open-source** weight function
  (median → rank → mechanism → weight vector), then sets weights itself.
  Identical inputs + identical fn → identical vectors → **Yuma consensus** clips
  any deviator on chain. Trust the signatures + the function, not a peer or the
  API.
- **Reproducible/seeded data generation** (`seed` in each ledger entry) so any
  score can be re-run and challenged → dispute path before heavier redundancy.
- Final mechanism (KOTH / top-K / Pareto / …) is TBD — see
  [`docs/incentive-mechanism.md`](docs/incentive-mechanism.md).

### D4 — Smoke test (initial)
Basic gate only: harness passes Rust lint + compiles + builds. Expand later
(manifest structure, import allowlist, schema diff, runtime sanity).

---

## 2. Phases

### Phase 0 — Split & infra (unblocks everyone)
- [x] **[Dan]** Split API → `ditto-platform` repo (api_server + db + chain +
      api_models + payment_verifier + pricing + storage).
- [x] **[Ethan]** Merge miner CLI → `dev` in `ditto-subnet` (PR #10; UX bundle #13).
- [~] **[Nick]** Provision API compute instance; create API repo shell (CI,
      deploy, env); stand up Postgres + minio beside the API. *(platform repo +
      dev VM exist; the VM still serves a stub — real API deploy pending the
      deploy-SA IAM fix.)*
- [x] **[Nick]** Confirm access to the hosted local subtensor; document endpoints.
      *(`ws://68.183.141.180:80`, netuid 3 — see `docs/dev-e2e-handoff.md`.)*
- [ ] **[Nick]** Stand up a **testnet** target for the chain.

### Phase 1 — Walking skeleton (the plumbing) — top priority
One agent flows miner → API → validator → on-chain weights, everything stubbed.
- [x] API up against the dev chain (run locally against `ws://68.183.141.180:80`,
      netuid 3; `/health` shows db + chain ok) (Dan + Nick).
- [x] Miner uploads successfully against the chain — verified end-to-end on the
      dev chain (real eval-fee payment → verifier → agent stored) (Ethan + Dan).
- [~] Validator skeleton: poll queue → download tarball → submit a (mock) score →
      set weights. Worker relocated into `ditto-subnet`; mock-bench hook in;
      weight-setting blocked on the localnet (staking disabled → no vpermit) (Dan).
- [ ] **Exit criteria:** upload an agent, watch `uploaded → evaluating →
      scored`, see weights land on chain. *(Blocked on the vpermit gate — enable
      subtoken via Alice/sudo + stake the validator. See `docs/dev-e2e-handoff.md`.)*

### Phase 2 — Real components (parallel, after skeleton)
- [ ] **[Nick]** Evaluator sandbox + build + runtime; egress proxy (OpenRouter
      allowlist + cost cap).
- [ ] **[Nick + Omar]** Synthetic data generation (seeded/reproducible) + `ditto
      bench` + eval_runner + failure_classifier.
- [ ] **[Ethan]** Finish miner: wire deferred tar checks once harness interface
      is frozen; UX polish; `logs` command (optional).
- [ ] **[Dan]** Validator-facing endpoints + pre-screen/smoke-test loop.
- [ ] **[Dan]** Scoring function + incentive mechanism + validator weight loop.
- [ ] **[TBD]** Validator deployment tooling: pm2 startup script + git-watching
      autoupdater (starts the validator under pm2, polls `main` every 5–10 min,
      and on a new commit pulls + `uv sync` + `pm2 restart`). Open-ended — assign
      later.

### Phase 3 — Observability
- [ ] **[Omar]** W&B run logging (scores, eval runs, winner history).
- [ ] **[TBD]** Public dashboard: current winner + key stats.

---

## 3. Validator-facing API build order (Dan)

Build one at a time (same cadence as intake). Each notes its DB addition.

> **Built so far** (platform `feat/validator`, names diverged from the original
> sketch below): `GET /api/v1/validator/queue` (agents awaiting eval),
> `GET /api/v1/validator/agent/{id}/artifact` (presigned tarball URL), and
> `POST /api/v1/validator/agent/{id}/score` (signed score → `scores` table). The
> register/lease/heartbeat steps below are still pending.

1. **Validator auth + `POST /validator/register`** — sr25519 sig auth +
   vpermit/stake check. Gates everything below. → `validators` table.
2. **`GET /validator/request-evaluation`** — atomically lease the next
   `screening_passed` agent, flip to `evaluating`. → `evaluations`/lease rows
   (the `agents_status_evaluating_idx` partial index already exists).
3. **`GET /validator/agent/{id}/download`** — stream tarball from S3.
   **Add `S3StorageClient.get_object`** (only put/exists today).
4. **`POST /validator/heartbeat`** — liveness + progress; reclaim stalled leases.
5. **`POST /validator/submit-score`** — write raw scores to the pool; agent →
   `scored`. → `scores` table.
6. **`GET /scoring/scores`** — public score pool the validators read to compute
   weights. (Weights computed validator-side, not here.)
7. **`/admin/*`** — ban hotkey (`banned_hotkeys`, already referenced as a
   deferred upload check), force re-eval. Lowest priority.

Pre-screen/smoke-test (the `screening` states) slots between intake and step 2.

---

## 4. Owner summary

| Person | Owns |
| --- | --- |
| **Dan** | Repo split (P0) · validator-facing endpoints + pre-screen (P2) · scoring/incentive + validator weight loop (P2) |
| **Nick** | API compute + repo shell + infra (P0) · testnet (P0) · evaluator sandbox + proxy (P2) · data-gen + bench *with Omar* (P2) |
| **Ethan** | Merge miner CLI (P0) · finish miner: deferred tar checks, UX, `logs` (P2) |
| **Omar** (½) | W&B (P3) · data-gen + bench *with Nick* (P2) |
| **Joint** | Phase 1 walking skeleton |

---

## 5. Risks / open items

- **Harness interface must be frozen early** — both Ethan's deferred tar checks
  and Nick/Omar's evaluator depend on it. Write the contract before Phase 2.
- **Omar is half-time on critical-path bench work** — keep the evaluator sandbox
  (Nick) separable from data-gen (shared) so Nick isn't blocked.
- **Incentive mechanism undecided** — blocks the scoring function; resolve from
  [`docs/incentive-mechanism.md`](docs/incentive-mechanism.md) before P2 scoring.
- **Copy/plagiarism resistance** — uploaded harnesses are downloadable; the
  mechanism + first-seen timestamps + plagiarism checks must address resubmission
  of the current winner. (Central design risk; see incentive doc.)
