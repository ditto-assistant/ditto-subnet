# Road to Production: SN118

**Snapshot: 2026-07-12.** Only the remaining work and decisions before a mainnet
(finney) rollout. Completed items are the footnote at the end. Status: TODO, PARTIAL (started),
DECISION (needs a human call). Code that is merged but unproven on a live chain sits in the
footnote and re-appears here only as the finney first-run that proves it.

> There is no testnet, only the dev localnet and prod (finney). finney is the
> first real chain the production weight path (Pylon delegation, commit-reveal,
> version_key, u16 normalization) ever touches. Maximize the localnet rehearsal
> and bring finney up guarded: low stake, small run, verify each hop before full.

---

## Critical path to mainnet (ordered, each gates the next)

1. **Full `run_size=full` E2E** (localnet). The mechanism is proven; the gap is
   content: register and sweep a *working* miner through the validator to land a
   real full composite. The only queued agent is a broken stub (0.000). The stock
   harness scores composite ~0.492 off-chain, so a good full run is demonstrated,
   just not yet on-chain.
2. **Maximize the Pylon weight-path localnet rehearsal** (W-PYLON / E1). Validated
   on the dev sidecar; push coverage so the finney-only unknowns (real
   commit-reveal, chain version_key, u16 at scale) are the only firsts on finney.
3. **Multi-validator deploy** (F-MV). Code is done; run >=3 independent validators
   and prove 3-scores to median to weights on the localnet.
4. **Guarded finney cutover** (E4). The only real-chain step: finney permit +
   owner-UID stake (E2), chain hyperparameters + commit-reveal enabled
   (E3 / W-PARAMS), then verify each hop guarded before full.

Everything else parallelizes.

---

## Remaining work

### Chain / weights (finney-first, no rehearsal)
- **W-PARAMS** (TODO): set tempo, immunity, weights-rate-limit, permit threshold, registration burn/recycle on finney.
- **W-PYLON** (PARTIAL): the prod weight path is 100% Pylon and validated on localnet; real commit-reveal, chain version_key, and u16 normalization are guarded first-runs at cutover.
- **E1** (PARTIAL): the Pylon write token is self-serve (`openssl rand`), proven on dev; redo the flip in finney host_vars against real stake.
- **E2 / E3 / E4** (TODO): finney permit + owner-UID stake; chain params + commit-reveal on; guarded cutover runbook.
- **B-KOTH** (z-band LIVE; P4 dormant): the composite_stderr z-band is live end to end and engages on every dethrone comparison in the weight fold (it does not depend on re-scoring), so within-run noise (~0.041 vs the ~0.005 flat margin) is handled at launch. P4 multi-seed + CRN common-seed are code-complete but only fire on a `bench_version` bump (none at launch). Before the first bump they need **B-RESCORE-TICKET** (below), because a SCORED champion cannot currently obtain a re-score ticket.
- **B-RESCORE-TICKET** (TODO, pre-launch-not-required): the champion/tail version-bump re-score sweep submits scores for SCORED agents, but `issue_ticket` only seats tickets for EVALUATING agents and `submit_score` requires an open ticket, so a re-score 409s. Add a platform path that re-opens (or re-tickets) the reigning champion + tail when their ledger `bench_version` is stale, so CRN/P4 re-scores can land. Consensus-sensitive (touches the ticket/quorum flow); sequence it deliberately before the first benchmark version bump, not under launch pressure.

### Decentralization / trust
- **C-OPEN** (TODO): open the dittobench-api scoring engine so any validator runs its own and the composite is third-party-verifiable. The prerequisite for trustless independent validators. Scope below.
- **F-MV** (deploy pending): see the critical path.
- **X-TRAJ** (DECISION): the forwarded `ScoreReport` carries only tool names, not `(name, args, hop)`. Enrich the export before building the behavioral anti-copy gate, or the convergence-robust clone signal can only compare name-sequences.
- **X-SHADOW** (KNOWN): semantic-clone prevention is not live (the code-embedding vector is stored but not gating; the prompt-fusion hold is deferred on X-TRAJ; the embedder service is off). State it plainly at launch.

### Anti-gaming / abuse
- **C-REPLAY** (covered by tickets; nonce-cache deferred): replay is already prevented by the one-ticket-one-score lifecycle (`submit_score` requires an open ticket, consumed atomically with the score under a row lock, 30-min TTL), so a captured submission 409s. A signed nonce+expiry cache would only add defense-in-depth and requires a coordinated change to the signing message across repos (the engine's `generated_at` is a fixed constant, not a live timestamp), so it is low-value pre-launch. Revisit only if the ticket gate is ever relaxed.
- **C-TUNE** (PARTIAL): tune the plagiarism thresholds against a real corpus and build a reviewer workflow (today `ath_pending_review` is drained by hand).
- **C-RATE / X-HARDEN** (PARTIAL): the dev permit-bypass flag is now refused on finney in code (still keep it unset in prod). Remaining is deploy-layer: front the platform with a TLS + rate-limiting reverse proxy and global/per-hotkey rate limits. Per-request nonce/timestamp signatures on the validator read-GETs stay deferred (those endpoints are already permit-gated and the ledger is public), revisit if artifact/dataset reads need requester-proof auth.

### Infra / ops
- **X-INFRA-PROD** (TODO, largest gap): no prod environment. dev and "prod" share the GCP project + tfstate; validator/embedder gated off; everything targets dev netuid 3; the DB password lands in tfstate; a single non-HA Postgres holds both DBs; the validator reuses the platform SA. Needs a genuine prod-isolation build, not a flag flip.
- **O-DB** (TODO): backups/PITR, pooling, retention, a read replica for the public ledger.
- **O-HA** (TODO): API redundancy, DR, cost ceilings + alerts.
- **O-OBS** (PARTIAL): add validator + platform metrics and real alerting (W&B + dashboard already live).
- **O-UPD** (PARTIAL, operational): an autoupdater; and set `enable_validator = true` in the env's `terraform.tfvars` (`infra/terraform/envs/gcp-platform`, var defaults to false) so an apply never plans to destroy the validator VM + its secrets without the `-var` flag. The tfvars fix is an operator action against live infra, not a speculative code edit.
- **O-SEC** (PARTIAL): document and exercise a secrets-rotation runbook.
- **Q-CI / Q-CHAOS** (TODO): a localnet E2E suite in CI; load + chaos testing.

### Screener / miner CLI
- **S-GATE** (TODO): deeper screener gate (`/seed` + `/run` smoke, failure-reason persist, stale-claim reset, a distinct screener permit).
- **S-RETRY** (TODO, needs a migration): a broken agent that fails to score keeps getting re-ticketed and re-scored every epoch, re-burning tokens. Design: add a terminal `evaluation_failed` status + an `eval_attempts` column on the agent (Alembic migration); a validator failure-report endpoint (POST `/validator/agent/{id}/evaluation_failed`, signed) increments the counter and, past N, transitions the agent terminal so `issue_ticket` stops selecting it; exclude the terminal status from the ledger/leaderboard. Deferred off the launch path (schema change; a cost optimization, not launch-blocking), sequence deliberately.
- **M-CLI** (PARTIAL): deferred upload validations (tar manifest, import allowlist, schema diff) pending the frozen harness interface.

### Docs
- Publish the miner (`MINER-FAQ.md`) and validator (`VALIDATOR-ONBOARDING.md`) onboarding guides; write the subnet landing / lightpaper.
- **X-BENCHHOST** (DOC): add a one-line pointer distinguishing the practice dittobench-api (Cloud Run, `harness_url`) from the mode-B Docker instance on the validator VM.

---

## Open-source scope for independent validators (C-OPEN)

A trustless independent validator must run the whole evaluation itself, which
fixes which repos are public and what to open.

| Repo | Role | Status | Action |
|---|---|---|---|
| `dittobench-datagen` | dataset generator + judge-free grader | PUBLIC | none; the validator regenerates the dataset from the seed and grades per-case |
| `ditto-harness` | miner agent/memory library | PUBLIC | none; fetched during the miner's Docker build inside the sandbox |
| `dittobench-starter-kit` | miner harness kit | PUBLIC | none; miner-side |
| `ditto-subnet` | the validator worker | PUBLIC target | OPEN: a validator runs this process |
| `dittobench-api` | the scoring engine (build/run miner, model+egress lock, composite + gates) | PRIVATE | OPEN (see below) |
| `ditto-platform` | central coordinator (dataset issuance, ticket lease, ledger, median) | PRIVATE | STAYS private: the validator talks to it but verifies everything it returns (seed re-derivation, tarball sha256, signed scores, public audit log) |
| `ditto-data-pipeline` | upstream corpus extraction | PRIVATE | STAYS private: off the scoring path |

So exactly two repos open beyond what is already public: **`ditto-subnet`** and
**`dittobench-api`**.

**dittobench-api: open the whole repo in place, do not extract a subset.** The
scoring path already pulls in nearly every internal package
(`internal/{scorer,sandbox,runner,netguard,astfp,store,llm,ratelimit}` plus
`cmd/{model-relay,egress-proxy}`), so extraction would move ~90% of the repo for
no benefit. Nothing to hide: no answer key is hardcoded (regenerated per seed via
public datagen) and no keys are in source (env-only), and it is already framed as
the keyless public practice validator.

Pre-open checklist status:
- Secrets scan: DONE. Tracked files and full git history are clean of key
  patterns (sk-or / cpk_ / private keys / AWS).
- `cmd/model-relay` embeds no gateway secret: CONFIRMED. Key + model come from
  `RELAY_API_KEY` / `RELAY_MODEL` env, nothing hardcoded.
- `dittobench-datagen` pin: CURRENT (v0.7.0, the latest public tag). No re-pin
  needed.
- LICENSE + README pointer: PREPARED (held commit on dittobench-api, not pushed):
  the proprietary LICENSE is replaced with MIT and the README points at the
  independent-validator role + `VALIDATOR-ONBOARDING.md`. The only remaining steps
  are the owner actions: flip repo visibility to public and push. Gated on decision
  #1 below (open at launch vs fast-follow).

---

## Decisions needing a human

1. **Trust model / C-OPEN timing**: open dittobench-api for the launch (full independence) or fast-follow with team-run k=3 validators first.
2. **B-TAIL**: participation-tail economics (tail size, min-score floor, or winner-take-all).
3. **Registration / immunity / emission-split** target values per network.
4. **run_size for production**: confirm `full` (the real config, slow + real LLM spend) before mainnet.
5. **X-TRAJ**: enrich the trajectory export now, or ship name-only clone-matching.

---

## Exit checklist (definition of "production ready")

- [ ] Full `run_size=full` E2E proven end to end.
- [ ] Weights set via a verified Pylon identity-write on finney (commit-reveal on, version_key confirmed).
- [ ] >=3 validators converging on the KOTH champion via median-of-3.
- [ ] Observability + alerting + DB backups + a rotation runbook.
- [ ] A green localnet E2E + chaos suite in CI.
- [ ] Miner + validator onboarding published.
- [ ] Mainnet cutover runbook executed with a real on-chain E2E.

---

## Done (footnote)

Verified on the dev localnet or merged, no longer tracked above:

- The full pipeline runs unattended end-to-end on the localnet: upload, screener build-gate, validator sweep, dittobench (docker build / seed / run / deterministic grade), signed composite, scores ledger, KOTH+ATH weights, `set_weights` accepted on chain. First real E2E at `run_size=small` (composite 0.522, 2026-07-07).
- **Benchmark content (bench_version 2)**: judge-free deterministic scoring, hardened (bounded canary + multi-family metamorphic, 0.5/0.5 composite), datagen public and pinned v0.7.0, reference baseline published (composite 0.492 ± 0.013 SE at `full`). Pre-prod hardening P1/P2/P3/P5/P6 landed (P4 parked, see B-KOTH).
- **C-ISO**: sandbox egress allowlist + isolation, applied and verified on dev.
- **F-MV**: k=3 leased tickets + median-at-quorum + deterministic median-ledger fold, code-done and tested (`ditto-platform TestMultiValidatorConsensus`); deploy pending.
- **B-KOTH z-band**: live across all three repos. The engine's gated `composite_stderr` (dittobench-api scales the SE by the gate factors) is surfaced by the platform ledger and folded by the validator (`dethrone_z=1.64`), so a challenger inside measurement noise cannot flip the crown. **P4 multi-seed** is also code-done (validator re-scores each stale champion/tail agent over K common CRN seeds, `VALIDATOR_KOTH_CONFIRMATION_SEEDS=3`, and submits one median-run score carrying `confirmation_composites`; the ledger surfaces it and the fold dethrones on the median over seeds) but is dormant until a version bump and gated on B-RESCORE-TICKET (see above).
- **W-VK / W-PERMIT / W-CADENCE / W-CR / V-ROBUST**: weight-path conformance code-done (first proven at the finney cutover, no testnet).
- **W-PYLON / E1**: Pylon identity write validated through the deployed dev sidecar.
- **X-LEDGER-N**: ledger surfaces `n`, so a small run can no longer be champion.
- **X-BENCHVER**: comments reconciled to bench_version 2. **S-CONTRACT**: screener wire-contract guard.
- Anti-copy fingerprint gate (lexical + AST), LLM cost caps, banned-hotkeys enforcement, emissions on localnet, W&B telemetry + public dashboard.

---

*Ownership: we own the whole stack (platform, screener, validator, miner CLI,
dittobench scorer, chain/emissions config, economics). There are no external
owners to hand a workstream to.*
