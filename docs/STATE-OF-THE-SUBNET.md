# State of the Subnet — Ditto SN118

**As of 2026-06-30.** Audience: leadership + team. A snapshot of where the subnet is, what
we've proven, and the high-level path to a production launch.

---

## Executive summary

The **end-to-end incentive loop works.** On 2026-06-30 we ran the full pipeline live against the
dev chain through the **deployed platform API on GCP**: a miner submitted an agent, the platform
verified the on-chain payment and stored it, a validator scored it and set weights on chain, and
Bittensor's **Yuma consensus selected that miner as the winner-take-all recipient (incentive =
1.0)**. This is the "walking skeleton" the project plan set as the Phase-1 exit criteria — now
demonstrated, real components and all, not stubbed.

What's left is **depth, not plumbing**: real evaluation (DittoBench) in place of the mock scorer,
the pre-screen gate, multi-validator scoring at scale, the final incentive mechanism, dev-chain
emission tuning, and productionization onto testnet → mainnet.

---

## Architecture (today)

```
   miner CLI                Platform API  (GCP, deployed)              validator worker
  (ditto-subnet)   HTTP →   Postgres · GCS · Pylon          HTTP ←   (ditto-subnet)
        │  pays eval fee          │ verifies payment on-chain              │ scores agent
        ▼                         ▼ stores agent + score ledger           ▼ sets weights (SDK)
                       Bittensor chain  ──────  Yuma consensus  ──────►  emissions to winner
```

- **`ditto-subnet`** — the miner CLI (`ditto upload/status/verify`) and the validator worker
  (`python -m ditto.validator`).
- **`ditto-platform`** — the team-operated API (miner intake, on-chain payment verification, object
  storage, the validator-facing queue + signed score ledger). Deployed on GCP, auto-deploys from
  `dev`.
- **`ditto-harness`** — the Rust reference memory-harness miners fork.
- The contract between them is the platform's OpenAPI schema; the validator is stateless (no DB).

---

## What's been achieved

- **Repo split + deploy.** API extracted into `ditto-platform`, deployed to a GCP VM behind
  `platform-api-dev.heyditto.ai`, running the real FastAPI app against the dev chain.
- **Miner CLI — complete.** Upload / status / pre-flight; signs + pays the eval fee on chain and
  streams the agent + payment proof to the API. Proven against the live API.
- **On-chain payment verification.** The platform re-verifies the miner's eval-fee extrinsic on
  chain before accepting an agent (replay-protected). Two real encoding bugs fixed to get here.
- **Validator worker.** Pulls the evaluation queue, scores an agent, submits a **signed** score to
  the public ledger, and sets weights on chain. Score-signing means no one has to trust the API.
- **Full E2E on the dev chain (2026-06-30).** Verified live — see below.
- **Incentive mechanism (winner-take-all) confirmed** via Yuma consensus on chain.

### Verified end-to-end run (dev chain, netuid 3)

| Step | Evidence |
| --- | --- |
| Miner upload → GCP API | eval fee paid on chain (block 600849); payment verified; tarball in GCS; DB row (agent `7bf5bb99…`) |
| Validator scores it | signed `submit_score` → agent `scored`; row in the `scores` ledger (composite, signature) |
| Validator sets weights | on chain: validator (UID 4) → miner (UID 3), `Weights[4] = [(3, 65535)]` |
| Consensus picks the winner | **miner UID 3 `incentive = 1.0`** (winner-take-all); validator dividends = 1.0 |

---

## What's left to complete the subnet (high-level goals)

1. **Real evaluation (DittoBench).** Replace the mock scorer with the hosted bench: synthetic,
   seeded data generation + eval runner + failure classifier, in an isolated evaluator sandbox with
   an OpenRouter egress allowlist + cost cap. *(Nick + Omar)*
2. **Pre-screen / screener.** A cheap gate (Rust lint + compile + build) that promotes submissions
   `uploaded → evaluating` automatically; today that transition is manual. *(Dan)*
3. **Scoring + weights at scale.** The k=3 sharded evaluation queue, median-of-3 finalization, the
   public signed score ledger, and the deterministic validator-side weight function so every
   validator computes identical weights (Yuma clips deviators). Move from one validator to the
   full set. *(Dan)*
4. **Final incentive mechanism.** Decide KOTH / top-K / Pareto, and the copy/plagiarism resistance
   (first-seen timestamps + similarity checks) so the current winner can't simply be resubmitted.
   *(team — see `docs/incentive-mechanism.md`)*
5. **Dev-chain emission economics.** The pipeline drives consensus correctly (incentive = 1.0), but
   the subnet's *per-block emission* currently computes to **0**, so winners don't yet accrue alpha
   — a dev-pool tuning matter, not a pipeline issue. **Details for Ethan below.**
6. **Productionization.** Testnet → finney; re-enable commit-reveal in production (the worker then
   needs a reveal step); decide Pylon identity write-path vs. the bittensor-SDK weight path;
   validator deployment (pm2 + git autoupdater); observability (W&B run logging, public winner
   dashboard); deploy hardening.

---

## For Ethan — emission tuning (goal #5, specifics)

The miner is the on-chain consensus winner (`Incentive[3] = 65535`), but no alpha is flowing:

- `BlockEmission` (global) = `1 TAO/block` — the chain *is* minting.
- `FirstEmissionBlockNumber[3] = 597799` — the subnet is already "started" (`start_call` is a no-op).
- **But** `SubnetTaoInEmission[3] = 0` and `SubnetAlphaOutEmission[3] = 0`, and the miner's
  `TotalHotkeyAlpha` did not change across blocks — the subnet's **emission share is computing to
  zero**, most likely from the alpha pool price / `TaoWeight` setup (`SubnetTAO ≈ 7108τ`,
  `SubnetAlphaIn ≈ 3.47e18` → a very low alpha price).
- **Ask:** tune the netuid-3 alpha pool / emission split so the subnet receives a non-zero per-block
  emission; then a re-run should show the winning miner's alpha accrue.

---

## Dev-chain caveats (current setup)

- **Chain:** `ws://68.183.141.180:80`, netuid 3, `node-subtensor` spec 393. It's older than
  bittensor 10.3.2 / btcli 9.22, so `btcli metagraph`/`wallet overview` fail — read state via Pylon
  or targeted SDK calls.
- **GCP dev API** is repointed at the dev localnet (netuid 3). Revert-to-finney backup is on the VM
  at `/opt/ditto-platform/.env.bak-pre-localnet`.
- **Commit-reveal is currently disabled** on netuid 3 (so weights apply directly for the dev proof).
  Production keeps it on → the validator worker will need a reveal step.
- **Weights** on the localnet are set via the **bittensor SDK** (`VALIDATOR_USE_SDK_WEIGHTS`), since
  Pylon's identity write-path isn't stood up here.

See [`docs/dev-e2e-handoff.md`](dev-e2e-handoff.md) for the step-by-step runbook and
[`PROJECT.md`](../PROJECT.md) for the detailed plan + owners.
