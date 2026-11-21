# CPM scorer on Bittensor compute: spec

Status: proposal, 2026-11-18. Nothing here is built yet.

This adds a second home for the CPM (`model_context`) scorer, on Bittensor GPU
subnets. It does not remove or change the AWS lane (`aws-cpm-scorer`,
`infra/ansible/roles/cpm_scorer`). Both lanes run the same signed image and
talk to the same scorer API. Platform decides which results it trusts.

Prices and availability below are snapshots taken on 2026-11-18. Each one cites
its source.

## 1. What the scorer needs

From [competition-v1.md](competition-v1.md) §2.4, §4 and §6.1:

- **Hardware:** one GPU with at least 24 GB of memory (the bases are Qwen3.5
  0.8B and 2B in bf16), about 100–200 GB of disk, and outbound HTTPS only.
- **Process:** one long-lived container, `cpm-scorer serve`. It polls
  `/model-context-scorer/work` with a bearer token, downloads the base and the
  miner's weights from Hugging Face, runs `cmd/generate` and scoring, and
  posts the result or a fault.
- **Determinism:** the same weights, seed and GPU SKU must give bitwise-equal
  scores. Each season pins one `gpu_sku`; today that is `NVIDIA L40S`.
- **Image:** `ghcr.io/ditto-assistant/ditto-subnet-stack:cpm-scorer-*`,
  cosign-signed by `release.yml`. It needs CUDA ≥ 13.2
  (`NVIDIA_REQUIRE_CUDA`, driver branch r595).

## 2. Threat model on a rented host

On a normal rental, the host operator has root. It can read the container's
environment, files and memory, and it can change what the process does.

**What v1 exposes is small.** The season seeds come from the season's open
block hash (§4.1), the generator is public, and miner weights are public at
submit. So a host learns nothing a miner could not compute already. The one
secret on the host is the **scorer bearer token**.

| Risk | Without a TEE | Mitigation (§4) |
| --- | --- | --- |
| Bearer theft, then forged results for any lease | Real | Per-scorer tokens; untrusted results never final alone |
| The host tampers with a run (edits scores) | Real | Bitwise quorum across **distinct operator groups** (§4.2); trusted audits |
| One operator runs every quorum pod (colluding nodes) | Real; today's L40S supply looks like one operator (§3.1) | Not defended by quorum: such results stay `provisional` and never final or payable (§4.2) |
| A spoofed GPU (reports L40S, is not) | Real; seen on Lium, per [sn481 notes](../../design/sn481-miner-hostable-workloads.md) | Reproduce the season reference bitwise before any lease |
| The host is one of our own miners | Plausible | All of the above; no host or single operator group is trusted alone |
| Pod lost, restarted or slow | Common (§3) | Lease expiry already re-queues; the controller replaces the pod |
| Private data (P0-T team traces) | Not allowed | Stays on Ditto-controlled infrastructure (the AWS lane); see §6 |

A TEE (Intel TDX plus NVIDIA confidential computing) hides memory and disk
from the host. It does not prove the pod runs **our** image: Lium says its
quote "measures the machine", and our miners can rent the same CVMs. v1 has no
secret data, so it does not need a TEE. Quorum and audits defend integrity
better, at about a fifth of the price, but only across operators that are
actually independent.

**Scope of the integrity claim.** Quorum defends against one dishonest host,
not against one party that controls every scorer in the quorum. §4.2 makes
that rule enforceable: results that only one operator group can vouch for
stay provisional. Some collusion remains undefended: one party running nodes
on two marketplaces, or a provider that misreports ownership. Only trusted
audits and trusted re-runs bound that.

## 3. Providers

| Provider | Runs our image | 1 GPU $/hr | Pay | TEE we can verify | Fit |
| --- | --- | --- | --- | --- | --- |
| **Lium** (SN51) | Yes: any Docker image, root SSH | L40S $0.38–0.73; A6000 $0.40–0.67; H200 $4.25+ | Card, crypto, TAO, alpha; per second | CVM nodes only (TDX quote via dstack). CVM capacity unconfirmed | **Primary** |
| **Targon** (SN4) | VM with preset images; Docker inside unconfirmed | `h100-small` $3.09; `h200-small` $4.00 (0 in stock) | TAO/alpha credits | All VMs, but verified by Manifold's key service, not by us | TEE fallback |
| **Chutes** (SN64) | No: Chutes builds and signs the image | H100 $1.79, L40S $0.85 (API rates); 3× hourly deploy fee | Card, TAO, alpha | Yes, per instance | Poor fit for a poller |
| **Green Compute** (SN110) | Not documented | RTX 4090 from $0.40, 5090 from $0.70 | Card, stablecoins, TAO, alpha | None | No (consumer GPUs only, ≤ 32 GB) |
| Basilica (SN39) | Yes | Not published | TAO | None | No (ownership of SN39 unclear since April 2026) |
| ComputeHorde (SN12) | One-off jobs, needs a validator hotkey | Not published | — | None | No (no pods) |
| Nodexo (SN106, formerly NI SN27) | Not documented | Not published | USDC | Unverified | No |

**Comparison:** AWS g6e.xlarge (one L40S) is about $1.86/hr on demand.

### 3.1 Lium details

**L40S supply.** The public feed
([`/api/public/v1/nodes`](https://lium.io/api/public/v1/nodes)) listed nine
L40S nodes on 2026-11-18:
- most are "secure" tier, US, $0.38/GPU-hr, with a 99.5 reliability score and
  months of uptime;
- they share one price and driver, so they likely belong to **one operator**;
- the feed reports `gpu_driver`, `cuda_driver`, `tier`, `reliability_score`
  and `uptime_minutes` per node, and `lium up <NODE_ID>` rents an exact node
  ([docs](https://lium.io/llms-full.txt)).

**Driver gap (blocking).** Every L40S node listed runs driver **580 (CUDA
13.0)**. The release image declares `cuda>=13.2`. Only a few A6000 and
RTX 6000 Ada nodes run 595. Two fixes, to be checked in B0:
1. Enable CUDA forward compatibility. The L40S is a data-centre GPU and r580 is
   a supported branch. The `nvidia/cuda` base ships the compat libraries; our
   `ENTRYPOINT` bypasses NVIDIA's entrypoint, which normally switches them on.
2. Or build the scorer against CUDA 13.0 wheels.

Either way, B0 must show bitwise-equal scores against the AWS L40S on driver
595.

**Caveats** ([security](https://docs.lium.io/pod-users/security),
[private models](https://docs.lium.io/pod-users/private-models.md)):
- On non-CVM nodes, the provider can read everything in the pod.
- Pods can restart on their own, and a reboot recreates the pod.
- There is no SLA and no Terraform provider. Lium offers REST, a CLI and a
  Python SDK.

### 3.2 Targon

Ditto ran screeners on Targon rentals from 2026-08-10 until retiring them in
#2357 (2026-09-27). We saw:
- `POST /workloads` 409s on most launches, plus provisioning stalls;
- a DELETE that answered 204 and then killed the rental;
- suspended one-shot rentals billed for hundreds of hours.

The rental product looks gone now; only TDX VMs are listed
([inventory](https://api.targon.com/tha/v3/inventory),
[VMs](https://docs.targon.com/guides/virtual-machines)). That makes it the
fallback if a later score version needs confidential data, not the default.

## 4. Design (additive)

### 4.1 Platform: scorer identities

New table `model_context_scorers`:

| Column | Meaning |
| --- | --- |
| `scorer_id` | Unique id |
| `lane` | `aws`, `lium`, `targon`, … |
| `trust` | `trusted` or `untrusted` |
| `token_sha256` | Hash of the bearer; Platform never stores the token itself |
| `gpu_sku` | GPU model the scorer was registered for |
| `node_ref` | Provider node or rental id |
| `enabled`, `expires_at`, `created_at`, `revoked_at` | Lifecycle |

- **The AWS lane is unchanged.** The existing `DITTO_MODEL_CONTEXT_SCORER_TOKEN`
  keeps working and maps to an implicit `trusted` AWS scorer.
- **Bearer lookup:** an incoming bearer matches by hash (constant time), and
  the `scorer_id` in the request must belong to that bearer.
- **Revocation:** revoking one scorer locks out only that pod.
- **Admin and Backroom:**
  - `list_model_context_scorers`, `revoke_model_context_scorer` and a scorer
    health read;
  - a register call that stores only a hash. The plaintext token goes
    straight to the pod and never passes through Backroom.

### 4.2 Platform: result acceptance

Today the first result for a lease is final. The change:

- **Per-scorer results.** Each result lands in
  `model_context_run_results` (`run_id`, `scorer_id`, `lane`, `trust`,
  `result_sha256` over the canonical `score`, `components` and `per_seed`).
- **Operator groups.** At registration, the controller gives every untrusted
  scorer an `operator_group`; Platform stores it and the scorer cannot change
  it. The group is:
  - `<lane>:<owner>` when the provider exposes the node owner through an
    authenticated API we verify, such as the executor's SN51 miner hotkey or
    coldkey on Lium;
  - `<lane>:unknown` otherwise. **Every pod whose owner cannot be determined
    shares one group per lane**, so two such Lium pods count as a single
    operator.

  Each lane (`lium`, `targon`, …) is a different marketplace, so groups in
  different lanes are distinct.
- **When a run is final.** One `trusted` result, or `quorum_untrusted`
  untrusted results (default 2) with equal `result_sha256` from distinct
  `scorer_id`s, distinct `node_ref`s **and at least two distinct
  `operator_group`s**. Bitwise equality holds only on one SKU, so every
  quorum scorer runs the season's `gpu_sku`. A season's SKU is therefore
  picked where at least two operator groups (or the trusted lane) can supply
  it.
- **Provisional runs.** A matching quorum that spans fewer than two operator
  groups makes the run `provisional`:
  - the board shows the score marked provisional;
  - the ledger never marks it final or `weight_eligible`;
  - Platform leases it to a scorer in another operator group, or to the
    trusted lane at 100% (not sampled) when that lane is enabled;
  - a run nobody can finalize stays provisional; it never times out into
    final.
- **Fan-out.** Until quorum, `/work` leases the same run to another eligible
  scorer. A scorer never gets a run it has already posted.
- **Disagreement.** Mark the run `disputed`. Lease it to a trusted scorer if
  one is enabled; otherwise hold it for an operator. Record both scorers.
  When one scorer disagrees `dispute_limit` times, disable it automatically.
- **Season qualification.** A scorer gets submission work only after it
  reproduces the season's reference run (unmodified base, §4.5) bitwise. A
  wrong or spoofed GPU fails here. The reference itself is set by a trusted
  scorer, or by the first untrusted quorum that spans two operator groups.
  Until then, the season's floor and every run that depends on it are
  provisional.
- **Audits.** The new setting `audit_rate` (default 0.05) sends a sample of
  untrusted-final runs to the trusted lane when one is enabled. Audits may run
  on another SKU and then compare within `audit_tolerance` (default 1e-3 on
  `score`), because bitwise equality holds only on one SKU. A miss disables
  every scorer that signed that result.
- **New settings** (one revision, visible in Backroom): `quorum_untrusted`,
  `min_operator_groups` (default 2, floor 2), `audit_rate`,
  `audit_tolerance`, `dispute_limit` and `lanes_enabled`.
- **Leaving shadow.** `model_context_weight_eligible` may be turned on only
  while every run in the ledger is final under the rule above, and while the
  trusted lane audits at `audit_rate > 0`.

The result wire shape (`ModelContextResultRequest`) does not change. The
ledger adds `scorer_ids` and `lanes` per run, for provenance on the board.

**What a Lium-only fleet gives.** Two Lium pods of unknown or shared
ownership cost about $0.76/hr. They give reproducible but **provisional**
scores: good enough to run the shadow board and to catch a single bad node,
not to finalize or pay. Final results need a second operator group on the
same SKU:
- Lium nodes with verified, distinct owners;
- a second lane that offers the season's SKU;
- or the trusted lane, re-running each provisional run. The AWS lane is an
  L40S, the same as the current season.

### 4.3 Scorer and image

- `cpm-scorer serve` already reads the bearer from
  `DITTO_MODEL_CONTEXT_SCORER_TOKEN` or a file, and reports the GPU name as
  `gpu_sku`. No wire change.
- Add a startup check that fails with an `infra` fault before leasing when
  any of these is wrong: driver below the image's minimum, compat libraries
  inactive, or a GPU name that does not match `--expect-gpu`.
- **Pod entrypoint.** Pods have no systemd, so the Ansible role and the
  auto-update timer do not apply. The pod runs the immutable digest directly:
  `cpm-scorer serve …`.
- **Image updates.** On a new release, the controller (§4.4) replaces pods.
  Platform also gets `min_scorer_version`; an older scorer receives `426` from
  `/work` and exits.
- **B0 checks** whether Lium's runtime honours our `ENTRYPOINT` and non-root
  `USER`, or needs a thin wrapper image. A wrapper would be signed by the same
  workflow.

### 4.4 Provisioning: `cpm-scorer-fleet` controller

Lium and Targon have no Terraform providers. We follow the screener capacity
controller pattern: reviewed code, the provider API key in Secret Manager, and
policy in Platform settings.

1. **Read.** From Platform it reads `lanes_enabled`, the target pod count per
   lane, the season's `gpu_sku`, and the released digest of
   `cpm-scorer-stable-1`.
2. **Scale.** It rents exact nodes by id, filtered on SKU, minimum driver,
   tier `secure` and a reliability floor. It scales to the queue: zero pods
   when no runs are open, and at least `quorum_untrusted` pods when any are.
   Lium bills per second with no minimum, so idle costs nothing.
3. **Provision each pod:**
   - generate a fresh bearer;
   - register its hash and `node_ref` with Platform;
   - write the token to the pod over SSH as a 0400 file, never in the pod's
     environment;
   - start the container from the digest.
4. **Clean up.** It deletes pods on release, on revocation, after
   `max_pod_age`, or when heartbeats stop. After each delete it confirms the
   pod is gone and billing has stopped; Targon taught us this check is needed.
5. **Report.** It writes its state and spend to Platform, and Backroom shows
   them.

The Lium API key is owner-provisioned. Nothing can be rented until it exists
and the account holds credit (§4.5).

### 4.5 Funding: a Gamma service bucket (decided 2026-11-18)

The scorer is infrastructure for the competition, so **miner emissions pay for
it**, through Gamma like the GM, Bitsec and Bitcast buckets. Nothing here uses
a card.

**How Lium takes crypto** (CLI `lium fund`, from
[Datura-ai/lium](https://github.com/Datura-ai/lium) and
[#347](https://github.com/Datura-ai/lium/pull/347)):
- A **TAO** payment is a plain transfer to Lium's `LIUM_FUNDING_ADDRESS`. It
  must come from a wallet verified on the Lium account: `lium fund` signs an
  add-wallet token with that wallet once.
- An **alpha** payment (`lium fund --alpha -k <hotkey> --netuid N`) is a stake
  transfer of free alpha, priced in USD, and only for the subnets Lium accepts.
- Either way, the credit is held in USD (`lium balance`).

**Flow**, reusing the existing Gamma pieces
([sn118-service-treasury-v2.md](../../sn118-service-treasury-v2.md),
[gamma-manual-transfers.md](../../gamma-manual-transfers.md),
[gamma-manual-funding.md](../../gamma-manual-funding.md)):

1. **New bucket** `cpm_compute`, purpose "CPM scorer GPU compute (Lium)".
   - It gets its own holding coldkey and starts at `allocation_bps = 0`.
   - A nonzero allocation follows the normal Gamma policy path: a
     coldkey-signed range plus matching Platform and validator digests.
2. **Collector to holding wallet.** The existing bounded manual transfer
   (`preview_` / `queue_treasury_manual_transfer`, with the confirmation
   "TRANSFER SN118 ALPHA ONCE") moves SN118 alpha to the `cpm_compute`
   holding coldkey. It has the same caps, retained floor and public receipt
   as today.
3. **Holding wallet to Lium.** The holding coldkey is the wallet verified on
   Ditto's Lium account.
   - **If Lium accepts SN118 alpha:** `lium fund --alpha --netuid 118` moves
     stake directly, with no sale. Check this with `alpha subnets`.
   - **Otherwise:** sell SN118 alpha for TAO within `max_slippage_bps`, then
     `lium fund` sends the TAO. That sale is the same "DITTO sale" event GM
     funding records.
4. **Payee rule.** A `cpm_compute` rule names Lium's funding address (asset
   `TAO`), or Lium's deposit coldkey and hotkey for alpha. The public
   treasury receipts then classify the payment exactly.
5. **Reconciliation.** Add a Lium variant of `FundingBatch`. Today's
   purchase record is GM-specific (`x402-relayed`). The Lium variant pairs
   each chain payment with the credited USD from Lium's billing history
   (`lium billing history`), and per-pod spend appears on the board's
   compute line.

**Sizing.**
- `allocation_bps ≈ monthly_compute_usd / monthly_miner_emission_usd × 10,000`.
- Measure `monthly_compute_usd` in B0, then cap it with a Lium per-key budget
  (`POST /keys`) so a runaway controller cannot drain the credit.
- Until B2 runs real seasons, fund by hand in small tranches from the
  existing bucket instead of carving a standing allocation.

**Limits.**
- The holding wallet signs only payments to Lium; the controller never holds
  a key.
- The controller's Lium API key can rent and delete pods. It cannot move
  funds.
- Custody, the collector journal and the signed range are unchanged.

### 4.6 What does not change

- The AWS stack (#3573), the Ansible role, the GCP federation and the token
  secret. They become one lane among several.
- The submission flow, the score, the seeds, the ledger shape, and the
  validator (shadow, 0 bps).
- P0-T and any eval on team traces. That data stays on Ditto-controlled
  infrastructure: the consent covers Ditto processing it, and no rented host
  qualifies.

## 5. Phases

| Phase | Work | Exit gate |
| --- | --- | --- |
| **B0 rehearsal** (no Platform change) | Fund Lium with a small manual Gamma tranche (§4.5). Rent one Lium L40S by hand, run the signed image with `cpm-scorer score` on public seeds (no bearer), and compare it bitwise with a second Lium node and with an AWS L40S. Fix the CUDA 13.2 vs driver 580 gap. Time a run. | Equal bits across nodes, or a measured reason why not; $/run |
| **B1 Platform** | `model_context_scorers`, per-scorer bearers, `model_context_run_results`, quorum, qualification, audits, settings, Backroom tools. One migration. | Tests: a forged result alone never finalizes; a quorum within one operator group (incl. `<lane>:unknown`) stays provisional; a dispute holds; a spoofed SKU fails qualification |
| **B2 fleet** | `cpm-scorer-fleet` for Lium: scale to the queue, SSH token handoff, release replacement, billing check | Two pods pass qualification on a real season in shadow; their runs show as provisional until a second operator group or the trusted lane finalizes them |
| **B3 optional** | A TEE lane (Lium CVM or a Targon VM) if a later score version must hide data on rented hosts | A Ditto-verified quote plus proof the pod is ours, not just a machine quote |

## 6. Costs (one season, shadow)

The run time per submission is unmeasured until B0. At $0.38/GPU-hr on Lium,
two quorum pods cost $0.76/hr while busy and nothing while idle. Even running
around the clock, two pods cost about $550 a month. For comparison:
- one always-on AWS g6e.xlarge costs about $1,360 a month;
- two Targon `h100-small` VMs cost about $4,500 a month.

## 7. Owner decisions

1. **Paying Lium: decided.** Miner emissions pay, through a Gamma
   `cpm_compute` bucket (§4.5). Still open:
   - which custody operator holds the bucket's coldkey;
   - whether Lium accepts SN118 alpha or we sell it for TAO;
   - the first manual tranche.
2. **Trusted lane:** keep AWS as an auditor that re-runs provisional runs and
   samples final ones (credits, small hours). Without it, Lium-only results
   stay provisional unless decision 3 adds a second operator group.
3. **Second operator group:** the listed L40S supply looks like one operator,
   so a Lium-only fleet cannot finalize results (§4.2). Ways to get a second
   group:
   - ask Lium for verifiable node ownership, then rent from two owners;
   - pick a season SKU that two lanes both offer. Targon lists only H100/H200
     and Lium lists no H100 today, so this needs new supply and a season
     boundary;
   - accept provisional-only results while in shadow.
