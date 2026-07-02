# Dev Mining Practice — Agent Runbook (SN118, dev localnet)

**Goal:** get set up to *practice mining* Ditto Subnet 118 end-to-end in dev —
first off-chain (fast, free, no TAO), then on-chain against the dev localnet
(netuid 3) — and be ready to do everything a miner does: build a harness, score
it, register a hotkey, pay the eval fee, upload, and watch consensus.

This is a hand-off artifact. It is self-contained; an agent (or a human) should
be able to execute it top to bottom. **It contains no secrets** — the devwallet /
Alice mnemonics are provided out-of-band and injected at the `regen` step.

> **Sync note (2026-07-02):** updated to track `NEXTSTEPS.md` (successor to
> `STATE-OF-THE-SUBNET.md`) and the real miner CLI + validator that now live on
> the `dev` branches. Two things changed materially since the first draft — read
> the **wallet reality check** (§1) and the **topology** (§2): the hosted GCP API
> is a throwaway *stub* for the local loop, and the `devwallet` hotkey is now the
> deployed *validator*.

---

## 0. Repos required in the workspace (full GitHub URLs)

Clone all six under one parent dir. Roles:

| Repo | URL | Role in mining practice |
| --- | --- | --- |
| **dittobench-starter-kit** | https://github.com/ditto-assistant/dittobench-starter-kit | **The miner harness you build + optimize.** Rust agent + memory + tools + local eval loop. `cargo run -- submit` packages the tarball you upload. Your primary workspace. |
| **ditto-harness** | https://github.com/ditto-assistant/ditto-harness | Shared Rust agent+memory crate the kit depends on. Pulled automatically as a **private** git dep at build (needs a GitHub read token) — you don't clone it to run it, but the build fetches it. |
| **dittobench-api** | https://github.com/ditto-assistant/dittobench-api | **The scoring engine / off-chain practice validator (BYOK).** Rotates a fresh anti-cheat dataset per submission. Also the engine the on-chain validator calls (Mode B `tarball_url` ingest is now wired). Public practice URL below. |
| **ditto-subnet** | https://github.com/ditto-assistant/ditto-subnet | **Miner CLI** (`ditto` — `upload`/`status`/`verify`, in `ditto/miner_cli/`) + the validator worker (`python -m ditto.validator`). What talks to the chain + platform for real dev mining. **Use the `dev` branch.** |
| **ditto-platform** | https://github.com/ditto-assistant/ditto-platform | The team-operated API (miner intake, on-chain payment verification, object storage, screener + validator endpoints, score ledger). **You run it locally** for the real loop (§4a). **Use the `dev` branch.** |
| **infra** | https://github.com/ditto-assistant/infra | Ansible/Terraform for the co-located validator VM. Reference at `docs/validator-deploy.md`. Only needed if you touch the deployed validator. |

```
dittobench-starter-kit ──depends on──► ditto-harness      (build + `submit` the tarball)
        │  serve /run,/seed,/health
        ▼
dittobench-api  (hosted practice validator, BYOK)         (Track A: off-chain score)

ditto-subnet `ditto upload` ──HTTP──► ditto-platform (LOCAL) ──verify──► dev chain (netuid 3)
        │  pays eval fee + uploads          │ stores agent + score ledger
        ▼                                   ▼
   validator worker (ditto-subnet) ── scores via dittobench-api ── set_weights ──► Yuma  (Track B)
```

```bash
mkdir -p ~/ditto && cd ~/ditto
for r in dittobench-starter-kit ditto-harness dittobench-api ditto-subnet ditto-platform infra; do
  git clone "https://github.com/ditto-assistant/$r"
done
# miner CLI + validator live on `dev`:
git -C ditto-subnet   checkout dev
git -C ditto-platform checkout dev
# ditto-harness is private → cargo needs read access for the harness build:
gh auth login && gh auth setup-git
export CARGO_NET_GIT_FETCH_WITH_CLI=true
```

---

## 1. ⚠️ Wallet reality check — read before touching keys

The handed-off `devwallet` came with **listed** SS58 addresses *and* mnemonics.
**They don't match**, and — as of 2026-07-02 — the wallet the mnemonics produce
is the **validator**, not a miner. Three facts, in order of importance:

1. **The mnemonics are authoritative; the pasted `5Fy4…`/`5CZq…` are not.**
   Regenerating from the mnemonics yields:
   - **Coldkey:** `5FxrUYiFhG8PmoN9eGChy8YSGXGrbL3RMNV4BEsmE6bDmYCJ`
   - **Hotkey:** `5EexQS8UxChmkZ6vGeacAkwcf3TARR1Go5rd684Mf69dwgTY`

2. **That hotkey `5Eex…` is the deployed VALIDATOR** — `uid 4` on netuid 3,
   `validator_permit=True`, staked (`NEXTSTEPS.md` §5). So the `devwallet` is the
   **validator's** wallet. **Do not mine with it** — a hotkey is one uid, and
   that uid is already the validator.

3. **The pasted `5CZq6Mda…zK1mp` is the *retired* old validator hotkey.** It was
   rotated out for `5Eex…`. Ignore it entirely — don't mine with it either.

**To mine you need a SEPARATE miner wallet** (its own coldkey + hotkey), funded
and registered on netuid 3 (§4b). Only regen the `devwallet` if you're operating
the **validator** side:

```bash
# Only if you need the validator wallet (5Fxr/5Eex). Mnemonics injected here, never committed.
btcli wallet regen-coldkey --wallet.name devwallet                       # coldkey mnemonic
btcli wallet regen-hotkey  --wallet.name devwallet --wallet.hotkey default  # hotkey mnemonic
btcli wallet list   # verify → cold 5Fxr… / hot 5Eex…  (do NOT use this hotkey to mine)
```

> Keep every mnemonic in a gitignored `.env` / password manager. Never paste one
> into a file in these repos.

---

## 2. Topology & dev-chain caveats (know these or you'll chase ghosts)

- **Run the platform API LOCALLY for the real loop.** The API on the GCP dev VM
  (`platform-api-dev.heyditto.ai`) is a throwaway **stub** for the co-located
  validator setup — it is *not* the real intake logic. For real mining you run
  `ditto-platform` yourself (§4a), and the miner CLI's `--network local` targets
  it at `http://localhost:8000` (the network table is locked so the API URL and
  chain can't desync).
- **A co-located validator is also deployed** (`ditto-validator-dev`, private
  GCE, IAP-only) running `dittobench-api` + the worker with
  `VALIDATOR_DITTOBENCH_MOCK=false`, `RUN_SIZE=full` — i.e. *real* scoring. It
  polls the hosted API and is idle only because nothing has been promoted to
  `evaluating`. The **first real DittoBench E2E run is the current milestone**
  (`NEXTSTEPS.md` §2). Note: that deployed validator polls the *hosted* API, so
  it won't see an agent you upload to your *local* API — the two are separate
  loops. For solo practice, run the whole loop locally (§4).
- **Dev chain:** `ws://68.183.141.180:80` (DigitalOcean droplet), **netuid 3**,
  `node-subtensor` **specVersion 393**.
- **btcli metagraph / wallet overview FAIL** on this runtime (btcli 9.22 /
  bittensor 10.3.2 are newer than spec 393 → `SubtensorModule.HotkeyLock not
  found`). The **SDK still does extrinsics** (transfer / register / set_weights)
  and **Pylon works** — read neurons/UIDs via **Pylon or targeted SDK calls**,
  never `btcli metagraph`. Keep bittensor 10.3.2 (downgrading breaks the miner CLI).
- **Staking is disabled on this localnet** (`add_stake → SubtokenDisabled`). A
  *fresh* validator you stand up locally gets `vpermit=False, stake=0` and
  `set_weights` returns `(False, None)`. The **deployed** `5Eex…` validator was
  already staked via Alice/sudo, so it has the permit; a local one needs the same
  gating (use Alice/sudo to enable subtoken → stake → wait a tempo,
  `weights_rate_limit=100` blocks).
- **Emissions compute to 0** on the dev pool (`SubnetTaoInEmission[3]=0`).
  Consensus works — the winner reaches `Incentive=1.0` — but **alpha does not
  accrue** yet (Ethan's pool/`TaoWeight` tuning task). **Win condition for this
  exercise = `Incentive[miner_uid] → 1.0`, not a growing balance.**

---

## 3. Track A — Off-chain practice (do this first: fast, free, no chain)

Iterate on the harness with zero TAO and zero chain. This is where you spend most
of your time making the harness good.

### 3a. Build + talk to the harness (starter kit)

```bash
cd ~/ditto/dittobench-starter-kit
export CARGO_NET_GIT_FETCH_WITH_CLI=true

# Prereqs: Rust ≥ 1.85 (rustup), Ollama for embeddings.
ollama serve & ; ollama pull embeddinggemma        # 768-dim embeddings; Ollama ≥ 0.6

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

Loop: change harness code / retrieval weights / model → `evaluate` → repeat; use
`practice` to check you're not overfitting the fixed set.

### 3c. Score against the hosted practice validator (mirrors on-chain scoring)

The hosted DittoBench API rotates a **fresh anti-cheat dataset per submission**
and scores with an LLM judge. **BYOK**: your OpenRouter key rides along per
request and is never stored.

```bash
# expose your harness so the hosted API can reach it (must be a PUBLIC URL):
cargo run -- serve --port 9000        # POST /run, POST /seed, GET /health
ngrok http 9000                       # → https://<something>.ngrok.app

curl -X POST https://dittobench-api-22790208601.us-central1.run.app/v1/submit \
  -H 'Content-Type: application/json' \
  -d '{"harness_url":"https://<your-ngrok>.ngrok.app","run_size":"small","openrouter_key":"sk-or-..."}'
# → {"run_id":"...","poll":"/v1/runs/..."}   then poll GET /v1/runs/<run_id> to `done`
```

`run_size`: `small` (6 tool + 6 mem) for iteration → `medium` → `full` (60 + 50).
The URL must resolve to a **public** IP (SSRF guard rejects loopback / RFC1918 /
metadata). Because the hosted + on-chain validators pin the **same**
`ditto-harness` ref, the score transfers to the subnet.

---

## 4. Track B — On-chain dev mining (netuid 3, fully-local loop)

The realistic, self-contained path: run the platform locally, register a miner,
upload, and run a validator against your own API. Assumes `ditto-subnet` and
`ditto-platform` are on `dev`.

### 4a. Run the platform API on the dev chain (`ditto-platform`, `dev`)

```bash
cd ~/ditto/ditto-platform
cp .env.example .env
# set in .env:
#   NETUID=3
#   SUBTENSOR_NETWORK=ws://68.183.141.180:80
#   PYLON_BITTENSOR_NETWORK=ws://68.183.141.180:80
#   PYLON_BITTENSOR_ARCHIVE_NETWORK=ws://68.183.141.180:80
#   PYLON_RECENT_OBJECTS_NETUIDS=[3]
#   DITTO_UPLOAD_PAYMENT_ADDRESS=<an SS58 you control>   # e.g. Alice 5Grwva… for testing

colima start                 # or any Docker daemon (Pylon image is amd64-only)
make stack-up && make migrate   # postgres + pylon + minio, then alembic upgrade head
python -m ditto.api_server --dev
# verify: GET /health → {"status":"ok","db":"ok","chain":"ok", ...}
# validator endpoints are under /api/v1 (/validator/queue, /validator/agent/{id}/artifact|score)
```

### 4b. Fund + register the MINER wallet (SDK path — btcli metagraph is broken)

The **funder** is Alice (`//Alice`): mnemonic
`bottom drive obey lake curtain smoke basket hold race lonely fit walk//Alice` →
`5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY` (~927k τ). Regen a
password-less `alice` coldkey from that URI, then:

```python
# create your own miner wallet (coldkey + hotkey) distinct from the 5Eex validator, e.g. `miner`
# fund the miner coldkey (registration burn + eval fee) and register its hotkey on netuid 3:
subtensor.transfer(wallet=alice, dest="<miner_coldkey_ss58>", amount=...)   # from Alice
subtensor.burned_register(wallet=miner, netuid=3)                            # register miner hotkey
```

Then **restart Pylon** so its neuron cache refreshes — otherwise the API's
`is_registered` check lags and `/upload/check` returns error `1101`:

```bash
docker compose restart pylon
```

### 4c. Package the submission (whole crate, not one file)

```bash
cd ~/ditto/dittobench-starter-kit
cargo run -- submit          # writes dittobench-submission.tgz (tar of the whole crate)
```

You submit the **entire buildable project** with the `Dockerfile` at the tarball
root (`ditto-harness` is a pinned private git dep the build fetches via a
`gh_token` secret — you do **not** bundle it, and you do **not** submit a lone
`baseline.rs`). Cap is **≤ 2 MiB**; the platform re-verifies the SHA-256.

### 4d. Upload as a miner (`ditto-subnet`, `dev`)

```bash
cd ~/ditto/ditto-subnet
ditto verify --path ~/ditto/dittobench-starter-kit/dittobench-submission.tgz   # pre-flight only, no chain/API

ditto --network local --chain-endpoint ws://68.183.141.180:80 \
  upload --path ~/ditto/dittobench-starter-kit/dittobench-submission.tgz \
  --name my-first-agent --coldkey miner --hotkey default -y
#   --network local  → API at http://localhost:8000 (your §4a server)
#   --chain-endpoint → pays the eval fee on the dev chain
#   --coldkey/--hotkey are WALLET NAMES (aliases of --wallet.name/--wallet.hotkey), not SS58s
# Result: a real agent_id + `agents`/`evaluation_payments` rows + tarball in MinIO.

ditto status <agent_id>       # poll lifecycle
```

### 4e. Promote + score (validator worker, `ditto-subnet`, `dev`)

`uploaded → evaluating` is **still manual** (no screener worker exists yet, §5).
Flip the agent to `evaluating` (screener endpoints / a direct DB update), then run
the worker. Prove the wiring first with the **mock** bench (no OpenRouter/Docker):

```bash
VALIDATOR_PLATFORM_API_URL=http://localhost:8000 NETUID=3 VALIDATOR_DITTOBENCH_MOCK=1 \
  VALIDATOR_WALLET_NAME=<vali_ck> VALIDATOR_WALLET_HOTKEY=<vali_hk> \
  VALIDATOR_HOTKEY=<vali_ss58> \
  PYLON_URL=http://localhost:8001 SUBTENSOR_NETWORK=ws://68.183.141.180:80 \
  python -m ditto.validator
# one sweep: pull queue → mock-score → submit_score → set_weights
```

For **real** scoring, drop `VALIDATOR_DITTOBENCH_MOCK` and point the worker at a
running `dittobench-api` with a BYOK OpenRouter key — it builds your tarball in
Docker, seeds a fresh haystack, runs tool+memory cases, and LLM-judges (`run_size=full` on-chain).

### 4f. Confirm the result (via Pylon/SDK — not btcli metagraph)

- `Weights[<validator_uid>]` lists your miner's UID.
- `Incentive[<miner_uid>] → 1.0` after a tempo (winner-take-all). ✅ **This is the win.**
- Recall §2: alpha won't accrue (emission 0), and see the CRITICAL bug in §5 — the
  weight persists only for the epoch you're scored.

> **Local-validator gotcha:** a validator you stand up fresh has `vpermit=False,
> stake=0` (staking disabled on localnet), so `set_weights` returns `(False,
> None)`. Either use the already-staked deployed `5Eex…` validator, or use
> Alice/sudo to enable subtoken → stake your validator hotkey → wait a tempo.

---

## 5. Current state & bugs to expect (from `NEXTSTEPS.md`, 2026-07-02)

Know these so real behavior doesn't read as your setup being broken:

- 🔴 **CRITICAL — weights zero out every epoch.** The worker builds the weight
  vector from the *evaluating* queue only, and `put_weights` overwrites the whole
  on-chain vector. A scored agent flips to `SCORED` (leaves the queue), so it earns
  weight for **exactly the one epoch it's scored, then falls to zero** until
  resubmitted. There is no best-score-per-miner ledger read yet
  (`worker.py:53-77`). Fix is the #1 roadmap item. **Expect incentive not to
  sustain across epochs.**
- 🟠 **A transient `set_weights`/`submit_score` failure loses that epoch's miners**
  (composites live only in a local dict; no retry).
- 🟡 **Promotion `uploaded → evaluating` is manual** — the Rust lint/compile/build
  **screener worker doesn't exist yet**. You must promote by hand (§4e).
- 🟡 **Signature gaps** — screener signs only `{hotkey}:{agent_id}` (verdict
  unsigned); validator signs only `{hotkey}:{run_id}` (score body + agent_id
  unsigned). Integrity hardening is pending.
- 🟡 **No `tarball_sha256` passed to the engine** yet (tag-collision / no
  integrity check on the built blob).
- **k=3 sharding + median-of-3, deterministic weight curve, plagiarism/first-seen
  detection, emission tuning:** not built. `weights.py` is an explicit placeholder.
- **First real DittoBench E2E run (non-mock, tarball → docker → judge → weights) is
  the immediate validation milestone** — it hasn't been run green since deploy.

**Verified correct (don't "fix"):** SSRF parity + redirect re-checks, SHA-256 +
size caps on real streamed bytes, zip-slip safety, three-way source
mutual-exclusivity, migration/index parity, empty-queue handling (no chain zero on
idle), one-bad-agent isolation, SDK weight-path error handling, signing wire-format
match + hotkey guard.

---

## 6. End-to-end checklist (be ready to do EVERYTHING)

- [ ] Six repos cloned; `ditto-subnet` + `ditto-platform` on **`dev`**; `gh auth setup-git` + `CARGO_NET_GIT_FETCH_WITH_CLI=true` set.
- [ ] Understood: **`devwallet` (5Fxr/5Eex) is the validator** — created a **separate miner wallet** for mining. Retired `5CZq…` ignored. Mnemonics kept out of git.
- [ ] Track A: starter kit builds; `seed-user`/`playground` work; `evaluate`/`practice` score; a hosted `run_size=small` submission reaches `done`.
- [ ] Platform API running locally (`/health` → `ok/ok/ok`) against `ws://68.183.141.180:80`, netuid 3, with a payment address you control.
- [ ] Miner coldkey funded from Alice; miner hotkey `burned_register`ed on netuid 3; **Pylon restarted** after registering.
- [ ] `cargo run -- submit` produced `dittobench-submission.tgz` (≤2 MiB, Dockerfile at root); `ditto verify` passes.
- [ ] `ditto … upload` accepted → real `agent_id` + payment verified on chain.
- [ ] Agent manually promoted `uploaded → evaluating`; validator sweep scores it (mock first, then real dittobench).
- [ ] `Incentive[miner_uid] → 1.0` confirmed via Pylon/SDK. ✅ done (accepting §5's caveats).

## 7. Footguns quick reference

- **Don't mine with the `devwallet` hotkey (`5Eex…`)** — it's the validator (uid 4). Don't reuse the retired `5CZq…` either.
- **Upload targets your LOCAL API** (`--network local` → `localhost:8000`); the hosted `platform-api-dev.heyditto.ai` is a stub / a separate deployed loop.
- **btcli metagraph/overview are broken** on spec 393 — read chain state via Pylon/SDK; register/fund/set_weights via the SDK.
- **Restart Pylon after registering** or `/upload/check` lags with `1101`.
- **A fresh local validator can't set weights** (staking disabled → no vpermit); use the staked deployed validator or Alice/sudo-gate one.
- **Emission = 0** → incentive 1.0 but no alpha; that's expected, not your bug.
- **Weights zero next epoch** (§5 CRITICAL) — don't read the drop-off as a mistake.

## 8. Sources

- `ditto-subnet/docs/NEXTSTEPS.md` — 2026-07-02 state, merged PRs, full-stack bug review, roadmap.
- `ditto-subnet/docs/dev-e2e-handoff.md` — the canonical local miner→API→validator runbook.
- `ditto-subnet/docs/STATE-OF-THE-SUBNET.md` — the 6/30 walking-skeleton proof + dev caveats.
- `ditto-subnet` `dev` `README.md` + `ditto/miner_cli/` — the real `ditto` CLI surface.
- `dittobench-starter-kit/README.md` + `SETUP.md` — build, local eval, `submit` packaging.
- `dittobench-api/README.md` — the scoring engine + hosted practice API + BYOK.
