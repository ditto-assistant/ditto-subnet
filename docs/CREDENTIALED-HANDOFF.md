# SN118 — Credentialed Handoff

**2026-07-07.** Self-contained: every step left before prod, credential-gated and
engineering alike. Rationale in [`ROAD-TO-PRODUCTION.md`](ROAD-TO-PRODUCTION.md) /
[`NEXT-STEPS.md`](NEXT-STEPS.md).

> **Infra & secrets → Nick.** Nick has GCP access and applies all infra/secret
> actions (GCP Secret Manager + re-converge), doable **first thing tomorrow
> (2026-07-07)**. Nick can **generate every credential except the Pylon write
> creds** — the **one external dependency** (from the Pylon team). Gated items
> marked **→ Nick**.

Keys: ✅ done · 🔶 code-done, needs a live network/infra · ⬜ to do · ◆ decision.

---

## 0. Already done

Proven **end-to-end, non-mock, on the dev localnet (netuid 3)**: `upload →
screener → validator → dittobench (build·seed·run·judge) → signed composite →
ledger → KOTH+ATH weights → set_weights accepted on-chain`.

- ✅ **A1 real E2E** — composite 0.522, signed, on-chain weight accept (at `small`).
- ✅ **Screener** live (auto `uploaded → evaluating`).
- ✅ **Chain-conformance** — version_key pin, permit self-check, tempo cadence.
- 🔶 **Sandbox egress** — phase-1 plumbing in (enforcement = Hop 2).
- ✅ **Seeding fix** (starter-kit #9) — miners need the fixed kit for `full`.
- ✅ W&B + dashboard, cost caps, plagiarism gate, `banned_hotkeys`.

Emission already flows on localnet; validation runs under the **owner UID** (no
registration/burn).

---

## 1. Credentials

Nick generates + applies all of these → GCP Secret Manager, **except Pylon write
creds** (the lone external dependency).

| Secret | Env | Unblocks | Today |
| --- | --- | --- | --- |
| **Owner hotkey** staked to the permit threshold (owner UID, no burn) | `VALIDATOR_MNEMONIC` / wallet | Weight-setting | localnet |
| **Pylon write creds** 🔴 *only external dep* | `PYLON_IDENTITY_NAME/TOKEN` | Prod `put_weights` | read-only only; **prod path unverified** |
| **OpenRouter key** + account spend cap | `VALIDATOR_OPENROUTER_KEY` | `run_size` scoring | dev key; needs the cap |
| **GitHub read token** | BuildKit `gh_token` | private-dep builds | set |
| **Prod Postgres** | `POSTGRES_*` | platform DB | dev only |
| **S3/MinIO** | `STORAGE_*` | tarball storage | dev only |
| **TAO** to stake the owner hotkey | owner coldkey | permit threshold | localnet |
| **W&B key** | `WANDB_API_KEY` | telemetry | ✅ |

Secrets live in GCP Secret Manager, never the repo. Public repos
(`dittobench-starter-kit`, `ditto-harness`): no run IDs / eval-scale in commits.

---

## 2. Critical path (in order)

Hops 1–4 cleared on localnet; live frontier is **Hop 5**.

**1 — Full-scale E2E** ✅ small / ⬜ full. Set `VALIDATOR_RUN_SIZE=full` and score a
miner **built from starter-kit#9** (older agents fail full seeding on the old 2 MB
limit); a fixed tarball is ready — submit needs a funded coldkey + a hotkey
**registered on netuid 3** (also so the champion weight resolves to a UID).

**2 — Cost + egress** ⬜, before volume. (a) OpenRouter account spend cap **→ Nick**.
(b) Tune `LLM_RUN_TOKEN_BUDGET` to a `full` run's tokens. (c) **Egress enforcement**
(plumbing merged): allowlisting proxy (`openrouter.ai` only) + `ditto-sandbox`
network + iptables/nft (DROP all egress except → proxy and → host-gateway,
fail-closed); set `DITTOBENCH_SANDBOX_EGRESS_NETWORK/PROXY` + `_HARDEN=true`. ⚠️
Live-VM firewall — **→ Nick**, staged.

**3 — Screener** ✅ live, no action.

**4 — Emissions** ✅ localnet. Confirm champion `TotalHotkeyAlpha` rises (needs
registered miners); re-tune the pool per network.

**5 — Migration** 🔴. (1) **Pylon write creds → Nick** — first time the prod weight
path hits a real chain; **verify normalization/u16/commit-reveal/version_key on
testnet**. (2) **Stake owner hotkey** on testnet. (3) **Chain params** — tempo,
immunity, weights-rate-limit, permit threshold, **enable commit-reveal**; align
`VALIDATOR_EPOCH_SECONDS` + `VALIDATOR_WEIGHT_VERSION_KEY`. (4) Point at the network,
flip `enable_validator`, **re-run Hop 1 (full)**.

**6 — Decentralize** ⬜. Multi-validator k=3 + median-of-3 (§3). Localnet: 2 hotkeys
under the existing validator coldkey. Prod needs ≥1 independent validator.

**7 — Finney cutover** ⬜. Repeat Hop 5 on finney, run the runbook, real full E2E.

---

## 3. Remaining engineering (not credential-gated)

**Robustness/anti-gaming** — ⬜ sandbox egress enforcement (Hop 2, 🔴 top gap) ·
⬜ deeper isolation (seccomp→gVisor) · ⬜ bounded re-score (failed harness re-runs
full LLM cost every epoch — seen live) · ⬜ replay-nonce + server cache (C2) ·
⬜ plagiarism tuning + review automation (C1) · ⬜ API rate limits (C6) ·
◆ verifiable scoring (C5).

**Conformance residuals** — 🔶 verify Pylon on testnet (Hop 5) · ⬜ commit-reveal
step (B2) · ⬜ weight-set residuals (on-chain tempo read + backoff + min-stake).

**Decentralization** — ⬜ multi-validator: lease table + stub→target endpoint
rename + median fold + onboard >1 (A3, Hop 6).

**Ops** — ⬜ observability (validator + platform metrics + alerts) · ⬜ Postgres
backups/PITR/pooling/replica · ⬜ HA/DR + cost ceilings · ⬜ autoupdater +
zero-downtime · ⬜ secret-rotation runbook · ⬜ validator-onboarding package ·
⬜ CI E2E (localnet) + chaos tests.

**Screener/CLI** — ⬜ screener contract guard · ⬜ deeper gate (`/seed`·`/run`
smoke, stale-claim reset, `screener_permit`) · ⬜ miner-CLI validations + UX.

**Docs** — ⬜ miner onboarding · ⬜ validator onboarding · ⬜ lightpaper.

**Decisions** ◆ — trust model (C5) · tail economics (B3) · registration/immunity
(B4) · emission split (B1) · endpoint-rename timing · prod `run_size`.

---

## 4. Deploy & verify quick-reference

| Task | Command / knob |
| --- | --- |
| DB schema | platform `make migrate` |
| Bring up validator/screener/dittobench | terraform (`enable_validator=true`; **plain apply DESTROYS validator resources**) + ansible `dittobench`, `validator_worker`, `screener_worker` |
| IAP converge (dev) | `cd infra/ansible && GCP_OSLOGIN_USER=nickanderson_omniaura_ai ANSIBLE_HOST_KEY_CHECKING=False ansible-playbook -i inventory/validator-static.yml playbooks/gcp-validator.yml --private-key ~/.ssh/google_compute_engine --limit ditto-validator-dev` |
| Weight path | `VALIDATOR_USE_SDK_WEIGHTS=true` (dev) vs `PYLON_IDENTITY_*` (prod) |
| Cadence / KOTH | `VALIDATOR_SWEEP_SECONDS` 120, `_EPOCH_SECONDS` 3600, `_WEIGHT_VERSION_KEY`; `_KOTH_MARGIN` 0.01, `_CHAMPION_SHARE` 0.9, `_TAIL_SIZE` 4 |
| Run size | `VALIDATOR_RUN_SIZE` (full needs starter-kit#9 miners) |
| Scorer/cost | dittobench `GENERATOR_MODEL`, `SCORER_MODEL`, `LLM_MAX_TOKENS`, `LLM_RUN_TOKEN_BUDGET` |
| Sandbox egress | `DITTOBENCH_SANDBOX_EGRESS_NETWORK/PROXY`, `_HARDEN`, `_PIDS_LIMIT` |
| Ban / review | `scripts/ban_hotkey.py <hotkey>` · `scripts/resolve_review.py <agent_id> --decision scored\|banned` |
| Verify | score: `GET /api/v1/scoring/scores` · emission: champion `TotalHotkeyAlpha` rises |

CI: platform/subnet full-repo `mypy` (py3.11+3.12); dittobench-api `go build/vet/test` (1.23).

---

## 5. Production-ready checklist

- [ ] Full-scale `full` E2E (Hop 1)
- [ ] Sandbox egress-restricted + isolated, fail-closed (Hop 2)
- [ ] Verified Pylon write on testnet + commit-reveal + version_key (Hop 5)
- [ ] ≥3 validators, median-of-3 (Hop 6)
- [ ] Cost cap + observability + alerts + DB backups + rotation runbook
- [ ] CI E2E + chaos suite green
- [ ] Miner + validator onboarding docs
- [ ] Finney cutover with a real on-chain full E2E (Hop 7)

**Only external blocker: Pylon write creds** (from the Pylon team). Nick generates
everything else — owner hotkey staked · OpenRouter key + cap · prod Postgres ·
S3/MinIO — first thing 2026-07-07.

---

Refs: [`ROAD-TO-PRODUCTION.md`](ROAD-TO-PRODUCTION.md) · [`NEXT-STEPS.md`](NEXT-STEPS.md) ·
[`STATE-OF-THE-SUBNET.md`](STATE-OF-THE-SUBNET.md) · [`dev-e2e-handoff.md`](dev-e2e-handoff.md).
