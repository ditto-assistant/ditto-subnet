# SN118 — Credentialed Handoff

**As of 2026-07-06.** Audience: the operator who holds (or can obtain) the
**production credentials, chain keys, and infra access**. This document is the
bridge between the code — which is where it needs to be for the next steps — and
the things that **cannot be done from a keyboard alone**. It is deliberately
scoped to *"what must a person with credentials do, and in what order."*

Read it alongside [`NEXT-STEPS.md`](NEXT-STEPS.md) (the full engineering roadmap)
and [`STATE-OF-THE-SUBNET.md`](STATE-OF-THE-SUBNET.md) (the on-chain specifics,
§"For Ethan"). This doc does **not** duplicate the roadmap's per-item detail; it
sequences the credential-gated hops and names the exact secrets/knobs each needs.

---

## 0. What just landed (so you know the starting line)

Three hardening PRs merged 2026-07-02, on top of the KOTH+ATH + weight-ingestion
work:

| Repo | PR | Effect |
| --- | --- | --- |
| `dittobench-api` | #8 → `main` | Per-run OpenRouter **cost cap** (`LLM_MAX_TOKENS` / `LLM_RUN_TOKEN_BUDGET`), presigned-URL **leak redaction**, extractor **CPU-DoS** guard + `ctx`. |
| `ditto-subnet` | #24 → `dev` | Validator **forwards `tarball_sha256`** to the scorer (tag pin + byte re-verify) and cross-checks queue-vs-artifact digest. |
| `ditto-platform` | #11 → `dev` | **`banned_hotkeys`** table + upload/retrieval enforcement (migration `a3f1c9d27b40`). |

The pipeline (miner → platform → screener → validator → dittobench → chain) is
plumbed and the validator has run **live on the dev localnet (netuid 3)** with the
mock scorer off.

**Landed since (through 2026-07-06):** the first **real E2E scoring run** (2026-07-03),
public transparency (leaderboard/health API + dashboard + opt-in W&B telemetry), and
the **two-channel content-plagiarism gate** (lexical + AST fingerprint; C1). Two
earlier "blockers" turned out not to be: emission **already flows on localnet**
(`SubnetTaoInEmission[3]` non-zero), and production validation runs under the
**subnet owner UID** (no separate validator registration/burn). What remains is
credential/infra wiring — Pylon write creds, per-network staking + tuning, cost
cap, and the network migration.

---

## 1. Credentials & secrets you must hold

Everything here is an *external* dependency — a person/account, not code. Until
each is provisioned, the mapped capability stays on its dev fallback (or off).

| Secret / access | Goes where | Unblocks | Today |
| --- | --- | --- | --- |
| **Subnet owner hotkey** (mnemonic) **staked to the `validator_permit` threshold** on the target network — validation runs under the **owner UID**, so **no separate validator registration/burn** | `VALIDATOR_MNEMONIC` (or `VALIDATOR_WALLET_NAME` + `VALIDATOR_WALLET_HOTKEY`); must match `VALIDATOR_HOTKEY` | Weight-setting on that network | Owner UID validates on localnet netuid 3 |
| **Pylon identity (write) creds** | `PYLON_IDENTITY_NAME`, `PYLON_IDENTITY_TOKEN` (Secret Manager → `platform.env`) | Production `put_weights` via Pylon (commit-reveal handled by Pylon) | Only a **read** token exists; SDK path is the dev fallback |
| **OpenRouter API key** (with an **account-level spend cap** set on OpenRouter's side) | `VALIDATOR_OPENROUTER_KEY` (subnet validator, which forwards it) and/or `OPENROUTER_API_KEY` (dittobench-api's own env / the crate) | `run_size` scoring (generator + judge) | Needed for any real run |
| **GitHub token (read)** for pulling `ditto-harness` during `docker build` | dittobench-api `GitHubTokenFile` → BuildKit `--secret gh_token` | Crate builds that depend on the reference harness | Provisioned in dev; confirm on target host |
| **GCP infra access** (Terraform apply, Secret Manager, systemd) | n/a (operator identity) | Deploy + the `enable_validator` gate | Dev deploy done, gated |
| **Prod Postgres credentials** | `POSTGRES_*` (`.env` / Secret Manager) | Platform DB (ledger, agents, bans) | Dev DB only |
| **S3/MinIO storage creds** | `STORAGE_*` on the platform | Tarball storage + presigned URLs | Dev MinIO only |
| **TAO to stake the owner hotkey** to the permit threshold | the owner coldkey | Meeting `validator_permit` on testnet/finney (no *registration* burn — owner UID already exists) | localnet only |
| **W&B API key** | `WANDB_API_KEY` (Secret Manager → validator env) | Opt-in aggregate telemetry + dashboard link | ✅ provisioned — `docs/wandb-keys.yml` (gitignored) |

> **Secret hygiene:** all of the above live in **GCP Secret Manager** on the dev
> deploy; keep them there, never in the repo. The validator hotkey has already
> been rotated once — capture the rotation runbook (roadmap D6) the next time you
> touch it.

---

## 2. Critical path (each hop gates the next)

This is [`NEXT-STEPS.md` §2](NEXT-STEPS.md) rendered as **operator actions**. Do
them in order; do not skip ahead.

### Hop 1 — First real end-to-end scoring run (roadmap A1) · 🔴 do first
Prove the non-mock path works before anything else matters.

1. Ensure `VALIDATOR_DITTOBENCH_MOCK` is **unset/false** and
   `VALIDATOR_OPENROUTER_KEY` + `VALIDATOR_DITTOBENCH_API_URL` are set (see
   [`dev-e2e-handoff.md`](dev-e2e-handoff.md)).
2. Get one agent to `evaluating` (screener is still **manual** — see Hop 3), then
   let the validator sweep: `get_artifact` → dittobench `tarball_url` (+ the new
   `tarball_sha256`) → `docker build` (pulls `ditto-harness` via the GH token) →
   datagen → tool+memory cases → judge → `ScoreReport`.
3. Confirm the signed score is accepted at `POST /validator/agent/{id}/score`, the
   row lands in `scores`, and the agent appears in `GET /scoring/scores`.
4. Confirm KOTH weights (0.9 to the champion) are computed and `put_weights`
   succeeds, and that the weight **persists across the next epoch** (the whole
   point of the ledger fix).

**Acceptance:** a real composite for a real harness is visible in the ledger and
drives a persistent on-chain weight. Do this on **localnet first**, then re-run on
the target network after Hop 5.

### Hop 2 — Cost controls live (roadmap C3) · before any volume
The code caps (PR #8) are in, but they are only half the story:

- Set an **account-level spend cap** on the OpenRouter account (the code cap can't
  stop a compromised key used elsewhere).
- **Tune the code caps against Hop 1's observed usage:** run one `full` profile,
  read the run's `usage.total_tokens`, and set `LLM_RUN_TOKEN_BUDGET` to a value
  with headroom above it. The defaults (`LLM_MAX_TOKENS=8192`,
  `LLM_RUN_TOKEN_BUDGET=8_000_000`) are generous placeholders — **a too-low budget
  fails legitimate full runs; a too-high one under-protects.**
- Egress allowlist for the sandbox container is still **open** (default bridge).
  That needs an egress proxy (roadmap C3) — an engineering task, tracked, not a
  blocker for a controlled run.

### Hop 3 — Screener promotion (roadmap A2) · today it is manual
There is **no screener worker** yet, so `uploaded → evaluating` does not happen on
its own. Until A2 is built you must promote submissions **by hand** (an operator
DB action on the platform side). Note this so a real miner submission isn't left
stuck in `uploaded`. Building the Rust/Python screener daemon is the durable fix.

### Hop 4 — Emissions (roadmap B1) · ✅ done on localnet
Consensus picks the winner (`Incentive[3] = 65535`) **and
`SubnetTaoInEmission[3]` is non-zero**, so **alpha flows** on netuid 3 (the earlier
"= 0" reading was stale). Remaining is per-network tuning, which you own:

1. Confirm the winning miner's `TotalHotkeyAlpha` **increases** on each real run.
2. On migration, **re-tune** the netuid's alpha-pool / `TaoWeight` for the target
   network (exact values + backup notes:
   [`STATE-OF-THE-SUBNET.md`](STATE-OF-THE-SUBNET.md) §"For Ethan").

### Hop 5 — Network migration (roadmap E)
Move off the dev localnet:

1. **E1 — Pylon identity (write) creds** provisioned (§1) so `put_weights` works
   in production instead of the SDK fallback.
2. **E2 — Stake the subnet owner hotkey** on **testnet** to clear the
   `validator_permit` threshold. **No validator registration or registration burn** —
   validation runs under the owner UID (same as localnet).
3. **E3 — Chain params:** set tempo, immunity period, weights-rate-limit,
   validator-permit threshold, and **enable commit-reveal** (roadmap B2 — dev has
   it off; the worker offloads reveal to Pylon in identity mode).
4. Point the deploy at the target network (`SUBTENSOR_NETWORK`, `NETUID`), flip
   `enable_validator`, and **re-run Hop 1** on that network.

### Hop 6 — Decentralize (roadmap A3) · engineering + onboarding
Multi-validator (k=3 sharded queue + median-of-3) is **not built** — it needs a
lease table, the stub→target endpoint rename, and the median fold. It is an
engineering task (flagged in §3). **To test it on localnet with no new funding:**
create **2 new hotkeys under the existing localnet validator coldkey** and register
them on netuid 3 (fallback if that misbehaves: generate a fresh coldkey/hotkey pair
and transfer localnet TAO to it from the current validator key). That gives 3
distinct validator hotkeys to exercise the median fold. Production still needs **≥1
additional independent validator onboarded** (a partner action once roadmap D3 exists).

### Hop 7 — Mainnet (finney) cutover (roadmap E4)
Repeat Hop 5 against finney SN118, run the deploy runbook, verify each hop, and
run a real E2E on mainnet. Keep the revert-to-finney backup notes handy
([`STATE-OF-THE-SUBNET.md`](STATE-OF-THE-SUBNET.md) §"For Ethan").

---

## 3. Code-ready but needs a **decision**, not a credential

These do **not** wait on you-with-credentials; they wait on eng time and (some) a
product call. Sequence them against the hops above:

- **Multi-validator consensus** (A3) — endpoint rename + lease table + median.
- **Signature replay-nonce** (C2) — a wire-contract change (new field in both
  `api_models/validator.py` copies + golden regen + signing message).
- **Content-level plagiarism** (C1) — a fingerprint computed where the tarball is
  already unpacked (screener/dittobench), fed into the anti-copy gate.
- **Sandbox egress allowlist** (C3) — needs an egress proxy.
- **Observability** (D1) — structured logs, W&B, metrics, a public leaderboard.

**Open product decisions** (roadmap §4): trust model (owner-scorer vs verifiable
scoring, C5), participation-tail economics (B3), registration/immunity economics
(B4), emission split target (B1), and endpoint-rename timing (A3).

---

## 4. Deploy & verify quick-reference

| Task | Command / knob |
| --- | --- |
| Apply DB schema (incl. the new `banned_hotkeys` migration `a3f1c9d27b40`) | platform: `make migrate` (alembic `upgrade head`) |
| Bring up the validator | infra `terraform` (`enable_validator=true` in `terraform/envs/gcp-platform/validator.tf`) + `ansible` roles `dittobench`, `validator_worker` |
| Validator weight path | `VALIDATOR_USE_SDK_WEIGHTS=true` (dev/localnet fallback) **vs** unset + `PYLON_IDENTITY_*` (production Pylon path) |
| KOTH knobs | `VALIDATOR_KOTH_MARGIN` (0.01), `VALIDATOR_KOTH_CHAMPION_SHARE` (0.9), `VALIDATOR_KOTH_TAIL_SIZE` (4) — tune against real score spread once Hop 1 gives you composites |
| Scorer models | dittobench `GENERATOR_MODEL`, `SCORER_MODEL`; cost caps `LLM_MAX_TOKENS`, `LLM_RUN_TOKEN_BUDGET` |
| Ban a miner hotkey | platform: `uv run python scripts/ban_hotkey.py <hotkey> --reason "…"` (`--unban` to remove) |
| Clear/ban a review hold | platform: `uv run python scripts/resolve_review.py <agent_id> --decision scored|banned` |
| Verify a score landed | `GET /api/v1/scoring/scores` (validator-gated) |
| Verify emission | on-chain: `TotalHotkeyAlpha` of the champion increases after Hop 4 |

**CI gotcha (don't get bitten):** platform/subnet CI runs **`mypy` over the whole
repo including tests** on **Python 3.11 and 3.12**. Run the full-repo `mypy` (not a
subpackage) before pushing. `make lint typecheck test` (platform) or the ruff +
mypy + pytest equivalent (subnet) must be green.

---

## 5. Hard external blockers to go-live (the short list)

1. The **subnet owner hotkey staked to the `validator_permit` threshold** on the
   target network (testnet, then finney) — validation runs under the owner UID, so
   **no separate validator registration or registration burn**.
2. **Pylon identity (write) credentials** (§1 / roadmap E1).
3. An **OpenRouter key** for `run_size` **plus** an account-level cost cap.
4. ~~Non-zero netuid emission~~ — **done on localnet** (Hop 4 / B1); re-tune per network.

Everything else on the critical path is unblocked once these exist.

---

## 6. References

- [`NEXT-STEPS.md`](NEXT-STEPS.md) — the full engineering roadmap (workstreams A–G).
- [`STATE-OF-THE-SUBNET.md`](STATE-OF-THE-SUBNET.md) — on-chain specifics + the
  6/30 dev-chain proof (§"For Ethan": exact emission values + finney backup notes).
- [`dev-e2e-handoff.md`](dev-e2e-handoff.md) — step-by-step dev runbook.
- [`incentive-mechanism.md`](incentive-mechanism.md) — KOTH+ATH rationale.
- Merged hardening: dittobench-api **#8**, ditto-subnet **#24**, ditto-platform
  **#11**. Prior incentive/ledger work: ditto-platform **#10**, ditto-subnet **#22**.
