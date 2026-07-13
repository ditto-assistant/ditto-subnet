# Road to Production — SN118

**Snapshot: 2026-07-08.** The single, current checklist of everything remaining
before a mainnet (finney) rollout. This is the *forward-looking* companion to
`NEXT-STEPS.md` (which carries the full history + rationale); when the two
disagree, this file is newer. Status verbs: **DONE** (built + verified) ·
**CODE-DONE** (merged, not yet proven on a live network) · **PARTIAL** ·
**TODO** · **DECISION** (needs a human call).

> **⚠ There is no testnet — only the dev localnet and prod (finney).** Every
> pre-prod rehearsal happens on the localnet; **finney is the first real chain**
> the production weight path (Pylon delegation, commit-reveal, real `version_key`,
> u16 normalization) ever touches. There is no testnet dress rehearsal, so the
> localnet rehearsal must be maximized and the finney bring-up must be *guarded*
> (low stake / small run / verify each hop before full). Earlier revisions of
> this doc assumed a `testnet → finney` step; that step does not exist.
>
> **2026-07-08 updates:** C-ISO applied + verified on the dev validator (§3);
> E1 Pylon write-path is self-serve + infra-prepped (§5); the migration spine is
> re-framed localnet → finney below.
>
> **2026-07-12 updates:** DittoBench scoring hardened and redeployed (bounded
> canary gate, multi-family metamorphic factor, 0.5/0.5 composite), datagen
> published and the api pinned to v0.7.0, and the first reference baseline is
> published (`dittobench-api/docs/BASELINES.md`): the stock harness scores
> composite **0.492 ± 0.013 SE** at `run_size=full` under Qwen3-32B. Scoring is
> judge-free (deterministic grader, no LLM judge). The measured noise floor is
> folded into B-KOTH (§5).

---

## 1. Where we are (verified)

The whole pipeline now runs **unattended, non-mock, end to end on the dev
localnet**:

```
miner upload → screener (auto build-gate) → validator sweep → dittobench
  (docker build · seed · run · deterministic grade) → signed composite → scores
  ledger → KOTH+ATH weights → set_weights ACCEPTED on-chain
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
- **Benchmark content ready for `bench_version 2`** (2026-07-12): judge-free
  deterministic scoring, hardened (bounded canary + multi-family metamorphic),
  datagen public and pinned (v0.7.0), reference baseline published. The stock
  reference harness scores composite **0.492 ± 0.013 SE** at `run_size=full`, so a
  valid non-zero full composite for a real harness is demonstrated off-chain; the
  §2.1 gap is narrowed to registering + sweeping a working miner through the
  validator (the queued on-chain agent is a broken stub scoring 0.000).

Everything below is what stands between that and a real network.

---

## 2. Critical path to mainnet (ordered — each gates the next)

1. **Full-scale E2E proof** (§2.1) — prove the real production `run_size=full`
   path end to end, incl. the just-fixed `/seed` body limit. *(localnet)*
2. **Sandbox egress allowlist + isolation** (§3, C-ISO) — ✅ **DONE**: applied +
   verified on the dev validator (2026-07-08). Optional deeper isolation
   (seccomp/gVisor) remains.
3. **Commit-reveal weights** (§4, W-CR) — ✅ **code landed** (2026-07-08):
   commit-reveal **v3** needs no reveal call (`set_weights`/Pylon do the timelock
   commit; the chain auto-reveals), and the validator now **detects + logs +
   guards** the CR mode (`VALIDATOR_REQUIRE_COMMIT_REVEAL`). Remaining is
   operational, not code: enable the `CommitRevealWeightsEnabled` hyperparameter
   on finney (E3) and confirm it at the guarded cutover (first real chain).
4. **Maximize the localnet rehearsal of the Pylon weight path** (§4/§5, E1/W-PYLON)
   — the production weight path is 100% delegated to Pylon; `put_weights` is
   already validated live on the localnet. Push localnet coverage as far as it
   goes (identity write, permit/stake self-checks, version_key stamping) so the
   finney-only unknowns (real commit-reveal, u16 at scale, chain `version_key`)
   are the *only* things first seen on finney. *Highest-leverage rehearsal.*
5. **Multi-validator consensus (k=3 + median-of-3)** (§3, F-MV) — decentralize
   scoring off the single owner validator.
6. **Guarded finney cutover under the subnet-owner UID + real E2E on-chain**
   (§5, E4) — **the only real-chain step; there is no testnet before it.** Bring
   it up guarded: small `run_size` / low stake first, verify each hop (Pylon
   write, normalization, commit-reveal, weight resolution to UID) before full.

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
- [x] **Two C-ISO host-channel regressions found + fixed (2026-07-08, infra#13).**
      C-ISO moved the harness off the default docker0 bridge onto the isolated
      sandbox subnet, taking it outside the pre-existing `172.17.0.0/16` host
      allow, so both host-local dittobench services it needs went dark: (a) the
      **Ollama** embedding endpoint (memory seeding) — every full run 500'd at
      `/seed` since ~18:32; (b) the Phase-C **`tool_endpoint`** (ephemeral host
      port) — `observed=0`, so every observable tool case was capped at 0.5 and
      the efficiency term lost. Both re-granted with scoped UFW **INPUT** allows
      (host-local; external egress stays proxy-only). Verified live: full runs
      now clear seeding and reach `done`; sandbox→host ephemeral probe blocked
      before / reachable after.
- [ ] **Miner submits a *working* harness** (funded coldkey + a hotkey
      **registered on netuid 3**). Owner-run (key custody). The only agent in the
      dev queue (`0453574c`, miner `5E7e…`) is a **broken stub** — it burns ~20k
      LLM tokens but emits no valid tool calls and recalls nothing, so it
      correctly scores `composite=0.000` (tool + memory both 0). Not a pipeline
      bug; it just proves nothing about a *good* full run.
- [ ] Agent auto-flows screener (compiles #9) → `evaluating` → full scoring →
      real full composite in the ledger.
- [x] **Platform surfaces `n` on `GET /scoring/scores`** (X-LEDGER-N, DONE
      2026-07-08 — platform#38 + subnet#65, merged + deployed). The validator's
      `MIN_ELIGIBLE_CASES=100` floor now bites instead of failing open, so a
      small run (n=12) can no longer be the on-chain champion. With no eligible
      full run yet, the fold is correctly empty (the prior small-run champion is
      no longer reinforced).
- **Acceptance:** a real `full` composite for a real harness in the ledger, and
  the champion weight resolves to the miner's UID on-chain.

**Status 2026-07-08:** the *mechanism* is proven end-to-end — full-size
datagen+seeding completes, weights resolve to a registered miner's UID on chain
(uid 5 = 0.9), and that write now goes over the **Pylon identity path** (§2.4).
What's left for a clean acceptance is content, not plumbing: a *good* agent
scored at `full` (the queued one is broken) **and** the platform surfacing `n`
so the full composite — not a stale small run — is the champion.

**Localnet weight-resolution note:** a registered miner (`5CLUBKGj` = uid 5) now
resolves its champion weight on chain (0.9); an *unregistered* scored hotkey
(`5FHneW46`) is still correctly skipped from the weight vector — a localnet
artifact that disappears where miners must register to submit.

---

## 3. Robustness & anti-gaming (before real volume)

| ID | Item | Status | Notes |
|----|------|--------|-------|
| C-ISO | **Sandbox egress allowlist + seccomp/gVisor** | **DONE** (deeper isolation optional) | **Applied + verified on the dev validator 2026-07-08** (infra#12). Sandbox-side plumbing (`dittobench-api/internal/sandbox`: `--cap-drop ALL`, `--network`, proxy-env injection, `--pids-limit`, `no-new-privileges`) + the allowlisting proxy (`cmd/egress-proxy`, dittobench-api#21) + the host enforcement (Ansible `dittobench` role: `ditto-sandbox` docker network, proxy systemd unit, `DOCKER-USER` firewall that DROPs sandbox egress except → the proxy). Live smoke test passed: allowlisted `openrouter.ai` via proxy → 200; non-allowlisted → denied; **direct dial → blocked (fail-closed)**. **Follow-up (2026-07-08, infra#13):** moving the sandbox off the default docker0 bridge silently broke the two host-local dittobench services the harness needs (both were outside the pre-existing `172.17.0.0/16` allow) — Ollama (memory seeding, `/seed` 500s) and the Phase-C `tool_endpoint` (ephemeral host port → `observed=0`, tool cases capped). Re-granted with scoped UFW **INPUT** allows (host-local; external egress unchanged). Lesson: enabling C-ISO needs a same-host-service reachability check, not just an external-dial test. Optional deeper isolation (seccomp default-deny profile, gVisor/Kata, read-only rootfs) tracked in `dittobench-api/docs/sandbox-egress-hardening.md`. |
| C-REPLAY | **Signature replay-cache / nonce+expiry** | **PARTIAL** | Sigs bind the full payload (no cross-agent replay), but add a server-side nonce+expiry replay cache so a captured signed message can't be re-applied. |
| C-TUNE | **Plagiarism threshold tuning + review automation** | **PARTIAL** | Two-channel fingerprint gate merged; lexical (0.75/0.95) + structural (0.85/0.98) tolerances are conservative guesses — tune against a real corpus. `ath_pending_review` drained by hand (`scripts/resolve_review.py`); build a reviewer workflow. |
| C-RATE | **API abuse controls** | **TODO** | Global + per-hotkey rate limits, request-size limits, auth throttling on public platform endpoints (today: permit-check + signatures only). |
| C-VERIFY | **Verifiable / replicable scoring** | **DECISION** | Scoring is trusted to the single dittobench operator today. Reproducible seeds are already in the ledger; decide whether/when to build toward replicable scoring (couples to multi-validator). Our call, our timeline. |
| F-MV | **Multi-validator: k=3 sharded queue + median-of-3** | **TODO** | Lease-based assignment to 3 distinct validators, finalize the median of 3 signed scores, migrate stub→target endpoint names, onboard >1 validator. Decentralizes trust off the single owner validator. |
| V-ROBUST | **Weight-setting robustness (residual)** | **CODE-DONE** | version_key/permit/tempo done. Residuals merged in [#39](https://github.com/ditto-assistant/ditto-subnet/pull/39): on-chain tempo/`weights_rate_limit` read stretches the effective epoch, exponential backoff (block-time base on rate-limit rejection), `VALIDATOR_MIN_STAKE_TAO` self-check arm. Unproven on a live network — first proven at finney cutover (no testnet), with W-PYLON. |

---

## 4. Bittensor-ecosystem conformance

The production weight path **delegates all chain conformance to Pylon**
(normalization, u16, UID resolution, commit-reveal, version_key) and does **not
verify any of it in-repo**. With **no testnet**, the localnet is the only
rehearsal and **finney is the first real chain** this path touches — so verify
everything the localnet *can* exercise there, and treat the finney-only pieces
(real commit-reveal, chain `version_key`, u16 at production scale) as guarded
first-runs on finney. The in-repo SDK/localnet path is a declared fallback.

| ID | Item | Status | Notes |
|----|------|--------|-------|
| W-VK | version_key pin | **CODE-DONE** | SDK path stamps `version_key` (default `ditto.__spec_version__`, env `VALIDATOR_WEIGHT_VERSION_KEY`). Confirm the Pylon-derived version_key matches at finney cutover (no testnet to confirm it on first). |
| W-PERMIT | validator_permit self-check | **CODE-DONE** | Skips (fail-open) when the hotkey lacks a permit. Min-stake arm (`VALIDATOR_MIN_STAKE_TAO`) added in PR #39. |
| W-CADENCE | Tempo-decoupled cadence | **CODE-DONE** | `VALIDATOR_SWEEP_SECONDS` (120s) vs `VALIDATOR_EPOCH_SECONDS` (3600s). PR #39 additionally reads the target network's on-chain `weights_rate_limit` and stretches the effective epoch to it. |
| W-CR | **Commit-reveal** | **CODE-DONE** | Corrected: under commit-reveal **v3** (bittensor 10.3.2) there is **no separate reveal call** — `set_weights`/Pylon do the timelock commit and the chain auto-reveals after `RevealPeriodEpochs`. The worker now **reads + logs** the CR mode each weight-set and guards it: `VALIDATOR_REQUIRE_COMMIT_REVEAL` logs an error (still submits — refusing would zero the chain) when CR is off but expected on. So the "first-class reveal step" earlier docs described is obsolete. Remaining is **operational**: enable the `CommitRevealWeightsEnabled` hyperparameter on finney (owner sudo, E3) + confirm at the guarded cutover (W-PYLON/E4). Off on dev netuid 3. |
| W-PYLON | **Verify Pylon delegation (localnet → finney)** | **PARTIAL — deployed-role validated on localnet** | `put_weights` via Pylon identity validated live on the localnet twice over: first by hand (2026-07-07), then **through the deployed `validator_pylon` Ansible sidecar role (2026-07-08, infra#13)** — dev validator flipped off the SDK fallback (`validator_use_sdk_weights: false`), sidecar materialized the hotkey from the mnemonic and connected to netuid 3, and the worker set weights via `ditto.chain.client put_weights submitted for netuid=3 with 2 entries`. So the *prod weight path + its provisioning role* are both exercised. What the localnet *cannot* prove — real commit-reveal, chain `version_key`, u16 `max_weight_limit` normalization at scale — has **no testnet to prove it on**, so it is a guarded first-run at finney cutover (E4). |
| W-PARAMS | Chain hyperparameters | **TODO** | Set tempo, immunity period, weights-rate-limit, validator-permit threshold, registration burn + recycle for the target network. |

---

## 5. Network migration (localnet → finney — no testnet)

| ID | Item | Status | Notes |
|----|------|--------|-------|
| E1 | **Pylon identity (write) credentials** | **PARTIAL — self-serve + infra-prepped, NOT an external dependency** | The Pylon write token is a **self-generated bearer secret** (`openssl rand -base64 32`) — Pylon holds the mounted hotkey and signs `set_weights` itself; the token just authorizes the client. Confirmed from resi-labs-ai/RESI-models (same `backenddevelopersltd/bittensor-pylon` image) and **validated live on the dev localnet** — first by hand (2026-07-07), then **through the deployed `validator_pylon` sidecar role on the dev VM (2026-07-08, infra#13)**: tokens generated (`openssl rand -base64 32`) + stored in Secret Manager + SA-granted, `validator_pylon_identity_enabled: true` + `validator_use_sdk_weights: false`, converged, and the worker's real `put_weights` landed over the sidecar (no SDK fallback). So the flag-flip below is now a *proven* procedure, not just prepped. Remaining for finney is the same flip in the finney host_vars against real stake. **No testnet — first live-chain proof is finney (W-PYLON, E4).** |
| E2 | Finney permit + stake (owner UID) | **TODO** | Validation runs under the **subnet owner's UID** — no separate validator registration/burn. Stake the owner hotkey past the `validator_permit` threshold on finney (no testnet stake step first). |
| E3 | Chain parameters on finney | **TODO** | See W-PARAMS + enable commit-reveal (W-CR); re-tune the alpha pool / `TaoWeight`. Set directly on finney (owner sudo) — no testnet to trial them on. |
| E4 | **Guarded finney cutover** | **TODO** | The only real-chain step. Point platform + validator at finney SN118, flip `enable_validator`, run the deploy runbook, and **verify each hop guarded** (small `run_size` / low stake first): Pylon write → normalization/u16 → commit-reveal → weight resolves to the champion UID → then full. No testnet dress rehearsal precedes this. |
| B-KOTH | Validate KOTH+ATH params vs real scores | **TODO (data in, 2026-07-12)** | Measured over N=24 full runs (`dittobench-api/docs/BASELINES.md`): within-run `composite_stderr` ~0.041, between-seed dataset-difficulty sd ~0.049, k=3 median model-noise sd ~0.031. The flat 1% relative margin (~0.005 at composite 0.49) is roughly 10x below this, so it must not be the only guard. The v3 z-band (`_beats` in `weights.py`) already takes `max(flat margin, composite_stderr-based band)`, which covers within-run noise **only if the platform surfaces `composite_stderr`** (`weights.py:154` flags it optional: verify it does, else the fold silently falls back to the ~0.005 flat margin). Even then, each agent is scored on its own agent-bound seed (P2/N1), so the champion/challenger comparison also carries the ~0.049 seed-difficulty spread that `composite_stderr` does NOT capture: a challenger can take the crown by drawing an easier dataset. The designed fix is **P4 (multi-seed champion confirmation)**: require the challenger to beat the champion on the median over K=3 common CRN seeds before a dethrone, which removes the seed-difficulty confound. P4 is spec'd and implemented on the parked `nick/p4-multi-seed-confirmation` branches (`prod-final-hardening-plan.md`), frozen pre-launch and sequenced for the week after launch. These measured numbers are the quantitative case for it: the ~0.049 seed spread and ~0.041 within-run stderr both dwarf the 0.005 flat margin, so until P4 lands, at minimum surface `composite_stderr` and set `dethrone_z` so the flat margin is never the sole guard. Tune via `VALIDATOR_KOTH_*`. |
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
- [x] Sandbox egress-restricted + isolated (C-ISO) — applied + verified on dev
      2026-07-08; deeper isolation (seccomp/gVisor) optional.
- [ ] Weights set via **verified** Pylon identity-write on **finney** (no
      testnet), with commit-reveal on and version_key confirmed (W-CR, W-PYLON, E1).
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
| X-BENCHVER | `bench_version` mislabel | **DONE (2026-07-12)** | The live benchmark is `bench_version = 2` (authoritative: `const BenchVersion = 2` in the datagen `protocol/epoch.go`). The four flagged comments now correctly read "bench_version 2" (`ditto-platform` `docs/submission-contract.md:66`, `ditto/api_server/scoring_gate.py:40`; `ditto-subnet` `ditto/validator/config.py:279`, `ditto/tests/validator/test_config.py:32`). Remaining "3" references are legitimate forward-references (future `bench_version 3` work, or tests exercising the resume-at-3 bump policy), not mislabels. |
| X-TRAJ | **Behavioral anti-copy channel exports names only** | **DECISION 🔴** | The tool-call trajectory is our only forge-proof runtime copy signal, and it gates the prompt-fusion hold in `SEMANTIC-CLONE-PREVENTION.md`. But the forwarded `ScoreReport`/`CaseScore` carries only the ordered observed tool **names** (`CaseScore.Called []string`, `dittobench-api/pkg/protocol/protocol.go:218`); the full `(name, args, hop)` is *recorded* server-side (`ToolExecRequest`) but **not exported**. `PROTOCOL.md:205-210` overstates it as an `(name, args, hop)` sequence forwarded to the platform. Decision: enrich the export (per-case args/hop) before building the behavioral gate, or the convergence-robust signal that unblocks the prompt-fusion hold can only compare name-sequences. Doc corrected to match today's shipped shape meanwhile. |
| X-SHADOW | **Semantic-clone gate is shadow-only** | **KNOWN (S2)** | Production anti-copy today = exact-bytes / repack / normalized-source / lexical / structural / size → *human review* (`ditto-platform/scoring_gate.py`). The **code-embedding vector is stored but not gating** (`upload.py:336-341`, disabled by default), the **prompt-fusion hold is deferred** pending an orthogonal signal (`scoring_gate.py:163-168`), and the embedder Cloud Run service is **gated OFF + unprovisioned** (`infra` `enable_embedder=false`). Expected per `SEMANTIC-CLONE-PREVENTION.md` S2, but state it plainly at launch: semantic clone *prevention* is not live; convergence-robust gating is blocked on X-TRAJ. Amplifies C-TUNE. |
| X-HARDEN | **Platform public-endpoint hardening** | **TODO** | Before public exposure: unset `DITTO_DEV_ALLOW_UNPERMITTED_VALIDATOR` (`ditto-platform/endpoints/validator.py:133-143`); front the app with a reverse proxy for **TLS + rate limiting** — public GET endpoints have no app-level limits (`retrieval.py:4`, deferred to a proxy not yet stood up); and bind validator read-GETs to a per-request nonce/timestamp signature (today: `X-Validator-Hotkey` + permit only, `validator.py:27-29`). Amplifies C-RATE. |
| X-BENCHHOST | **dittobench-api deploy target vs mode B** | **DOC** | Mode B (presigned `tarball_url`, the validator's real path) needs a Docker daemon; the README's "Deploy (Cloud Run)" section describes the **practice** service (no Docker → `harness_url` only). `infra` co-locates a **second** dittobench-api instance on the Docker-capable validator VM (`127.0.0.1:8080`), which is where mode-B scoring actually runs. Not a code gap — the README is correct that the Docker path is "the on-chain validator's path"; add a one-line pointer so the two deploy contexts aren't conflated. |
| X-INFRA-PROD | **No production infra exists** | **TODO 🔴** | Largest gap cluster (feeds E1/E2/E4/O-*). `infra` is dev-only: dev+"prod" share the `ditto-app-dev` project + tfstate; validator & embedder are **gated OFF and unprovisioned**; the validator/screener target the **dev localnet (netuid 3)**, not finney 118; ~~weights use the SDK path~~ (dev now rehearses the **Pylon identity path** — infra#13, W-PYLON); the platform **DB password lands in tfstate**; Postgres is a **single non-HA VM** holding both dev+prod DBs; the validator **reuses the platform SA** (`validator.tf:66` flags "prod should use a dedicated SA"). A finney deploy needs a genuine prod-isolation story, not a flag flip. |
| X-LEDGER-N | **Fold ledger doesn't surface `n` → eligibility fails open** | **DONE (2026-07-08)** | `GET /scoring/scores` returned `composite` per miner but **not `n`**, so the validator's `MIN_ELIGIBLE_CASES = 100` floor (`weights.py:_entry_eligible`) read `n` via `getattr`, found it absent, and **failed open** — a *small* run (n=12) counted as eligible and drove the on-chain champion (uid 5 = 0.9 was `5CLUBKGj`'s n=12 run). Fixed: `n` (required `int`) added to the `LedgerEntry` wire model + endpoint mapping — platform#38 + subnet#65, merged to `dev` + deployed. Verified live: `/scoring/scores` now returns `n` (12/12/114); the fold drops both n=12 small runs and the n=114 zero-composite stub. **Remaining for §2.1 acceptance is content, not plumbing:** a *good* agent scored at `full` (none currently eligible → the fold is correctly empty; the stale on-chain 0.9 persists until a real full champion overwrites it — an empty fold is a no-op, not an active zero). |

**Reconciliation status (2026-07-08):** X-BENCHVER + X-TRAJ (doc) + X-BENCHHOST
are being fixed now (comment/doc-only, one PR per repo). X-SHADOW / X-HARDEN /
X-INFRA-PROD are tracked here and sequence behind the §2 spine.

---

*Ownership: we own the whole stack — platform, screener, validator, miner CLI,
dittobench scorer, chain/emissions config, and every economic knob. The §2
sequence is how one team drives it to production; there are no external owners to
hand a workstream to.*
