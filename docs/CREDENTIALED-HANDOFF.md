# SN118 — Credentialed Handoff

**As of 2026-07-07.** Audience: the operator who holds (or can obtain) the
**production credentials, chain keys, and infra access**. This document is the
bridge between the code — which is where it needs to be for the next steps — and
the things that **cannot be done from a keyboard alone**. It is deliberately
scoped to *"what must a person with credentials do, and in what order."*

Read it alongside [`ROAD-TO-PRODUCTION.md`](ROAD-TO-PRODUCTION.md) (the current
prioritized remaining-work checklist), [`NEXT-STEPS.md`](NEXT-STEPS.md) (the full
engineering roadmap + history), and [`STATE-OF-THE-SUBNET.md`](STATE-OF-THE-SUBNET.md)
(on-chain specifics, §"For Ethan"). This doc does **not** duplicate their per-item
detail; it sequences the credential-gated hops and names the exact secrets/knobs
each needs.

---

## 0. What just landed (so you know the starting line)

The pipeline is now **proven end-to-end, non-mock, unattended, on the dev
localnet (netuid 3)**:

```
miner upload → screener (auto build-gate) → validator sweep → dittobench
  (docker build · seed · run · LLM judge) → signed composite → scores ledger
  → KOTH+ATH weights → set_weights ACCEPTED on-chain
```

Landed 2026-07-06 → 07 (on top of the earlier KOTH+ATH, weight-ingestion, cost
cap, `banned_hotkeys`, and content-plagiarism work):

| Area | Where | Effect |
| --- | --- | --- |
| **A1 first real E2E — PROVEN** | live, 2026-07-07 | Agent flowed the full non-mock path; real **composite 0.522**, sr25519-signed in the `scores` ledger, drove an on-chain `set_weights` **accept**. Several agents scored since (champion ~0.60). *At `small`; full-size still pending — see Hop 1.* |
| **Screener worker — DEPLOYED LIVE** | subnet #32/#34, infra #9 | `python -m ditto.screener` automates `uploaded → evaluating` (docker-build + `/health` gate, signed verdict). **Hop 3 is no longer manual.** Two live bugs fixed: 4→20 MiB tarball cap (#34); a dummy LLM key so the harness boots `/health` (#35). |
| **Validator chain-conformance** | subnet #36 (live) | `version_key` pin, `validator_permit` self-check (fail-open), and a **tempo-decoupled cadence** (`VALIDATOR_SWEEP_SECONDS` 120s vs `VALIDATOR_EPOCH_SECONDS` 3600s) so scoring latency isn't the weight-set interval. |
| **Sandbox egress hardening (phase-1)** | dittobench-api #13 | Config-driven egress allowlist + `--cap-drop`/`--pids-limit` plumbing (defaults unchanged). Enforcement (proxy + firewall) is the infra follow-up — see Hop 2 + `dittobench-api/docs/sandbox-egress-hardening.md`. |
| **Full-run seeding fix** | dittobench-starter-kit #9 (public) | The reference harness used axum's 2 MB default body limit, so a **full** seed haystack (842 pairs / 2258 subjects) 413'd at the seeding stage. Fixed (`DefaultBodyLimit::max(256 MiB)`). **Miners must build from the fixed kit for full runs.** |
| **W&B telemetry + public dashboard** | platform #12/#17, subnet #27, infra #7/#8 | Live on dev (`heyditto/ditto-sn118`); leaderboard + health API + SPA. |

**Two earlier "blockers" that aren't:** emission **already flows on localnet**
(`SubnetTaoInEmission[3]` non-zero), and production validation runs under the
**subnet owner UID** (no separate validator registration/burn). What remains is
credential/infra wiring — **Pylon write creds, per-network staking + tuning, the
OpenRouter account cap, the sandbox egress enforcement, commit-reveal, and the
network migration.**

---

## 1. Credentials & secrets you must hold

Everything here is an *external* dependency — a person/account, not code. Until
each is provisioned, the mapped capability stays on its dev fallback (or off).

| Secret / access | Goes where | Unblocks | Today |
| --- | --- | --- | --- |
| **Subnet owner hotkey** (mnemonic) **staked to the `validator_permit` threshold** on the target network — validation runs under the **owner UID**, so **no separate validator registration/burn** | `VALIDATOR_MNEMONIC` (or `VALIDATOR_WALLET_NAME` + `VALIDATOR_WALLET_HOTKEY`); must match `VALIDATOR_HOTKEY` | Weight-setting on that network | Owner UID validates on localnet netuid 3 |
| **Pylon identity (write) creds** | `PYLON_IDENTITY_NAME`, `PYLON_IDENTITY_TOKEN` (Secret Manager → `platform.env`) | Production `put_weights` via Pylon (it owns normalization / u16 / commit-reveal / version_key) | Only a **read** token exists; the SDK path is the dev fallback. **The prod weight path is unverified in-repo — testnet is its first real test.** |
| **OpenRouter API key** (with an **account-level spend cap** set on OpenRouter's side) | `VALIDATOR_OPENROUTER_KEY` (validator forwards it) and/or `OPENROUTER_API_KEY` (dittobench-api / the crate) | `run_size` scoring (generator + judge + the miner's own agent) | Provisioned in dev; needs the account cap for volume |
| **GitHub token (read)** for pulling `ditto-harness` during `docker build` | dittobench-api `GitHubTokenFile` / screener `gh_token` → BuildKit `--secret gh_token` | Crate builds that depend on the reference harness | Provisioned in dev (screener + dittobench both build the private dep) |
| **GCP infra access** (Terraform apply, Secret Manager, systemd) | n/a (operator identity) | Deploy + the `enable_validator` gate | Dev deploy done, gated |
| **Prod Postgres credentials** | `POSTGRES_*` (`.env` / Secret Manager) | Platform DB (ledger, agents, bans) | Dev DB only |
| **S3/MinIO storage creds** | `STORAGE_*` on the platform | Tarball storage + presigned URLs | Dev MinIO only |
| **TAO to stake the owner hotkey** to the permit threshold | the owner coldkey | Meeting `validator_permit` on testnet/finney (no *registration* burn) | localnet only |
| **W&B API key** | `WANDB_API_KEY` (Secret Manager → validator env) | Opt-in aggregate telemetry + dashboard link | ✅ provisioned — `docs/wandb-keys.yml` (gitignored) |

> **Secret hygiene:** all of the above live in **GCP Secret Manager** on the dev
> deploy; keep them there, never in the repo. The validator hotkey has already
> been rotated once — capture the rotation runbook (roadmap D6) the next time you
> touch it. **Public repos:** `dittobench-starter-kit` + `ditto-harness` are
> public — never put run IDs, eval-scale, or infra detail in their commit messages.

---

## 2. Critical path (each hop gates the next)

Rendered as **operator actions**. Hops 1–4 are essentially cleared on localnet;
the live credential-gated frontier is **Hop 5 (migration)**.

### Hop 1 — Real end-to-end scoring run (roadmap A1) · ✅ done at `small` · full pending
The non-mock path is **proven** on localnet at `run_size=small` (real composite,
signed, on-chain weight accepted). Two things remain to fully close it:

1. **Full-scale proof.** Set `VALIDATOR_RUN_SIZE=full` and score a miner **built
   from the fixed starter kit (#9)** — already-submitted agents bake in the old
   2 MB `/seed` limit and fail full seeding. A validated tarball of the fixed
   baseline is ready (`ditto upload` needs a funded coldkey + a hotkey registered
   on netuid 3). This one run also validates #9's compile (via the screener build).
2. **Weight actually lands on the miner.** In the small proof the scored miner
   hotkey was **not registered on netuid 3**, so its 0.9 champion weight mapped to
   no UID and was skipped (only the validator's tail 0.1 landed). **Register
   submitting miners on the localnet** (or accept it as a localnet-only artifact —
   on a real network a miner must be registered to submit).

### Hop 2 — Cost + egress controls (roadmap C3 / C-ISO) · before any volume
- **OpenRouter account-level spend cap** — the code caps (`LLM_MAX_TOKENS`,
  `LLM_RUN_TOKEN_BUDGET`) can't stop a compromised key used elsewhere. Set it on
  OpenRouter's side.
- **Tune the code caps against a `full` run's observed `usage.total_tokens`**
  (defaults are generous placeholders — too low fails legit full runs; too high
  under-protects).
- **Sandbox egress allowlist — phase-1 plumbing merged** (dittobench-api #13);
  the **enforcement is an infra task**: deploy an allowlisting CONNECT proxy
  (`openrouter.ai` only) + a `ditto-sandbox` docker network + iptables/nft rules
  (DROP all egress except → proxy and → host-gateway, **fail-closed**), then set
  `DITTOBENCH_SANDBOX_EGRESS_NETWORK/PROXY` + `DITTOBENCH_SANDBOX_HARDEN=true`.
  ⚠️ Touches live-VM firewall rules on the validator host — stage carefully so the
  validator's own egress stays intact. Design: `dittobench-api/docs/sandbox-egress-hardening.md`.

### Hop 3 — Screener promotion (roadmap A2) · ✅ automated + live
The screener worker is **deployed and running** on `ditto-validator-dev`;
`uploaded → evaluating` happens on its own. No operator DB action needed. (Keep
`SCREENER_MAX_TARBALL_BYTES` ≥ the platform's `DITTO_MAX_TARBALL_SIZE_BYTES`, and
`SCREENER_SMOKE_ENV` carries the dummy `OPENROUTER_API_KEY` so the harness boots.)

### Hop 4 — Emissions (roadmap B1) · ✅ done on localnet
Consensus picks the winner (`Incentive[3] = 65535`) **and `SubnetTaoInEmission[3]`
is non-zero**, so **alpha flows** on netuid 3. Remaining is per-network tuning:

1. Confirm the winning miner's `TotalHotkeyAlpha` **increases** on each real run
   (once miners are registered — see Hop 1.2).
2. On migration, **re-tune** the alpha-pool / `TaoWeight`
   ([`STATE-OF-THE-SUBNET.md`](STATE-OF-THE-SUBNET.md) §"For Ethan").

### Hop 5 — Network migration (roadmap E) · 🔴 the live frontier
Move off the dev localnet:

1. **E1 — Pylon identity (write) creds** provisioned (§1) so `put_weights` works
   in production instead of the SDK fallback. **This is the single
   highest-leverage step** — it is the first time the production weight path (all
   delegated to Pylon: normalization, u16, commit-reveal, `version_key`) touches a
   real chain. **Verify each of those on testnet**, don't assume.
2. **E2 — Stake the subnet owner hotkey** on **testnet** to clear the
   `validator_permit` threshold. **No validator registration or registration burn.**
3. **E3 — Chain params:** set tempo, immunity period, weights-rate-limit,
   validator-permit threshold, and **enable commit-reveal** (roadmap B2 — dev has
   it off). Then align `VALIDATOR_EPOCH_SECONDS` to the network's real tempo and
   set `VALIDATOR_WEIGHT_VERSION_KEY` to the agreed mechanism version (defaults to
   the package spec version; confirm it matches Pylon's).
4. Point the deploy at the target network (`SUBTENSOR_NETWORK`, `NETUID`), flip
   `enable_validator`, and **re-run Hop 1 (full)** on that network.

### Hop 6 — Decentralize (roadmap A3) · engineering + onboarding
Multi-validator (k=3 sharded queue + median-of-3) is **not built** — lease table +
stub→target endpoint rename + median fold. **To test on localnet with no new
funding:** create **2 new hotkeys under the existing localnet validator coldkey**
and register them on netuid 3 (fallback: a fresh coldkey/hotkey pair + transfer
localnet TAO). Production needs **≥1 additional independent validator onboarded**
(a partner action once roadmap D3/O-VAL exists).

### Hop 7 — Mainnet (finney) cutover (roadmap E4)
Repeat Hop 5 against finney SN118, run the deploy runbook, verify each hop, and
run a real full E2E on mainnet. Keep the revert-to-finney backup notes handy
([`STATE-OF-THE-SUBNET.md`](STATE-OF-THE-SUBNET.md) §"For Ethan").

---

## 3. Code-ready but needs a **decision** or eng time, not a credential

Sequence these against the hops (full detail + status in
[`ROAD-TO-PRODUCTION.md`](ROAD-TO-PRODUCTION.md)):

- **Sandbox egress enforcement** (C-ISO phase-2) — the infra proxy + firewall
  (Hop 2). Plumbing is merged; this is the deploy.
- **Multi-validator consensus** (A3) — endpoint rename + lease table + median.
- **Commit-reveal reveal step** (B2) — enable + verify on testnet (Hop 5.3).
- **Weight-set residuals** — read on-chain tempo/`weights_rate_limit` directly
  (today a hand-set proxy) + backoff on rate-limit rejection + a min-stake check.
- **Signature replay-nonce** (C2) — wire-contract change (both `api_models`
  copies + golden regen + signing message).
- **Bounded re-score / terminal fail** — a harness that fails scoring currently
  re-runs full datagen + LLM cost every epoch (seen live); add a retry cap.
- **Plagiarism threshold tuning + review-queue automation** (C1) — the gate is
  built; tune tolerances against a real corpus and automate `resolve_review`.
- **Observability, DB backups/PITR, HA/DR, autoupdater, secret rotation** (D/O).

**Open product decisions:** trust model (owner-scorer vs verifiable scoring, C5),
participation-tail economics (B3), registration/immunity economics (B4), emission
split target (B1), endpoint-rename timing (A3), and production `run_size`.

---

## 4. Deploy & verify quick-reference

| Task | Command / knob |
| --- | --- |
| Apply DB schema | platform: `make migrate` (alembic `upgrade head`) |
| Bring up validator + screener + dittobench | infra `terraform` (`enable_validator=true` in `terraform/envs/gcp-platform/validator.tf`; **a plain apply without the var wants to DESTROY validator resources**) + `ansible` roles `dittobench`, `validator_worker`, `screener_worker` |
| IAP converge (dev) | `cd infra/ansible && GCP_OSLOGIN_USER=nickanderson_omniaura_ai ANSIBLE_HOST_KEY_CHECKING=False ansible-playbook -i inventory/validator-static.yml playbooks/gcp-validator.yml --private-key ~/.ssh/google_compute_engine --limit ditto-validator-dev` |
| Validator weight path | `VALIDATOR_USE_SDK_WEIGHTS=true` (dev/localnet fallback) **vs** unset + `PYLON_IDENTITY_*` (production Pylon path) |
| Validator cadence | `VALIDATOR_SWEEP_SECONDS` (scoring, 120s) vs `VALIDATOR_EPOCH_SECONDS` (weight-set, 3600s); `VALIDATOR_WEIGHT_VERSION_KEY` (mechanism version) |
| KOTH knobs | `VALIDATOR_KOTH_MARGIN` (0.01), `VALIDATOR_KOTH_CHAMPION_SHARE` (0.9), `VALIDATOR_KOTH_TAIL_SIZE` (4) — tune vs real score spread |
| Run size | `VALIDATOR_RUN_SIZE` (small\|medium\|full); **full needs miners built from starter-kit#9** |
| Scorer models + cost caps | dittobench `GENERATOR_MODEL`, `SCORER_MODEL`; `LLM_MAX_TOKENS`, `LLM_RUN_TOKEN_BUDGET` |
| Sandbox egress (once infra ready) | `DITTOBENCH_SANDBOX_EGRESS_NETWORK`, `DITTOBENCH_SANDBOX_EGRESS_PROXY`, `DITTOBENCH_SANDBOX_HARDEN`, `DITTOBENCH_SANDBOX_PIDS_LIMIT` |
| Ban a miner hotkey | platform: `uv run python scripts/ban_hotkey.py <hotkey> --reason "…"` (`--unban`) |
| Clear/ban a review hold | platform: `uv run python scripts/resolve_review.py <agent_id> --decision scored\|banned` |
| Verify a score landed | `GET /api/v1/scoring/scores` (validator-gated) |
| Verify emission | on-chain: champion `TotalHotkeyAlpha` increases (miner must be registered) |

**CI gotcha:** platform/subnet CI runs **`mypy` over the whole repo including
tests** on **Python 3.11 and 3.12**. Run the full-repo checks (not a subpackage)
before pushing. `make lint typecheck test` (platform) or the ruff + mypy + pytest
equivalent (subnet). dittobench-api CI is `go build/vet/test` (Go 1.23).

---

## 5. Hard external blockers to go-live (the short list)

1. **Pylon identity (write) credentials** (§1 / E1) — and verify the delegated
   weight path (normalization / u16 / commit-reveal / version_key) on testnet.
2. The **subnet owner hotkey staked to the `validator_permit` threshold** on the
   target network — validation runs under the owner UID, **no registration burn**.
3. An **OpenRouter key** for `run_size` **plus** an account-level cost cap.
4. Prod **Postgres** + **S3/MinIO** credentials for the target deployment.
5. ~~First real E2E~~ — **done at small**; full-scale needs a miner from starter-kit#9.
6. ~~Screener automation~~ — **done + live**.
7. ~~Non-zero netuid emission~~ — **done on localnet**; re-tune per network.

Everything else on the critical path is unblocked once these exist.

---

## 6. References

- [`ROAD-TO-PRODUCTION.md`](ROAD-TO-PRODUCTION.md) — current prioritized
  remaining-work checklist + "definition of production ready".
- [`NEXT-STEPS.md`](NEXT-STEPS.md) — full engineering roadmap (workstreams A–G).
- [`STATE-OF-THE-SUBNET.md`](STATE-OF-THE-SUBNET.md) — on-chain specifics (§"For
  Ethan": exact emission values + finney backup notes).
- [`dev-e2e-handoff.md`](dev-e2e-handoff.md) — step-by-step dev runbook.
- [`incentive-mechanism.md`](incentive-mechanism.md) — KOTH+ATH rationale.
- Recent merges: subnet **#32/#34** (screener), **#36** (chain-conformance),
  **#37/#38** (docs); dittobench-api **#13** (sandbox egress); starter-kit **#9**
  (seed body limit). Earlier: dittobench-api #8, ditto-subnet #24, ditto-platform
  #10/#11, subnet #22.
