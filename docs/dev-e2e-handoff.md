# SN118 dev-chain end-to-end — how to run the pipeline

How to bring up the **miner → platform API → validator worker → weights → emissions** loop
against the dev local chain, and what still needs finishing. Assumes `ditto-subnet` and
`ditto-platform` are both on their `dev` branches.

> Companion to ditto-platform's `DEV-VM-HANDOFF.md`, which documents the throwaway API *stub* on the
> GCP dev VM. That stub is **not** the real API — for real logic, run the API locally as below.

## Topology

```
 miner CLI (ditto upload)             validator worker (python -m ditto.validator)
        │  HTTP                                 │  HTTP            │ set_weights
        ▼                                       ▼                  ▼
   ┌──────────────── platform API (local, real) ──────────────┐   dev chain
   │  FastAPI :8000  ─ Pylon :8001 ─┐   Postgres   MinIO       │   ws://68.183.141.180:80
   └────────────────────────────────┼──────────────────────────┘   netuid 3 ("ditto")
                                     └────────── ws://68.183.141.180:80 (Pylon → chain)
```

- **Dev chain:** `ws://68.183.141.180:80` (DigitalOcean droplet). Ditto subnet = **netuid 3**.
  Runtime: `node-subtensor` **specVersion 393**. SSH is root via password — the password lives in the
  *"Bittensor wallet setup and sharing"* session / ask Dan (kept out of git). Polkadot-JS explorer
  (from the VM): `https://polkadot.js.org/apps/?rpc=ws://127.0.0.1:9944`.

## Status
- **FULL loop works end-to-end on the dev chain (2026-07-07).** `ditto upload` → eval fee paid →
  on-chain payment verified → agent stored → manual promotion → validator **signed score** in the
  ledger → **KOTH `put_weights` via the Pylon identity write path** → `Weights[3][4]=[(6,65535)]`
  on chain. Staking is **already enabled** here (validator UID 4 has `validator_permit=true`,
  stake ~22001τ), so the old vpermit blocker is resolved. Proven with the **mock scorer**
  (`VALIDATOR_DITTOBENCH_MOCK=1`); real DittoBench scoring needs the OpenRouter key.
- The only manual step left in the loop is **screener promotion** (`uploaded → evaluating`).

## Tooling (Apple Silicon)
- **colima** + the **docker compose v2 plugin** (`~/.docker/cli-plugins/docker-compose`) provide the
  Docker daemon (`colima start`, then `docker info`).
- **sshpass** (scripted SSH/scp to the droplet), **btcli 9.22.1**, **bittensor 10.3.2**, **uv**.

## Version note (important)
btcli 9.22 / bittensor 10.3.2 are **newer than specVersion 393**, so `btcli subnet metagraph` and
`btcli wallet overview` fail with `SubtensorModule.HotkeyLock not found`. The **SDK still connects and
does extrinsics** (transfer / register / set_weights), and **Pylon works**. Keep bittensor 10.3.2
(downgrading breaks the miner CLI, built for ≥10.2.1). Read neurons/UIDs via **Pylon** or targeted
SDK calls — not `btcli metagraph`.

## 1. Platform API on the dev chain  (`ditto-platform`, `dev`)
`.env` (gitignored) key values:
- `NETUID=3`, `SUBTENSOR_NETWORK=ws://68.183.141.180:80`
- `PYLON_BITTENSOR_NETWORK=ws://68.183.141.180:80`, `PYLON_BITTENSOR_ARCHIVE_NETWORK=ws://68.183.141.180:80`, `PYLON_RECENT_OBJECTS_NETUIDS=[3]`
- `DITTO_UPLOAD_PAYMENT_ADDRESS=<an SS58 you control>` (e.g. Alice `5Grwva…` for testing)

The **Pylon→chain knob** is `PYLON_BITTENSOR_NETWORK` (pylon_commons `Settings`, env prefix `PYLON_`,
default `finney`); `docker-compose.yml` sets it + `platform: linux/amd64` (the Pylon image is
amd64-only and runs under colima qemu).

```sh
colima start
make stack-up && make migrate
python -m ditto.api_server --dev
```
Verify `GET /health → {"status":"ok","db":"ok","chain":"ok",…}` and the Pylon log shows it serving
`SubnetNeurons netuid=3`. The validator endpoints (`/validator/queue`, `/validator/agent/{id}/artifact`,
`/validator/agent/{id}/score`) are mounted under `/api/v1`.

## 2. Wallets
- **`alice-sudo` == `//Alice`.** The dev mnemonic
  `bottom drive obey lake curtain smoke basket hold race lonely fit walk//Alice` derives
  `5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY` — the **funder** (~927k τ). The on-disk
  `alice-sudo` coldkey is `$NACL`-encrypted; regenerate a **password-less `alice`** from the public
  mnemonic:
  `bt.Wallet(name="alice").set_coldkey(Keypair.create_from_uri(MNEMONIC_URI), encrypt=False, overwrite=True)`.
- A **miner** needs a coldkey funded (eval fee + registration burn) and a hotkey registered on
  netuid 3. A **validator** needs a hotkey registered on netuid 3 (the hotkey is plaintext → fine for
  `set_weights`).
- Register via the **SDK** (btcli metagraph is broken on this runtime): `subtensor.transfer` (fund
  from `alice`), `subtensor.burned_register` (register). After registering, **restart Pylon**
  (`docker compose restart pylon`) so its neuron cache refreshes — otherwise the API's
  `is_registered` check lags and `/upload/check` returns `1101`.

## 3. Miner upload  (`ditto-subnet`, `dev`)
```sh
ditto --network local --chain-endpoint ws://68.183.141.180:80 \
  upload --path <agent.tar.gz> --name <name> --coldkey <miner_ck> --hotkey <miner_hk> -y
```
`--network local` → API at `http://localhost:8000`; `--chain-endpoint` → pays on the dev chain.
Result: a real `agent_id`, `agents` + `evaluation_payments` rows, tarball in MinIO.

## 4. Validator worker  (`ditto-subnet`, `dev`)
Mock the bench (no dittobench-api / OpenRouter key needed): `VALIDATOR_DITTOBENCH_MOCK=1`.
```sh
VALIDATOR_PLATFORM_API_URL=http://localhost:8000 NETUID=3 VALIDATOR_DITTOBENCH_MOCK=1 \
  VALIDATOR_WALLET_NAME=<vali_ck> VALIDATOR_WALLET_HOTKEY=<vali_hk> VALIDATOR_HOTKEY=<vali_ss58> \
  PYLON_URL=http://localhost:8001 SUBTENSOR_NETWORK=ws://68.183.141.180:80 \
  python -m ditto.validator
```
One sweep: pull the queue → mock-score → submit_score → set weights.

## Weight path (validator → weights → emissions) — RESOLVED
1. ~~**Staking is disabled**~~ — **already enabled** on this localnet. Validator UID 4
   (`5EexQS8…`) has `validator_permit=true`, stake ~22001τ, so `put_weights` applies. Pylon
   applies after the `weights_rate_limit` (~a few blocks; logged "still got N blocks left to go" →
   "apply_weights finished successfully").
2. **Screener is still manual.** `/validator/queue` returns agents in `evaluating`; promote by hand
   (`UPDATE agents SET status='evaluating' WHERE agent_id='…'`) until a screener worker exists.
3. **Weight path = Pylon identity (write), validated.** Set `PYLON_IDENTITY_NAME=validator` +
   `PYLON_IDENTITY_TOKEN=<token>` (+ keep `PYLON_OPEN_ACCESS_TOKEN`) and leave
   `VALIDATOR_USE_SDK_WEIGHTS` **unset**. Stand the write identity up on the platform's Pylon
   container (`PYLON_IDENTITIES` + `PYLON_ID_VALIDATOR_*` + a read-only wallet mount) — see
   [`CREDENTIALED-HANDOFF.md`](CREDENTIALED-HANDOFF.md) Hop 5/E1 for the full recipe. The SDK
   fallback (`VALIDATOR_USE_SDK_WEIGHTS=1`) remains available but is no longer needed here.

## What's left
- **Real DittoBench scoring** in place of the mock: set `VALIDATOR_OPENROUTER_KEY` +
  `VALIDATOR_DITTOBENCH_API_URL` and flip `VALIDATOR_DITTOBENCH_MOCK` off (Hop 1/2).
- **A screener worker** to automate `uploaded → evaluating`.
