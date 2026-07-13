# Road to Production: SN118

Snapshot: 2026-07-13. Handoff state for the team that owns infra, chain access,
and repo visibility.

Launch model: **independent validators run the scoring and weight-setting from day
one. We do not host a validator.** Our pre-launch job is the coordinator, the open
scorer, the finney config, and enough independent validators onboarded to reach
quorum. We verify the full-size run in prod after ship, not before.

Part 1 is the pre-launch runbook (what we ship, ordered, with the access each step
needs). Part 2 is the post-launch work, none of which blocks launch.

> There is no testnet, only the dev localnet and prod (finney). The first real
> weight-set on the production path (Pylon delegation, commit-reveal, version_key,
> u16) happens in prod, run by an independent validator. Bring it up guarded: low
> volume, small runs, verify each hop before scaling.

All infrastructure-as-code lives in the separate `infra` repo, not in the app
repos. The app repos deploy themselves through GitHub Actions.

---

## Part 1: Pre-launch runbook

### Access we need

| Area | Permission | Where |
|---|---|---|
| Terraform apply (coordinator) | Write to the GCS state bucket `ditto-app-dev-tfstate`; project roles compute.admin, secretmanager.admin, cloudsql.admin, run.admin, dns.admin (or Editor) | `infra/terraform/envs/gcp-platform` |
| Secret values | `roles/secretmanager.admin` on the project | `gcloud secrets versions add` |
| Platform deploy | GitHub env secrets `GCP_WIF_PROVIDER`, `GCP_PLATFORM_DEPLOY_SA`, `DITTO_UPLOAD_PAYMENT_ADDRESS` | `ditto-platform/.github/workflows/deploy.yml` |
| dittobench-api deploy | none new; Actions pushes via WIF (`github-actions-deploy@ditto-app-dev`) on push to `main` | `dittobench-api/.github/workflows/ci.yml` |
| Repo visibility flip | GitHub org admin on `dittobench-api` | C-OPEN |
| Chain config | the subnet-owner (sudo) key for netuid 118; a funded coldkey for the registration burn | btcli sudo |

Independent validators bring their own coldkey/hotkey, stake, model-gateway key,
and Pylon token on their own infra (see the last subsection).

### Steps (ordered)

1. **Deploy the coordinator (ditto-platform).** The only piece we host: miner intake, the screener build-gate, ticket lease, the median-at-quorum finalize, the ledger, and the public API.

   `TF_VAR_db_password` is a new password you choose here, not an existing credential: Terraform sets it on the Postgres `ditto` role at creation, so any strong value works (generate one, e.g. `openssl rand -base64 24`). Pick it once and reuse the same value everywhere below; it is sensitive and currently lands in tfstate (X-INFRA-PROD).

   ```
   cd infra/terraform/envs/gcp-platform
   export TF_VAR_db_password="$(openssl rand -base64 24)"   # or any strong value; save it
   # terraform.tfvars: enable_validator = false, enable_embedder = false
   terraform init && terraform plan && terraform apply
   ```

   This builds Postgres, DNS (`platform-api.heyditto.ai`), the object store, Cloud NAT, and the empty Secret Manager shells. `enable_validator = false` is correct: we host no validator. Populate the platform secrets out of band (values never touch state). The `db_password` secret must hold the exact value you exported above, or the platform cannot connect:

   ```
   printf %s "$TF_VAR_db_password" | gcloud secrets versions add db_password --data-file=-
   gcloud secrets versions add validator-gh-token --data-file=-     # only if the screener build needs a private dep
   gcloud secrets versions add validator-wandb-key --data-file=-    # optional telemetry
   ```

   Set the GitHub env secret `DITTO_UPLOAD_PAYMENT_ADDRESS` (the miner payment SS58) and the `pylon_open_access_token` map. Then push `ditto-platform` `main`: `deploy.yml` runs IAP-SSH, uv sync, Alembic migrate, pm2 reload. Confirm Docker is up on the VM (the screener build-gate needs it) and `/health` is green. Exact secret list and commands: `infra/docs/validator-deploy.md`.

2. **Deploy the practice scorer (dittobench-api).** Push to `main` auto-deploys the keyless public practice validator to Cloud Run (the `harness_url` path) so miners can self-test before uploading. No manual step.

3. **Open the scoring engine (C-OPEN).** Independent validators must run their own scorer, so the engine has to be public. The MIT LICENSE and validator-role README are already on `main`; the only action is flipping `dittobench-api` repo visibility to public (GitHub org admin). Tracked files and git history are secret-clean (verified); no keys in source.

4. **Configure finney (netuid 118).** With the owner (sudo) key and a funded coldkey:
   - Confirm the subnet is registered on netuid 118.
   - Set the hyperparameters: tempo, immunity, weights-rate-limit, permit threshold, registration burn/recycle. Target values are decision 1 below and gate this step.
   - Enable commit-reveal on the subnet.
   - Set the permit threshold so a properly staked independent validator earns `validator_permit`.

5. **Onboard independent validators to quorum.** We provide the open engine (step 3) and the published guides (`VALIDATOR-ONBOARDING.md`, `VALIDATOR-MODEL-HOSTING.md`). The platform finalizes a score only at the **k=3 quorum**, so launch needs at least 3 independent validators registered and sweeping, or the quorum lowered for bootstrap (decision 2 below).

6. **Guarded launch check (in prod).** No testnet exists for the chain hops, so verify in prod at low volume: one miner uploads, the screener passes it, tickets issue, independent validators score, the platform medians at quorum, and an independent validator sets weights via Pylon on finney (confirm commit-reveal and version_key). Keep runs small at first. The full `run_size=full` E2E is a post-launch verification (step in Part 2), by intent.

### What independent validators run (their infra; we supply docs + the open engine)

- Co-located dittobench-api (:8080), model lock on, egress proxy fail-closed.
- The locked Qwen3-32B gateway: `model-relay` fronting Chutes `Qwen/Qwen3-32B-TEE`, or local Ollama / vLLM.
- The ditto-subnet validator worker (work_loop + weight_loop), exactly one process per hotkey.
- A Pylon sidecar holding the hotkey, signing `set_weights` (u16, version_key, commit-reveal).

Each brings its own coldkey/hotkey + stake, model-gateway key, and self-generated
Pylon token (`openssl rand -base64 32`). Full setup: `infra/docs/validator-deploy.md`
and `ditto-subnet/docs/VALIDATOR-ONBOARDING.md`.

---

## Part 2: Post-launch work

Everything below ships after launch. None of it blocks launch.

### Verify in prod
- **Full `run_size=full` E2E**, observed on finney. We test in prod, by intent.
- **At least 3 independent validators converging** on the KOTH champion via median-of-3 at scale.

### Chain / weights
- **B-RESCORE-TICKET**: the version-bump re-score sweep submits scores for SCORED agents, but `issue_ticket` only seats tickets for EVALUATING agents and `submit_score` requires an open ticket, so a re-score 409s. Add a platform path that re-opens (or re-tickets) the reigning champion + tail when their ledger `bench_version` is stale. Consensus-sensitive; sequence it before the first benchmark version bump. Until then P4 multi-seed (code-done) stays dormant.
- **B-KOTH z-band** is live and needs nothing at launch: the composite_stderr z-band engages on every dethrone comparison, so within-run noise (~0.041 vs the ~0.005 flat margin) is handled.

### Decentralization / trust
- **X-TRAJ** (DECISION): the forwarded `ScoreReport` carries only tool names, not `(name, args, hop)`. Enrich the export before building the behavioral anti-copy gate, or the clone signal can only compare name-sequences.
- **X-SHADOW** (KNOWN): semantic-clone prevention is not live (the code-embedding vector is stored but not gating; the embedder is off). State it plainly at launch.
- **C-TUNE**: tune the plagiarism thresholds against a real corpus and build a reviewer workflow (today `ath_pending_review` is drained by hand).
- **C-RATE / X-HARDEN**: the dev permit-bypass flag is refused on finney in code (keep it unset in prod). Remaining is deploy-layer: front the platform with a TLS + rate-limiting reverse proxy and global/per-hotkey limits.

### Infra / ops
- **X-INFRA-PROD** (largest gap): dev and "prod" share the GCP project, tfstate, and a single non-HA Postgres; everything targets dev netuid 3. A genuine prod-isolation build (own project, state, DB) is post-launch.
- **O-DB**: backups/PITR, pooling, retention, a read replica for the public ledger.
- **O-HA**: API redundancy, DR, cost ceilings + alerts.
- **O-OBS**: validator + platform metrics and real alerting (W&B + dashboard already live).
- **O-SEC**: document and exercise a secrets-rotation runbook.
- **O-UPD**: an autoupdater for the platform.
- **Q-CI**: a localnet E2E suite in CI.
- Optional: a team-hosted validator later (`enable_validator = true`) if we ever want one; not needed for launch.

### Screener / miner CLI
- **S-GATE**: deeper screener gate (`/seed` + `/run` smoke, failure-reason persist, stale-claim reset, a distinct screener permit). The smoke needs a model gateway in the screener (which by design runs with no LLM key); failure-persist needs a DB migration; the distinct permit is a chain change.
- **S-RETRY** (needs a migration): a broken agent that fails to score keeps getting re-ticketed every epoch. Add a terminal `evaluation_failed` status + an `eval_attempts` column (Alembic migration) and a signed validator failure-report endpoint that transitions the agent terminal past N attempts so `issue_ticket` stops selecting it. A cost optimization.
- **M-CLI**: deferred upload validations (tar manifest, import allowlist, schema diff) pending the frozen harness interface.

### Docs
- Subnet landing / lightpaper (net-new content).

---

## Open-source scope for independent validators (C-OPEN)

A trustless independent validator runs the whole evaluation itself, which fixes
which repos are public and what is left to open.

| Repo | Role | Status | Action |
|---|---|---|---|
| `dittobench-datagen` | dataset generator + judge-free grader | PUBLIC | none; the validator regenerates the dataset from the seed and grades per-case |
| `ditto-harness` | miner agent/memory library | PUBLIC | none; fetched during the miner's Docker build inside the sandbox |
| `dittobench-starter-kit` | miner harness kit | PUBLIC | none; miner-side |
| `ditto-subnet` | the validator worker | PUBLIC | a validator runs this process |
| `dittobench-api` | the scoring engine (build/run miner, model+egress lock, composite + gates) | PRIVATE | OPEN: flip visibility (pre-launch step 3) |
| `ditto-platform` | central coordinator (dataset issuance, ticket lease, ledger, median) | PRIVATE | STAYS private: the validator talks to it but verifies everything it returns (seed re-derivation, tarball sha256, signed scores, public audit log) |
| `ditto-data-pipeline` | upstream corpus extraction | PRIVATE | STAYS private: off the scoring path |

So exactly one repo remains to open: **`dittobench-api`**. Open the whole repo in
place, do not extract a subset: the scoring path pulls in nearly every internal
package (`internal/{scorer,sandbox,runner,netguard,astfp,store,llm,ratelimit}`
plus `cmd/{model-relay,egress-proxy}`), so extraction would move ~90% of the repo
for no benefit. Nothing to hide: no answer key is hardcoded (regenerated per seed
via public datagen) and no keys are in source (env-only).

Pre-open checklist status:
- Secrets scan: DONE. Tracked files and full git history are clean of key patterns (sk-or / cpk_ / private keys / AWS).
- `cmd/model-relay` embeds no gateway secret: CONFIRMED. Key + model come from `RELAY_API_KEY` / `RELAY_MODEL` env.
- `dittobench-datagen` pin: CURRENT (v0.7.0, the latest public tag).
- LICENSE + README pointer: DONE and pushed. Only the visibility flip remains.

---

## Decisions needing a human

1. **Chain economics (gates pre-launch step 4)**: target values for registration burn, immunity, permit threshold, tempo, weights-rate-limit, and the emission split on finney.
2. **Launch quorum bootstrap**: recruit at least 3 independent validators for the k=3 quorum, or lower `SCORING_QUORUM` until enough join.
3. **B-TAIL**: participation-tail economics (tail size, min-score floor, or winner-take-all).
4. **X-TRAJ** (post-launch): enrich the trajectory export, or ship name-only clone-matching.

Resolved: open dittobench-api at launch with independent validators from day one
(no self-hosted validator); verify `run_size=full` in prod rather than gating on it.

---

## Launch checklist (pre-launch, gating)

- [ ] Coordinator deployed to prod: intake, screener, tickets, ledger, public API, `/health` green.
- [ ] Practice scorer live on Cloud Run.
- [ ] `dittobench-api` public (C-OPEN).
- [ ] finney netuid 118 hyperparameters + commit-reveal set; permit threshold live.
- [x] Miner + validator onboarding published.
- [ ] Enough independent validators registered to reach quorum, each setting weights via Pylon on finney.
- [ ] First on-chain weight-set observed at low volume.

Not gating (post-launch): full `run_size=full` E2E in prod, 3-validator convergence
at scale, and the Part 2 items.

---

## Done (footnote)

Verified on the dev localnet or merged, no longer tracked above:

- The full pipeline runs unattended end-to-end on the localnet: upload, screener build-gate, validator sweep, dittobench (docker build / seed / run / deterministic grade), signed composite, scores ledger, KOTH+ATH weights, `set_weights` accepted on chain. First real E2E at `run_size=small` (composite 0.522, 2026-07-07).
- **Benchmark content (bench_version 2)**: judge-free deterministic scoring, hardened (bounded canary + multi-family metamorphic, 0.5/0.5 composite), datagen public and pinned v0.7.0, reference baseline published (composite 0.492 ± 0.013 SE at `full`). Pre-prod hardening P1/P2/P3/P5/P6 landed (P4 parked, see B-RESCORE-TICKET).
- **Public-repo launch polish (2026-07-13)**: copy/consistency pass across all five public repos, pushed; judge-free reconciliation, dethrone margin fixed to 0.05, Cloud Run ops removed from the validator-facing README, validator role clarified.
- **C-ISO**: sandbox egress allowlist + isolation, applied and verified on dev.
- **F-MV**: k=3 leased tickets + median-at-quorum + deterministic median-ledger fold, code-done and tested (`ditto-platform TestMultiValidatorConsensus`); runs live once independent validators reach quorum.
- **B-KOTH z-band**: live across all three repos. The engine's gated `composite_stderr` is surfaced by the platform ledger and folded by the validator (`dethrone_z=1.64`), so a challenger inside measurement noise cannot flip the crown. P4 multi-seed is code-done but dormant until a version bump, gated on B-RESCORE-TICKET.
- **W-VK / W-PERMIT / W-CADENCE / W-CR / V-ROBUST**: weight-path conformance code-done (first proven in prod, no testnet).
- **W-PYLON / E1**: Pylon identity write validated through the deployed dev sidecar.
- **X-LEDGER-N**: ledger surfaces `n`, so a small run can no longer be champion.
- **X-BENCHVER**: comments reconciled to bench_version 2. **S-CONTRACT**: screener wire-contract guard.
- Anti-copy fingerprint gate (lexical + AST), LLM cost caps, banned-hotkeys enforcement, emissions on localnet, W&B telemetry + public dashboard.

---

*Ownership: we own the whole stack (platform, screener, miner CLI, dittobench
scorer, chain/emissions config, economics). Scoring and weight-setting run on
independent validators from launch.*
