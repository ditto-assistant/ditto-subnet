# Road to Production — SN118

**Snapshot: 2026-07-07.** The single, current checklist of everything remaining
before a mainnet (finney) rollout. This is the *forward-looking* companion to
`NEXT-STEPS.md` (which carries the full history + rationale); when the two
disagree, this file is newer. Status verbs: **DONE** (built + verified) ·
**CODE-DONE** (merged, not yet proven on a live network) · **PARTIAL** ·
**TODO** · **DECISION** (needs a human call).

---

## 1. Where we are (verified)

The whole pipeline now runs **unattended, non-mock, end to end on the dev
localnet**:

```
miner upload → screener (auto build-gate) → validator sweep → dittobench
  (docker build · seed · run · LLM judge) → signed composite → scores ledger
  → KOTH+ATH weights → set_weights ACCEPTED on-chain
```

- **A1 first real E2E — PROVEN (2026-07-07)** at `run_size=small`: agent
  `2b52b610` produced a real composite **0.522**, signed (sr25519) into the
  `scores` ledger, and drove an on-chain `set_weights` accept. *Full-size proof
  is in flight (Phase 2, see §2.1).*
- **Screener** live on `ditto-validator-dev`; two live bugs fixed (20 MiB cap;
  dummy LLM key so the harness boots `/health`).
- **Validator chain-conformance** (version_key pin, `validator_permit`
  self-check, tempo-decoupled sweep vs weight cadence) merged + deployed.
- **dittobench** cost-capped (`LLM_MAX_TOKENS` / `LLM_RUN_TOKEN_BUDGET`),
  tarball-sha cross-checked, error paths redacted.
- **Anti-copy** two-channel fingerprint gate (lexical + AST) merged.
- **Emissions** flow on the localnet; **W&B telemetry + public dashboard** live.
- **Banned-hotkeys** table + enforcement live.

Everything below is what stands between that and a real network.

---

## 2. Critical path to mainnet (ordered — each gates the next)

1. **Full-scale E2E proof** (§2.1) — prove the real production `run_size=full`
   path end to end, incl. the just-fixed `/seed` body limit.
2. **Sandbox egress allowlist + isolation** (§3, C-ISO) — before running real
   scoring at any volume; untrusted miner code currently has full network egress.
3. **Pylon write credentials on testnet** (§5, E1) — the production weight path
   is 100% delegated to Pylon and **unverified in-repo**; standing it up on
   testnet is the first real test of normalization / u16 / commit-reveal /
   version_key. *Highest-leverage single item.*
4. **Testnet cutover under the subnet-owner UID** (§5, E2) — move off the dev
   localnet; no separate validator registration/burn.
5. **Commit-reveal weights** (§4, W-CR) — enable + implement the reveal step for
   the target network.
6. **Multi-validator consensus (k=3 + median-of-3)** (§3, F-MV) — decentralize
   scoring off the single owner validator.
7. **Mainnet (finney) cutover + real E2E on-chain** (§5, E4).

Everything outside this spine (ops hardening, tuning, docs) parallelizes.

---

## 2.1 Phase 2 — full-scale E2E proof · IN PROGRESS

**Goal:** one agent flows the entire path at `run_size=full` and lands a real
composite + resolved on-chain weight.

- [x] Root-cause + fix the full-run blocker: the reference harness used axum's
      2 MB default body limit, so a full seed haystack (842 pairs / 2258
      subjects) 413'd at the **seeding** stage — every starter-kit miner.
      Fixed in `dittobench-starter-kit#9` (`DefaultBodyLimit::max(256 MiB)`).
- [x] Validator reconverged to `run_size=full`; screener + conformance code live.
- [x] Submission tarball built from the fixed kit + pre-flight passed.
- [ ] **Miner submits** the tarball (funded coldkey + a hotkey **registered on
      netuid 3**; the dev-API/localnet-chain wiring). Owner-run (key custody).
- [ ] Agent auto-flows screener (compiles #9) → `evaluating` → full scoring →
      real full composite in the ledger.
- [ ] **Merge cleanup:** `dittobench-starter-kit#9` merged; its compile is
      validated by the screener build at submit time.
- **Acceptance:** a real `full` composite for a real harness in the ledger, and
  the champion weight resolves to the miner's UID on-chain (needs the miner
  registered — see the localnet gap below).

**Localnet weight-resolution gap:** in the small proof the scored miner hotkey
was *not* registered on netuid 3, so its 0.9 champion weight mapped to no UID and
was skipped (only the validator's tail 0.1 landed). Register submitting miners on
the localnet, or accept it as a localnet-only artifact that disappears on a real
network where miners register to submit.

---

## 3. Robustness & anti-gaming (before real volume)

| ID | Item | Status | Notes |
|----|------|--------|-------|
| C-ISO | **Sandbox egress allowlist + seccomp/gVisor** | **TODO 🔴** | dittobench builds + runs attacker-controlled tarballs on the host daemon with full egress. Needs an egress proxy/allowlist + deeper isolation. Cost cap bounds spend meanwhile but not exfiltration/abuse. **Top robustness gap.** |
| C-REPLAY | **Signature replay-cache / nonce+expiry** | **PARTIAL** | Sigs bind the full payload (no cross-agent replay), but add a server-side nonce+expiry replay cache so a captured signed message can't be re-applied. |
| C-TUNE | **Plagiarism threshold tuning + review automation** | **PARTIAL** | Two-channel fingerprint gate merged; lexical (0.75/0.95) + structural (0.85/0.98) tolerances are conservative guesses — tune against a real corpus. `ath_pending_review` drained by hand (`scripts/resolve_review.py`); build a reviewer workflow. |
| C-RATE | **API abuse controls** | **TODO** | Global + per-hotkey rate limits, request-size limits, auth throttling on public platform endpoints (today: permit-check + signatures only). |
| C-VERIFY | **Verifiable / replicable scoring** | **DECISION** | Scoring is trusted to the single dittobench operator today. Reproducible seeds are already in the ledger; decide whether/when to build toward replicable scoring (couples to multi-validator). Our call, our timeline. |
| F-MV | **Multi-validator: k=3 sharded queue + median-of-3** | **TODO** | Lease-based assignment to 3 distinct validators, finalize the median of 3 signed scores, migrate stub→target endpoint names, onboard >1 validator. Decentralizes trust off the single owner validator. |
| V-ROBUST | **Weight-setting robustness (residual)** | **CODE-DONE** | version_key/permit/tempo done. Residuals merged in [#39](https://github.com/ditto-assistant/ditto-subnet/pull/39): on-chain tempo/`weights_rate_limit` read stretches the effective epoch, exponential backoff (block-time base on rate-limit rejection), `VALIDATOR_MIN_STAKE_TAO` self-check arm. Unproven on a live network (testnet, with W-PYLON). |

---

## 4. Bittensor-ecosystem conformance

The production weight path **delegates all chain conformance to Pylon**
(normalization, u16, UID resolution, commit-reveal, version_key) and does **not
verify any of it in-repo** — so testnet is the first real test. The in-repo
SDK/localnet path is a declared fallback.

| ID | Item | Status | Notes |
|----|------|--------|-------|
| W-VK | version_key pin | **CODE-DONE** | SDK path stamps `version_key` (default `ditto.__spec_version__`, env `VALIDATOR_WEIGHT_VERSION_KEY`). Confirm the Pylon-derived version_key matches on testnet. |
| W-PERMIT | validator_permit self-check | **CODE-DONE** | Skips (fail-open) when the hotkey lacks a permit. Min-stake arm (`VALIDATOR_MIN_STAKE_TAO`) added in PR #39. |
| W-CADENCE | Tempo-decoupled cadence | **CODE-DONE** | `VALIDATOR_SWEEP_SECONDS` (120s) vs `VALIDATOR_EPOCH_SECONDS` (3600s). PR #39 additionally reads the target network's on-chain `weights_rate_limit` and stretches the effective epoch to it. |
| W-CR | **Commit-reveal** | **TODO 🔴** | Off on dev netuid 3; production needs a first-class reveal step + the chain param enabled. Without it, weights are copy-able (front-runnable). |
| W-PYLON | **Verify Pylon delegation on testnet** | **TODO 🔴** | Prove normalization/u16/`max_weight_limit`/commit-reveal/version_key actually do the right thing against a live chain via Pylon identity-write. Gated on E1. |
| W-PARAMS | Chain hyperparameters | **TODO** | Set tempo, immunity period, weights-rate-limit, validator-permit threshold, registration burn + recycle for the target network. |

---

## 5. Network migration (testnet → finney)

| ID | Item | Status | Notes |
|----|------|--------|-------|
| E1 | **Pylon identity (write) credentials** | **TODO 🔴 blocker** | Only a read token exists; the SDK path is the dev fallback. Provision `PYLON_IDENTITY_*` write creds so the production weight path works — and verify it (W-PYLON). |
| E2 | Testnet permit + stake (owner UID) | **TODO** | Validation runs under the **subnet owner's UID** — no separate validator registration/burn. Stake the owner hotkey past the `validator_permit` threshold. |
| E3 | Chain parameters on target network | **TODO** | See W-PARAMS + enable commit-reveal (W-CR); re-tune the alpha pool / `TaoWeight` per network. |
| E4 | **Mainnet (finney) cutover** | **TODO** | Point platform + validator at finney SN118, flip `enable_validator`, run the deploy runbook, verify each hop, run a real E2E on mainnet. |
| B-KOTH | Validate KOTH+ATH params vs real scores | **TODO** | Once real composites exist, sanity-check the 1% margin + 90/10 split against the observed score spread + between-seed variance; tune via `VALIDATOR_KOTH_*`. |
| B-TAIL | Participation-tail economics | **DECISION** | Tail size, min-score floor, or pure winner-take-all at mainnet. |

---

## 6. Reliability & operations

| ID | Item | Status | Notes |
|----|------|--------|-------|
| O-OBS | Observability (metrics + alerts) | **PARTIAL** | W&B + public dashboard live. Add validator metrics (sweep duration, put_weights success, ledger size), platform request/error/DB metrics, and real alerting (Datadog MCP available). |
| O-DB | Production Postgres | **TODO** | Automated backups + PITR, migration runbook, connection pooling, retention/archival for `scores`/`agents`, a read replica for the public ledger read. |
| O-HA | HA / DR / cost ceilings | **TODO** | Platform API redundancy, dittobench scaling, queue durability, DR/state-reconstruction, LLM/VM/storage budget ceilings + alerts. |
| O-UPD | Deploy lifecycle / autoupdater | **PARTIAL** | Terraform/Ansible dev deploy done (gated; TF needs `-var=enable_validator=true` — a plain apply wants to destroy validator resources). No git-watching autoupdater; verify zero-downtime restart weight-set safety. |
| O-SEC | Secrets management & rotation | **PARTIAL** | All secrets in GCP Secret Manager. Document + exercise a rotation runbook (hotkey mnemonic, OpenRouter key, GH token, W&B key). |
| O-VAL | Third-party validator onboarding | **PARTIAL** | Run-a-validator guide drafted (`docs/VALIDATOR-ONBOARDING.md`): requirements, full env reference, key custody, verification. Still needed: the k=3 "onboard to the queue" flow (gated on F-MV) + publish. |
| Q-CI | E2E integration suite in CI (localnet) | **TODO** | Exercise the full pipeline behind the `e2e`/`localnet` markers, gated in CI. |
| Q-CHAOS | Load & chaos testing | **TODO** | Many miners/validators; inject chain outages, dittobench failures, partial writes; confirm no lost-update / no zeroed-chain / graceful degradation. |

---

## 7. Screener & scoring follow-ups

| ID | Item | Status | Notes |
|----|------|--------|-------|
| S-CONTRACT | Screener wire-model contract guard | **DONE** | Golden generated from the platform checkout + drift test + `scripts/gen_screener_contract.py`, mirroring the validator guard ([#40](https://github.com/ditto-assistant/ditto-subnet/pull/40)). |
| S-GATE | Deeper screener gate | **TODO** | `POST /seed`/`/run` smoke, a failure-reason persist, a stale-claim reset sweep, a distinct `screener_permit` vs the validator permit. |
| S-RETRY | Bounded re-score on transient failure | **TODO** | A miner whose harness errors mid-scoring is re-swept and re-run (full datagen + LLM cost) every epoch. Add a retry bound / terminal `evaluation_failed` after N attempts so a broken agent doesn't burn tokens forever. *(Surfaced by the full-run seeding failure.)* |
| M-CLI | Miner CLI completion | **PARTIAL** | Deferred upload validations (tar manifest, import allowlist, schema diff) pending the harness interface; miner UX (clearer errors, status/logs). Stale 200 MB local cap (vs the platform's 20 MiB) fixed in [#41](https://github.com/ditto-assistant/ditto-subnet/pull/41) — `ditto verify` no longer passes tarballs the server rejects. |

---

## 8. Documentation & ecosystem

- **Miner onboarding** — **drafted** (`docs/MINER-FAQ.md`): full pipeline
  walkthrough, submission contract, scoring rubric (0.6 tool / 0.4 memory),
  KOTH rules, anti-copy, transparency/verification. Still needed: a
  build-a-harness guide (with the starter kit) + a practice endpoint + publish.
- **Validator onboarding** — **drafted** (`docs/VALIDATOR-ONBOARDING.md`, O-VAL).
- **Subnet landing / lightpaper** — what SN118 rewards and why (best-artifact
  competition + KOTH+ATH anti-copy rationale).

---

## 9. Open decisions needing a human

1. **Trust model** — owner-centralized scorer (today) vs permissionless-verifiable
   scoring (C-VERIFY). Gates how much of F-MV / C-VERIFY to build now.
2. **Participation-tail economics** (B-TAIL).
3. **Registration / immunity / emission-split** target values per network.
4. **Endpoint-name migration timing** (stub → target, tied to F-MV).
5. **run_size for production** — `full` is the real config (slow, real LLM spend);
   confirm before mainnet.

---

## 10. Definition of "production ready" (exit checklist)

- [ ] Full-scale (`run_size=full`) E2E proven end to end (§2.1).
- [ ] Sandbox egress-restricted + isolated (C-ISO).
- [ ] Weights set via **verified** Pylon identity-write on testnet, with
      commit-reveal on and version_key confirmed (W-CR, W-PYLON, E1).
- [ ] ≥3 validators converging on the KOTH champion via median-of-3 (F-MV).
- [ ] Observability + alerting + DB backups + a rotation runbook (O-*).
- [ ] A green localnet E2E + chaos suite in CI (Q-CI, Q-CHAOS).
- [ ] Miner + validator onboarding docs published (§8).
- [ ] Mainnet cutover runbook executed with a real on-chain E2E (E4).

---

## 11. Cross-repo contract & doc reconciliations (2026-07-08 audit)

A full-stack read across all six repos (`ditto-subnet`, `ditto-platform`,
`infra`, `dittobench-api`, `ditto-harness`, `dittobench-starter-kit`) surfaced
the items below. None is a runtime defect in the proven localnet path; they are
contract/doc drifts and productionization gaps that amplify §3–§6. IDs are
`X-*` so they don't collide with the spine items.

| ID | Item | Status | Notes |
|----|------|--------|-------|
| X-BENCHVER | **`bench_version` mislabel** | **RECONCILING** | The live benchmark is `bench_version = 2` (authoritative in `dittobench-api/pkg/protocol/epoch.go:31`; mirrored by `ditto-platform` `endpoints/public.py:54` and the dashboard). Four **comments** wrongly label DittoBench v2 as "bench_version 3": `ditto-platform` (`docs/submission-contract.md:66`, `scoring_gate.py:40`) + `ditto-subnet` (`ditto/validator/config.py:229`, `ditto/tests/validator/test_config.py:32`). No runtime mismatch — relabel the comments to `2`. The bump policy correctly *resumes* at 3 for the first scoring change **after** v2 is live (`epoch.go:11-17`, `BENCHMARK-V2.md:527`). |
| X-TRAJ | **Behavioral anti-copy channel exports names only** | **DECISION 🔴** | The tool-call trajectory is our only forge-proof runtime copy signal, and it gates the prompt-fusion hold in `SEMANTIC-CLONE-PREVENTION.md`. But the forwarded `ScoreReport`/`CaseScore` carries only the ordered observed tool **names** (`CaseScore.Called []string`, `dittobench-api/pkg/protocol/protocol.go:218`); the full `(name, args, hop)` is *recorded* server-side (`ToolExecRequest`) but **not exported**. `PROTOCOL.md:205-210` overstates it as an `(name, args, hop)` sequence forwarded to the platform. Decision: enrich the export (per-case args/hop) before building the behavioral gate, or the convergence-robust signal that unblocks the prompt-fusion hold can only compare name-sequences. Doc corrected to match today's shipped shape meanwhile. |
| X-SHADOW | **Semantic-clone gate is shadow-only** | **KNOWN (S2)** | Production anti-copy today = exact-bytes / repack / normalized-source / lexical / structural / size → *human review* (`ditto-platform/scoring_gate.py`). The **code-embedding vector is stored but not gating** (`upload.py:336-341`, disabled by default), the **prompt-fusion hold is deferred** pending an orthogonal signal (`scoring_gate.py:163-168`), and the embedder Cloud Run service is **gated OFF + unprovisioned** (`infra` `enable_embedder=false`). Expected per `SEMANTIC-CLONE-PREVENTION.md` S2, but state it plainly at launch: semantic clone *prevention* is not live; convergence-robust gating is blocked on X-TRAJ. Amplifies C-TUNE. |
| X-HARDEN | **Platform public-endpoint hardening** | **TODO** | Before public exposure: unset `DITTO_DEV_ALLOW_UNPERMITTED_VALIDATOR` (`ditto-platform/endpoints/validator.py:133-143`); front the app with a reverse proxy for **TLS + rate limiting** — public GET endpoints have no app-level limits (`retrieval.py:4`, deferred to a proxy not yet stood up); and bind validator read-GETs to a per-request nonce/timestamp signature (today: `X-Validator-Hotkey` + permit only, `validator.py:27-29`). Amplifies C-RATE. |
| X-BENCHHOST | **dittobench-api deploy target vs mode B** | **DOC** | Mode B (presigned `tarball_url`, the validator's real path) needs a Docker daemon; the README's "Deploy (Cloud Run)" section describes the **practice** service (no Docker → `harness_url` only). `infra` co-locates a **second** dittobench-api instance on the Docker-capable validator VM (`127.0.0.1:8080`), which is where mode-B scoring actually runs. Not a code gap — the README is correct that the Docker path is "the on-chain validator's path"; add a one-line pointer so the two deploy contexts aren't conflated. |
| X-INFRA-PROD | **No production infra exists** | **TODO 🔴** | Largest gap cluster (feeds E1/E2/E4/O-*). `infra` is dev-only: dev+"prod" share the `ditto-app-dev` project + tfstate; validator & embedder are **gated OFF and unprovisioned**; the validator/screener target the **dev localnet (netuid 3)**, not finney 118; weights use the **SDK path, not Pylon identity** (`validator_use_sdk_weights=true`); the platform **DB password lands in tfstate**; Postgres is a **single non-HA VM** holding both dev+prod DBs; the validator **reuses the platform SA** (`validator.tf:66` flags "prod should use a dedicated SA"). A finney deploy needs a genuine prod-isolation story, not a flag flip. |

**Reconciliation status (2026-07-08):** X-BENCHVER + X-TRAJ (doc) + X-BENCHHOST
are being fixed now (comment/doc-only, one PR per repo). X-SHADOW / X-HARDEN /
X-INFRA-PROD are tracked here and sequence behind the §2 spine.

---

*Ownership: we own the whole stack — platform, screener, validator, miner CLI,
dittobench scorer, chain/emissions config, and every economic knob. The §2
sequence is how one team drives it to production; there are no external owners to
hand a workstream to.*
