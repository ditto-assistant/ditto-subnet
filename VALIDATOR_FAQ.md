# Validator FAQ: Ditto Subnet (Bittensor SN118)

What to expect for release and what to have ready. For the full runbook (every
env var, boot self-checks, verification, current caveats) see
[`docs/VALIDATOR-ONBOARDING.md`](docs/VALIDATOR-ONBOARDING.md). This is the short
version.

---

## What a validator is

One stateless Python process, `python -m ditto.validator`, that loops forever:

1. Pull agents awaiting evaluation from the platform's `/validator/*` API.
2. Score each through the hosted dittobench-api (by presigned tarball URL, using
   your own OpenRouter key), sign the result, and report it back.
3. Set weights on chain via Pylon on an epoch cadence.

It has no database and no local state. It builds nothing and hosts no models: the
docker build, the seeded benchmark, and the LLM judge all run in the hosted
dittobench service. You can kill and restart the worker at any time with nothing
to back up.

---

## Requirements

Suggested starting point. The worker is HTTP plus signing, so it is deliberately
light.

- **1-2 vCPU, 2 GB RAM.** No GPU.
- **Linux host.**
- **Python 3.11+ and [`uv`](https://docs.astral.sh/uv/).**
- **Stable outbound network** to the platform API, the dittobench-api, and a
  chain endpoint (Pylon or a subtensor node). The worker listens on nothing.
- **A few GB of disk** for the repo and the uv environment.

---

## What you need before release day

- [ ] **A hotkey registered on SN118 with a `validator_permit`** (stake above the
      permit threshold). The chain only accepts weights from permitted validators.
- [ ] **The hotkey's signing source** on the box: wallet files or a mnemonic. The
      coldkey is never needed.
- [ ] **An OpenRouter API key** for the LLM-judge portion of each dittobench run.
      Bring your own. We store nothing for you.
- [ ] **A weight-submission path**: Pylon identity for production, or the
      bittensor SDK fallback for localnet.
- [ ] **The repo pulled and synced** (`git clone` + `uv sync`).

Keep the mnemonic or wallet key and the OpenRouter key in a secret manager and
inject them as env. They must never be logged or committed. The validator hotkey
(an SS58 address) is public.

---

## Setup

```sh
git clone https://github.com/ditto-assistant/ditto-subnet
cd ditto-subnet
uv sync
```

Configuration is entirely env-driven (`ditto/validator/config.py`). The worker
fails fast at boot on anything missing or malformed. The core settings:

```sh
VALIDATOR_PLATFORM_API_URL    # platform API base URL
VALIDATOR_HOTKEY              # your validator hotkey (SS58)
VALIDATOR_WALLET_NAME + VALIDATOR_WALLET_HOTKEY   # or VALIDATOR_MNEMONIC
NETUID                        # 118 on finney
VALIDATOR_DITTOBENCH_API_URL  # hosted dittobench-api base URL
VALIDATOR_OPENROUTER_KEY      # your LLM-judge key (secret)

# weight path, pick one:
PYLON_URL + PYLON_IDENTITY_NAME + PYLON_IDENTITY_TOKEN   # production
VALIDATOR_USE_SDK_WEIGHTS=1 + SUBTENSOR_NETWORK          # fallback / localnet
```

The onboarding doc lists the rest of the knobs (sweep and epoch cadence, run
size, consensus mechanism values, telemetry).

---

## Run it

```sh
python -m ditto.validator
```

Run it under a supervisor (systemd or pm2) with restart-on-exit. It drains
cleanly on SIGTERM/SIGINT. Run exactly one instance per hotkey. Two instances
double-submit weights.

For local plumbing without a real key, set `VALIDATOR_DITTOBENCH_MOCK=1` to
return a canned score.

---

## FAQ

**Do I need a GPU?**

No. The judge runs via OpenRouter and the benchmark build runs in the hosted
dittobench service. The validator itself is HTTP plus signing.

**How much will OpenRouter cost?**

It scales with how many agents you score. You bring your own key for the judge.

**Does my hotkey need to be registered before I can validate?**

Yes, on SN118 with a `validator_permit`. Without the permit the worker still
scores, but it skips weight submission and logs that loudly each epoch.

**How do I know it is working?**

Logs show `sweep complete: N agent(s)` and `submitted weights for N miner(s)`.
Your signed scores show up on the platform's public score ledger, and your
hotkey's last-update block advances each epoch. See the onboarding doc's "Verify
it's working" for the full checklist.

**What is still being finalized?**

Third-party validator onboarding opens with the testnet/mainnet migration. The
subnet currently runs a single team validator. See the onboarding doc's status
section for the live caveats.
