# Verifiable DittoBench execution on Platform infrastructure

Status: **design proposal**, 2026-11-07, tracked as
[DITTO-2543](https://omniaura-team.atlassian.net/browse/DITTO-2543). This
document changes no runtime, wire contract, gate or setting. Nothing in it is
built, deployed or activated.

Goal: run scored DittoBench evaluations on infrastructure we operate (AWS was
the example given) and give validators a compact proof object that they verify
and sign. Validators would stop running the bench.

## Recommendation in five lines

1. **Fix the inputs before the run.** The miner makes an on-chain commitment to
   the exact harness bytes, batched with the upload payment. Each seed comes
   from a drand round at least `Δ_min` after that commitment, never from a
   block Platform picks, and no Platform-assigned value feeds the seed.
   Pre-register every attempt in an append-only log and anchor its root on
   chain once per epoch, so runs cannot be rerolled silently.
2. **Attest the execution.** Run the scorer, sandbox and inference broker in an
   EC2 instance attested by NitroTPM and built from an Attestable AMI. The
   runtime key bound to that attestation signs a run statement. The statement
   binds the screened image, scorer, seed, dataset and witness root.
3. **Prove the deterministic part.** A Go-native zkVM (ZKM Ziren) proves the
   judge-free grader and aggregation over the committed witness. Validators
   verify the proof, the attestation and the log inclusion in milliseconds,
   then sign off.
4. **Say plainly what stays trusted.** Inference cannot be proven: zkML for
   these runs is about 60 H100-hours per run, and it would cover a different
   model. Inference trust stays where it already sits, on Platform's relay and
   the provider. We bound it with the attested relay and beacon-sampled
   independent audits of crown-moving decisions.
5. **Keep the weight fold.** Ledger entries carry proof references. Validators
   fold only entries whose statements verify under the epoch-pinned verifier
   policy. Activation uses the same no-force `fleet_ready` gate as every other
   fold change.

---

## 1. What exists today

Everything in this section was read from the monorepo at `main` (`999f23d`) or
observed on the public Platform API on 2026-11-07.

### 1.1 One scored run

| Element | Today | Where |
| --- | --- | --- |
| Commit | Miner uploads a harness tarball and pays. Screening builds a verified image. | `apps/platform/ditto/api_server/endpoints/screener.py` |
| Seed | When screening passes, Platform reads the **latest** block via Pylon. `seed = SHA-256(block_hash ":" agent_id [":" hotkey])[:8] & (2^63-1)`. If the chain read fails it falls back to a CSPRNG seed and leaves the block columns null. | `screener.py` `_derive_dataset_seed`; `ditto/validator/onchain_seed.py` |
| Dataset | Public, deterministic generator. `(seed, bench_version)` gives byte-identical bytes and a `dataset_sha256`. A private transport (`platform-private-v1`) exists, default off. | `research/dittobench-datagen`; `services/dittobench-api/PROTOCOL.md` |
| Quorum | k=3 validators lease tickets. Each gets a hotkey-bound seed (`seed_scope: validator`), so the three runs see different datasets. Platform finalizes the median. | `docs/VALIDATOR.md` |
| Execution | The validator's `dittobench-api` runs the screened image in `sandbox-docker` with deny-all egress. It sends `/seed` waves, then overlapping `/run` per case. The live board uses 175 datagen cases plus a generator slice. | `PROTOCOL.md`; `.agents/skills/ditto-subnet-benchmark/references/bench-runtime.md` |
| Inference | A ticket-scoped broker in the scorer forwards to **Platform's** relay. The relay forces the locked model (`openai/gpt-oss-20b`; live config says "OpenRouter dynamic provider route"), meters tokens, and records hashed claim spans and the tool-emission ledger. No provider key reaches the validator. | `services/model-relay`; `PROTOCOL.md` (v10 provenance, v13 claim spans, v16 priced cost) |
| Grading | Judge-free and deterministic. A score is a pure function of the dataset, the transcript and the broker evidence. | `services/dittobench-api/docs/judge-determinism.md`; `research/dittobench-datagen/grade` |
| Outputs | `ScoreReport` (composite, per-case results, gate details), a canonical transcript artifact (`transcript_sha256`) and v9 base evidence (`base_evidence_sha256`). | `services/dittobench-api/cmd/dittobench-api/transcript.go` |
| Signature | sr25519 over `hotkey:agent_id:deadline:run_id:composite:seed[:bench_version][:transcript_sha256][:base_evidence_sha256]` | `ditto/validator/signing.py` `score_signing_message` |
| Weights | Platform pins the ledger once per chain epoch with a `ledger_digest`. Every validator folds it deterministically: KOTH, bands, incumbency, ties. Pylon submits with commit-reveal. | `docs/VALIDATOR.md` (epoch-pinned ledger, protocols 19–35) |

Observed on the public API on 2026-11-07:

- `GET /api/v1/public/bench/config` reports `bench_version` 17, a locked model,
  `judge_free: true` and `public_transcript_url_template: null`.
- The rank-1 agent's `/public/agent/{id}/scores` shows three validator rows,
  each with its own seed, `transcript_sha256` and signature.
- One of those transcripts' telemetry shows 175 cases and 595 model requests.
- The leaderboard reports `average_run_cost_microusd = 463962` for that agent
  over 2 inference runs.

### 1.2 What validators contribute today

1. **Independent execution:** three separate machines each run the harness.
2. **Independent grading:** each validator runs the same grader.
3. **Accountable signatures**, which Platform medians.
4. **The deterministic weight fold and submission.**

Items 1–2 are what this design replaces. Validators do **not** independently
check inference today: the relay belongs to Platform. Validators also do not
check the seed block against the chain. `ditto/validator/worker.py` says the
validator "does not read `seed_block_hash` back from the chain, so it agrees
with Platform's pin, not independently with the chain".

### 1.3 Gaps found while reading

These matter for any proof design. Several are worth fixing anyway.

| # | Gap | Evidence | Consequence |
| --- | --- | --- | --- |
| G1 | Validators trust the block hash Platform serves | `worker.py` comment on `_confirmation_pin_binding_mismatch`; the `seed_matches` check uses `job.dataset_seed_block_hash` | A malicious Platform could serve any hash consistent with its own seed |
| G2 | Platform picks *which* block seeds a run (the latest at verdict time) | `screener.py` `_derive_dataset_seed` | Platform can delay a verdict across 12 s blocks and preview each candidate dataset with the public generator. This is a seed-grinding window that only the operator has |
| G3 | Block hashes on Subtensor can be biased by the block author | Subtensor is proof of authority, with Aura slots held by a permissioned authority set (Bittensor docs). Substrate documents parent-hash randomness as manipulable by the author | The seed source is only as unbiased as the authority set |
| G4 | The transcript artifact alone does not hold everything the v13+ gates read | `transcriptArtifact` holds responses, observed calls, execution and a relay summary. Claim-span hashes, tool-emission matches and priced-cost meters live in report details and base evidence | "Re-grade the transcript" (seed-and-scoring.md, step 4) needs the full witness. That witness must be defined before anything can be proven |
| G5 | The scoring path reads postures from the environment | 5 `os.Getenv` sites in `internal/scorer` (transform audit, v13 postures, twin rule and rate); more in `cmd/dittobench-api` | The scoring function is not yet a pure function of explicit inputs |
| G6 | Public transcripts are off | `public_transcript_url_template: null` (live); #2204 put the mirror behind an operator setting because transcripts carry behavioral data before quorum | Third-party re-grading is unavailable today. That makes witness-hiding verification valuable, not just nice to have |
| G7 | The upload payment names no artifact or agent, and `agent_id` is assigned by Platform | Payment is a bare `Balances.transfer_keep_alive` (`payment_verifier/verifier.py`); the payment→agent binding lives only in `evaluation_payments`, and credits can move a payment to a different artifact; `agent_id = uuid.uuid4()` at upload (`endpoints/upload.py`) | Nobody outside Platform can check on chain which payment anchors which agent. `agent_id` is a Platform-chosen seed input. Section 4.5 replaces both |

---

## 2. Goals and non-goals

Goals:

- A validator can check, without running a harness, sandbox or inference, that
  a published score is the approved scorer's output for this agent's screened
  image on an unbiased dataset, and that the run was not rerolled.
- Verification is deterministic, so honest validators reach identical verdicts
  and identical weights.
- No existing gate is relaxed. Quorum semantics, bench-version negotiation,
  provenance postures, the no-automatic-retry policy and `fleet_ready` stay as
  they are.

Non-goals:

- Proving LLM inference. Section 4.4 explains why this is not possible today.
- Changing score semantics. Every scoring change still goes through a bench
  version bump (`$ditto-subnet-bench-version-bump`).
- Hiding plaintext from Platform host administrators. The current
  private-execution profile already trusts them
  (`docs/coding-platform-private-execution-v2.md`).

---

## 3. Threat model

### 3.1 Actors

| Actor | Controls | Wants |
| --- | --- | --- |
| **Operator (us):** Platform, AWS account, relay, release pipeline | Scheduling, infrastructure, keys, provider routing, which runs happen | To favour or disfavour an agent, look busy, save cost, or recover from incidents quietly |
| **Miner** | Harness code (frozen at upload), resubmission cadence, payment | A higher score than the harness earns, a peek at tasks, a rival's demise |
| **Validator** | Its hotkey, its verifier software, whether it verifies at all | Rewards for minimal work, collusion, or censorship of a rival |
| **Third parties:** AWS, the provider (OpenRouter and upstream), the drand League of Entropy, Subtensor authorities | Their own infrastructure | Assumed honest-but-fallible; listed as trust roots below |

### 3.2 Attacks

"Proposed" refers to the protocol in section 6.

| Attack | Today | Proposed | Residual |
| --- | --- | --- | --- |
| **Operator edits a score number** | Three validators compute and sign; median | zk proof that `score = scorecore(P, D, W)` for the approved program `P` | None, given a sound zkVM and reviewed `P` |
| **Operator edits the transcript or witness** (changes a miner's answer) | Validator-owned run; signed transcript digest | Witness root signed by the attested runtime key. Forging it means breaking the attestation | AWS Nitro is the trust root; bugs inside the measured image |
| **Operator swaps the scorer binary or a posture** | Validator verifies scorer identity against its stack descriptor | Program id, config digest and PCRs pinned in an epoch-pinned verifier policy | The policy is ours, but public and versioned; changes need `fleet_ready` |
| **Operator grinds seeds or task selection** | G1, G2 and G7 above: a real window | drand round fixed by the miner's on-chain artifact commitment + `Δ`, where validator code enforces `Δ ≥ Δ_min`. Validators check the commitment extrinsic (signer, remark, first match, unique use), the BLS signature and the block on their own chain node. No Platform-assigned value feeds the seed (§4.5) | Honesty of a drand threshold; Platform–miner collusion on admission-token timing (open question 11) |
| **Operator cherry-picks runs** (reruns until the LLM noise helps, kills unfavourable runs) | Retries are audited and append-only but **private** (`docs/no-automatic-retries.md`) | Public, chain-anchored attempt log. Only the attested runtime can sign a terminal outcome (scored or failure), and it commits the record's hash to the log before releasing it, so Platform decides on any abort without seeing the result. A scored result cannot be relabelled as a failure. A committed but unrevealed record blocks the replicate. Only the first verified scored attempt counts. Aborts are blind, public and capped by `max_retries`. Retries reuse the same seed | We can still refuse or delay a run (liveness), and run unlogged previews to *learn* about a harness. The relay is ours, so we see model traffic mid-run and could estimate the score and abort *before* the runtime commits. That is at most `max_retries` extra draws of serving noise per replicate, every one public. Closing it fully needs the relay inside the attested boundary (open question 2) |
| **Operator degrades inference for one miner** (other model, truncation, latency) | Same trust as today; validators cannot see it | Relay inside the attested boundary; route profile in the statement; provider-reported model and usage in the witness; sampled independent audits | **No cryptographic guarantee.** The provider and OpenRouter's dynamic route remain trusted |
| **Operator replays an old result** | Validator signs run id + seed + deadline | Statement binds agent, image digest, ticket, replicate, seed, beacon round and log leaf; leaves are unique | None |
| **Operator equivocates** (shows validators different statements) | Ledger digest per epoch | Statement root in the on-chain commitment; inclusion proofs | None once anchored |
| **Operator leaks private tasks to a favoured miner** | `platform-private-v1` off; admins trusted with plaintext | Key release can be bound to PCRs. But the AWS account owner controls the KMS key policy | **Organizational, not cryptographic,** unless key custody leaves the operator (open question 8) |
| **Miner fakes outputs** (self-reported tools, rewriter, launderer) | v10 tool provenance, v13 claim-span provenance, deterministic grader | Unchanged; the proof covers these gates because they are part of `scorecore` | As today; enforce postures stay operator decisions |
| **Miner swaps the model or exfiltrates** | Screened image digest, deny-all egress, locked relay | Unchanged, and the attestation now proves the sandbox configuration | As today |
| **Miner sees tasks early** | Seed postdates the frozen harness | Unchanged; the seed still postdates the commit | Resubmission grinding is bounded by fee and cooldown, as today |
| **Validator rubber-stamps** (signs without verifying) | n/a (validators execute) | Forced-invalid canary statements at a published rate; signing one loses signoff eligibility | Detection is probabilistic (section 4.6) |
| **Validator censors** (refuses to sign valid statements) | n/a | q-of-n signoff; deterministic verifier; refusals are logged | Liveness depends on q honest verifiers |
| **Validator leaks transcripts** | Validators hold full transcripts | Validators receive statements and proofs, not transcripts | Improvement over today |
| **Outsourced prover sees private witnesses** | n/a | Prove private witnesses on our own GPUs; a prover network only for public witnesses | Confidentiality from our own staff is organizational |

### 3.3 What the proof object guarantees

Assumed trust roots: AWS Nitro (hypervisor and attestation PKI), the zkVM's
soundness and its trusted setup if one is wrapped in Groth16, the drand
threshold, the Subtensor finalized chain, and the reviewed scorer source.

Given those, a validator that accepts a statement knows that:

1. the composite and gate values are exactly what scorer program `P` (bench
   version `v`, source revision `r`) computes from dataset `D` and witness `W`;
2. `D` hashes to the pinned `dataset_sha256`, and for public datasets it
   regenerates from the seed;
3. the seed comes from a beacon round that was not yet published when the
   miner's on-chain commitment to these harness bytes was included (§4.5);
4. `W` was committed by a runtime whose measured boot chain matches the
   published reference measurements. That runtime ran image digest `I`, the
   screened image for this agent;
5. this is replicate `j`, attempt `a` of a pre-registered ticket. Every other
   attempt for the ticket is visible in the anchored log. Each earlier
   attempt ended in a runtime-signed failure, committed before release, or in
   a blind abort within `max_retries`. No scored result was relabelled; and
6. the statement is the one every other validator saw.

### 3.4 What it cannot guarantee

- **Which model the provider ran, or that its outputs were honest.** The relay
  can only record what the provider returned.
- **That the measured image has no bugs or side channels**, or that AWS is
  honest. Attestation proves code identity, not code correctness.
- **That we run every miner promptly.** Censorship and delay are visible in the
  log but not prevented.
- **That we did not observe a harness outside the pipeline.** That is an
  information advantage. It matters if an operator-affiliated agent is eligible
  for emissions (open question 6).
- **Confidentiality of private tasks from the operator.** See the KMS row
  above.
- **That the scoring rules are fair.** The proof shows the rules were applied,
  not that they are right.
- **Measurement noise.** Two honest runs of the same harness still differ,
  because the locked model's serving is nondeterministic. The median of three
  replicates stays.

---

## 4. Options, compared honestly

Every number below is cited in section 9 with source, date and caveat. Figures
marked *(vendor)* are self-reported by the project. *(inferred)* means our
arithmetic on cited figures. **unknown** means it has not been measured.

### 4.1 Full zkVM proof of the whole run: not feasible

A run is an arbitrary miner container (Rust, Python or anything else) inside
nested Docker. It makes network calls to an LLM and runs for minutes to hours.
zkVM guests are single programs without threads or networking. RISC Zero
guests are single-threaded with a 192 MB memory limit. Ziren fakes `clone` and
never frees memory. No zkVM can express "run this container against a remote
LLM". This option is rejected.

### 4.2 zkVM proof of the deterministic scoring over a committed witness: recommended core

The proven statement:
`journal = (bench_version, config_digest, dataset_sha256, witness_root) → (composite, tool_mean, memory_mean, gate factors)`.

- **Language fit.** The scorer is Go: about 14k non-test lines across `grade`
  (6.4k), `internal/scorer` (4.1k), `internal/scoregates` (2.9k) and
  `internal/efficiency` (0.7k), plus gates that still live in
  `cmd/dittobench-api`. Porting it to Rust for SP1 or RISC Zero would create a
  second scorer. That breaks the benchmark invariant "research adapters do not
  create alternate production scoring paths", unless the port *replaces* the
  production scorer.
- **Go-native zkVMs.** ZKM Ziren has supported Go guests since v1.2.0
  (2025-10-21), on a "partial Linux ABI". It has no threads, a bump allocator
  that never frees, and a no-op clock. It is the only zkVM with a published
  real-Go benchmark: Geth, proven at **10.4 MHz on 1× RTX 5090** and **65.3 MHz
  aggregate on 8× RTX 5090**, measured 2026-11-01 *(vendor)*.
  - Its compressed proof is **290,530 B** and verifies natively in **25.5 ms**
    *(vendor, Fibonacci workload)*.
  - TinyGo on RISC Zero (`go-zkvm`) is a 10-star experiment. TinyGo's
    `encoding/json` fails its standard-library tests, so we rule that route
    out.
- **Cycles: unknown.** This is the PoC's first output. Section 8 gives
  proving-time sensitivity instead of a made-up estimate. Hashing is cheap: on
  RISC Zero, SHA-256 costs 68 cycles per 64-byte block, so even a 32 MiB
  private dataset is roughly 35 M cycles of hashing plus paging *(inferred)*.
- **Determinism hazard.** The Go spec lets the compiler fuse floating-point
  operations (FMA) unless an explicit conversion forces rounding. The guest
  result must equal the native amd64 result byte for byte, so Phase 0 tests
  this across architectures. A longer-term option is integer micros, as v16
  priced cost already uses basis points. That would be a scoring-contract
  change and needs a bench bump.
- **Zero-knowledge.** SP1 STARK proofs hide nothing until they are wrapped in
  Groth16 or PLONK *(secondary sources)*. RISC Zero's docs say every receipt is
  zero-knowledge. Ziren's hiding property is **unknown**, so the PoC checks it.
  Witness hiding matters only where the witness is private (gap G6, private
  datasets).

### 4.2b Validators re-run the scoring natively: the baseline

If the witness can be published, validators can simply rerun `scorecore`
natively, in seconds of CPU (**unknown**, to be measured). That proves the
same thing as 4.2 without a zkVM. It cannot work for private witnesses, and it
costs witness download and CPU on every validator. We recommend it as the
Phase-0 deliverable, as the dispute path and as a cross-check of the zk
program. It is not the end state.

### 4.3 TEE attestation, with an optional zk wrapper

| Variant | Fit | Notes |
| --- | --- | --- |
| **AWS Nitro Enclaves** | Poor | No external networking (vsock to the parent only), no persistent storage, no GPU, CPU and memory carved from the parent. Our runtime is Docker-in-Docker with a broker and a network-attached relay. No extra charge beyond EC2. Attestation is a COSE_Sign1 ECDSA P-384 document chained to the AWS Nitro root, with PCR0/1/2 (image, kernel, app), PCR3 (IAM role), PCR4 (instance), PCR8 (signing cert). Debug mode yields all-zero PCRs |
| **EC2 instance attestation (NitroTPM + Attestable AMI)** | **Good** | GA 2025-09-23 in all commercial Regions, including GPU instances. The NitroTPM document is signed by the Nitro Hypervisor and binds a caller-supplied public key. PCR4, PCR12 and PCR7 validate an Attestable AMI whose boot chain refuses unmeasured binaries. Linux only. Pricing is not stated in the announcement (**unknown**) |
| AMD SEV-SNP on EC2 | Unnecessary | Memory confidentiality from AWS, which we do not need. m6a/c6a/r6a only, +10% of On-Demand, two Regions with shared tenancy, cannot combine with Nitro Enclaves |
| GCP Confidential Space | Alternative | Attestation token carries `submods.container.image_digest`. That is a closer match to "this container ran" than an AMI measurement. Relevant because subnet infrastructure is GCP today (`infra/terraform/stacks/*` has no AWS stack). See open question 1 |
| **zk-wrapping the attestation** | Not needed | Automata's verifier costs 390.6 M cycles on RISC Zero (285.6 M on SP1) with no cert cache, and 66.1 M (49.5 M) with five certs cached. On-chain verify is 257,741 gas on RISC Zero and 220,333 on SP1. Useful only for EVM verification. Our validators are off-chain and can check P-384 natively |

A TEE proves **code identity at boot**. It does not prove correct output. That
is why we pair it with 4.2: the TEE vouches for where the witness came from,
and the zkVM vouches for the arithmetic over it.

### 4.4 zkML for inference: not feasible (numbers)

The model is gpt-oss-20b: 20.91 B total and 3.61 B active parameters, a
mixture of experts with MXFP4 expert weights. One published v16 example run
used **22,318,268 prompt + 1,690,233 completion tokens** over 350 cases
(`PROTOCOL.md`).

| System | Published result | Applied to one run *(inferred)* |
| --- | --- | --- |
| FlashProve (Anthropic, 2026-11-02) | Llama 4 Maverick (400B MoE, about 17B active) at about **110 tokens/s** on 1× H100 + 24 cores. Proof 18.78 MB, verify 17 s, about 72× vLLM's time. **Contexts ≤ 1,024 tokens**; integer model under greedy decoding | 24.0 M tokens ÷ 110 ≈ **60.6 H100-hours**. That run averages about 63.8k prompt tokens per case, far beyond the 1,024-token limit. The model is not gpt-oss-20b |
| zkLLM (CCS '24) | LLaMA-2-13B: 803 s to prove one 2,048-token pass on 1× A100 | 63.8k prompt tokens per case needs ≥ 32 such passes; × 350 cases × 803 s ≈ 2,500 A100-hours as a lower bound. Different model |
| ZKTorch (2025) | LLaMA-2-7B: 2,645 s per token on 64 CPU threads | Not meaningful at 24 M tokens |
| DeepProve, Jolt Atlas | GPT-2 / Gemma 3 at 512 tokens in 7.6–19 min; GPT-2 with sequence 16 in 14.9 s | Toy scale |

Two reasons rule this out, beyond cost:

1. Every system proves a **quantized or integer model we host ourselves**. It
   cannot prove what Bedrock or OpenRouter served from a floating-point
   deployment. FlashProve itself says a floating-point or sampled reply "could
   depart at some position".
2. The bench deliberately uses a provider-served, priced, locked model, and the
   v16 contract prices its tokens. Proving inference would mean self-hosting a
   different serving stack. That is a bench contract change.

zkML is therefore out of scope for the foreseeable future. If inference
integrity becomes a hard requirement, the realistic step is to **self-host
gpt-oss-20b inside an attested GPU instance**, with the weights hash in the
measurement. That still proves only that our code ran, and it needs a bench
bump.

### 4.5 Commit-reveal with a randomness beacon

The current design already makes the seed postdate the miner's commit
(`seed-and-scoring.md`). The problems are gaps G1–G3 and G7: *who picks the
block*, *whether the block hash can be biased*, and *whether the anchor can be
tied to the agent on chain at all*. Two facts about today's upload flow shape
the fix:

- **The payment names nothing.** It is a bare `Balances.transfer_keep_alive`
  (`payment_verifier/verifier.py`). Platform binds a payment to an agent only
  in its own `evaluation_payments` table. A payment can also become a credit
  later spent on a *different* artifact (`EvaluationPayment.credit_for_agent_id`).
  So a validator cannot check on chain that a given payment block belongs to a
  given agent. Pointing at "the payment block" would let a malicious Platform
  pick among a miner's payments, or cite any later block.
- **`agent_id` is assigned by Platform.** It is a server-side `uuid4()`
  created at upload, after payment (`endpoints/upload.py`). Any value Platform
  assigns after the beacon round is known is a grinding knob, so it must not
  feed the seed.

Proposed design:

**Anchor commitment (on chain, made by the miner).** `/upload/check` already
issues a single-use, expiring admission token. Each token reserves exactly
one **evaluation authorization**:

- `paid`: the payment travels inside the anchor extrinsic itself;
- `credit:<block_hash>:<extrinsic_index>`: names the original on-chain
  payment that the credit came from.

Platform appends an issuance leaf to the attempt log. It commits to
`SHA-256(admission_token)`, the hotkey, the `artifact_sha256`, the
authorization, and the issuance and expiry blocks.

The miner then submits **one** extrinsic, signed by the coldkey that owns
`miner_hotkey` at the anchor block. On the paid path the payment is in that
same extrinsic, so this is exactly the payment-time owner that today's
payment verifier checks:

- paid uploads: `utility.batch_all([balances.transfer_keep_alive(payment_address, quote), system.remark(C)])`;
- credit-funded uploads: `system.remark(C)` alone.

Here `C = SHA-256("dittobench-anchor-v1" ‖ miner_hotkey ‖ artifact_sha256 ‖ admission_token)`.

The **anchor** is that extrinsic, identified by
`(anchor_block_hash, anchor_extrinsic_index)`. It is the first point where the
miner is bound on chain to these exact harness bytes. This needs changes to
the miner CLI and the payment verifier, which today accepts only a bare
`transfer_keep_alive`.

**Authorization accounting.** A token's issuance leaf reserves its
authorization until the token is either *consumed* by a valid anchor or
*burned*. A burned token's authorization is forfeited.

- **Extrinsics whose remark is not `C`.** These are not commitments for this
  token at all, whether the remark is missing or simply different. They
  consume nothing, and the token stays open until a `C`-carrying extrinsic or
  expiry. A transfer inside such an extrinsic is still paid and is neither
  refunded nor credited.
- **The first `C`-carrying extrinsic is the token's only anchor candidate.**
  "First" is by `(block, index)` (check 4). It counts whatever its outcome:
  - if it passes every check, it is the anchor and consumes the
    authorization;
  - if it fails any anchor check (failed dispatch, wrong signer, short
    payment, expired token), the token is **burned**: no later extrinsic
    carrying `C` can anchor, a paid transfer inside it is not refunded, and a
    reserved credit is forfeited.

  A miner therefore cannot get a second chance at the same token by sending
  a corrected extrinsic. They need a new token and a new authorization.
- **Expiry.** A token that reaches `expires_block` with no `C`-carrying
  extrinsic is also burned. Its reserved credit is forfeited and cannot be
  reused.
- Credits exist only for payments made **before** this anchor scheme. A
  payment inside an anchor extrinsic never becomes a credit (see "Anchors are
  final" below), so the paid and credit spend identities in check 5 never
  overlap.

**Anchors are final.** A valid anchor consumes its authorization whether or
not the miner completes the upload, because the seed is fixed at the anchor.
- An artifact that is anchored but never uploaded is logged as forfeited.
- So is an upload whose bytes do not hash to `artifact_sha256`.
- In both cases the payment never turns back into a credit.

Otherwise a miner could anchor twice, wait for both beacon rounds, upload the
luckier artifact and recycle the other payment.

**Beacon round.** `R` is the first drand quicknet round whose scheduled time is
≥ `timestamp(anchor_block) + Δ`. Quicknet has a 3 s period, uses scheme
`bls-unchained-g1-rfc9380`, and is run by the League of Entropy. Bittensor's
CR v3 already timelocks weights to quicknet.

`Δ` must guarantee that `R` was **unpublished when the anchor was included**.
Two facts make that possible:

- A round's randomness does not exist before its scheduled time, as long as
  fewer than a threshold of drand nodes collude.
- The only way to pull an already-published round into the window is a block
  timestamp set *earlier* than real time. A later timestamp only selects a
  later round.

Therefore:

- `Δ_min = 3 s (one quicknet period) + S`. `S` is the maximum backward skew
  Subtensor admits between a block's timestamp and real time. Aura's slot rule
  and the timestamp pallet bound `S`, but I have not read its exact value from
  the runtime (**unknown**). Phase 2 reads it from the runtime and pins it.
- The verifier policy pins `Δ` and `Δ_min` as separate values. **Validator
  code**, not the policy, enforces `Δ ≥ Δ_min`, and it rejects any policy
  below that. A policy edit cannot quietly shrink the window.
- In operation `Δ` should sit far above `Δ_min`: for example 120 s, which is a
  design choice, not a measurement. `Δ` only delays when the seed becomes
  known, and screening already takes longer than that.

**Seed.**
`seed_j = SHA-256("dittobench-seed-v2" ‖ drand_randomness(R) ‖ anchor_block_hash ‖ anchor_extrinsic_index ‖ artifact_sha256 ‖ bench_version ‖ j)[:8] & (2^63-1)`
for replicate `j ∈ {0,1,2}`. Every input is fixed once the anchor is included,
and none of them is chosen by Platform. `agent_id` remains the database key
but is no longer a seed input.

**Anchor checks.** Each validator runs these against its **own** chain node
(§6.5 step 4) and rejects the statement if any fails:

1. The anchor block is finalized and its hash equals `anchor_block_hash`.
2. The extrinsic at `anchor_extrinsic_index` succeeded. It was signed by the
   coldkey that owned `miner_hotkey` **at the anchor block**.
   - Paid path: the payment is in this extrinsic, so this is the same
     ownership rule Platform's payment verifier applies today.
   - Credit path: there is no payment in the anchor. The statement names the
     credited payment from the issuance leaf's authorization. Validators check
     that it was a successful payment to the published address by the coldkey
     that owned `miner_hotkey` at that payment's block.
3. Its remark equals `C`, recomputed from the statement's `miner_hotkey` and
   `artifact_sha256` and the admission token. The token is revealed in the
   statement and must hash to the commitment in the issuance leaf. The
   statement's `authorization` must equal the leaf's, so the paid-or-credit
   path and the uniqueness checks below all use one value. That leaf must come
   with a Merkle inclusion proof against an epoch root **already anchored on
   chain**. Until that root is anchored the statement stays
   pending and cannot verify, so Platform cannot present an issuance record
   made up after the fact. On the paid path, the batched transfer pays the
   published payment address at least the logged quote.
4. Let `W` be the token validity in blocks, pinned in the verifier policy.
   - The leaf's blocks must satisfy
     `issued_block ≤ anchor_block ≤ expires_block ≤ issued_block + W`. The
     token must still be valid at the anchor, and no token may live longer
     than `W`.
   - Order extrinsics by position `(block, index)`. "Carry `C`" means the
     extrinsic's call data contains the remark `C`, **whether or not it
     dispatched successfully**. Failed extrinsics count, so a failed first
     attempt burns the token instead of quietly leaving room for a second
     one. No extrinsic from the same signer, other than the anchor itself,
     may carry the same `C` at a position `p` with
     `(anchor_block − W, 0) ≤ p < (anchor_block, anchor_extrinsic_index)`.
     The scan covers blocks `anchor_block − W` through `anchor_block`
     **inclusive**. Within the anchor block it covers only indices below
     `anchor_extrinsic_index`.
   - Without that last rule, a miner could put the same `C` in two
     extrinsics of one block. The seed includes `anchor_extrinsic_index`, and
     check 5 scopes uniqueness to the anchor itself, so the miner could then
     choose between two seeds.
   - The window ends at the anchor and does **not** start at the reported
     `issued_block`, so post-dating the issuance record cannot hide an earlier
     commitment. A token is valid for at most `W` blocks, so any earlier
     commitment carrying `C` must fall inside that window.

   This makes the *first* matching commitment, by `(block, index)`, the
   canonical one. A miner cannot post several commitments, in different blocks
   or in the same block, and keep the one with the luckiest seed.
5. **Uniqueness, scoped correctly.** These rules are checked across all
   anchored statement roots and the anchored log.
   - **An anchor backs exactly one ticket.** Every statement that cites it must
     name the same agent, artifact and ticket.
   - **Replicates and retries share it by design.** Inside that ticket, the
     three replicates `j` share the anchor. An authorized retry of replicate
     `j` reuses both the anchor and `seed_j`.
   - **One attempt counts per replicate.** For each `(anchor, j)`, only the
     first terminal **scored** attempt counts. Every earlier attempt for that
     pair must end in a non-scored terminal record that the runtime signed and
     committed before releasing it, or in a blind Platform `aborted` leaf
     within the retry budget (§6.2, §6.5 step 7). Platform cannot relabel a
     scored result as a failure.
   - **An authorization is spent once.** Uniqueness is checked on a *spend
     identity*, which depends on the path:
     - **Paid:** the transfer inside the anchor extrinsic, i.e.
       `(anchor_block_hash, anchor_extrinsic_index)`. A paid issuance leaf
       records only `paid`, because the payment does not exist yet when the
       token is issued. A single extrinsic is a single anchor, so this
       identity backs exactly one ticket under the anchor rule above.
     - **Credit:** the credited payment `credit:<block_hash>:<extrinsic_index>`.
       It must appear in exactly one issuance leaf in the anchored log and
       back exactly one anchor.

     A statement whose spend identity appears behind a second anchor, or in a
     second credit leaf, is rejected.

   This closes grinding across tokens: a credit cannot be spread over several
   tokens to compare beacon rounds.
6. `R` is derived from the `Δ` pinned in the verifier policy, never from the
   statement. A statement whose `delta_s` differs from that pinned `Δ` is
   rejected, which removes any reading of `delta_s` as a per-run knob. The
   pinned `Δ ≥ Δ_min`, and `R`'s BLS signature verifies against the pinned
   drand chain hash.

**Residual risk.**

- A miner can still buy another seed for byte-identical source by paying
  again. That is what `allow_identical_rescore` permits today, so each seed
  still costs one evaluation fee.
- On the credit path a remark costs only the transaction fee, but a credit
  still yields at most one seed. It backs exactly one issuance leaf and one
  anchor (check 5), the token allows only one first-match anchor (check 4),
  and anchors are final.
- Platform alone cannot exploit the issuance record. The leaf is fixed by an
  anchored inclusion proof, and the first-match scan does not depend on its
  reported time. A fabricated leaf is useless without a remark signed by the
  miner that carries its token.
- Collusion is still possible. Platform could hand a miner a token whose
  *real* validity exceeds `W`, or issue it before logging it. The miner then
  commits once early and again more than `W` blocks later, and Platform logs
  the issuance just before the second commitment. Only an honest Platform
  enforces real token validity (open question 11).
- A block author can delay the anchor extrinsic by a few blocks. With
  `Δ ≫ S`, though, the candidate rounds are all still in the future, so the
  delay cannot be aimed at a known dataset.

No classic hidden commit-then-reveal step is needed, because no party
contributes secret entropy. Retries **reuse** `seed_j`, so an engineered
"infrastructure failure" cannot reroll the dataset. CRN confirmation seeds stay
as they are (`crn.py`), but confirmation runs go through the same attempt log.

Cost per statement: one BLS verification, a few block reads, and a scan no
longer than the token's validity window.

### 4.6 Optimistic verification and fraud-proof spot checks

For the parts no proof covers (execution environment and inference),
independent re-execution is the only external check:

- If a fraction `p` of runs is audited independently, an operator who cheats
  on `k` runs is caught with probability `1 − (1 − p)^k`. With `p = 5%`, that
  is 64% over 20 runs and 95% over 60 *(inferred, elementary)*.
- Truebit's forced-error idea, about 1 per 1,000 tasks, keeps verifiers honest.
  We use it for validator canaries.
- Proof of Sampling (Hyperbolic) finds honest behaviour is the unique Nash
  equilibrium at a sampling rate as low as about 0.74% when the penalty is
  large. Ours is reputational, not slashable, so expect to need a higher `p`.
- **An audit is statistical, not exact.** Serving noise means an audited
  composite never equals the hosted one. Compare it with the existing paired
  CRN statistic: replicate distribution against audit, inside the
  `KOTH_DETHRONE_Z` band. Audit seeds come from a drand round *after*
  finalization, so we cannot predict which runs get audited.
- Audit cost is `p × today's per-run cost` for the auditor: sandbox, hardware
  and wall-clock. Inference still goes through the Platform relay unless the
  auditor pays for its own provider key. That would check our relay too, at
  the auditor's expense.

Recommendation: always audit **crown-moving decisions** (contested dethrones,
staleness confirmations), where emissions move. Sample a small `p` elsewhere.
Treat the fraction as a budget (open question 3).

### 4.7 Matrix

| Option | Proves | Prover cost / run | Validator cost / run | Maturity | Verdict |
| --- | --- | --- | --- | --- | --- |
| 4.1 zkVM over whole run | everything | infeasible | — | — | reject |
| 4.2 zkVM over scoring | arithmetic over committed witness | **unknown cycles**, about 15 s–2.7 h per run depending on cycles and GPUs (§8) | about 290 KB, about 25 ms *(vendor)* | Ziren Go: about 1 year | **adopt** |
| 4.2b native re-grade | same, public witness only | 0 | witness download + CPU seconds (**unknown**) | exists today in principle | Phase 0, disputes |
| 4.3 EC2 instance attestation | runtime identity and witness provenance | EC2 only (attestation price **unknown**) | P-384 chain check, milliseconds *(inferred)* | GA 2025-09 | **adopt** |
| 4.3 zk-wrapped attestation | same, verifiable on EVM | 66–390 M cycles | about 220–258k gas | exists (Automata) | skip |
| 4.4 zkML | inference | ≈ 60 H100-h, wrong model, context limit | 17 s per proof | research | reject |
| 4.5 beacon + attempt log | unbiased seed, no silent reroll | negligible | BLS verify + Merkle path | drand since 2023 | **adopt** |
| 4.6 sampled audits | execution and inference, statistically | `p` × today's run | — | our existing CRN machinery | **adopt, small `p`** |

---

## 5. Recommended hybrid

```
 miner: batch_all[pay, remark(C = H(hotkey‖artifact_sha256‖admission_token))]
        = anchor extrinsic in block A
                │
                ▼
 drand round R = first ≥ ts(A)+Δ   (Δ ≥ Δ_min, enforced by validator code)
                │
                ▼
 seeds s0,s1,s2 = H(R ‖ anchor id ‖ artifact_sha256 ‖ v ‖ j)
                                                               │
 Platform registers attempt (ticket, j, a) in the attempt log ◄┘
                │ log leaf, anchored per epoch via the commitments pallet
                ▼
 attested EC2 runner (Attestable AMI: scorer + sandbox-docker + broker + relay client)
   runtime key K bound by the NitroTPM attestation document
   runs screened image I on D(s_j) → witness W
   signs RunStatement{agent, I, v, r, P, s_j, R, D_sha, root(W), meters, result}
                │
                ▼
 prover (our GPUs for private W; any prover for public W)
   Ziren receipt: P(D, W, config) → journal == statement fields
                │
                ▼
 validators: verify attestation (cached per K), statement signature,
             anchor extrinsic + BLS(R) on own node (§4.5 checks),
             log inclusion, receipt → sign off
                │
                ▼
 Platform: q-of-n signoffs per replicate → median of 3 → epoch-pinned ledger
           (entries carry statement digests) → validators fold as today
```

---

## 6. Protocol

### 6.1 Pinned identities: the verifier policy

Each bench version has a **verifier policy** document, published and digested
into every epoch pin:

- `scoring_program_ids`: Ziren program or verifying-key hashes allowed for
  each `bench_version`. Built reproducibly from `scorecore` at a reviewed
  revision.
- `runtime_measurements`: allowed `{PCR4, PCR7, PCR12}` sets for the Attestable
  AMI, released through the same signed release graph as validator stacks.
- `route_profiles`: allowed inference route profile ids. These already exist
  as reviewed identities, e.g. `openrouter-route-6a097486af3c178d-v1`.
- `config_digest`: the canonical posture and config set (claim provenance,
  twin rule, priced-cost posture, …) that today comes from the environment.
- `beacon`: drand chain hash, `Δ`, and `Δ_min` with the Subtensor skew bound
  `S` it is derived from. Validator code rejects any policy where
  `Δ < Δ_min` (§4.5).
- `token_validity_blocks` (`W`): the maximum admission-token lifetime. It is
  also the width of the fixed first-match scan window (§4.5 check 4).
- `canary_rate` and `signoff_quorum`.
- `max_retries`: the per-replicate number of retry slots. The initial
  attempt `a = 1` is free, and each attempt `a ≥ 2` uses one slot, whether the
  attempt before it failed or was aborted (§6.2). Every use is public in the
  log.
- `reveal_grace_blocks`: how long after `deadline_block` a committed record
  may still be revealed before the replicate is marked suppressed.

A policy change takes effect at an epoch pin. It is gated on every
recently-live weight-setting validator advertising the verifier protocol, with
no force override, exactly like protocols 20, 27, 29 and 34.

### 6.2 Commitments

**Admission-token issuance leaf**: `{schema, miner_hotkey, artifact_sha256, SHA-256(admission_token), authorization (paid | credit:<block_hash>:<extrinsic_index>), issued_block, expires_block}`,
where `expires_block − issued_block ≤ W` (`token_validity_blocks` in the
verifier policy). This leaf is appended when `/upload/check` issues the token.
It lands in the same append-only log, and so in the same anchored epoch roots,
as attempt and terminal leaves. The token itself is revealed only in the
statement, after the upload has consumed it.

**Attempt log leaf** (canonical JSON, SHA-256):
`{schema, ticket_id, agent_id, image_digest, bench_version, replicate j, attempt a, reason (initial | authorized_retry:<backroom audit id>), registered_at, deadline_block, R}`.
The leaf is appended **before** the runner may start. The runner refuses to
start without a log receipt containing the leaf hash.

**Attempts and retry slots.** Attempts are numbered from `a = 1`, which is
the initial attempt and uses no retry slot. Every attempt with `a ≥ 2` uses
exactly one of the policy-pinned `max_retries` slots, however the previous
attempt ended: runtime-signed failure or `aborted`. So a replicate has at most
`1 + max_retries` attempts, and an abort carries no extra charge. Each
`a ≥ 2` needs an `authorized_retry` reason.

**One authoritative deadline, serialized writes.** `deadline_block` in the
attempt leaf is the single deadline for that attempt's terminal commitment.
The log is one append-only sequence, and every leaf is stamped with the chain
block at append. The log service applies two rules as an atomic
compare-and-append on the attempt key `(ticket_id, j, a)`:

- A terminal commitment is accepted only if it is stamped
  `≤ deadline_block` **and** no `aborted` leaf for that attempt is already in
  the log.
- An `aborted` leaf is accepted only if it is stamped `> deadline_block`
  **and** no terminal commitment for that attempt is already in the log.

Exactly one of the two can ever be written, and validators re-check both rules
from log order and stamps.

A runner whose commitment is refused as late gets a rejection instead of a
receipt and never releases its record. The attempt is then `aborted`.
Platform assigns the stamps, so it could stamp an honest commitment late. That
is equivalent to a blind abort, because the record was never released, and it
consumes a retry slot publicly like any other abort.

**Terminal records come only from the attested runtime.** For each attempt,
the runner emits exactly one terminal record, signed by its attested key `K`:

- `scored`: the RunStatement below;
- `infra_failure` or `harness_fault`: a **FailureStatement**
  `{schema, ticket_id, j, a, log_leaf, outcome, failure_class, evidence_digest, times, runtime_key}`.
  The failure class is decided by the measured runtime's own code, not by
  Platform.

Platform cannot author a `scored`, `infra_failure` or `harness_fault` outcome.

**Commit before release.** Before the runner shows its terminal record to
anyone, Platform included, it appends a K-signed **terminal-commitment leaf**
`{ticket_id, j, a, SHA-256(record), runtime_key}`. It waits for the log's
receipt and only then releases the record. The record exists only inside the
isolated instance until it is committed. So any decision by Platform to let
an attempt finish or to kill it is made **without seeing its result**.

**Terminal leaf**: `{ticket_id, j, a, outcome (scored | infra_failure | harness_fault | aborted), record_digest}`.

- For every outcome except `aborted`, `outcome` must equal the K-signed
  record's outcome, and `record_digest` must equal the hash in the
  terminal-commitment leaf.
- `aborted` is the only outcome Platform writes. It is valid only under the
  serialized rule above: stamped after `deadline_block`, with no terminal
  commitment for the attempt earlier in the log. That covers a runner that
  vanished, was stopped, or committed too late. Each `aborted` is public.
  A retry after it is the next attempt and uses one retry slot, like a retry
  after any failure.
- If a terminal commitment exists but its record is not revealed by
  `deadline_block` + the policy's `reveal_grace_blocks`, the replicate is
  marked **suppressed**. Validators then refuse to count any later attempt
  for that `(anchor, j)`.
- A committed, revealed RunStatement that fails verification (§6.5 steps
  1–6) is not counted. Examples: a receipt that fails, a dataset hash that
  does not regenerate, a field that disagrees with the policy or the log.
  The replicate is **quarantined**, so no later attempt counts automatically,
  and only an owner-reviewed dispute resolves it. A *missing* receipt is
  different: the statement stays pending until it is re-proved (§6.7).

**RunStatement v1** (canonical JSON, SHA-256 = `statement_digest`), signed by
runtime key `K`:

```jsonc
{
  "schema": "dittobench-run-statement-v1",
  "network": "finney", "netuid": 118,
  "agent_id": "…", "miner_hotkey": "…", "image_digest": "sha256:…",
  "bench_version": 17, "scorer_revision": "…", "scoring_program_id": "…",
  "config_digest": "…", "verifier_policy_digest": "…",
  "ticket_id": "…", "replicate": 0, "attempt": 1, "log_leaf": "…",
  "artifact_sha256": "…",
  "seed": {"anchor_block": 0, "anchor_block_hash": "…", "anchor_extrinsic_index": 0,
           "admission_token": "…", "issuance_leaf": "…",
           "authorization": "paid | credit:<block_hash>:<extrinsic_index>",
           "drand_round": 0, "drand_randomness": "…",
           "delta_s": 0,                  // must equal the policy-pinned Δ
           "derivation": "dittobench-seed-v2", "seed": 0},
  "dataset_sha256": "…", "run_size": "full",
  "witness_root": "…",            // Merkle root over transcript, broker evidence,
                                  // base evidence, projection commitment
  "inference": {"route_profile": "…", "requests": 0, "prompt_tokens": 0,
                "completion_tokens": 0, "embedding_tokens": 0, "decision_input_tokens": 0},
  "result": {"composite": 0.0, "tool_mean": 0.0, "memory_mean": 0.0,
             "gate_factors_bps": {"…": 10000}},
  "times": {"registered_at": "…", "started_at": "…", "finished_at": "…", "deadline": "…"},
  "runtime": {"attestation_doc_sha256": "…", "runtime_key": "…"}
}
```

**Scoring receipt**: a Ziren proof whose public journal is
`H(bench_version, config_digest, dataset_sha256, witness_root, result)`.
The dataset and witness are private inputs. The guest hashes them and checks
the hashes against the journal.

### 6.3 On chain vs off chain

| Item | Where | Size and cost |
| --- | --- | --- |
| Anchor extrinsic: payment batched with the artifact-commitment remark (a remark alone on the credit path) | On chain, **new**: the miner CLI submits `utility.batch_all` instead of a bare transfer | one extrinsic per upload, as today, plus a 32-byte remark |
| drand round | Public beacon; also consumed by Subtensor for CR v3 | — |
| Epoch root = `H(epoch_index ‖ log_root ‖ statement_root ‖ ledger_digest ‖ verifier_policy_digest)` | On chain: one `Raw` (≤128 B) commitment per epoch via the `commitments` pallet | 32 B + index. The pallet allows 3 fields, `Raw` ≤128 B, `BigRaw` ≤512 B, and the hotkey must be registered on the subnet (open question 7) |
| Weights | On chain, existing commit-reveal | unchanged |
| Statements, failure statements, receipts, attestation documents, inclusion proofs | Platform public API + content-addressed public bucket | about 300 KB per replicate (receipt dominates) |
| Witness (transcript + broker evidence) | Private bucket. Public after finalization only if the mirror setting allows (#2204) | unchanged policy |
| Private datasets | Platform-owned, as today | unchanged |

### 6.4 Proof pipeline

1. **Register.** Platform appends the attempt leaf and returns a receipt.
2. **Boot.** The runner boots from the Attestable AMI and generates `K`. It
   obtains a NitroTPM attestation document binding `K`'s public key and
   publishes it once per boot.
3. **Run.** The runner pulls the screened image `I` by digest and verifies the
   lease. It derives `s_j` from the beacon and generates `D(s_j)`, or verifies
   private bytes against the pin. Execution is as today: sandbox, broker,
   relay, grader.
4. **Seal.** The runner builds the witness and computes `witness_root`. It
   signs its terminal record with `K`: the RunStatement, or a
   FailureStatement if the run failed. It appends the terminal-commitment
   leaf `SHA-256(record)` and waits for the log receipt. Only then does it
   release the record, and Platform writes the terminal leaf that points to
   it.
5. **Prove.** A prover job (outside the runner; soundness does not depend on
   the prover) proves `scorecore` over `(D, W, config)`.
   - Private witnesses are proven only on our own hardware.
   - Re-proving is deterministic and never reruns the bench.
6. **Publish.** Platform publishes the bundle and includes `statement_digest`
   in the next statement root.

### 6.5 Validator verification and its cost

For each new statement (cached by digest):

1. Policy check: `scoring_program_id`, measurements, `route_profile` and
   `config_digest` are in the pinned policy for `bench_version`.
2. Attestation: verify the COSE_Sign1 P-384 chain to the AWS Nitro root and
   the PCR set, then check that the document binds `runtime_key`. This is
   cached per boot key, so it costs milliseconds per boot *(inferred)*.
3. Statement signature under `runtime_key`.
4. Seed: run all six §4.5 anchor checks against the validator's **own** chain
   node. This closes G1 and G7. Reject on any mismatch. The seven bullets
   below map onto §4.5 checks 1–6; "Token" and "First match" together form
   check 4.
   - **Block:** finalized, and its hash matches the statement.
   - **Signer:** the extrinsic succeeded and was signed by the coldkey that
     owned `miner_hotkey` at the anchor block. On the credit path, the
     credited payment must also check out.
   - **Remark:** it equals `C` for this `artifact_sha256` and admission token.
     On the paid path, the batched transfer pays the quote.
   - **Token:** the issuance leaf is proven against an anchored root, with
     `issued_block ≤ anchor_block ≤ expires_block ≤ issued_block + W`, and
     the statement's `authorization` equals the leaf's.
   - **First match:** the anchor is the first matching commitment by
     `(block, index)` over blocks `anchor_block − W` through `anchor_block`,
     including lower indices in the anchor block itself.
   - **Scope:** the anchor and its spend identity back exactly one ticket.
     The spend identity is the anchor extrinsic on the paid path and the
     credited payment on the credit path. Replicates and authorized retries
     of that ticket may share the anchor.
   - **Beacon and seed:** the statement's `delta_s` equals the pinned `Δ`,
     and the pinned `Δ ≥ Δ_min`. Verify the drand BLS signature for `R` and
     the rule `R = first ≥ ts(anchor) + Δ`, then re-derive `s_j`.
5. Dataset: for public datasets, optionally regenerate and compare
   `dataset_sha256`. This is cached; cost **unknown**, seconds of CPU.
6. Receipt: about 25 ms to verify a 290 KB Ziren proof *(vendor)*, then check
   that the journal matches the statement.
7. Log: verify a Merkle inclusion proof for **each** of the statement's
   issuance, attempt and terminal leaves. Each proof must check against an
   epoch root already anchored on chain, read from the validator's own node.
   A statement with any leaf not yet in an anchored root stays pending and
   cannot verify. Then check the attempt policy for every attempt of this
   `(anchor, j)` up to and including the one being counted:
   - **Terminals are bound to signed records.** Each non-`aborted` terminal
     leaf's `outcome` equals the outcome of a record signed by an attested `K`
     whose PCRs are in policy. Its `record_digest` equals the hash in that
     attempt's terminal-commitment leaf, and the commitment leaf is itself in
     an anchored root.
   - **Scored means verified.** A `scored` outcome needs a RunStatement that
     passes steps 1–6, including the receipt.
   - **No relabelling.** If an attempt's terminal commitment points to a
     K-signed RunStatement, any non-scored terminal for that attempt is
     rejected. The replicate is flagged.
   - **Blind aborts only.** `aborted` is accepted only for an attempt with no
     terminal-commitment leaf.
   - **Suppression blocks the replicate.** A commitment whose record was never
     revealed marks the replicate suppressed, and no later attempt counts.
   - **Budget and authorization.** Retries after failures or aborts stay
     within `max_retries`, so `a ≤ 1 + max_retries`, and each has an
     authorized retry leaf. Commitment and abort for an attempt obey the
     serialized deadline rule (§6.2), checked from log order and stamps.
   - **Unverifiable scored statements quarantine.** A committed RunStatement
     that fails steps 1–6 is not counted, and the replicate is quarantined:
     no later attempt counts without an owner-reviewed dispute. A missing
     receipt only leaves the statement pending.
   - **Which attempt counts.** The first attempt with a verified `scored`
     outcome is the one counted.
8. Sign `VerifiedStatement{statement_digest, verdict, verifier_policy_digest, validator_hotkey}`
   with sr25519 and submit it.

For comparison, today each validator hosts eight sandbox slots on 160 GB of
disk and 32 GB of RAM, and holds 180-minute scoring leases. Under this design
it needs a CPU, a chain node or Pylon, and about 300 KB of download per
replicate.

### 6.6 Weight-setting integration

- **Replicates replace validator-scoped seeds.** Platform runs three hosted
  replicates `j = 0..2` with distinct seeds. "Median of three" keeps its
  meaning. As the private-execution v2 doc warns, several validators checking
  one receipt are **checks, not independent executions**. Replicates must be
  distinct attempts with distinct seeds and fresh sessions.
- **Finalization.** A replicate counts once `q` validators have signed it off.
  An agent finalizes on three counted replicates, as today.
- **Fold.** Each ledger entry carries its replicate `statement_digest`s. The
  epoch pin includes `verifier_policy_digest`. A validator folds an entry
  **only if** every referenced statement verified locally under that policy.
  Verification is deterministic, so honest validators exclude the same entries
  and compute identical weights. A disagreement can only come from an
  out-of-date verifier, and is handled by `fleet_ready` like any other
  protocol.
- **KOTH, CRN confirmation, bands, incumbency and ties are unchanged.**
  Confirmation runs become hosted replicates under the same log.

### 6.7 Failure and dispute handling

| Event | Handling |
| --- | --- |
| Receipt missing or prover failed | Entry not yet eligible; re-prove (deterministic; no new attempt) |
| Receipt invalid | Escalate: either `scorecore` and program id diverged (a release bug) or someone tampered. Quarantine the entry; Backroom shows it |
| Attestation invalid (unknown PCRs, all-zero debug PCRs, expired key) | Statement rejected; attempt treated as an infra failure; a retry needs a Backroom-authorized retry leaf (existing no-automatic-retry policy) and reuses the seed |
| Dangling attempt (registered, no terminal commitment by `deadline_block`) | Platform writes an `aborted` leaf through the serialized compare-and-append; a retry is the next attempt, uses one retry slot and needs authorization; the dashboard shows aborted attempts per agent |
| Runner commits just as the deadline passes | The log's atomic rule admits exactly one of commitment or `aborted`. A refused late commitment gets no receipt, the record is never released, and the attempt is `aborted` |
| Terminal commitment exists but the record was never revealed within `reveal_grace_blocks` | Replicate marked **suppressed**; no later attempt for that `(anchor, j)` counts; public, and resolvable only through an owner-reviewed dispute |
| Committed RunStatement fails verification (receipt fails, dataset hash does not regenerate, a field disagrees with the policy or the log) | Not counted; replicate **quarantined**; no later attempt counts without an owner-reviewed dispute; a rejected RunStatement can never be relabelled as a failure |
| Terminal leaf disagrees with its K-signed record (e.g. a signed RunStatement relabelled as `infra_failure`) | Rejected; replicate flagged; treated as evidence of operator tampering, not as an infra fault |
| Repeated aborts for one agent | Visible pattern; policy threshold to flag for review (open question) |
| On-chain anchor defect: the first `C`-carrying extrinsic failed to dispatch, had the wrong signer or a short payment, or came after token expiry; or the cited anchor is not that first extrinsic | Statement rejected and the **token is burned** (§4.5 authorization accounting). A paid transfer is not refunded and a reserved credit is forfeited. A new attempt needs a new token and authorization. Backroom shows it as a miner fault, not an infra retry |
| Extrinsic with a missing or non-`C` remark | Not a commitment for the token: consumes nothing, and the token stays open until a `C`-carrying extrinsic or expiry. Any transfer in it is not refunded |
| Statement-level defect on a sound anchor: `delta_s ≠` pinned `Δ`, `authorization` ≠ leaf, the anchor or spend identity cited by another ticket, or `Δ < Δ_min` in the policy | Operator fault. Statement rejected and replicate **quarantined** for owner review; the on-chain anchor stays this ticket's anchor |
| Anchored artifact never uploaded, or uploaded bytes do not hash to `artifact_sha256` | Logged as forfeited; the authorization is spent and does not return as a credit |
| drand unavailable at `R` | Wait (drand rounds are recoverable later); no fallback to an operator-chosen seed. The current CSPRNG fallback is not carried forward |
| Chain anchoring failed | Roots queue. Statements stay pending, and entries finalize only once every referenced leaf (issuance, attempt, terminal) is in an anchored root |
| Validator signs a forced-invalid canary | Its signoffs stop counting toward `q` for a cooling-off period; the event is public |
| Miner disputes a score | Owner-authorized witness access → native re-grade (4.2b). A mismatch with the journal is a P0 bug. A match with a claim of bad serving goes to a CRN audit run |
| Audit lands outside the noise band | Freeze the entry's eligibility pending review; it is a lead, not proof (serving noise) |

---

## 7. Phased rollout

Every phase lands default-off and shadow-first. Merge, deploy and activation
stay separate.

| Phase | Deliverable | Exit evidence |
| --- | --- | --- |
| **0: witness and purity** (no runtime behaviour change) | Extract `scorecore` as a pure package. Replace environment posture reads with an explicit `Config`. Define the scoring witness v1. Add a `rescore` CLI. Audit float determinism | `rescore(witness)` is byte-equal to the signed composite and report on ≥ 50 recent v17 production runs (operator-run against the private bucket); golden vectors unchanged for v2–v17; no `os.Getenv`, clock or network in `scorecore` (enforced by test) |
| **1: PoC**: prove the scoring step | Build `scorecore` as a Ziren guest. Execute one public practice-run witness, then prove and verify it | Measured cycles, prover hardware, wall-clock, proof size, verify time, ZK property; journal equals native `rescore` |
| **2: commitments** (also improves today's system) | Validators verify the seed block against their own chain node (G1). Anchor commitment extrinsic in the miner CLI and payment verifier (G7). Read and pin Subtensor's timestamp skew bound `S`. Beacon seed v2 as a new `seed_scope`. Attempt log, including issuance leaves, with public read API and Backroom read tool. Per-epoch chain anchor | Log inclusion proofs verify; anchor-check and seed v2 vectors pinned in Go and Python, including negative vectors (wrong signer, missing remark, earlier match, reused anchor, `Δ < Δ_min`); anchor visible on chain |
| **3: hosted shadow** | Attestable AMI runner (AWS Terraform module), runtime key and RunStatement. Hosted replicates run **beside** validator runs. Validators verify in shadow (new heartbeat protocol). Canaries | Hosted and validator composite distributions compared per agent; 0 verifier disagreements across the fleet; `shadow_only=true`, `weight_eligible=false` |
| **4: activation** (owner decision) | Proof-referenced ledger entries; validators stop leasing routine scoring tickets; audits on crown-moving decisions | `fleet_ready` at 100%; rollback means re-enabling validator leases (code path kept) |
| 5: optional | Self-hosted gpt-oss-20b in an attested GPU instance (needs a bench bump); threshold key custody for private datasets; zk-wrapped attestation only if an EVM consumer appears | Separate designs |

### PoC scope (Phase 1)

The smallest useful proof: **one public practice-run witness**, i.e. generated
dataset, transcript and broker evidence, with no private data, through
`scorecore` in Ziren.

Measure:

- guest cycles (executor only);
- prove time on 1 GPU and on N GPUs (GPU model recorded);
- proof size;
- native verify time;
- whether the proof hides the witness.

Confirm that the journal composite equals native `rescore` byte for byte.
Phase 0 is a hard prerequisite: until `scorecore` is pure and the witness is
complete (G4, G5), any proof would prove the wrong function. Boat runs Phase 0.
Proving needs a GPU host, and none was used for this document.

---

## 8. Cost and time estimates

| Item | Estimate | Basis |
| --- | --- | --- |
| Scoring proof cycles per run | **unknown** | Phase 1 measures it |
| Proving time at C cycles | C=1 G: 96 s (1×5090) / 15 s (8×5090); C=10 G: 16 min / 2.6 min; C=100 G: 2.7 h / 26 min | Ziren Geth throughput 10.4 MHz / 65.3 MHz *(vendor; inferred arithmetic)*. AWS does not offer RTX 5090s; throughput on AWS GPUs (L4, L40S, H100) is **unknown** |
| Proving price at C cycles | C=1 G: $0.03; 10 G: $0.30; 100 G: $3.00 | Boundless v1.4.0 example `min_mcycle_price = 0.00003 USD` (a config example, not a market price); public witnesses only. Ethproofs real-time provers report $0.0032–$0.0068 per Ethereum block proof (normalized GPU rate not published). Bonsai shut down 2025-12-17 |
| Run inference (unchanged) | example $1.82 per 350 cases; rank-1 agent averages about $0.46 per run | `PROTOCOL.md` v16 example (`cost_nano_usd 1816989710`); public leaderboard on 2026-11-07 |
| Hosted execution | **unknown** | Depends on instance type and slot density; today's envelope is 8 slots per 32 GB / 160 GB host (`docs/VALIDATOR.md`) |
| Attestation | EC2 cost only for Nitro Enclaves; EC2 instance attestation pricing **unknown** | AWS docs; announcement silent |
| Validator verification | about 25 ms + a few ms of signature checks + about 300 KB | Ziren *(vendor)*; *(inferred)* |
| zk-wrapped attestation (skip) | 66–390 M cycles; 220–258k gas | Automata README |
| zkML inference (reject) | ≈ 60.6 H100-hours per example run, at a 1,024-token context limit | FlashProve *(inferred arithmetic)* |
| Monthly budget | **unknown** | Needs runs/day (open question 10) |

---

## 9. Jira-ready breakdown

Epic: DITTO-2543. Sizes are rough: S ≤ 2 days, M ≤ 1 week, L > 1 week.

| # | Summary | Acceptance | Depends | Size |
| --- | --- | --- | --- | --- |
| 1 | Extract `scorecore`: pure DittoBench scoring package | v13 claim provenance, catalog gate, v16 priced cost, twin post-pass and aggregation live in one package with explicit `Config`; `go test ./...` green; report bytes unchanged on golden vectors v2–v17; a test forbids environment, clock and network imports | — | L |
| 2 | Scoring witness v1 schema | Canonical encoding of transcript + broker evidence + base evidence + projection commitment; Merkle `witness_root`; emitted privately beside the transcript; schema test vectors | 1 | M |
| 3 | `rescore` CLI and production equality sweep | Byte-equal composite and report on ≥ 50 recent v17 runs; inspected/equal/mismatch counts printed; mismatches triaged | 1, 2 | M |
| 4 | Float determinism audit | FMA hazards fixed with explicit conversions or documented; cross-arch equality test (amd64, arm64, mips32 softfloat under qemu) | 1 | S |
| 5 | Ziren PoC: execute `scorecore` guest | Guest builds; executor cycle count for one practice witness recorded | 1–4 | M |
| 6 | Ziren PoC: prove and verify | Proof time / GPU / size / verify time recorded; ZK property determined; journal equals native `rescore` | 5 | M |
| 7 | Verifier library spike | Go verifier callable from the validator (cgo or sidecar); latency measured | 6 | S |
| 8 | Validator checks seed block against its own chain node | Closes G1; refusal code on mismatch; test with a forged hash | — | S |
| 9 | Beacon seed derivation v2 (`seed_scope: beacon-v2`) | drand quicknet BLS verification in Go and Python; shared vectors; the rule is "first round ≥ anchor block timestamp + Δ"; validator code rejects `Δ < Δ_min` (with `S` read from the Subtensor runtime and pinned); no Platform-assigned input; retries reuse the seed | 8, 20 | M |
| 10 | Public attempt log | Append-only Merkle log of issuance, attempt, terminal-commitment and terminal leaves; inclusion proofs served per leaf; public read API; Backroom read tool; aborted-attempt leaves | — | M |
| 11 | Per-epoch chain anchor | One `commitments` write per epoch with the epoch root; anchor hotkey decided; monitor for missed anchors | 10 | S |
| 12 | Attestable AMI hosted runner | Reproducible image (scorer + sandbox-docker + broker); reference PCRs published by release; AWS Terraform module through protected plan/apply | 1 | L |
| 13 | Runtime key, RunStatement v1 and FailureStatement | Key generated in the runner and bound by the NitroTPM document. Statement and failure schemas, both signed by `K`; failure class decided by runtime code. The runner commits `SHA-256(record)` to the log and waits for the receipt before releasing any terminal record. Test: Platform cannot obtain the record before the commitment exists | 2, 10, 12 | M |
| 14 | Hosted shadow lane | Platform issues hosted replicates beside validator runs, `shadow_only`; distribution comparison report per agent | 9–13 | L |
| 15 | Validator verify-and-sign (shadow) | New heartbeat protocol; the full §6.5 check including step 7's attempt policy; signoff submission; forced-invalid canaries. Negative vectors: a signed RunStatement relabelled as a failure; `aborted` after a terminal commitment; a commitment stamped after `deadline_block`; both a commitment and an `aborted` for one attempt; a committed record never revealed; attempt `a > 1 + max_retries`; a terminal leaf whose digest does not match its commitment; a committed RunStatement whose dataset hash does not regenerate (must quarantine). Positive vector: an abort at `a = 1` followed by a scored `a = 2` with `max_retries = 1` | 6, 7, 13 | M |
| 16 | Activation policy and ledger `proof_ref` | Deterministic exclusion rule; `fleet_ready`, no force; rollback path tested | 14, 15 | M |
| 17 | Crown-decision audits | Beacon-sampled audit selection; paired CRN comparison; Backroom surface | 14 | M |
| 18 | Dispute workflow | Quarantine and re-prove tools; owner-gated witness access | 15 | M |
| 19 | Article and X draft (after the Phase 1 numbers) | Per the feature-article convention; owner Peyton | 6 | S |
| 20 | Anchor commitment extrinsic | The miner CLI submits `utility.batch_all[transfer_keep_alive, system.remark(C)]` (a remark alone for credits). The payment verifier accepts and verifies the batch and records `(anchor_block_hash, anchor_extrinsic_index)`. Issuance leaves are logged with `expires_block ≤ issued_block + W`. A shared Go/Python anchor-check library covers all six §4.5 checks, including the anchored issuance inclusion proof and the fixed-window first-match scan by `(block, index)` from `anchor − W` through the anchor block's lower indices, with negative vectors: post-dated issuance; an unanchored leaf; an earlier commitment in an earlier block; an earlier commitment at a lower index in the anchor block; a token expired at the anchor; `delta_s` ≠ the pinned Δ; a credit authorization in two issuance leaves; one anchor cited by two tickets. Also a positive vector where three replicates and an authorized retry share one anchor. The forfeited-anchor path is logged and never re-credits | 10, 11 | M |

---

## 10. Open questions for Peyton

1. **AWS or GCP?** Subnet infrastructure is GCP and Cloudflare today, with no
   AWS stack in `infra/terraform`. GCP Confidential Space attests a container
   digest directly. AWS needs an Attestable AMI, built and measured by us. Is
   AWS a requirement or an example?
2. **Inference trust.** Is it acceptable that inference integrity stays
   "Platform relay + provider" (as today, unprovable)? Or should we fund
   self-hosted gpt-oss-20b in an attested GPU instance? That changes the
   serving contract and the v16 price constants, so it needs a bench bump.
3. **Audit budget.** Keep any independent re-execution? Proposal: always for
   crown-moving decisions, plus a small sampled `p`. Who runs audits:
   volunteer validators, a paid auditor, or nobody?
4. **Signoff quorum `q`.** How many validators must verify each replicate? What
   happens to validators that never verify (vtrust)?
5. **Transcript publication.** Should finalized witnesses become public (the
   #2204 mirror setting)? If yes, anyone can re-grade natively. If no, the zk
   proof is the only third-party check.
6. **Conflict of interest.** If any operator-affiliated agent is ever eligible
   for emissions, the "malicious operator" row is no longer hypothetical, and
   audits should be mandatory rather than sampled.
7. **Anchor hotkey.** Which registered SN118 hotkey writes the per-epoch
   commitment (the pallet requires a registered hotkey)?
8. **Private dataset custody.** Are we comfortable that confidentiality from
   the operator is organizational? Or do we want threshold custody, e.g.
   validators holding key shares released only to attested measurements?
9. **Vendor risk.** Ziren is the only zkVM with a credible real-Go benchmark.
   Is a single-vendor dependency acceptable, versus making a Rust port *the*
   production scorer?
10. **Volume.** Runs per day per bench, needed to turn §8 into a monthly
    budget.
11. **Anchor extrinsic and credits.** Is it acceptable to require miners to pay
    with a `batch_all` that carries the artifact commitment? That means a miner
    CLI update and a payment-verifier change. Should credit-funded uploads stay
    eligible for proof-backed scoring? A colluding Platform–miner pair can
    still give a token a real lifetime longer than `W` and log it late, hiding
    an earlier commitment (§4.5, residual risk). The anchored log makes the
    claimed issuance immutable, but it cannot prove when a token was really
    handed out. What `W` should be is part of this question.

---

## 11. Sources

All read on 2026-11-07 unless stated. *(vendor)* = self-reported;
*(snippet)* = search excerpt, page did not load; *(secondary)* = third-party
summary.

Monorepo (at `999f23d`): `services/dittobench-api/PROTOCOL.md`,
`services/dittobench-api/docs/{seed-and-scoring,judge-determinism,calibration-trust}.md`,
`services/dittobench-api/cmd/dittobench-api/transcript.go`,
`ditto/validator/{signing,onchain_seed,worker,dittobench}.py`,
`apps/platform/ditto/api_server/endpoints/{screener,validator,public,upload}.py`,
`apps/platform/ditto/api_server/payment_verifier/verifier.py`,
`apps/platform/ditto/api_models/upload.py`, `apps/platform/ditto/db/models.py` (`EvaluationPayment`),
`docs/{VALIDATOR,no-automatic-retries,coding-platform-private-execution-v2}.md`,
`.agents/skills/ditto-subnet-benchmark/references/bench-runtime.md`, PR #2204 and issue #464.
Public API: `https://platform-api.heyditto.ai/api/v1/public/{bench/config,leaderboard,agent/{id}/scores,bench/transcript/{sha}/telemetry}`.

zkVMs:
- ZKM Ziren performance (Geth/Reth throughput, proof size, verify time;
  measured 2026-10/11) *(vendor)*: https://docs.zkm.io/introduction/performance.html
- Ziren Go support v1.2.0 (2025-10-21): https://www.zkm.io/blog/ziren-v1-2-0
- Ziren Linux ABI limits: https://docs.zkm.io/mips-vm/linux-abi.html
- SP1 Hypercube on 16× RTX 5090 (2025-11-18): https://blog.succinct.xyz/real-time-proving-16-gpus/
- SP1 proof types (Groth16 ~260 B, PLONK ~868 B): https://docs.succinct.xyz/docs/sp1/generating-proofs/proof-types
- RISC Zero cycle costs (SHA-256 68 cycles/block, paging, float 60–140): https://dev.risczero.com/api/zkvm/optimization
- RISC Zero receipts are ZK: https://docs.rs/risc0-zkvm
- RISC Zero 3.0 ~0.99 MHz on RTX 4090 *(snippet)*: benchmarks.risczero.com
- Jolt >1 MHz on 32-core CPU: https://eprint.iacr.org/2025/105
- Ethproofs cost per proof: https://ethproofs.org
- Boundless v1.4.0 example pricing: https://github.com/boundless-xyz/boundless/releases/tag/v1.4.0
- Bonsai shutdown: https://docs.boundless.network/developers/tutorials/bonsai
- TinyGo stdlib support (`encoding/json` failing): https://tinygo.org/docs/reference/lang-support/stdlib
- SP1 STARK non-ZK before wrap *(secondary)*: https://medium.com/@gwrx2005/comparative-analysis-of-sp1-and-risc-zero-zero-knowledge-virtual-machines-4abf806daa70
- Go spec, floating-point fusion: https://go.dev/ref/spec#Arithmetic_operators

TEEs:
- Nitro Enclaves attestation, PCRs, debug mode: https://docs.aws.amazon.com/enclaves/latest/user/set-up-attestation.html
- Nitro Enclaves constraints and pricing: https://docs.aws.amazon.com/enclaves/latest/user/nitro-enclave.html
- Attestation document format: https://github.com/aws/aws-nitro-enclaves-nsm-api/blob/main/docs/attestation_process.md
- EC2 instance attestation: https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/nitrotpm-attestation.html
- NitroTPM document contents: https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/nitrotpm-attestation-document-content.html
- EC2 instance attestation GA (2025-09-23, GPUs included): https://aws.amazon.com/about-aws/whats-new/2025/09/aws-announces-ec2-instance-attestation
- SEV-SNP on EC2: https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/sev-snp.html
- GPU enclaves not supported (2024-04-16): https://aws.amazon.com/blogs/machine-learning/a-secure-approach-to-generative-ai-with-aws
- Automata Nitro verifier cycles and gas: https://github.com/automata-network/aws-nitro-enclave-attestation
- GCP Confidential Space token claims: https://docs.cloud.google.com/confidential-computing/confidential-space/docs/reference/token-claims

zkML:
- FlashProve (2026-11-02): https://www.anthropic.com/research/provable-inference
- zkLLM: https://arxiv.org/html/2404.16109
- ZKTorch: https://arxiv.org/html/2507.07031v2
- DeepProve: https://github.com/Lagrange-Labs/deep-prove
- Jolt Atlas: https://github.com/ICME-Lab/jolt-atlas
- gpt-oss model card: https://arxiv.org/pdf/2508.10925

Randomness and chain:
- Subtensor proof of authority (Aura/GRANDPA): https://bittensor.com/docs/concepts/chain-consensus
- Substrate insecure randomness: https://github.com/paritytech/substrate/tree/master/frame/insecure-randomness-collective-flip
- drand quicknet: https://docs.drand.love/blog/2023/10/16/quicknet-is-live
- Commitments pallet sizes: https://github.com/RaoFoundation/subtensor/blob/main/pallets/commitments/src/types.rs
- Bittensor timelock (CR v3): https://bittensor.com/docs/guides/timelock

Optimistic verification:
- Truebit: https://people.cs.uchicago.edu/~teutsch/papers/truebit.pdf
- Proof of Sampling: https://arxiv.org/html/2405.00295v3
- opML: https://arxiv.org/abs/2401.17555
