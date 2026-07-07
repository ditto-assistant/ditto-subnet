# SN118 — Credentialed Handoff

**As of 2026-07-07.** For the operator holding the production credentials, chain
keys, and accounts. **Self-contained:** this doc covers *everything* still needed
before production, credential-gated and engineering alike. Deeper rationale lives
in [`ROAD-TO-PRODUCTION.md`](ROAD-TO-PRODUCTION.md) / [`NEXT-STEPS.md`](NEXT-STEPS.md),
but you don't need them to know what's left.

> **Applying any secret / infra change:** you do **not** need GCP access. **Forward
> the credential to Nick and he applies it to GCP Secret Manager** and re-converges
> the deploy. Wherever this doc says "provision" or "set" a secret, it means: send
> it to Nick.

Status keys: ✅ done · 🔶 code-done, needs a live network/infra · ⬜ to do · ◆ decision.

---

## 0. Starting line — what's already done

Pipeline proven **end-to-end, non-mock, unattended on the dev localnet (netuid 3)**:
`upload → screener → validator → dittobench (build·seed·run·judge) → signed
composite → ledger → KOTH+ATH weights → set_weights accepted on-chain`.

- ✅ **A1 first real E2E** — real composite (0.522), signed, drove an on-chain
  `set_weights` accept. *At `small`; full-size pending (Hop 1).*
- ✅ **Screener** — live; automates `uploaded → evaluating` (Hop 3 no longer manual).
- ✅ **Chain-conformance** — `version_key` pin, `validator_permit` self-check,
  tempo-decoupled cadence (subnet #36).
- 🔶 **Sandbox egress hardening** — phase-1 plumbing in (dittobench-api #13);
  enforcement is Hop 2.
- ✅ **Full-run seeding fix** (starter-kit #9) — miners must build from the fixed kit for `full`.
- ✅ **W&B telemetry + public dashboard**, cost caps, content-plagiarism gate,
  `banned_hotkeys`.

Not blockers after all: emission already flows on localnet; validation runs under
the **subnet owner UID** (no validator registration/burn).

---

## 1. Credentials you must hold

Until each is provisioned, its capability stays on the dev fallback. **To apply any
of these: forward to Nick → GCP Secret Manager.**

| Secret / access | Env var | Unblocks | Today |
| --- | --- | --- | --- |
| **Owner hotkey** (mnemonic), staked to the `validator_permit` threshold — runs under the **owner UID**, no registration/burn | `VALIDATOR_MNEMONIC` (or wallet name+hotkey); matches `VALIDATOR_HOTKEY` | Weight-setting | Validates on localnet |
| **Pylon identity (write) creds** | `PYLON_IDENTITY_NAME`, `PYLON_IDENTITY_TOKEN` | Prod `put_weights` (Pylon owns normalization/u16/commit-reveal/version_key) | Read-only token only; SDK fallback on dev. **Prod path unverified in-repo.** |
| **OpenRouter key** + **account-level spend cap** | `VALIDATOR_OPENROUTER_KEY` / `OPENROUTER_API_KEY` | `run_size` scoring | Dev key set; needs the cap for volume |
| **GitHub read token** (pull `ditto-harness` in build) | BuildKit `gh_token` | Private-dep crate builds | Set on dev |
| **Prod Postgres** creds | `POSTGRES_*` | Platform DB | Dev DB only |
| **S3/MinIO** creds | `STORAGE_*` | Tarball storage + presigned URLs | Dev MinIO only |
| **TAO** to stake the owner hotkey | owner coldkey | Meeting the permit threshold | localnet only |
| **W&B key** | `WANDB_API_KEY` | Telemetry + dashboard | ✅ provisioned |

**Hygiene:** secrets live in GCP Secret Manager, never in the repo. Validator
hotkey rotated once — capture the rotation runbook next time. **Public repos**
(`dittobench-starter-kit`, `ditto-harness`): no run IDs / eval-scale / infra detail
in commit messages.

---

## 2. Critical path — credential & infra-gated (in order)

Hops 1–4 are cleared on localnet; the live frontier is **Hop 5**.

**Hop 1 — Full-scale real E2E** ✅ (small) / ⬜ (full). Proven at `small`. To close:
(a) set `VALIDATOR_RUN_SIZE=full` and score a miner **built from starter-kit#9**
(older agents bake in the 2 MB `/seed` limit → fail full seeding); a validated
fixed-baseline tarball is ready — submit needs a funded coldkey + a hotkey
registered on netuid 3. (b) **Register submitting miners on netuid 3** so the
champion weight resolves to a UID (in the small proof it was skipped).

**Hop 2 — Cost + egress controls** ⬜ (before volume). (a) **OpenRouter
account-level spend cap** → Nick. (b) Tune `LLM_RUN_TOKEN_BUDGET` to a `full` run's
observed tokens. (c) **Sandbox egress enforcement** (plumbing merged): deploy an
allowlisting proxy (`openrouter.ai` only) + a `ditto-sandbox` network + iptables/nft
rules (DROP all egress except → proxy and → host-gateway, fail-closed), then set
`DITTOBENCH_SANDBOX_EGRESS_NETWORK/PROXY` + `DITTOBENCH_SANDBOX_HARDEN=true`. ⚠️
Live-VM firewall — stage carefully. Infra work; Nick applies.

**Hop 3 — Screener** ✅ live, no action. Keep `SCREENER_MAX_TARBALL_BYTES` ≥ the
platform cap; `SCREENER_SMOKE_ENV` carries the dummy `OPENROUTER_API_KEY`.

**Hop 4 — Emissions** ✅ on localnet. Confirm the champion's `TotalHotkeyAlpha`
rises (needs registered miners); re-tune the alpha-pool per network on migration.

**Hop 5 — Network migration** 🔴 the frontier.
1. **Pylon write creds** (→ Nick). Highest-leverage step — first time the prod
   weight path hits a real chain. **Verify normalization / u16 / commit-reveal /
   version_key on testnet.**
2. **Stake the owner hotkey** on testnet to clear the permit threshold. No burn.
3. **Chain params:** tempo, immunity, weights-rate-limit, permit threshold,
   **enable commit-reveal**. Align `VALIDATOR_EPOCH_SECONDS` to the real tempo; set
   `VALIDATOR_WEIGHT_VERSION_KEY` to the agreed version.
4. Point the deploy at the network (`SUBTENSOR_NETWORK`, `NETUID`), flip
   `enable_validator`, **re-run Hop 1 (full)**.

**Hop 6 — Decentralize** ⬜. Multi-validator (k=3 + median-of-3) — see §3. Localnet
test with no new funding: 2 new hotkeys under the existing validator coldkey,
registered on netuid 3. Production needs ≥1 independent validator onboarded.

**Hop 7 — Mainnet (finney) cutover** ⬜. Repeat Hop 5 against finney SN118, run the
deploy runbook, verify each hop, run a real full E2E. Keep the finney backup notes
(STATE-OF-THE-SUBNET §"For Ethan").

---

## 3. Everything else needed before prod (engineering, not credentials)

Sequence against the hops. This is the complete remaining list.

**Robustness & anti-gaming**
- ⬜ **Sandbox egress enforcement** (C-ISO ph2) — the infra proxy + firewall (Hop 2). 🔴 top gap.
- ⬜ Sandbox **deeper isolation** — seccomp profile, later gVisor/Kata runtime.
- ⬜ **Bounded re-score** — a harness that fails scoring re-runs full datagen + LLM
  cost every epoch (seen live); add a retry cap / terminal `evaluation_failed`.
- ⬜ **Signature replay-nonce + server cache** (C2) — wire-contract change (both
  `api_models/validator.py` copies + golden regen + signing message).
- ⬜ **Plagiarism threshold tuning + review-queue automation** (C1) — gate is built;
  tune tolerances vs a real corpus, automate `resolve_review`.
- ⬜ **API abuse controls** (C6) — global + per-hotkey rate limits, request-size limits.
- ◆ **Verifiable/replicable scoring** (C5) — trusted single scorer today; decide whether/when.

**Bittensor conformance residuals**
- 🔶 **Verify Pylon delegation on testnet** (Hop 5.1) — the biggest unknown.
- ⬜ **Commit-reveal reveal step** (B2) — enable + verify (Hop 5.3).
- ⬜ **Weight-set residuals** — read on-chain tempo/`weights_rate_limit` directly
  (today a hand-set proxy) + backoff on rate-limit rejection + a min-stake check.

**Decentralization**
- ⬜ **Multi-validator k=3 + median-of-3** (A3, Hop 6) — lease table, stub→target
  endpoint rename, median fold; onboard >1 validator.

**Reliability & ops**
- ⬜ **Observability** — validator metrics (sweep duration, put_weights success,
  ledger size) + platform request/error/DB metrics + alerting.
- ⬜ **Prod Postgres** — backups + PITR, pooling, retention/archival, read replica.
- ⬜ **HA / DR / cost ceilings** — API redundancy, dittobench scaling, queue
  durability, DR reconstruction, LLM/VM/storage budget alerts.
- ⬜ **Deploy lifecycle** — git-watching autoupdater, zero-downtime restart safety.
- ⬜ **Secret rotation runbook** (D6) — hotkey mnemonic, OpenRouter, GH, W&B.
- ⬜ **Third-party validator onboarding** package (docs, hw reqs, config, key custody).
- ⬜ **CI E2E (localnet) + chaos tests** — full pipeline behind `e2e`/`localnet`;
  inject chain outages, dittobench failures, partial writes → no lost-update/zeroed-chain.

**Screener & miner-CLI follow-ups**
- ⬜ Screener **contract-test guard** (wire models mirrored by hand today).
- ⬜ Screener **deeper gate** — `/seed`/`/run` smoke, failure-reason persist,
  stale-claim reset, a distinct `screener_permit`.
- ⬜ **Miner CLI** — deferred upload validations (tar manifest, import allowlist,
  schema diff), clearer errors / status.

**Docs & ecosystem**
- ⬜ Miner onboarding (build-a-harness, submission contract, scoring rubric, KOTH rules).
- ⬜ Validator onboarding guide. ⬜ Subnet landing / lightpaper.

**Open product decisions** ◆
- Trust model (owner-scorer vs verifiable, C5) · tail economics (B3) ·
  registration/immunity (B4) · emission split (B1) · endpoint-rename timing (A3) ·
  production `run_size`.

---

## 4. Deploy & verify quick-reference

| Task | Command / knob |
| --- | --- |
| Apply DB schema | platform `make migrate` |
| Bring up validator/screener/dittobench | infra `terraform` (`enable_validator=true`; **plain apply DESTROYS validator resources**) + ansible roles `dittobench`, `validator_worker`, `screener_worker` |
| IAP converge (dev) | `cd infra/ansible && GCP_OSLOGIN_USER=nickanderson_omniaura_ai ANSIBLE_HOST_KEY_CHECKING=False ansible-playbook -i inventory/validator-static.yml playbooks/gcp-validator.yml --private-key ~/.ssh/google_compute_engine --limit ditto-validator-dev` |
| Weight path | `VALIDATOR_USE_SDK_WEIGHTS=true` (dev) vs unset + `PYLON_IDENTITY_*` (prod) |
| Cadence | `VALIDATOR_SWEEP_SECONDS` (120), `VALIDATOR_EPOCH_SECONDS` (3600), `VALIDATOR_WEIGHT_VERSION_KEY` |
| KOTH | `VALIDATOR_KOTH_MARGIN` (0.01), `_CHAMPION_SHARE` (0.9), `_TAIL_SIZE` (4) |
| Run size | `VALIDATOR_RUN_SIZE` (small\|medium\|full); **full needs starter-kit#9 miners** |
| Scorer + cost | dittobench `GENERATOR_MODEL`, `SCORER_MODEL`, `LLM_MAX_TOKENS`, `LLM_RUN_TOKEN_BUDGET` |
| Sandbox egress | `DITTOBENCH_SANDBOX_EGRESS_NETWORK/PROXY`, `DITTOBENCH_SANDBOX_HARDEN`, `_PIDS_LIMIT` |
| Ban a hotkey | platform `uv run python scripts/ban_hotkey.py <hotkey> --reason "…"` |
| Clear/ban a review hold | platform `uv run python scripts/resolve_review.py <agent_id> --decision scored\|banned` |
| Verify a score | `GET /api/v1/scoring/scores` |
| Verify emission | champion `TotalHotkeyAlpha` rises (miner must be registered) |

**CI:** platform/subnet run full-repo `mypy` on py3.11+3.12; dittobench-api is
`go build/vet/test` (Go 1.23). Run whole-repo checks before pushing.

---

## 5. Definition of production-ready (exit checklist)

- [ ] Full-scale (`run_size=full`) E2E proven end-to-end (Hop 1).
- [ ] Sandbox egress-restricted + isolated, fail-closed (Hop 2 / §3).
- [ ] Weights via **verified** Pylon identity-write on testnet, commit-reveal on,
      version_key confirmed (Hop 5).
- [ ] ≥3 validators converging on the KOTH champion via median-of-3 (Hop 6).
- [ ] Cost cap (account + code), observability + alerting, DB backups, rotation runbook.
- [ ] Green localnet E2E + chaos suite in CI.
- [ ] Miner + validator onboarding docs published.
- [ ] Finney cutover runbook executed with a real on-chain full E2E (Hop 7).

**Hard external blockers (all → forward to Nick for GCP Secret Manager):** Pylon
write creds · owner hotkey staked to the permit threshold · OpenRouter key + cost
cap · prod Postgres + S3/MinIO creds.

---

## 6. References (supplementary)

[`ROAD-TO-PRODUCTION.md`](ROAD-TO-PRODUCTION.md) · [`NEXT-STEPS.md`](NEXT-STEPS.md) ·
[`STATE-OF-THE-SUBNET.md`](STATE-OF-THE-SUBNET.md) · [`dev-e2e-handoff.md`](dev-e2e-handoff.md) ·
[`incentive-mechanism.md`](incentive-mechanism.md). Recent merges: subnet #32/#34
(screener), #36 (conformance), #37/#38/#43 (docs); dittobench-api #13 (egress);
starter-kit #9 (seed limit).
