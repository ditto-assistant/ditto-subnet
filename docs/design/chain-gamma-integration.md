# Chain gamma on SN118: the Gamma Integration Standard V1.2

Tracking: [SN-501](https://omniaura-team.atlassian.net/browse/SN-501).
Standard: [Gamma Integration Standard V1.2](https://gist.github.com/mcjkula/a10336ddc42efad8e344175049b35074).

Status: **design only (2026-11-22).** Nothing in this document has been
registered, signed, enabled, provisioned or spent. `gamma_enabled` is off on
SN118, SN118 has no published service, and no `set_service` was sent. The
companion draft PR adds an inert library behind a default-off flag. It mounts no
route, adds no table and reads no chain.

## 0. Names

Three things share the word "gamma". This document is only about the first.

| Concept | What it is | Where it lives | Identifier rule |
| --- | --- | --- | --- |
| **Chain gamma** (this doc) | Bittensor runtime credit: `GammaDeposits`, `mint_gamma`, `spend_gamma`, `GammaSpent`, `set_service` | the Finney chain; any future SN118 code goes in `chain_gamma` modules | always `chain_gamma`, never a bare `gamma` |
| **Treasury Gamma bucket** | SN118's internal emission allocation, paid by validator weights to a collector hotkey and then swept to holding wallets for GM inference credit, audits, ads and bounty payouts | `KIND_GAMMA`, `gm_credits`, `ditto/treasury/`, Backroom `/gamma`; SN-236, SN-367, SN-251, SN-252, SN-490 | unchanged |
| **DITTO-2803** | Pay with TAO for Ditto product usage credits, through chain gamma | heyditto-stack `docs/engineering/chain-gamma-credits.md`, backend | that thread's own |

This design does not change the treasury bucket, its settings, its custody or
its payouts. Section 6 describes how the two would sit side by side.

## 1. How Gamma works (runtime 493)

### 1.1 Verified facts

Sources: the standard, the subtensor `v493` tag (`pallets/subtensor/src/subnets/gamma.rs`,
`emission_weights.rs`, `lib.rs`, `sdk/python/bittensor/http_auth.py`,
`docs/guides/{gamma,services,signed-requests}.mdx`,
`docs/concepts/emission-weights.mdx`), and read-only storage reads at finalized
Finney block **9,564,081** (2026-11-22). No transaction was signed or submitted.

| Fact | Value |
| --- | --- |
| Finney runtime | `specVersion` **493** (release v493, 2026-11-20) |
| `GammaEnabled` (network-wide) | `true` |
| `SubnetGammaEnabled[118]` | unset, so **off** |
| SN118 `Services` | none published |
| SN118 `GammaSpent` keys | 0 |
| `RegisteredSubnetCounter[118]` | 0 (never written; 0 is the value to store) |
| `SubnetGammaEmissionWhitelisted[118]` | unset (other subnets cannot send emission to SN118 gamma) |
| Subnets with gamma enabled | 25, 28, 37, 51, 53, 58, 75, 76, 80, 81, 94, 103, 117 |
| Subnets with a published service | 25, 28, 51, 53, 58, 75, 103, 117 |
| **Whitelisted gamma-emission targets** | **51 (Lium) and 53 (engy)** only |

Published service documents, fetched 2026-11-22 and treated as untrusted data:

| Netuid | Service | Sells | Notes |
| --- | --- | --- | --- |
| 53 | engy | OpenAI-compatible LLM API | credits $1 per gamma **plus 5%**; btauth/1; balance endpoint; pricing page |
| 51 | Lium | GPU pods by the second | btauth/1; balance endpoint; OpenAPI spec |
| 75 | Hippius | storage, S3 and compute | gamma payment listed without a btauth/1 signature; not an emission target |
| 28 | GM | inference | the on-chain URL is a `.md` path that returns 404. GM is not an emission target. |

Chutes (SN64) has gamma disabled and no service.

### 1.2 Roles and the payment flow

| Role | Does |
| --- | --- |
| User coldkey | mints gamma into a deposit at a hotkey it picks, by burning subnet alpha (bought with TAO or taken from stake); sets that deposit's daily cap; moves or closes it |
| User hotkey | the "API key": spends its deposit at a service with fee-free `spend_gamma`, and signs requests to the service with btauth/1 |
| Service | publishes once with `set_service`; after that it **only reads** finalized chain state. It credits, reports and charges accounts in its own database. It sends no transaction and holds no authority over deposits. |
| Service hotkey | a hotkey with a validator permit on the subnet, or the subnet owner hotkey. Users spend at it and name it as the receiver of signed requests. |
| Subnet owner | turns on minting with `gamma_enabled`; may also pay shares of emission into other subnets' gamma |

1. The user's coldkey burns SN118 alpha to mint SN118 gamma (1 gamma = 1 USD,
   10^9 units per gamma) into a deposit at a user hotkey.
2. The user hotkey signs `SubtensorModule.spend_gamma(service, netuid, amount)`
   (call index 168, `Pays::No`). The chain burns `amount`, adds it to
   `GammaSpent(netuid, service, hotkey)` and emits
   `GammaSpent { netuid, hotkey, service, amount }`. The chain refuses a spend
   at a hotkey with no published service, a spend below 0.1 gamma unless it
   empties the deposit, and a spend above the daily cap. A hotkey has at most
   one spend pending per subnet.
3. The service reads the total at a finalized block and credits the growth,
   in USD, to the hotkey's account.
4. The hotkey sends btauth/1-signed requests, with the service hotkey as the
   receiver. The service verifies each one and charges its USD price.
5. When the balance cannot cover a request, the service replies
   `402 {"error": "insufficient_balance"}` and does no work.

**Gamma is per subnet.** SN118 gamma is minted only by burning SN118 alpha, and
it can be spent only at services published on SN118. engy accepts SN53 gamma,
not SN118 gamma.

**The chain pays the service nothing.** The burned alpha's value goes to the
subnet's remaining alpha holders. A service that accepts gamma takes in no TAO
and no alpha. It is paid in "proof of burn", and it still pays its own
upstream costs.

### 1.3 Gamma shares: emission that pays for services

The subnet owner sets the whole emission split with
`AdminUtils.sudo_set_subnet_emission_weights_v2(netuid, split)`. That split may
include up to 16 `Gamma { netuid }` shares, each at most 10% of `alpha_out`,
and each naming a gamma-enabled subnet that root has whitelisted. The five fixed
slots (burn, recycle, owner, validator, miner, with miner at least 33%) then sum
to one minus the shares.

At each SN118 epoch the chain sells the share's SN118 alpha for TAO, buys the
target's alpha with it and burns that alpha. It then credits the gamma to the
**SN118 owner coldkey's deposit at the SN118 owner hotkey on the target
subnet**. That deposit opens with a daily spend cap of 0, so the owner coldkey
must set a cap, or move the gamma to another hotkey, before anything can spend
it. Conversion waits while a price is out of bounds, the TAO/USD feed is stale,
the result would be under 0.1 gamma, or the owner coldkey does not own the owner
hotkey.

### 1.4 What runtime 493 changed (and why it matters for DITTO-2803)

Subtensor #3343 (merged 2026-11-19, live in runtime 493):

- removed the deposit **spender**, `set_gamma_deposit_spender`, and the default
  spender;
- retired the batched spender call (index 164) and calls 158, 159, 162 and 165;
- made `ProxyType::GammaSpend` deny all calls;
- made the deposit's own hotkey the only thing that can spend it.

The DITTO-2803 design (merged in heyditto-stack on 2026-11-14) was written for
runtime 490. It relies on a Ditto spender multisig, a KMS-held `GammaSpend`
proxy, batched Ditto-signed spends, `mint_gamma(..., spender)` and keyless
Ditto-issued deposit keys. None of that works on runtime 493. Worse, gamma
minted into a keyless deposit key is now **unspendable** until the user's
coldkey moves it to a hotkey somebody controls. This thread does not touch
DITTO-2803. Question 18 asks Peyton to decide whether to re-scope it.

## 2. Two separate directions for SN118

| | A. Ditto **accepts** SN118 gamma | B. SN118 **pays** with other subnets' gamma |
| --- | --- | --- |
| Who burns what | users burn SN118 alpha ($DITTO) | the chain sells SN118 emission alpha, then buys and burns the target's alpha |
| Market effect on $DITTO | buy-and-burn | sell pressure (like any treasury spend) |
| Chain actions (Peyton signs) | `set_service` once; `gamma_enabled` on SN118 | `sudo_set_subnet_emission_weights_v2` with a gamma share; a cap on or `move_gamma` from the owner-hotkey deposit |
| Our code | crediting worker, ledger, balance endpoint, btauth/1 verification, 402 | relay upstream that signs btauth/1 requests to the provider |
| Keys on servers | **none** (verification needs only public addresses) | one API-key hotkey on the relay hosts, bounded by its deposit and daily cap |

Each direction can ship without the other.

## 3. Direction A: Ditto as a service that accepts SN118 gamma

### 3.1 Who is the service, and which endpoints are paid

| Option | Paid endpoints | Owner | Fit |
| --- | --- | --- | --- |
| A1. Ditto product (Ditto Router) | OpenAI-compatible inference at `inference.heyditto.ai` and other usage-credit APIs | backend and heyditto-stack (the DITTO-2803 area) | the natural "pay for Ditto with $DITTO" product, comparable to engy |
| A2. SN118 Platform | miner-facing APIs, for example: (a) practice inference on the exact on-chain scoring provider and model, through the existing relay; (b) upload, readmission and bounty fees, which today are TAO `transfer_keep_alive` proofs; (c) hosted practice evaluations, if offered | this repo, `apps/platform` | miners already have hotkeys, and Platform already verifies sr25519, consumes nonces in Postgres, and reads finalized Finney |
| A3. Both | both | both | requires two service hotkeys and two documents, so that **one ledger owns each service hotkey's totals** (section 3.4) |

The model relay already has a "Ditto Router upstream": linked miners are billed
to their Ditto account, and a Router refusal comes back as a 402
`ditto_router_payment_required`
([services/model-relay/README.md](../../services/model-relay/README.md#ditto-router-upstream-dogfood-off-by-default)).
That makes A1 and A2(a) close relatives. A hotkey-funded balance could back the
same relay lane.

**Recommendation:** pick one owner for the first service hotkey. If the goal is
"buy Ditto usage with $DITTO", that owner is A1, in the backend. A2 is the right
place only if we want miner-facing paid endpoints. The draft PR's library is
deliberately storage- and framework-agnostic, so its semantics port either way.

### 3.2 On chain versus off chain

| On chain (Peyton signs; never code or an agent) | Off chain (our code) |
| --- | --- |
| `set_service(118, service_hotkey, url)`, signed by the coldkey that owns the service hotkey (about $0.03 fee; URL at most 256 bytes) | services document: JSON, `Access-Control-Allow-Origin: *` |
| `gamma_enabled = true` on SN118 (owner coldkey; refused inside the weights window) | finalized-chain reader and crediting worker |
| republish under a new hotkey before any hotkey swap, and keep old hotkeys in `publishers` | hotkey-keyed USD ledger, balance endpoint, btauth/1 verification, prices, charging, 402 |
| users: mint (coldkey, normal fee) and spend (hotkey, no fee) | Backroom read tools for every knob and counter |

The service never needs a private key at runtime. Only `set_service` needs a
signature, once, from the coldkey that owns the service hotkey.

### 3.3 Books

Integer units (10^9 per USD) everywhere. They become decimal strings only in
responses.

| Table (proposed) | Key | Value |
| --- | --- | --- |
| `chain_gamma_totals` | `(netuid, registered_counter, service_hotkey, user_hotkey)` | `last_total_units`, `observed_block`, `observed_block_hash` |
| `chain_gamma_accounts` | `user_hotkey` | `credited_units`, `used_units` (`CHECK used_units <= credited_units`) |
| `chain_gamma_charges` | `id`; unique `(user_hotkey, nonce_ns)` | endpoint, reserved and settled units, state, timestamps (audit trail, and refunds of over-reservation) |
| `chain_gamma_nonces` | `(user_hotkey, nonce_ns)` | `expires_at`; pruned by a janitor. Same pattern as `validator_request_nonces`. |
| `chain_gamma_scan_cursor` | singleton | last finalized block scanned, scan lag |
| `chain_gamma_settings_revisions` | append-only | publishers, per-endpoint USD prices, scan cadence, accepting on or off. Same pattern as the other `*_settings_revisions`. |

### 3.4 Crediting exactly once

At finalized block `B`, for each publisher netuid:

1. Read `C = RegisteredSubnetCounter(netuid)` at `B`.
2. For each publisher hotkey `s`, read `query_map(GammaSpent, [netuid, s])` at `B`.
3. For each `(u, T)`, in **one transaction**, let `t` be the stored total for
   `(netuid, C, s, u)`, or 0. If `T > t`, add `T - t` to `u`'s credited units
   and store `T`. Use `INSERT … ON CONFLICT DO UPDATE … WHERE excluded.total > stored.total RETURNING`
   (or `SELECT … FOR UPDATE`), so that the comparison and the write are atomic.

Why this cannot double-credit:

- totals only grow, and the stored total moves forward only by compare-and-set;
- a repeated, older or concurrent read credits nothing;
- only finalized blocks are read, so reorgs do not apply;
- a restart resumes from the stored totals;
- the counter is part of the key, so a re-registered netuid starts from zero
  rather than being stuck behind larger old totals.

Two failure modes are ours to prevent:

1. **Two ledgers crediting the same service hotkey.** If the product backend
   and Platform both read `GammaSpent(118, S, ·)`, each user is credited twice.
   Rule: one ledger per service hotkey, with separate hotkeys and documents if
   both surfaces accept gamma.
2. **The dissolution block.** Dissolution can clear totals in the same block
   that emits `NetworkRemoved`. The final totals are the previous block's totals
   plus that block's `GammaSpent` events at our hotkeys.

Cadence:

- a full scan every ≤10 finalized blocks (about 2 minutes), which meets the
  standard's "credit within ~10 finalized blocks";
- an on-demand single-key read for the requesting hotkey on each balance
  request and before any 402;
- the first scan credits everything spent before it.

Platform's existing chain client already pins a finalized head per connection,
refuses non-finalized heights and falls back across archive providers
([apps/platform/ditto/chain/client.py](../../apps/platform/ditto/chain/client.py)).
New storage items need runtime-fingerprint pins
([apps/platform/ditto/chain/audited_runtime_interfaces.py](../../apps/platform/ditto/chain/audited_runtime_interfaces.py)).

### 3.5 Verifying and charging a paid request (btauth/1)

| Step | Rule | Failure |
| --- | --- | --- |
| 1 | take the raw body bytes, and the request target exactly as received (`scope["raw_path"]` plus `?query_string`) | |
| 2 | headers well formed; `X-Bittensor-Version: 1`; the nonce is decimal; the scheme comes from `X-Bittensor-Crypto` (sr25519 if absent) | 401 |
| 3 | `X-Bittensor-Receiver` is one of **our publisher hotkeys** | 401 |
| 4 | nonce no more than 10 s old and no more than 2 s ahead | 401 |
| 5 | for an sr25519 or ed25519 sender, refuse any sender with an entry in `HashedAccounts.Accounts`, `HashedAccounts.MlDsaAccounts` or `HashedAccounts.DerivedAuthorities` at the finalized head. Entries are never removed, so a positive answer can be cached forever; a negative answer is cached briefly. This chain read runs after the cheap checks, so stale or misaddressed traffic never reaches it. | 401 |
| 6 | the signature verifies over the 8-line payload (sr25519 with the `substrate` context, or ed25519), under the declared scheme only, with no fallback | 401 |
| 7 | `(hotkey, nonce)` is new, recorded **only after** the signature verifies | 401 |
| 8 | charge with one conditional update: `used += price WHERE credited - used >= price` | |
| 9 | if nothing was charged, credit that hotkey at the finalized head and retry once | |
| 10 | still nothing charged | `402 {"error":"insufficient_balance"}`, with no work done |
| 11 | do the work. For inference, reserve an upper bound (`max_tokens` × price) and settle to the actual cost, which lowers `used` by the unused part. This matches the reserve/settle pattern of inference grants and the Router. | |

The hashed schemes (`hashed`, `hashed-ed25519`) need the RaoFoundation
`bittensor` 11 SDK (`bittensor-core`). This repo pins `bittensor` 10.x
([uv.lock](../../uv.lock), [apps/platform/uv.lock](../../apps/platform/uv.lock)).
V1 can accept sr25519 and ed25519 only and refuse hashed senders with 401
(question 19).

The balance endpoint is unauthenticated, and its on-demand chain read is a cheap
amplification target. Rate-limit it per IP and per hotkey, and reuse one
finalized read per block.

### 3.6 Services document

```json
{
  "version": 1,
  "name": "<service name>",
  "description": "<what it sells>",
  "publishers": [{ "netuid": 118, "hotkey": "<SERVICE_HOTKEY_SS58>" }],
  "services": [{ "type": "http", "endpoint": "<API base URL>" }],
  "payments": [
    { "type": "gamma", "signature": "btauth/1", "balance": "<API base URL>/chain-gamma/balance" }
  ],
  "pricing": "<pricing URL>"
}
```

The URL must be stable and at most 256 bytes. Choosing the host is an infra and
DNS decision (question 4). `publishers` keeps every hotkey the service has ever
published under.

### 3.7 Feature flags and rollout order

| Gate | Kind | Default |
| --- | --- | --- |
| `DITTO_CHAIN_GAMMA_ENABLED` | env master switch. While off: no routes, no worker, no chain reads. | `false` |
| `chain_gamma_settings_revisions.accepting` | runtime kill switch. While off, paid endpoints refuse with 503; balance still answers. | `false` |
| SN118 `Services` entry | chain (Peyton) | absent |
| `SubnetGammaEnabled[118]` | chain (Peyton) | off |

Order:

1. merge the code dark;
2. rehearse the five acceptance steps on testnet;
3. flip the env flag in production with `accepting=false` (Peyton);
4. `set_service` (Peyton);
5. `gamma_enabled` (Peyton);
6. a mainnet self-test of about $1 (Peyton's test hotkey);
7. `accepting=true`;
8. Peyton replies with the checklist.

No user can spend before step 4, and nobody can mint SN118 gamma before step 5.

### 3.8 Application versus infrastructure

| Application code (this repo; no infra) | Infra, ops or chain (Peyton) |
| --- | --- |
| `chain_gamma` library: amounts, services document, btauth/1, ledger rules, charging pipeline (draft PR) | where the crediting worker runs: in the Platform API process, a pm2 process on the app VM, or AWS |
| Alembic migration and Postgres ledger, nonce store and janitor | RPC: public entrypoint plus archive fallback, or our own node |
| chain reader (`GammaSpent`, `RegisteredSubnetCounter`, `HashedAccounts.*`), runtime pins, crediting loop | domain, TLS and CORS for the services document; DNS and Cloudflare |
| balance and services-document routes; FastAPI btauth/1 dependency; 402 envelope | production env flag and settings changes |
| Backroom MCP read tools: scan lag, totals, accounts, charges, 402 counts, settings | `set_service`, `gamma_enabled`, any test mint or spend |

## 4. Direction B: SN118 pays for inference with gamma

1. **Precondition:** the target is gamma-enabled and whitelisted. Today that
   means SN53 (engy, LLMs) or SN51 (Lium, GPUs).
2. Peyton sets a gamma share (≤10%) in the SN118 split with
   `sudo_set_subnet_emission_weights_v2`. Peyton chooses which slots shrink.
   Miner must stay ≥33%. The call has the same rate limit and freeze window as
   the existing emission-weights setter.
3. Each epoch, the gamma lands in the SN118 owner deposit on the target subnet,
   opened with a cap of 0.
4. The owner coldkey moves the gamma (`move_gamma`) to a dedicated API-key
   hotkey that has no stake and no registration, and sets a daily cap. That key
   spends at the provider's service hotkey (no fee) and signs btauth/1 requests
   from the model relay.
5. The relay gains a provider upstream (engy is OpenAI-compatible). It needs a
   btauth/1 signer on the relay hosts: a **hot key**. Its blast radius is the
   deposit balance and the daily cap. To rotate it, move the balance to a new
   hotkey.

Consequences:

- This is chain-native "emission pays for inference": no collector hotkey, no
  `transfer_stake` sweeps, and no manual x402 top-ups. It covers only
  whitelisted providers, though. **GM cannot be paid this way today**: SN28 is
  not whitelisted, and its service URL 404s. Chutes has no gamma.
- The share is set at the chain level, so every validator honours it
  immediately. It needs no validator release and no weight change. It also
  converts every epoch, whether or not we use the credit.
- Pointing scoring inference at a new provider changes what a benchmark
  measures. It belongs with a `bench_version` contract decision. Screener-side
  or non-scored inference is the low-risk first use.

## 5. How this sits next to existing billing

| Existing | Relation |
| --- | --- |
| Treasury Gamma bucket (validator weights → collector → holding coldkeys → GM via x402; [docs/sn118-service-treasury-v2.md](../sn118-service-treasury-v2.md)) | **Untouched.** Direction B is an alternative way to fund the same purpose, inference. If both run, size `gm_credits` net of the gamma share (SN-252) so inference is not paid for twice. |
| Upload, readmission and bounty fees (`evaluation_payments`, `readmission_payments`, `bounty_fee_payments`, unique on the extrinsic) | A2(b) would add a second funding source. The TAO proof path stays as it is. |
| Inference grants and costs (`inference_grants`, `inference_requests.cost_microusd`) | the reserve/settle precedent for charging inference against a hotkey balance |
| Model relay Ditto Router upstream (`DITTO_ROUTER_*`, `X-Ditto-On-Behalf-Of`, 402 `ditto_router_payment_required`) | the precedent for a lane billed to a per-identity balance; direction B adds a provider upstream next to it |
| Ditto product credits (backend `purchases`, `users.balance`, Router `resourcebilling.Reserve/Settle`) | if A1: a hotkey account maps to a Ditto account, or stays standalone. The crediting key becomes the totals row, not an event ID. |

## 6. Draft PR (inert scaffolding)

Pure Python with no I/O, under `apps/platform`:

- `ditto/api_models/chain_gamma.py`: the wire shapes (Pydantic lives only in
  `api_models`). That is the services document of requirement 1 (JSON,
  `publishers`, the gamma payment entry, https URLs, the 256-byte
  `set_service` URL limit), the balance response, and the exact 402 body.
- `ditto/chain_gamma/amounts.py`: units to and from USD decimal strings, and
  the balance view.
- `ditto/chain_gamma/btauth.py`: the canonical payload, header parsing,
  receiver binding, freshness, the protected-sender hook, sr25519 and ed25519
  verification, and a replay-store protocol. Tested against the guide's
  vectors: the payload byte for byte, and the fixed ed25519 signature.
- `ditto/chain_gamma/ledger.py`: the books and rules of section 3.4
  (compare-and-credit keyed by counter, conditional charge, settle and refund).
  It is a protocol with an in-memory reference implementation that the Postgres
  version must match.
- `ditto/chain_gamma/crediting.py`: the periodic scan and the on-demand
  single-hotkey credit, against a `ChainGammaReader` protocol.
- `ditto/chain_gamma/charging.py`: the section 3.5 pipeline. It returns 401,
  402, 503 (paused) or a charge, and the route returns the 402 body directly
  rather than through the Platform error envelope.
- `ditto/chain_gamma/config.py`: `DITTO_CHAIN_GAMMA_ENABLED`, default off. It
  requires publisher hotkeys (public ss58 only) when on.
- `ditto/tests/chain_gamma/`: 68 tests. They include the standard's acceptance
  steps 3 to 5 against an in-memory chain, concurrent crediting and charging,
  re-registration, and a guard that nothing in `api_server`, `db` or `chain`
  imports the package yet.

Nothing imports it from the API server or the workers. It adds no migration,
route, chain I/O or key. The tests sign sr25519 requests with the public
`//Alice` development key, the same key as the guide's own vectors.

## 7. Next PRs, after Peyton's answers

1. Migration and Postgres ledger, nonce store and janitor (one Alembic head;
   safe helpers).
2. Chain reader, runtime pins, crediting loop (flag-gated) and Backroom read
   tools.
3. Balance and services-document routes, plus the FastAPI btauth/1 dependency.
4. First paid endpoint and price settings.
5. Testnet acceptance rehearsal.
6. Mainnet steps 3 to 8 of section 3.7 (Peyton).

Direction B is its own stack: the relay provider upstream and signer, then
Peyton's emission split change.

## 8. Acceptance test mapping

| Standard step | Satisfied by |
| --- | --- |
| 1. `btcli query services --netuid 118`, then fetch the document | `set_service`, plus the services-document route with CORS |
| 2. mint to a fresh hotkey and spend about $1 | the user side, needs `gamma_enabled` |
| 3. balance shows the credit within ~10 finalized blocks | ≤10-block scan, plus an on-demand read on the balance request |
| 4. a signed request succeeds and the balance drops | btauth/1 dependency and conditional charge |
| 5. repeat until zero; the next request gets 402 | credit-and-retry, then 402 with no work done |

## 9. Open questions

The numbered list for Peyton is in SN-501 and in the thread's final message. It
is repeated here so the document stands alone.

1. **Infra – worker:** where should the crediting worker run? Options: a
   background task in the Platform API process, a separate pm2 process on the
   Platform app VM, or AWS (DITTO-2803 recorded "new Bittensor processing goes
   on AWS").
2. **Infra – RPC:** is the public entrypoint plus Platform's existing archive
   fallback enough for finalized reads every ≤10 blocks? Or do we want our own
   node or a paid provider?
3. **Infra – replay store:** can the nonce store be a Postgres table, as with
   `validator_request_nonces`? Platform has no Redis.
4. **Infra – document URL:** which host serves the services document (JSON, ACAO
   `*`, URL ≤256 bytes, never changing)? Options: a Platform route, the landing
   site, or Cloudflare.
5. **Keys – service hotkey:** should it be the SN118 owner hotkey (the owner
   coldkey signs; it cannot be deregistered) or a Ditto validator hotkey (its
   coldkey signs; the permit is checked only at publish; deregistering
   unpublishes)?
6. **Keys – signer:** will you sign `set_service` and `gamma_enabled` yourself
   with the owning coldkeys, from prepared dry-run commands, with no agent ever
   holding them? `gamma_enabled` is refused inside the weights window.
7. **Keys – direction B custody:** is a hot API-key hotkey on the relay hosts
   acceptable? It would sign requests to engy or Lium, bounded by its deposit and
   daily cap. And who periodically moves gamma into it from the owner-hotkey
   deposit with `move_gamma`?
8. **Emission share:** should SN118 send emission into gamma at all? If yes:
   which target (SN53 engy or SN51 Lium), what percentage (≤10% each), and which
   slot gives it up (owner, validator, burn, or miner, which must stay ≥33%)?
   This is a chain-level split that sells SN118 alpha every epoch, not the
   validator-weights treasury bucket.
9. **Emission vs treasury bucket:** if a gamma share funds inference, should it
   replace or shrink the treasury bucket's `gm_credits` (SN-252 sizing), or run
   alongside it? GM cannot be paid this way today: SN28 is not whitelisted and
   its service URL 404s.
10. **Is Ditto the service?** If yes, which surface? (A1) the Ditto product or
    Ditto Router in the backend, (A2) SN118 Platform miner endpoints (practice
    inference, or upload, readmission and bounty fees), or (A3) both, on
    separate service hotkeys.
11. **Providers:** which do we want to pay with gamma? engy (LLMs, +5% bonus),
    Lium (GPUs, for example screener overflow), Hippius (we already use its S3;
    it accepts SN75 gamma, but SN75 is not an emission target), chutes (no
    gamma), or GM (enabled on SN28 but not usable today)?
12. **Scoring impact:** may engy become a relay upstream for scored inference?
    That needs a `bench_version` decision. Or should it serve only screener and
    non-scored traffic at first?
13. **Accounting – ledger ownership:** do you confirm one ledger per service
    hotkey? If both product and Platform accept gamma, each gets its own hotkey
    and document, so no two ledgers credit the same `GammaSpent` totals.
14. **Accounting – rate and COGS:** do we credit at par ($1 per gamma) or with a
    bonus (engy adds 5%)? Accepting gamma brings Ditto no revenue, because the
    burn accrues to $DITTO holders. Which budget pays the upstream cost of what
    we serve?
15. **Accounting – balance scope:** in v1, does the balance report gamma
    credits only, or also other funding for the same hotkey (the standard says
    "however funded"), such as a miner's linked Ditto account?
16. **Accounting – refunds and compromised hotkeys:** is credit
    non-refundable? Can a balance move to another hotkey on proof of coldkey
    ownership? Credit at our service stays under the hotkey that spent it.
17. **Accounting – reporting:** should chain gamma credit and usage appear in
    the public treasury audit feed, or in a separate public ledger? How should
    RegisteredSubnetCounter-keyed totals show up in Backroom?
18. **Blocker – DITTO-2803:** runtime 493 removed spenders and the GammaSpend
    proxy, so that thread's conversion design no longer works, and gamma minted
    into keyless Ditto-issued deposit keys is stranded until the user's coldkey
    moves it. Do you want DITTO-2803 re-scoped (this thread did not touch it)?
    Should both efforts share one SN118 service hotkey, or keep separate ones?
19. **Blocker – SDK:** hashed key schemes and the standard's SDK path need the
    `bittensor` 11 SDK, but this repo pins 10.x. Is v1 with sr25519 and ed25519
    only (hashed senders get 401) acceptable, or should we plan the SDK upgrade?
20. **Acceptance test:** who runs our mainnet self-test? It needs a fresh test
    hotkey and a small mint from your wallet. Who sends the checklist to the
    spec author? This thread contacts no one.
21. **Prices:** which endpoints are paid, at what USD prices, and where is the
    pricing page?
22. **Public comms:** do miner docs, announcements and any public pricing page
    all wait for your explicit OK?
