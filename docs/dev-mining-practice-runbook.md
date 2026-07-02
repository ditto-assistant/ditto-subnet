# Dev Mining Practice — Agent Runbook (SN118, dev localnet)

**Goal:** get set up to *practice mining* Ditto Subnet 118 end-to-end in dev —
first off-chain (fast, free, no TAO), then on-chain against the dev localnet
(netuid 3) — and be ready to do everything a miner does: build a harness, score
it, register a hotkey, pay the eval fee, upload, and watch consensus pick the
winner.

This is a hand-off artifact. It is self-contained; an agent (or a human) should
be able to execute it top to bottom. **It contains no secrets** — the devwallet
mnemonics are provided out-of-band and are injected at the `btcli regen` step.

> Audience note: read the dev-chain caveats and the **wallet reality check**
> below *before* touching keys. There are two known footguns baked into the
> details we were handed.

---

## 0. Repos required in the workspace (full GitHub URLs)

Clone all six under one parent dir. Roles:

| Repo | URL | Role in mining practice |
| --- | --- | --- |
| **dittobench-starter-kit** | https://github.com/ditto-assistant/dittobench-starter-kit | **The miner harness you build + optimize.** Rust agent + memory + tools + local eval loop. Your primary workspace. |
| **ditto-harness** | https://github.com/ditto-assistant/ditto-harness | Shared Rust agent+memory crate the kit depends on (embedded Turso + vector search). Pulled automatically as a git dep — you don't run it directly, but you need read access. |
| **dittobench-api** | https://github.com/ditto-assistant/dittobench-api | **Hosted off-chain practice validator (BYOK).** Rotates a fresh anti-cheat dataset per submission and scores your harness — mirrors the on-chain run+score loop with no chain. Public URL below. |
| **ditto-subnet** | https://github.com/ditto-assistant/ditto-subnet | **Miner CLI** (`ditto upload/status/verify`) + the validator worker (`python -m ditto.validator`). This is what talks to the chain + platform for real dev mining. |
| **ditto-platform** | https://github.com/ditto-assistant/ditto-platform | The team-operated API (miner intake, on-chain payment verification, object storage, score ledger). Deployed at `platform-api-dev.heyditto.ai`; you consume it, you don't run it. |
| **infra** | https://github.com/ditto-assistant/infra | Ansible/Terraform for the validator + platform VMs. The dev on-chain runbook lives at `docs/validator-deploy.md`. Reference only unless you're standing up the validator. |

```
dittobench-starter-kit ──depends on──► ditto-harness        (build the harness)
        │  serve /run,/seed,/health
        ▼
dittobench-api  (hosted practice validator, BYOK)           (Track A: off-chain score)

ditto-subnet (miner CLI) ──HTTP──► ditto-platform ──verify──► dev chain (netuid 3)
        │  pays eval fee + uploads              │ stores agent + score ledger
        ▼                                       ▼
   validator worker (ditto-subnet) ── scores ── sets weights ──► Yuma consensus  (Track B: on-chain)
```

```bash
mkdir -p ~/ditto && cd ~/ditto
for r in dittobench-starter-kit ditto-harness dittobench-api ditto-subnet ditto-platform infra; do
  git clone "https://github.com/ditto-assistant/$r"
done
# ditto-harness is private until ditto-assistant/ditto-harness#1 lands — cargo needs read access:
gh auth login && gh auth setup-git
export CARGO_NET_GIT_FETCH_WITH_CLI=true
```

---

## 1. ⚠️ Wallet reality check — read before touching keys

We were handed a `devwallet` with **listed** SS58 addresses *and* mnemonics.
**They do not match**, and one of the listed addresses belongs to the validator.
Two corrections apply:

1. **The mnemonics are authoritative; the listed SS58s are not.** Regenerating
   the wallet from the mnemonics produces **different** addresses than the ones
   pasted alongside them. Per Ethan, the mnemonics resolve to:
   - **Coldkey:** `5FxrUYiFhG8PmoN9eGChy8YSGXGrbL3RMNV4BEsmE6bDmYCJ`
   - **Hotkey:** `5Eex…` *(the value Ethan pasted was truncated — confirm the
     full 48-char SS58 from the `btcli regen` output; do not trust a partial
     string).*
   - The pasted `5Fy4Sw…wrsQH` (cold) and `5CZq6Mda…zK1mp` (hot) are **wrong** —
     ignore them.

2. **The pasted hotkey `5CZq6Mda…zK1mp` is the *validator's* hotkey**, not a
   miner key. It is hardcoded in `infra/ansible/host_vars/ditto-validator-dev.yml`
   as the registered validator on netuid 3. A miner **must** use its own,
   distinct hotkey — never mine with the validator's key.

**Rule for this exercise:** whatever addresses fall out of `btcli regen`
(the `5Fxr…` / `5Eex…` pair) are the ones you fund, register, and mine with.
Every on-chain check must be against *those*, not the pasted values.

### Regenerate the wallet (mnemonics injected here, never committed)

```bash
# Coldkey (spend authority — pays the eval fee):
btcli wallet regen-coldkey --wallet.name devwallet
#   → paste the COLDKEY mnemonic when prompted

# Hotkey (signs miner ops / registration):
btcli wallet regen-hotkey --wallet.name devwallet --wallet.hotkey default
#   → paste the HOTKEY mnemonic when prompted

# VERIFY the regenerated addresses match Ethan's 5Fxr… / 5Eex… pair:
btcli wallet list
```

> Keep the mnemonics in a local password manager / `.env` that is **gitignored**.
> Do not paste them into any file in these repos.

---

## 2. Dev-chain caveats (know these or you'll chase ghosts)

- **Chain endpoint:** `ws://68.183.141.180:80`, **netuid 3**, `node-subtensor`
  spec 393. It's **older** than bittensor 10.3.2 / btcli 9.22, so
  `btcli metagraph` and `btcli wallet overview` **fail** against it. Read chain
  state via **Pylon** or targeted **SDK** calls instead.
- **Platform API (dev):** `https://platform-api-dev.heyditto.ai`, repointed at
  the dev localnet (netuid 3). Auto-deploys from the platform repo's `dev`
  branch. Revert-to-finney backup lives on the VM at
  `/opt/ditto-platform/.env.bak-pre-localnet`.
- **Commit-reveal is DISABLED** on netuid 3 (weights apply directly for the dev
  proof). Production keeps it on → the validator worker will need a reveal step
  there. Not your concern for dev practice.
- **Weights are set via the bittensor SDK** (`VALIDATOR_USE_SDK_WEIGHTS`), since
  Pylon's identity write-path isn't stood up on the dev chain.
- **Emissions currently compute to 0** on the dev pool. Consensus works —
  the winning miner reaches `Incentive = 1.0` — but **alpha does not accrue** to
  the winner yet (a pool/`TaoWeight` tuning matter owned by Ethen, not a pipeline
  bug). **Success criterion for this exercise = `Incentive[miner_uid] → 1.0`,
  not a growing wallet balance.**

---

## 3. Track A — Off-chain practice (do this first: fast, free, no chain)

Iterate on the harness with zero TAO and zero chain. This is where you spend
most of your time getting the harness good.

### 3a. Build + talk to the harness (starter kit)

```bash
cd ~/ditto/dittobench-starter-kit
export CARGO_NET_GIT_FETCH_WITH_CLI=true

# Prereqs: Rust ≥ 1.85 (rustup), Ollama for embeddings.
ollama serve &
ollama pull embeddinggemma        # 768-dim embeddings; needs Ollama ≥ 0.6

cp .env.example .env
#   edit .env → OPENROUTER_API_KEY=sk-or-v1-...   (chat model)
#   defaults: DITTOBENCH_MODEL=google/gemini-3.1-flash-lite, embeddings via Ollama
#   fully local option: DITTOBENCH_PROVIDER=ollama, DITTOBENCH_MODEL=qwen2.5:7b (no key)

cargo run -- seed-user      # one-time: load the LongMemEval seed user (~2 min)
cargo run -- playground     # http://127.0.0.1:8088 — chat, watch retrieval + tool calls
```

### 3b. Score yourself locally

```bash
cargo run -- mem-eval --k 10   # retrieval recall@k over the seed user (no LLM, free)
cargo run -- evaluate          # FIXED benchmark — comparable run-to-run; use to iterate
cargo run -- practice --n 20   # ROTATING random dataset (anti-overfit), like the validator
```

Loop: change harness code / retrieval weights / model → `evaluate` → repeat.
Use `practice` to check you're not overfitting the fixed set.

### 3c. Score against the hosted practice validator (mirrors on-chain scoring)

The hosted DittoBench API rotates a **fresh anti-cheat dataset per submission**
(paraphrased tool cases + a freshly assembled LongMemEval haystack), seeds your
harness, runs every case, and scores with an LLM judge. **BYOK**: your
OpenRouter key rides along per request and is never stored.

```bash
# 1. expose your harness so the hosted API can reach it:
cargo run -- serve --port 9000        # POST /run, POST /seed, GET /health
ngrok http 9000                       # → https://<something>.ngrok.app  (must be PUBLIC)

# 2. submit for a full-pipeline practice run (small = cheap/fast):
curl -X POST https://dittobench-api-22790208601.us-central1.run.app/v1/submit \
  -H 'Content-Type: application/json' \
  -d '{"harness_url":"https://<your-ngrok>.ngrok.app","run_size":"small",
       "openrouter_key":"sk-or-..."}'
# → {"run_id":"...","status":"queued","poll":"/v1/runs/..."}

# 3. poll to completion:
curl https://dittobench-api-22790208601.us-central1.run.app/v1/runs/<run_id>
#   queued → generating → seeding → running → scoring → done
#   done → {composite, tool_mean, median_ms, ...}
```

`run_size`: `small` (6 tool + 6 mem cases) for iteration → `medium` → `full`
(60 tool + 50 mem). The hosted URL must resolve to a **public** IP (SSRF guard
rejects loopback / RFC1918 / metadata IPs). You can also run the API yourself
(`go run ./cmd/dittobench-api` in `dittobench-api`, listens on :8000) and point
it at `http://localhost:9000` with `DITTOBENCH_ALLOW_PRIVATE_HARNESS=true`.

**Exit criterion for Track A:** a `full` run returns a composite you're happy
with. Because the hosted + on-chain validators pin the *same* `ditto-harness`
ref, that score transfers to the subnet.

---

## 4. Track B — On-chain dev mining (netuid 3)

Now do the real thing against the dev localnet: register, pay the eval fee,
upload, and watch consensus.

> **Miner CLI location:** `ditto upload/status/verify` lives on **ditto-subnet's
> `dev` branch**. If your ditto-subnet checkout predates the repo split (no
> `ditto` CLI / no `ditto.validator` module — it'll still look like the API
> monolith), fetch dev first:
> ```bash
> cd ~/ditto/ditto-subnet && git fetch origin dev && git checkout dev && uv sync
> ```

### 4a. Point the miner CLI at the dev chain + platform

```bash
cd ~/ditto/ditto-subnet
cp .env.example .env
# set for the dev localnet:
#   SUBTENSOR_NETWORK=ws://68.183.141.180:80
#   NETUID=3
#   platform base URL → https://platform-api-dev.heyditto.ai
#   wallet → name=devwallet, hotkey=default   (the 5Fxr…/5Eex… pair from §1)
```

### 4b. Fund + register the miner hotkey on netuid 3

```bash
# Confirm the coldkey has dev TAO (needed for the registration burn + eval fee).
# If empty, request a transfer to 5Fxr…  from whoever holds the dev faucet/treasury.

# Register the miner hotkey on subnet 3 (burned registration):
btcli subnet register \
  --netuid 3 \
  --wallet.name devwallet --wallet.hotkey default \
  --subtensor.chain_endpoint ws://68.183.141.180:80
# NOTE: `btcli metagraph`/`overview` fail on this old spec — verify registration
# via Pylon or an SDK call (look for your hotkey 5Eex… getting a UID on netuid 3).
```

### 4c. Submit an agent as a miner

Use the harness you built + validated in Track A (a path or a repo the CLI can
package). The CLI signs + pays the eval fee on chain and streams the tarball +
payment proof to the platform.

```bash
ditto upload --harness <path-or-repo>     # pays eval fee, uploads tarball
ditto status                              # poll submission state
ditto verify                              # pre-flight / re-verify helpers
```

Flow to expect (from the platform state machine):
`uploaded → evaluating → scored → …`. The `uploaded → evaluating` promotion may
be **manual** on dev (the auto screener isn't wired yet) — check the platform
repo / ask the platform operator if it stalls at `uploaded`.

### 4d. Watch the validator score it + set weights

The validator worker (running on the `ditto-validator-dev` VM per
`infra/docs/validator-deploy.md`) pulls the queue each sweep
(`VALIDATOR_EPOCH_SECONDS`, default 3600 — can be lowered for testing), scores
via DittoBench, submits a **signed** score to the ledger, and sets weights.

Confirm on chain (SDK / Pylon read, since btcli metagraph is broken here):
- `Weights[<validator_uid>]` lists your miner's UID.
- `Incentive[<miner_uid>]` moves toward **1.0** after a tempo (winner-take-all).
- Validator dividends = 1.0.

**This is the walking-skeleton success state.** (Recall §2: alpha won't actually
accrue yet — `Incentive = 1.0` is the win condition for dev practice.)

### Fast plumbing test (no OpenRouter key, no Docker build)

To exercise queue → weights without real scoring, the validator can be run with
`validator_dittobench_mock: true` (in `host_vars/ditto-validator-dev.yml`) — it
returns a canned `ScoreReport` and still sets weights. Useful to prove the
chain/platform wiring independent of your harness quality.

---

## 5. End-to-end checklist (be ready to do EVERYTHING)

- [ ] All six repos cloned; `gh auth setup-git` + `CARGO_NET_GIT_FETCH_WITH_CLI=true` set (ditto-harness read access).
- [ ] `devwallet` regenerated from the **mnemonics**; addresses verified to match Ethan's `5Fxr…` (cold) / `5Eex…` (hot). Pasted `5Fy4…`/`5CZq…` discarded. Mnemonics stored out of git.
- [ ] Starter kit builds; `seed-user` + `playground` work; `evaluate`/`practice` produce scores.
- [ ] Hosted practice submission (`run_size=small`) reaches `done` with a composite (Track A green).
- [ ] ditto-subnet on `dev` with the `ditto` CLI present; `.env` points at `ws://68.183.141.180:80`, netuid 3, `platform-api-dev.heyditto.ai`, wallet `devwallet`.
- [ ] Coldkey `5Fxr…` funded with dev TAO; miner **hotkey** registered on netuid 3 (verified via Pylon/SDK, not btcli metagraph).
- [ ] `ditto upload` accepted by the platform; status advances past `uploaded`.
- [ ] Validator picks it up; `Incentive[miner_uid] → 1.0` on chain. ✅ done.

## 6. Known footguns (quick reference)

- **btcli metagraph/overview fail** on the dev chain (old spec 393) — use Pylon/SDK reads.
- **Emission = 0** on the dev pool — winner gets incentive 1.0 but no alpha; that's expected, not a bug in your setup.
- **Don't reuse the validator hotkey** (`5CZq6Mda…zK1mp`) for mining.
- **Never expose a loopback/private harness URL** to the hosted API — SSRF guard rejects it (that's why the on-VM sandbox sets `DITTOBENCH_ALLOW_PRIVATE_HARNESS=true`; don't do that for public submissions).
- **Manual promotion:** if a submission sticks at `uploaded`, the dev screener may need a manual `uploaded → evaluating` nudge (platform repo).

## Sources

- `ditto-subnet/docs/STATE-OF-THE-SUBNET.md` — the 2026-06-30 E2E proof + dev caveats + emission note.
- `infra/docs/validator-deploy.md` — the dev on-chain miner→validator→weights runbook.
- `dittobench-starter-kit/SETUP.md` — the local build + eval loop.
- `dittobench-api/README.md` — the hosted practice validator API + BYOK.
