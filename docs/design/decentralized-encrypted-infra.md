# Decentralized encrypted infra on Mac minis: threat model and pilot plan

| | |
|---|---|
| **Status** | Design and research only. **Not approved for implementation.** Peyton decides the direction. |
| **Tickets** | Epic [SN-472](https://omniaura-team.atlassian.net/browse/SN-472), this doc [SN-473](https://omniaura-team.atlassian.net/browse/SN-473) |
| **Owners** | Peyton Spencer (assignee), Omar Barazanji (co-owner) |
| **Builds on** | Omar's synthetic query-adapter pilot: [#3483](https://github.com/ditto-assistant/ditto-subnet/pull/3483) (contract and loopback miner), [#3484](https://github.com/ditto-assistant/ditto-subnet/pull/3484) (Ditto-side client), [#3485](https://github.com/ditto-assistant/ditto-subnet/pull/3485) (fixtures, proof, bench) |
| **Measurements** | [`experiments/decentralized-encrypted-infra/`](../../experiments/decentralized-encrypted-infra/README.md): throwaway scripts, synthetic data only, run on an Apple M4 Pro Mac mini |
| **Date** | 2026-11-11 |

## Summary

The question: can people with home Mac minis install Ditto Desktop, opt in to a
bundled miner, host storage and compute for Ditto users, and earn small daily
SN118 payouts, **while user data stays safe from a fully malicious miner**?

**Yes for storage. Yes for a narrow kind of compute. No for most of what
"run Ditto on miners" suggests.** Measured on a Mac mini (M4 Pro, 14 cores):

| | Feasible today on a Mac mini? | Evidence |
|---|---|---|
| Encrypted, erasure-coded blob storage with Ditto-held keys | **Yes.** The client side costs nothing that matters: AES-256-GCM seals 5.5 GiB/s per core, Reed–Solomon 10-of-30 encodes 11.4 GiB/s, and a storage proof is a 4.4 KiB response that verifies in 2.8 µs. Home bandwidth and churn are the real limits. | §3, [E-storage] |
| Proofs of storage and availability sampling that validators can check without secrets | **Yes.** Merkle challenges against published roots. 459 random challenges catch a 1% loss at 99% confidence. | §3.3, [E-durability] |
| The per-user query adapter (768→128→768, GELU) on an **encrypted query** (CKKS) | **Yes, but about 10,000× plaintext cost.** 184 ms per query on one core, 47 queries/s on 10 cores, and it **passes the pilot parity bar** (max abs 2.3e-8 against `reference.apply`). Each query is 1.75 MiB in and 256 KiB out, plus 378 MiB of evaluation keys per miner. | §4.2, [E-ckks] |
| The same adapter with **encrypted per-user weights** | **Technically yes, impractical at scale.** 340 ms per query, but 290 MiB of ciphertext per adapter version (376× the weights). | §4.2 |
| Encrypted similarity over a **private** embedding corpus | **Batch-only.** 292 ms per 10k vectors per core, with ciphertext 21× the size of the float32 corpus. | §3.5, §4.2 |
| TFHE for the adapter | **No.** 19.5 s per query on 14 cores and 785 MiB of keys. 6-bit quantization misses the parity bar by about 470×. | §4.2, [E-tfhe] |
| Rotated or randomly projected embeddings, split inference | **No.** Linear maps are recoverable, and embeddings invert to text. Our backend design already rejected rotation for this reason. | §4.4 |
| Secret-sharing MPC across anonymous miners | **No** for confidentiality: it needs non-collusion, and an SN118 UID costs 0.0005 TAO. **With Ditto as a party** it is secure but offloads no compute. | §4.3 |
| LLM inference on private data, or full user cloud computers, on miner hardware | **No.** No confidentiality on an owner-controlled box. FHE and MPC for transformers cost minutes and gigabytes per token. | §2.2, §4.3 |
| One SN118 UID per Mac | **Pilot: yes, under Null consensus.** SN118 runs Yuma today: 256 of 256 UIDs are in use, the burn is 0.0005 TAO, and immunity is 300 blocks. Null raises capacity to `floor(2500 / mechanism count)` and lets the elected validator pay every Mac exact integer shares. **Long term: compute and storage pools**, because 2,500 is not the ceiling a growing network needs. | §5.6, [E-chain], [BT-null] |

Four findings matter more than any single number:

1. **When Ditto decrypts, FHE on miners never saves Ditto compute.** Ditto
   holds the plaintext and computes the adapter in about 20 µs. The encrypted
   path costs about 10,000× that, plus megabytes on the wire. Ditto can
   therefore verify every result for free. That makes the work verifiable,
   but it is the same fact that makes it pointless as an offload. Encrypted
   compute earns its keep only in two cases:
   - as emission-funded work that leaks nothing (§6);
   - when **the user's device holds the key** (true end-to-end encryption).
     There, neither Ditto nor the miner sees plaintext. Apple uses this pattern
     to look up landmarks and caller IDs privately [Apple-HE-2024]. It is also
     the capability our own E2EE design says is missing [I-E2EE].
2. **Storage is the only track with real resource value, and its market value
   is small next to SN118 emissions.** Several terabytes of erasure-coded
   storage at market rates costs well under 1% of the current miner pool. A
   storage track must price units at a market reference and **burn the
   surplus**, or it pays for useless work (§6).
3. **The threat model is clean only because miners hold no secrets.** Nothing
   on a miner's box needs to stay hidden from its owner: not keys, not
   plaintext, not secret-dependent code. That one rule makes "assume the miner
   controls the box" survivable. It also removes TEEs, signed binaries and the
   Secure Enclave from the confidentiality story (§2).
4. **Mac minis are one more track, not a new subnet.** SN118 already splits its
   miner pool across tracks: memory KOTH 70%, the generator competition 30%,
   Gamma buckets, and a burn setting [I-status]. The infra track takes a share
   of the same allocation, so the agent and data-generation competitions stay
   switchable. One catch: Null consensus, which per-Mac UIDs need, is a
   **subnet-wide** mode. Switching to it changes how every track settles,
   because only the top-stake validator's weights count (§5.6, §8).

**Recommendation.** Extend Omar's loopback pilot in two synthetic, unpaid phases:

- **Phase 1:** encrypted, erasure-coded storage on team and volunteer Macs.
- **Phase 2:** the CKKS adapter as a verifiable compute lane.

Neither phase uses real user data or payouts. Each has its own kill criteria
(§7). Keep the agent and generator competitions running beside the pilot. The
infra track gets a share only after Phase 1 meets its metrics. The decisions
that change the design are listed in §8, starting with **who should hold the
decryption key** and **whether SN118 moves to Null consensus**.

## Contents

1. [Context](#1-context)
2. [Threat model](#2-threat-model)
3. [Storage track](#3-storage-track)
4. [Compute track: unintelligible but useful work](#4-compute-track-unintelligible-but-useful-work)
5. [Ditto Desktop integration](#5-ditto-desktop-integration)
6. [Economics](#6-economics)
7. [Phased pilot plan](#7-phased-pilot-plan)
8. [Open questions for Peyton and Omar](#8-open-questions-for-peyton-and-omar)
9. [Sources](#9-sources)

## 1. Context

### 1.1 The ask

From SN-472, in Peyton's words:

> build on Omniaura's draft stack and focus on things that we can host on Mac
> Minis and not things that we can't host. I'm thinking about sharded
> databases, sharded encrypted private databases, and how we can encrypt and
> shard the computer over the network. The compute operations are
> unintelligible but they're doing the work required and can be decrypted on
> our private servers.

The Ditto Desktop app would ship an opt-in miner. Owners contribute storage and
compute and earn small daily SN118 payouts. This could replace the agent
competition, which is not paying off as a miner incentive right now.

Peyton added two requirements while this doc was being written:

- **The subnet must not become Mac-only.** SN118 must still be able to turn on
  the agent competition or the data-generation competition alongside the Mac
  track (§6.4).
- **Per-Mac UIDs need Null consensus**, which allows up to 2,500 UIDs. That
  holds for the pilot. A growing network outgrows 2,500, so compute pools stay
  the long-term design (§5.6).

The two Wispr notes from 2026-11-11 (Subnet Leaderboard Debate; Seby + Ditto
sync) could not be read for this doc:

- The share pages render in the browser.
- Their API returns 404 without a viewer session.

This doc therefore works from the SN-472 summary of those notes. That summary
covers:

- the Mac mini vision;
- a signed desktop binary with a contribute button;
- an embedded bittensor wallet;
- a tailscale-like network;
- Nick's OIDC and binary-id idea;
- a rotated embedding space;
- data-availability sampling with about 5 copies;
- dropping competitions;
- miners hosting tiny models or encrypted storage;
- "the E2E encryption we have now".

### 1.2 What already exists

**Omar's pilot (draft, synthetic, loopback)** [I-pilot]:

- `query-adapter/v1`: Ditto sends an authorized 768-d query and an exact
  `(tenant, version, digest)`. The miner returns the adjusted vector.
- Ditto keeps:
  - authorization;
  - HMAC-keyed sampled verification against its own reference;
  - local fallback;
  - a usage ledger that always reports `payouts: "none"`.
- Miners see only an opaque `syn-<sha256>` tenant alias.
- The runbook lists the private path as future work: attested confidential
  compute, encrypted query and response, and no plaintext on untrusted hosts.
- Local measurements on an M5 Pro: 0.0175 ms p50 for the adapter in process,
  and 0.17 ms through the loopback miner [I-pilot-measure].

**Ditto's encryption today is at rest, not end to end.** DITTO-5 ships
server-side envelope encryption of memories, opt-in per graph:

- a random per-graph secret wrapped under the user's DEK;
- a vault passphrase and recovery kits;
- crypto-shredding by deleting the wrapped secret [I-memcrypto].

The product copy says "At-rest protection, not end-to-end encryption". The
private-search pilot decrypts on the server [I-app-encryption].

Two consequences for this doc:

- **Embeddings stay in plaintext, deliberately.** The backend design tried a
  per-graph orthogonal rotation of the embedding space and rejected it.
  Rotation is a linear map, recoverable from about 768 known pairs, and it
  preserves the Gram matrix, so a public corpus aligns against it
  [I-memcrypto §3.5].
- **The E2EE design is written but not built.** It is graph keys the server
  does not hold. Its feature matrix finds 13 of 79 capabilities that need "a
  server principal or an enclave" to keep working [I-E2EE]. Encrypted compute
  on someone else's machine is one candidate for those 13.

**Ditto already stores encrypted data on Bittensor.** SN-49 sends nightly
`age`-encrypted `pg_dump`s to a private bucket on Hippius (SN75) [I-SN49].
Hippius's storage network, Arion, already runs almost the design in §3
[Hippius]:

- Reed–Solomon with k = 10 and m = 20, in 2 MiB stripes.
- A Merkle commitment per piece.
- A warden challenges 4 random chunks every 30 seconds, and miners answer with
  a zero-knowledge proof within 60 seconds.
- Pricing is $6 per TB-month with no egress fees.

Its miners must run Ubuntu 22.04 or newer, so there is no macOS miner. A Ditto
storage track therefore competes with Hippius, Storj, Sia and Filecoin, not
with an empty market. The case for building one rests on what Hippius does not
offer:

- miners that run natively on Macs inside Ditto Desktop;
- the compute track;
- the product link to Desktop users.

It does not rest on storage itself (§8, Q6).

### 1.3 Scope

**In scope:** only what a Mac mini can host at useful latency and cost.

- encrypted, sharded storage;
- small leveled-FHE compute: per-user adapters and embedding similarity;
- verification;
- desktop packaging;
- economics.

**Out of scope:**

- LLM inference on private data on miner hardware;
- running whole user cloud computers;
- anything that needs a miner to see plaintext.

No production code, merges, payouts, accounts, or infrastructure come from this
doc.

## 2. Threat model

### 2.1 Assets

| ID | Asset | Why it matters |
|---|---|---|
| A1 | Memory content, attachments, artifacts, backups | The user's private data |
| A2 | Embeddings (768-d, `text-embedding-005`) | Invert to approximate text [Morris-2023] |
| A3 | Per-user adapter weights | Trained on the user's (query → memory) pairs [I-adapter], so they encode the user's interests |
| A4 | Query vectors and adapter outputs | Embeddings of what the user is asking right now |
| A5 | Metadata: tenant, timing, sizes, access patterns | Linkable behavior even when content is hidden |
| A6 | Ditto keys: KEKs and DEKs, CKKS secret keys, MAC keys | Open everything above |
| A7 | Payout integrity | Emissions must buy real work |
| A8 | Availability and durability | Withholding or losing data hurts users even when nothing leaks |

### 2.2 Trust assumptions

- **Ditto's private servers are trusted.** They hold every key, decrypt, and
  verify. This is Peyton's framing ("decrypted on our private servers"). It
  gives "promise A" in the backend's taxonomy: protection from storage
  operators. It does **not** give protection from Ditto itself
  [I-memcrypto §3.1]. User-held keys would be a separate, stronger product
  (§8, Q1).
- **A miner fully controls its box.** It has root. It can:
  - disable System Integrity Protection;
  - attach a debugger, dump memory, or patch the binary;
  - inject libraries;
  - run many identities;
  - lie about its hardware;
  - record every byte it sees, forever.

  Code signing and notarization prove who built a binary, not what runs:
  - "Notarization of macOS software is not App Review." It is an automated
    malware and signing check [Apple-notarization].
  - The hardened runtime protects a process "along with System Integrity
    Protection".
  - The owner can turn SIP off from Recovery mode [Apple-SIP]. Apple DTS says
    that "disables some of these protections" [Apple-DTS].

  Apple hardware offers no general-purpose TEE either:
  - The Secure Enclave is an isolated subsystem that boots only Apple-signed
    sepOS. Apple documents no way for an app to run code in it: apps can only
    ask it to create and use keys [Apple-PSG] [Apple-CryptoKit].
  - Private Cloud Compute is not open to third-party workloads. Small
    developers may call Apple's own models on it, but cannot deploy code there
    [Apple-PCC].
  Trusted execution environments give integrity, not privacy, against the
  machine's owner (Dan Ivanov, 2025-10-15). In 2025 that held even for server
  TEEs [TEE-2025]:
  - Battering RAM, a $50 DDR4 interposer, broke Intel Scalable SGX and AMD
    SEV-SNP attestation.
  - WireTap recovered SGX's attestation signing key.
  - TEE.fail, on DDR5, forged Intel TDX quotes and pulled keys out of
    SEV-SNP VMs.

  Intel and AMD treat physical interposers as out of scope, and a volunteer who
  owns the box is exactly that attacker.

  **Design rule: nothing on a miner may need to stay secret from its owner.**
- **Validators** follow the protocol, may collude with miners, and see only
  public commitments and proofs. SN118 validators stay stateless; Platform owns
  queues, leases, and persistence (`CLAUDE.md`).
- **The network** is passive and observable: the ISP, the miner's own LAN, and
  anyone who can watch timing.

### 2.3 Threats and mitigations

| # | Threat | What the attacker can do | Mitigation in this design | What is left |
|---|---|---|---|---|
| T1 | **Malicious single miner** | Read everything on its box. Tamper with stored data or results. Replay. Answer garbage. | Only ciphertext reaches the box: AES-256-GCM shards or CKKS ciphertexts under Ditto keys. AEAD tags and Merkle roots catch tampering. Ditto decrypts and verifies every compute result (§4.5). | Sizes, timing, the tenant alias, and its own uptime pattern |
| T2 | **Colluding miners** | Pool everything they hold | Confidentiality never depends on miners not colluding: the keys stay at Ditto. All miners together still hold only ciphertext. | They can make data unavailable together (T7). See the sybil table in §3.4. |
| T3 | **Sybils** | Run many identities cheaply: 0.0005 TAO per SN118 UID [E-chain], or no TAO at all if fee-free PoW registration is enabled [BT-null]; Ditto accounts are free. Concentrate shards. Farm payouts. Fake redundancy ("5 copies" on one disk). | Erasure coding with large n, so one owner's share of an object stays small. Placement diversity by IP prefix, ASN, account and enrollment age. Per-account caps. Payouts per verified unit, not per identity (§5.6). Apple App Attest is not available on Macs, so devices cannot be attested [Apple-AppAttest]. | Determined sybils with residential proxies. **Assume they exist** and size n−k against them. |
| T4 | **Tampered or debugged binary** | Run a modified miner. Read its memory. Skip work. | Nothing secret ships in the binary. Every guarantee comes from cryptography or verification, not from the binary's integrity. Signing, notarization, and any device attestation only add sybil friction. | None for confidentiality. Correctness relies on verification. |
| T5 | **Traffic analysis** | Link a tenant alias across requests. Correlate with a user's activity. Learn object sizes and access frequency. | Fixed size classes and padding. Batch and delay non-interactive work. Spread one tenant across many miners and many tenants per miner. Rotate aliases per epoch. Never send a stable user ID. | Coarse activity patterns. Leakage-abuse attacks turn access patterns into content (§3.5), so access-pattern hiding is a separate, costly layer. |
| T6 | **Side channels** | Timing, cache, power, or prefetcher attacks on the miner's own CPU | **Not a confidentiality risk on miners:** they hold no keys, and homomorphic evaluation is data-oblivious. The risk moves to wherever decryption happens: Ditto's servers, or the user's device under user-held keys. Apple-silicon data-memory prefetchers leaked keys from constant-time OpenSSL, Go RSA, Kyber and Dilithium on M1, with similar behavior on M2 and M3 [GoFetch-2024]. Decryption hosts therefore need hardened crypto and no co-tenancy: non-Apple servers, or M3 or later with DIT enabled. That DIT disables the prefetcher on M3 is the GoFetch authors' finding; Apple does not document it. | Decryption hosts, not miners |
| T7 | **Data loss or withholding** | Churn, deletion, ransom ("pay me or I drop your shards") | Reed–Solomon with lazy repair, continuous challenges, and repair from any k shards (§3.3–3.4). Withholding miners lose payouts and are evicted. | Correlated loss (a region-wide outage, one owner running many identities) |
| T8 | **Lazy miners faking work** | Storage: discard data and answer from elsewhere. Compute: return a random or cached ciphertext. | Storage: random challenges with deadlines. Shards are unique, so no peer holds the same bytes. Only Ditto can create shards, so a miner cannot regenerate them. Compute: Ditto decrypts and checks results against its own plaintext computation, plus canaries and deterministic redundancy (§4.5). | A miner that outsources storage to its own cloud bucket. That is acceptable unless we pay a premium for home hosting. |
| T9 | **Decryption-oracle and reaction attacks** | Submit crafted ciphertexts and watch whether Ditto accepts or rejects them. FHE is malleable, and accept/reject feedback can leak the key over many queries [Chenal-Tang-2014]. Apple's HE library warns that BFV "does not provide IND-CCA security" and that leaking decryption results gives a key-recovery oracle [Apple-swift-HE]. CKKS also leaks the key when **decrypted values** are shown to the adversary [Li-Micciancio-2021]. | Never return decrypted values or per-request verdicts to miners. Settle payouts per epoch in aggregate. Evict a miner on its first verified tamper. Rotate CKKS keys on a fixed schedule. Flood noise before any decrypted value leaves Ditto [LMSS-2022]. | Must be designed in from the start, not bolted on |
| T10 | **Harvest now, decrypt later** | Keep every ciphertext forever and wait for a key leak or a cryptanalytic break | Symmetric AES-256 for data at rest; no public-key encryption of stored data. Keys never leave Ditto's KMS. Erase data by destroying the key (crypto-shredding). The memcrypto design already uses a random per-graph secret for this [I-memcrypto §3.3]. CKKS rests on RLWE, believed to resist quantum attacks. | A leak of Ditto's keys exposes every ciphertext that miners ever kept. **Deleting data on miners can never be verified.** |
| T11 | **Dishonest operator (Ditto)** | Forge storage proofs for a favored miner: Ditto holds the plaintext and could regenerate shards | Validators issue challenges, and miners sign responses with their hotkeys, so forging requires the miner's key. Publish placement commitments. | Collusion between Ditto and one miner. This is the same oracle trust as today's Platform. |
| T12 | **Compromised desktop update channel** | A malicious update reads the volunteer's own Ditto data or keychain | Ship the miner as a separate helper with minimal entitlements and no access to the app's account vault. It runs under its own signing identity and updates on a separate, staged channel (§5.2). | Same supply-chain risk as the app itself |
| T13 | **Abuse and legal exposure of volunteers** | Volunteers host opaque ciphertext they cannot inspect | Only Ditto-generated data. No third-party uploads. Clear terms. | Jurisdiction-specific. Needs counsel (§8, Q7). |
| T14 | **Weight-setter capture or outage under Null consensus** | Under Null, only the single highest-stake validator's row sets miner rewards, for **every** track [BT-null]. Whoever out-stakes the current leader (a plurality, where Yuma needs a stake majority) redirects all miner emissions. If the elected row is empty or all zero, miner rewards are split equally across every registered UID, which pays sybils as much as working Macs. | Keep the elected validator Ditto-operated or tightly trusted, with high availability. Always include the burn destination in its row, so the row is never empty. Monitor the elected permit holder, the age of its row, and stake movements, and alert in Backroom. Rehearse failover on testnet (§7). | Plurality stake capture. Requires a governance decision (§8, Q2). |

### 2.4 What we can and cannot promise

**Can promise**, assuming Ditto's keys and servers stay uncompromised:

- A miner, or every miner together, learns nothing about stored content or
  computed values beyond sizes, timing, and which opaque tenant alias is
  involved.
- Tampering is detected. Stored data survives the loss of any n−k shard
  holders.

**Cannot promise:**

- Protection from Ditto itself.
- Deletion from miners. Erasure means destroying the key.
- Hidden access patterns, unless we pay for ORAM or PIR.
- Availability against an adversary that controls more than (n−k)/n of an
  object's holders.
- That volunteers' machines stay unharmed, or that they are paid fairly, if
  the economics in §6 are wrong.

## 3. Storage track

### 3.1 Object pipeline

```
Ditto private server (holds all keys)                        miners (hold ciphertext only)
──────────────────────────────────────────────────────────   ─────────────────────────────
object ─► pad to size class ─► AES-256-GCM (random per-object DEK,
          AAD = object id ‖ version ‖ size class)
       ─► DEK wrapped by a KEK in KMS (crypto-shred = delete wrap)
       ─► Reed–Solomon k-of-n ─► Merkle root per shard ─► place n shards ─► n distinct miners
       ─► record {object, version, k, n, shard roots, holders}
          in Platform; publish shard roots for validators
```

- **Keys:** reuse the memcrypto pattern. A random per-object or per-graph
  secret is wrapped under keys Ditto controls. Destroying the wrap erases the
  object everywhere [I-memcrypto §3.3]. Miners never see a key.
- **Binding:** Platform records each shard's Merkle root under
  `(object id, version, shard index)`, so a miner serving object A's shard 3
  as object B's fails the root check at once. The AEAD tag, with AAD
  `(object id, version)`, catches anything that survives decoding.
- **Padding:** round objects up to a few size classes. Ciphertext length is
  otherwise an exact plaintext-size leak [I-memcrypto §3.8].
- **"No miner holds a meaningful piece":** Ditto holds the keys, so a full
  ciphertext is already meaningless. With k-of-n coding each miner holds 1/k
  of one, and any k miners together still hold only ciphertext.
  Secret-sharing-style dispersal [Krawczyk-1993] adds nothing while Ditto keeps
  the key. It would matter only if we wanted confidentiality without a Ditto
  key, which brings back the non-collusion problem in §4.3.

**Measured client cost** (M4 Pro Mac mini, Go, arm64 crypto extensions)
[E-storage]:

| Step | Throughput |
|---|---|
| AES-256-GCM seal | 5,548 MiB/s on 1 core; 40,493 MiB/s on 10 |
| AES-256-GCM open | 7,539 MiB/s on 1 core |
| Reed–Solomon 10-of-30 encode / reconstruct | 11,444 / 11,176 MiB/s |
| Reed–Solomon 29-of-80 encode / reconstruct | 5,826 / 5,898 MiB/s |
| SHA-256 Merkle build (4 KiB leaves) | 2,189 MiB/s (16 MiB shard in 7.3 ms) |

CPU is free next to bandwidth. A 64 MiB object at 3× expansion means
192 MiB of uploads from Ditto. A volunteer's share of it is a few megabytes
down, but every retrieval and repair runs over home uplinks (§3.4).

### 3.2 Erasure coding, not "about 5 copies"

The Leaderboard Debate floated data-availability sampling with about 5 copies.
Erasure coding gives far more availability per byte stored. The table shows
nines of availability, −log10(P[object unreadable]), for independent nodes
[E-durability]:

| Each node online | 3 copies (3×) | 5 copies (5×) | RS 10-of-30 (3×) | RS 16-of-32 (2×) | RS 29-of-80 (2.76×) |
|---|---|---|---|---|---|
| 50% | 0.90 | 1.51 | 1.67 | 0.37 | 2.32 |
| 70% | 1.57 | 2.61 | 5.14 | 2.28 | 9.95 |
| 80% | 2.10 | 3.49 | 8.35 | 4.49 | ≥ 15 |
| 90% | 3.00 | 5.00 | 14.24 | 8.89 | ≥ 15 |

- At 80% node uptime, 5 copies give 99.97% availability. RS 10-of-30 gives
  more than eight nines while storing 40% fewer bytes.
- At 50% uptime, **every scheme fails.** Home-node uptime is the number that
  decides whether this works, and we do not know it yet. Measuring it is
  Phase 1's first job.
- Independence is optimistic. Power cuts, ISP outages, and one owner running
  ten "Macs" fail together. Placement diversity (§2.3 T3) is what makes the
  table believable.

Storj, Sia and Hippius make the same trade at scale:

- **Storj** encrypts, cuts objects into 64 MB segments, and keeps 80 pieces
  of which any 29 rebuild a segment (2.76×). Its release default is
  `29/35/80/110`: k, repair threshold, success threshold, and total pieces
  uploaded [Storj-WP] [Storj-code].
- **Sia's** `renterd` defaults to 10-of-30 [Sia-renterd].
- **Hippius's** Arion uses k = 10 data and m = 20 parity pieces [Hippius].

### 3.3 Proofs of storage and availability sampling

**Mechanism:** Merkle challenge and response. Sia hosts post a Merkle proof for
a challenged leaf of contract data, and lose their collateral if they miss it
[Sia-renterd]. Hippius's warden challenges 4 random chunks of a piece every 30
seconds against per-piece Merkle commitments [Hippius]. Ditto commits a Merkle
root for each shard (4 KiB leaves) and publishes it.

1. A challenger picks a leaf index from public randomness, such as a drand
   round or a finalized block hash, so the miner cannot predict it.
2. The miner returns the leaf plus the sibling path, signed with its hotkey.
3. Anyone holding the root can verify the response. **Validators need no
   secret, and they see only ciphertext.**

Measured [E-storage]: the response is 4,480 bytes (a 4 KiB leaf plus 384 B of
siblings for a 16 MiB shard), proving takes 0.14 µs, and verifying takes 2.8 µs.

**How many challenges:** to catch a holder missing a fraction f of its leaves
with confidence 1−δ, s = ⌈ln δ / ln(1−f)⌉ [PDP-2007]:

| Missing fraction f | δ = 1e-2 | δ = 1e-6 | δ = 1e-9 |
|---|---|---|---|
| 1% | 459 | 1,375 | 2,062 |
| 5% | 90 | 270 | 405 |
| 10% | 44 | 132 | 197 |
| 50% | 7 | 20 | 30 |

The 1% row reproduces the classic PDP example: about 460 blocks give 99%
detection [PDP-2007]. Proofs of retrievability add erasure coding inside the
proof, so passing challenges implies the whole file is recoverable
[Juels-Kaliski-2007] [Shacham-Waters-2008]. Our outer Reed–Solomon layer plays
that role across miners.

**Availability sampling** in the blockchain sense [DAS-2018] comes out of the
same machinery. With a rate-1/2 code, an object stays recoverable unless more
than half its chunks are withheld. So each uniform sample of an unrecoverable
object fails with probability at least 1/2, and 20 samples miss with
probability below 10⁻⁶. Validators can sample cheaply and publicly.

The blockchain version uses 2D codes. Making data unrecoverable there means
withholding at least (k+1)² of (2k)² shares, so each sample hits a missing
share with probability just over 1/4, and 15 samples detect withholding with
more than 99% probability [DAS-2018].

**Deadlines:** challenges carry a short deadline. A miner that does not hold
the shard locally cannot fetch it from elsewhere in time. Shards are unique,
so no peer holds the same bytes, and only Ditto can regenerate them.

**What we cannot afford on a Mac:** proof-of-replication sealing, which makes
each replica slow to create and so prevents deduplication. Filecoin's sealing
of one 32 GiB sector needs:

- about 3 hours for the first sealing step;
- 384 GiB of scratch space;
- 256 GiB of RAM;
- an NVIDIA GPU with at least 11 GB of VRAM.

Its daily proofs must also land within 30-minute deadlines or be slashed
[Filecoin-PoRep]. A Mac mini tops out at 64 GB of memory and has no CUDA GPU.
We do not need sealing: k-of-n shards are already distinct, and sybil
concentration is handled by placement and n−k (§3.4).

### 3.4 Repair, churn and sybil withholding

**Repair** is Ditto-driven at first. Ditto reads any k shards, re-encodes, and
pushes the missing ones to new holders. Miners never talk to each other, so
Phase 1 needs no NAT traversal.

Eager repair, which rebuilds every lost piece immediately, multiplies reads by
k. Lazy repair waits until the shard count falls to a threshold and then
rebuilds everything from a single k-shard read. This is the approach in the
Storj whitepaper [Storj-WP].

Repair reads per day, as a fraction of the stored object, for daily permanent
departure probability q [E-durability]:

| q per day | 5 copies | RS 10-of-30, eager | RS 10-of-30, lazy | RS 29-of-80, lazy |
|---|---|---|---|---|
| 0.1% | 0.5% | 3.0% | 0.21% | 0.24% |
| 0.5% | 2.5% | 15% | 1.1% | 1.2% |
| 2% | 10% | 60% | 4.3% | 4.7% |

Repair traffic leaves Ditto's servers, so where the gateway runs matters:

- Hyperscaler egress costs per GB.
- Hetzner, where we already run screeners, includes traffic (§6).

Regenerating codes and local-reconstruction codes cut repair reads further.
They are a later optimization.

**Sybil withholding:** an adversary that holds a fraction β of randomly placed
pieces can make an object unrecoverable by withholding n−k+1 of them
[E-durability]:

| β | 5 copies | RS 10-of-30 | RS 29-of-80 |
|---|---|---|---|
| 10% | 1.0 × 10⁻⁵ | 5.8 × 10⁻¹⁵ | 1.6 × 10⁻³² |
| 33% | 4.1 × 10⁻³ | 4.4 × 10⁻⁵ | 7.1 × 10⁻⁹ |
| 50% | 3.1 × 10⁻² | 2.1 × 10⁻² | 4.8 × 10⁻³ |

Large n is the cheapest sybil defense. It works only while one owner's share
of each object stays near β, so placement must spread each object across
prefixes, ASNs, accounts, and enrollment ages.

**Starting point:**

- RS 10-of-30 for Phase 1.
- RS 29-of-80 if measured churn is high or the network grows past a few
  hundred nodes.

### 3.5 From a blob store to a "sharded encrypted private database"

A blob store (put and get by opaque id) is easy. The leakage sits in letting a
miner **query** the data.

Ditto's main query is "which memories are nearest to this query embedding?".
The options, and what each one lets a miner learn:

| Approach | What the miner learns | Cost | Verdict |
|---|---|---|---|
| **Blob store. Ditto fetches a user's encrypted segment and searches it locally.** | Which segment, when, and its size | One segment transfer per cold query: 10k memories are about 30 MB of float32 vectors [I-memcrypto §3.9] | **Phase 1 target.** Ditto caches hot segments. |
| Encrypted vector search under FHE (measured, §4.2) | Which tenant, when, and corpus size | 292 ms per 10k vectors per core; 21.4× storage | Batch or background only. The product case is user-held keys (§4.2). |
| ORAM over the encrypted store | Request count and timing only | Path ORAM moves 2Z·log N ≈ 8 log N blocks per access at Z = 4, about 160 blocks for 2²⁰ objects. Ring ORAM cuts that to 2–3.5 log N overall and about 1 block online [PathORAM-2013] [RingORAM-2015]. Ditto keeps the stash and position map. | Plausible for a small, hot index. Poor latency and availability when every access walks a path of home nodes. |
| PIR over a public corpus | Nothing about which record | The server scans the whole database per query: SimplePIR at 10 GB/s/core with a 121 MB hint per GB; YPIR at 12.1 GB/s/core with no hint [SimplePIR-2023] [YPIR-2024]. | Suits public corpora, such as public memory search, better than private ones |
| **Oblivious ANN search** (Compass, Pacmann) | Request count and timing only | Compass: HNSW over Ring ORAM. On 768-d MS MARCO it returns results in 1.3 s, with 8.9 MB per query perceived and 226 MB including eviction, over an 80 ms link at Recall@10 ≥ 0.9 [Compass-2025]. Pacmann: 1.1 s and 1.5 MB online on MS MARCO, after a 2.7 GB per-client preprocessing download [Pacmann-2025]. | **The most interesting option for miners.** A Compass server only stores encrypted blocks and XORs them, which is a storage node's job. The client (Ditto, or the user's device) does the distance math. Every round trip to a home node adds latency. |
| Searchable symmetric encryption for keywords | Search pattern (which queries repeat) and access pattern (which items match) | Near plaintext | **Reject for user memories.** Access-pattern attacks identified about 80% of queries on the Enron emails [IKK-2012]. With known documents, the count attack recovers nearly all queries [CGPR-2015]. File injection recovers every searched keyword by planting ⌈log₂ K⌉ documents, which is easy in a chat product [ZKP-2016]. |
| Deterministic or order-preserving encryption, blind keyword index | Equality and order of every value; token sequences | Cheap | **Reject.** Frequency and sorting attacks recovered order-preserving encrypted attributes for over 80% of patients at 95% of hospitals [NKW-2015]. Our backend implemented a blind index and rejected it in review [I-memcrypto §3.4]. |
| Rotated or projected embeddings | Everything, up to a recoverable linear map | Free | **Reject** (§4.4) |

**Recommendation:** in Phase 1, "sharded encrypted private database" means a
**blob store**:

- per-user, per-graph encrypted segments: memory exports, attachments, artifact
  bodies, trace archives, and encrypted backups;
- Reed–Solomon sharded across miners;
- indexed by Platform;
- decrypted and queried only at Ditto.

No query runs on miners until a product needs it and accepts the leakage.

**What leaks no matter what:**

- object sizes (size classes reduce this);
- read and write timing;
- per-tenant linkage across shards;
- object counts per tenant.

The leakage-abuse literature says to assume that this leaks content once
combined with outside knowledge. Communication volume alone is enough to
reconstruct records from range queries, and that attack applies "even to
systems based on homomorphic encryption or oblivious RAM" [KKNO-2016]. Batch
writes, pad volumes, rotate aliases, and never store one object per memory.

## 4. Compute track: unintelligible but useful work

### 4.1 Three ways to make work unintelligible

| Regime | How it hides data | Trust needed | Cost shape |
|---|---|---|---|
| **Fully homomorphic encryption** | The miner computes on ciphertext; only the key holder decrypts | **None among miners.** One miner, or all of them, learn nothing. Evaluation is data-oblivious. | 10³–10⁶× compute, megabyte ciphertexts, hundreds of megabytes of evaluation keys |
| **Secret-sharing MPC** | Each party holds random-looking shares | Parties must not collude, unless Ditto is one of them | Rounds × round-trip time, plus bandwidth |
| **Obfuscation**: rotation, projection, split inference, noise | Hopes a transform hides content | Fails against linear algebra and inversion models | Free, and broken |

### 4.2 FHE on a Mac mini: measured

All numbers come from [E-ckks], [E-tfhe] and [E-storage]: one Apple M4 Pro
Mac mini (Mac16,11; 10 performance and 4 efficiency cores; 64 GB), macOS 27.2.
They are from the first of two end-to-end runs; the second reproduced the CKKS
numbers within ±2%.

**The adapter circuit** (CKKS [CKKS-2017], Lattigo v6.2.0, N = 2¹⁴, 128-bit security per
the HE standard's log QP ≤ 438 [HES-2018]):

1. Ditto encrypts the 768-d query, padded to 1,024 slots.
2. Layer 1 (768→128) is a 128-diagonal baby-step/giant-step transform plus an
   inner sum, so its output comes back replicated.
3. GELU is a Chebyshev polynomial on `[−B, B]`. Queries are unit norm, so
   Ditto can bound every pre-activation exactly with Cauchy–Schwarz:
   `|z_i| ≤ ‖W1[:,i]‖₂ + |b1_i|`.
4. Layer 2 (128→768) is a second 128-diagonal transform, then the residual add.
5. Ditto decrypts and normalizes.

The change of basis is folded into W1 and α into W2, so the circuit is
leveled. **No bootstrapping is needed.**

| Config | Miner p50 / p95, 1 core | 10 workers | Evaluation keys | Query in / result out | Max abs vs pilot `reference.apply` |
|---|---|---|---|---|---|
| Plaintext weights, GELU degree 7 (depth 5) | 140.4 / 143.6 ms | 56.9 q/s | 284 MiB + 11 MiB | 1.50 MiB / 256 KiB | 8.0e-6: passes, but with only 1.25× headroom |
| **Plaintext weights, GELU degree 15 (depth 6)** | **183.7 / 190.9 ms** | **47.4 q/s** | 378 MiB + 14 MiB | 1.75 MiB / 256 KiB | **2.3e-8: passes the 1e-5 bar by 400×** |
| Plaintext weights, GELU degree 31 (depth 7) | 248.4 / 256.0 ms | 34.7 q/s | 486 MiB + 18 MiB | 2.00 MiB / 256 KiB | 2.3e-8 |
| **Encrypted weights, GELU degree 15** | **339.9 / 350.7 ms** | **24.1 q/s** | 364 MiB + 14 MiB, **plus 290 MiB of encrypted weights per adapter** | 1.75 MiB / 256 KiB | 2.3e-8 |

Context for these numbers:

- **Ditto-side cost** is about 2.3 ms to encrypt a query and 0.13 ms to
  decrypt, decode, and normalize the result.
- **Plaintext is about 10,000× cheaper.** The pilot measured 0.0175 ms p50 for
  the same adapter in process [I-pilot-measure].
- **Bandwidth dominates on home links.** The 256 KiB result takes about 0.1 s
  to send over a 20 Mbit/s uplink. Each miner also needs the 378 MiB key set
  once per key rotation.
- **Weights are an asset (A3).** With plaintext weights, a miner holds a model
  trained on one user's data. Two ways to avoid that:
  - only host shared, non-personal models in the clear;
  - pay 290 MiB per encrypted adapter version. That is about 1,700 adapters
    on a 512 GB disk, against the pilot's 1,359 plaintext adapters per GiB.
- **Trained adapters may need a higher degree.** A trained adapter can have
  larger column norms, which widens the GELU interval. The degree-31 row shows
  the cost of that.
- **Library notes from this host:**
  - The PyPI `openfhe` wheel (1.6.1) is tagged `py3-none-any` but contains
    only a Linux x86_64 extension, so it is unusable on macOS arm64.
  - Lattigo is pure Go and native on arm64.
  - TenSEAL (Microsoft SEAL) ships arm64 wheels.
  - Concrete ML 1.9.0 installs from wheels only on Python 3.11.

**Encrypted similarity** (CKKS, N = 2¹³, depth 1). One ciphertext scores 4,096
vectors. The query is padded to 1,024 and replicated four times, and each block
is a 4,096 × 1,024 matrix applied by 1,024 diagonals. Scores matched the
plaintext dot products to within 8.7e-9, and the top 10 matched exactly.

| Corpus held by the miner | Per 4,096-vector block, 1 core | Per 10k vectors, 1 core | 10 workers | Miner storage vs float32 |
|---|---|---|---|---|
| Public corpus, in the clear (encoded) | 45.7 ms | 111 ms | 772k vectors/s | 16.0× |
| **Ditto-encrypted corpus** | 119.7 ms | **292 ms** | 260k vectors/s | **21.4×** |

The query ciphertext is 256 KiB, the score ciphertext 128 KiB, and the
evaluation keys 47 MiB. Ditto encrypts a query in 0.38 ms and decrypts a block
of scores in 0.12 ms.

**TFHE** [TFHE-2020] (Zama Concrete ML 1.9.0, 6-bit quantization) on the same adapter:

| Measure | Value |
|---|---|
| Latency | 1,664 programmable bootstraps per query; **19.5 s and 20.3 s** on 14 cores (21.2 s and 24.2 s on a second run) |
| Keys | 449 MiB bootstrap + 337 MiB key-switching |
| Output | 48 MiB of encrypted output per query |
| Accuracy | Max abs 4.7e-3 against the pilot reference: **fails the 1e-5 bar by about 470×** |

This is the same order as Zama's published CPU numbers. On a 192-core server
at p_fail 2⁻¹²⁸, one programmable bootstrap costs 5.6 ms at 4 bits, 52.9 ms at
6 bits, and 773 ms at 8 bits [TFHE-rs-bench]. At 52.9 ms each, 1,664 bootstraps
are about 88 s of sequential work, or about 6 s spread perfectly over 14 cores.
We measured about 3× that. More precision is exponentially more expensive.
TFHE fits exact low-bit logic (comparisons, lookups), not float embeddings.

**Who should decrypt?** This choice changes the economics more than any
parameter does.

| Decrypting party | What miners compute | Value to Ditto |
|---|---|---|
| **Ditto's servers** (SN-472 framing) | Work that Ditto could do itself in microseconds, because it holds the plaintext | **No compute saved.** Encrypted work costs about 10,000× more, plus bandwidth and keys. The only value is verifiable, emission-funded work that leaks nothing (§6). Verification is free for the same reason (§4.5). |
| **The user's device** (true E2EE graph) | Similarity over a corpus that neither Ditto nor the miner can read. Optionally the user's adapter, under encrypted weights. | **A capability Ditto cannot offer today**: vector search on sealed graphs, one of the 13 capabilities the E2EE matrix marks as needing a server principal or an enclave [I-E2EE]. The device-side cost is about 0.4 ms to encrypt a query and 0.12 ms per 4,096 scores (measured on the Mac mini; phones are slower). Apple ships the same pattern. Enhanced Visual Search sends an 8-bit-quantized embedding encrypted under BFV, the server scores one cluster homomorphically, and the device decrypts and ranks. The cluster choice is protected by an anonymizing relay and fake queries with differential privacy (ε = 0.8), not by cryptography. Live Caller ID Lookup uses PIR [Apple-HE-2024] [Wally-2024]. |

**CKKS safety rules** (any decrypting party):

- Never reveal decrypted values to a miner. CKKS loses IND-CPA security when
  decryptions are shared, up to key recovery [Li-Micciancio-2021].
- Flood noise if a decrypted value must leave the decrypting party
  [LMSS-2022].
- Give miners no per-request accept or reject signal, which would make a
  decryption oracle (T9).
- Rotate keys on a fixed schedule.

Evaluation keys reveal nothing about the secret key under the RLWE
circular-security assumption that all deployed CKKS relies on, so handing them
to miners is safe.

### 4.3 Secret-sharing MPC, with and without Ditto as a party

**With Ditto as one of two parties:** a miner's view is uniformly random
shares, so confidentiality holds **even if every miner colludes**. But:

- **Ditto still does work proportional to the computation.** It evaluates the
  linear layers on its own share and supplies or consumes correlated
  randomness, such as Beaver triples [Beaver-1991].
- **Nonlinear layers need interaction.** GELU costs several round trips over
  home links of 20–80 ms each.
- **For a 20 µs adapter this is strictly worse than computing locally.**
  Information-theoretic MACs give active security with abort [SPDZ-2012].
  They do not change this cost picture.

**Among miners only**, for example three miners with replicated shares or two
miners with Ditto as dealer:

- The compute really is offloaded.
- But **confidentiality depends on the miners not colluding.** On SN118 a UID
  costs 0.0005 TAO and a Ditto account is free, so one person can run every
  share-holder and reconstruct the data. Nothing in the protocol detects this.

**Cost anchors from the literature**, all two-party and semi-honest, on
data-center servers:

| System | Model | Time | Communication |
|---|---|---|---|
| Cheetah [Cheetah-2022] | ResNet-50 | 80.3 s LAN; 134.7 s WAN | 2.3 GB |
| BOLT [BOLT-2024] | BERT-base | 8.9 min LAN; 16.9 min WAN, as measured by BumbleBee | 59.6 GB (25.7 GB with word elimination) |
| BumbleBee [BumbleBee-2025] | BERT-base, 128 tokens | 2.6 min LAN; 4.9 min WAN | 6.4 GB |
| BumbleBee [BumbleBee-2025] | LLaMA-7B | about 8 min **per token** | — |
| SIGMA [SIGMA-2024] | GPT-2 | 1.5 s online, on A6000 GPUs, with a trusted dealer's preprocessing not counted | 0.82 GB |

Nonlinear layers dominate: secure GELU alone is over half the run time in
earlier systems [BOLT-2024]. Our adapter has only 128 GELUs, against about
3.9 million in GPT-2. So a two-party adapter would be bound by round trips:
sub-second on a LAN, and seconds over home links. That is an extrapolation,
not a measurement. Private LLM inference on home Macs is out of reach by orders
of magnitude.

**Verdict:** MPC is not used in Phases 1–2. Revisit it only if a second,
non-colluding operator exists, such as an independent company holding one
share. That is a "two clouds" model, not a volunteer network.

### 4.4 Rotated embeddings, projections and split inference are not encryption

- **Rotation:** a random orthogonal map is linear.
  - About 768 known (plaintext, rotated) pairs recover it by Gaussian
    elimination.
  - It preserves the Gram matrix, so the rotated cloud aligns to a public
    corpus embedded with the same model **with no known pairs at all**.
  - Our backend reached the same conclusion and kept embeddings in plaintext
    rather than ship this [I-memcrypto §3.5]. Distance-preserving perturbation
    has well-known attacks [LGK-2006].
- **Random projection** (Johnson–Lindenstrauss) is approximately
  distance-preserving and falls to known-input and independent-component
  attacks [LKR-2006].
- **Why approximate alignment is enough:** embeddings invert to text.
  - Vec2text recovers **92% of 32-token inputs exactly** from 768-d GTR
    embeddings (BLEU 97.3). On clinical notes it recovers 89% of full names
    [Morris-2023].
  - Inversion transfers to encoders the attacker never queried
    [Huang-2024].
  - Embeddings can be translated between models with no paired data, reaching
    cosine 0.92 [vec2vec-2025].

  A secret or rotated model is not protection.
- **Split inference:** running the first layers at Ditto and the rest on a
  miner leaks inputs through intermediate activations. Model-inversion attacks
  reconstruct inputs from them [He-2019].
- **Noise or differential privacy** at a useful privacy level destroys
  nearest-neighbor ranking.

None of these gives confidentiality against a motivated miner. "Rotated
embedding space" should leave the design space.

### 4.5 Verifying work

| Method | Catches | Cost | Who runs it |
|---|---|---|---|
| **Decrypt and compare with Ditto's plaintext result** (the pilot's HMAC-keyed `--verify-rate`, raised to 100%) | Any wrong result | About 20 µs per adapter call. A 10k-vector block is about 7.7M multiply-adds in plaintext. | Ditto, which holds the plaintext |
| **Canaries**: known-answer jobs under the same key and shapes, indistinguishable from real ones | Lazy or random answers | One extra encryption | Ditto |
| **Deterministic redundancy**: the same ciphertext job to two miners, compare result hashes | Wrong answers, unless both colluding miners got the same job | 2× compute | Platform. Validators can compare the published hashes. |
| **Storage challenges** (§3.3) | Missing or altered data | 4.4 KiB and 2.8 µs per check | Anyone with the roots, including validators |
| Homomorphic MACs or verifiable FHE | Wrong evaluation without recomputing | Homomorphic MACs need the verifier's secret key [GW-2013] [CF-2013]. Verifiable FHE in a TEE costs about 90×; with SNARKs, 3×10⁴–6×10⁵× [VKH-2023]. | Not ready |
| Succinct proofs of the FHE evaluation | Everything | Groth16 over a small BGV circuit took 634 s against 0.014 s unverified [VKH-2023]. Proving one TFHE bootstrap takes about 18 minutes, against milliseconds to run it [TW-2025]. | Not feasible on Macs |

Three rules:

- Settle verdicts per epoch in aggregate, never per request (T9).
- Evict a miner on its first verified tamper.
- Bit-exact redundancy requires the same library version, parameters, and
  operation order. Lattigo evaluation adds no randomness, but we have **not**
  checked cross-machine determinism yet. That check is a Phase 2 metric.

### 4.6 Feasibility at a glance

| Workload on a home Mac mini | Feasible? | Measured or cited cost | Confidentiality against the miner |
|---|---|---|---|
| Encrypted, erasure-coded blob storage | **Yes** | GB/s client cost; network-bound | Full (ciphertext only) |
| Storage proofs and availability sampling | **Yes** | 4.4 KiB, 2.8 µs per proof | Validators see ciphertext only |
| Query adapter, encrypted query, plaintext weights (CKKS) | **Yes, at about 10,000× cost** | 184 ms per core; 47 q/s per M4 Pro; 1.75 MiB / 256 KiB per query | Query hidden. **Weights visible.** |
| Query adapter, encrypted weights | **Marginal** | 340 ms; 290 MiB per adapter version | Full |
| Encrypted similarity over a private corpus | **Batch only** | 292 ms per 10k vectors per core; 21× storage | Full |
| Encrypted query over a public corpus | **Yes** | 111 ms per 10k vectors per core | Query hidden |
| TFHE adapter | **No** | 19.5 s; 785 MiB of keys; fails parity | Full, but unusable |
| MPC among anonymous miners | **No** | Interaction-bound | Breaks under sybil collusion |
| Rotation, projection, split inference | **No** | Free | None |
| LLM inference on private data | **No** | Minutes and gigabytes per token (§4.3) | — |
| Full user cloud computers | **No** | — | None on an owner-controlled box |
| TEE-based confidentiality on Macs | **No** | No app code runs in the Secure Enclave; Private Cloud Compute is closed to third-party code | — |

## 5. Ditto Desktop integration

### 5.1 What Ditto Desktop is today

Read from `ditto-assistant/ditto-app` [I-desktop]:

- **App:** an Electron app (`appId: ai.omniaura.ditto.desktop`) built for
  Apple silicon as DMG and ZIP. `desktop-release.yml` signs it with a
  Developer ID certificate, notarizes it with `notarytool`, and staples it.
  Assets and a `SHA256SUMS.txt` go to `ditto-desktop-releases`.
- **Entitlements:** the main process has `cs.allow-jit`,
  `cs.allow-unsigned-executable-memory`, and `cs.disable-library-validation`.
  These are normal for Electron. They also mean the main process loads
  unsigned code. **A miner must not live in that process.**
- **Identity:** passkeys (Touch ID keychain group), an account vault on
  Electron `safeStorage`, and **tailnet pairing**. Tailnet pairing exposes a
  loopback endpoint through `tailscale serve` on port 47443 and exchanges a
  10-minute one-time code for a per-peer token that can be revoked.
- **Tailnet scope:** that tailnet is the **user's own** network of their own
  machines. The "tailscale-like network" in the notes is a different thing: a
  Ditto-operated miner network. The two should not share credentials or
  routes.

### 5.2 Packaging the miner

- **A separate signed helper** (`ditto-miner`):
  - Written in Go, the language of the measured CKKS and Reed–Solomon code and
    of the Ditto backend.
  - Hardened runtime with **none** of the main app's code-signing exceptions.
  - Developer ID signed, notarized, and registered as a per-user LaunchAgent
    through `SMAppService`, available from macOS 13 [Apple-SMAppService].
  - Talks to the app over a local socket or XPC for status, pause, and quotas.
  - **Has no access to the account vault, passkeys, or the user's Ditto
    session.** Its only credential is its miner identity.
- **Its own data container**, with a hard disk quota and a free-space floor.
  It stores only shards (ciphertext), plus Merkle trees and keys it may hold
  publicly (CKKS evaluation keys).
- **Separate, staged update channel** with a Platform-side kill switch. The
  kill switch stops new work and keeps shards until Ditto releases them. A bad
  miner build then cannot spread as fast as an app update, and a bad app build
  cannot read miner state.
- **Nothing secret ships in it** (T4). Signing and notarization show that the
  binary came from Ditto [Apple-notarization]. They do not stop the owner from
  running something else under the same identity.

### 5.3 Opt-in UX

- **Off by default.** A **Contribute** screen explains, in one view:
  - what the Mac stores and computes: only encrypted data Ditto created;
  - what never touches the Mac: keys, readable data, other users' identities;
  - the caps the user sets;
  - an honest earnings estimate, small (§6);
  - the power, heat, and bandwidth impact.
- **Preflight before enrolling:** a disk, bandwidth, and uptime check.
  Enrollment fails closed on metered or capped links unless the user confirms.
- **Always visible:** pause, resume, and "leave and wipe". Leaving deletes the
  shards and tells Platform to repair them elsewhere.
- **A status page:** stored GB, challenges passed, uptime, work done, earned.
  During the pilot, test data is labeled **synthetic**.
- **One explanation is mandatory:** "Your Mac cannot read what it stores, and
  neither can anyone who takes it. Ditto can, because Ditto holds the keys."
  That is the true sentence for Peyton's framing (§2.4). If we later move to
  user-held keys, the sentence changes, and the copy must change with it.

### 5.4 Resource limits

| Limit | Mechanism |
|---|---|
| CPU | `ProcessType = Background` in the LaunchAgent, which applies launchd's resource limits to "work that was not directly requested by the user" [Apple-launchd]. Background QoS, which Apple says makes the system "more likely" to place threads on efficiency cores; that is a hint, not a guarantee [Apple-QoS]. The compute lane gets a duty-cycle cap, for example a quarter of the performance cores, and only on AC power. |
| Heat and battery | Pause when `ProcessInfo.thermalState` is `serious` or `critical` [Apple-thermal], in Low Power Mode (which itself pauses "discretionary and background activities") [Apple-LPM], or on battery. |
| Disk | A hard quota, and a floor of 15% free space. The miner never grows into the user's last free space. |
| Network | Separate upload and download caps, and backoff on observed congestion. A monthly data budget for capped plans: Xfinity's 1.2 TB cap now applies only to legacy plans, and uploads count toward it [Xfinity-cap]. The US median fixed uplink is about 73 Mbit/s (September 2026; Ookla via a secondary source) [Ookla-2026], but plan for much slower links. |
| Interactivity | An optional "only when idle" mode for compute. Storage challenges are tiny and always allowed. |

### 5.5 Network shape

- **Phases 1–2 are outbound-only.** The helper keeps one long-lived connection
  to a Ditto gateway over HTTPS or WebSocket, or QUIC. There are no inbound
  ports, no UPnP, and no NAT traversal. Platform pushes work over that
  connection.
- **Validators reach miners through the gateway.** Miners sign responses with
  their hotkeys, so the relay cannot forge them (T11).
- **Direct miner-to-miner repair comes later.** It needs a Ditto-run overlay
  (a WireGuard mesh with a coordination server, Tailscale-style) plus relays.
  Hole punching does not always work:
  - Tailscale estimates a direct path "over 90% of the time" [Tailscale-NAT].
  - A 4.4-million-attempt IPFS study measured 70% ± 7.1% hole-punch success,
    and it counted only attempts where relay setup and address discovery had
    already worked, so end-to-end success is lower [DCUtR-2026].

  Defer the overlay until measured repair bandwidth says Ditto-driven repair
  is too expensive.

### 5.6 Identity, registration and payouts

**Under today's Yuma setup, one UID per Mac does not fit.** At finalized block
9,487,564 [E-chain]:

- SN118 runs Yuma with one mechanism.
- 256 of 256 UIDs are in use, with 64 validator permits.
- Burn registration costs 0.0005 TAO, the `MinBurn` floor. PoW registration
  is off.
- Immunity lasts 300 blocks, about an hour.

When the subnet is full, each new registration evicts the non-immune UID with
the lowest emission-based pruning score [BT-docs].

**Pilot: one UID per Mac under Null consensus.** Null is subtensor's
owner-selected alternative to Yuma [BT-null]:

- **Capacity** is `floor(2500 / mechanism_count)`: 2,500 UIDs with SN118's one
  mechanism, or 1,250 with two. The chain allows at most two mechanisms.
- **One elected validator.** The registered UID with the highest stake weight
  holds the only validator permit. Only its row sets miner rewards. There is no
  consensus and there are no bonds.
- **Exact integer weights.** Raw u16 weights store exact ratios across
  thousands of UIDs (for example 65534:1), so tiny per-Mac shares survive.
- **Switching** is `epoch_consensus = Null`, with `max_allowed_uids` raised
  separately. Going back to Yuma means first pruning down to
  `floor(256 / mechanism_count)`, at most 64 UIDs per transaction.
- **Optional fee-free PoW registration** (`network_pow_registration_allowed`)
  lets a Mac register without holding any TAO.

What Null buys the pilot:

- **Each Mac's hotkey earns directly**, every tempo (about 72 minutes).
  "Small daily payouts" become native emissions to a hotkey the user's coldkey
  owns. There is no pool accounting, custody, transfer, or batching.
- **Identity and payout are both per device**, which matches what §5.7
  verifies.

What it costs:

- **The mode is subnet-wide.** The competitions settle the same way: the
  elected validator's single row carries the memory KOTH, generator, Gamma and
  burn destinations as well as the Macs. SN118 validators already fold the
  same Platform ledger deterministically, so an honest elected validator writes
  the row the whole fleet would agree on. What disappears is Yuma's protection
  against one deviant validator (T14).
- **Onboarding pace is limited.** Each registration multiplies its route's
  price by 1.26, and the price halves every 360 blocks [BT-burn] [E-chain].
  - The price holds steady at ln 2 / ln 1.26 ≈ **3 registrations per 360
    blocks, about 60 per day**.
  - A burst is exponential: the 50th registration in a row costs about
    1.26⁵⁰ ≈ 10⁵ times the floor, about 50 TAO.
  - Desktop needs an enrollment queue. PoW registration has its own,
    independent difficulty curve.
- **Tracks share one UID pool.** A full subnet evicts the lowest earner on
  each new registration, so a new agent miner with no emission yet competes
  with idle Macs for a slot. Keep headroom below capacity and pace enrollment.
  The competitions must never lose the ability to register.

**Long term: pools.** 2,500 UIDs (1,250 with two mechanisms) is a pilot
ceiling, not a network ceiling. Beyond it, **compute and storage pools** hold
the UIDs:

- Each pool aggregates many Macs and pays its members from pool earnings.
- A transfer costs about 0.000083 TAO, as set in runtime spec 467 (the current
  runtime is spec 490), and the chain accepts `Utility.batch` [BT-fees].
- Pools also let third parties run fleets.

Verification stays the same; only the identity becomes coarser.

**Wallet:**

- Bittensor keys are sr25519 or ed25519. CryptoKit's Secure Enclave offers
  only P-256, ML-KEM and ML-DSA key types [Apple-CryptoKit]. So a hotkey on a
  Mac is a software key in the Keychain.
- That is acceptable: a hotkey receives emissions and signs miner messages, but
  it cannot move the coldkey's funds.
- **Recommendation:** the miner holds only its hotkey. The owning coldkey stays
  off the box: the user's existing wallet, or a fresh one created with an
  exported recovery phrase. A wallet Ditto holds for users brings custody and
  regulatory duties (§8, Q7 and Q12).
- **Taxes:** in the US, crypto received for services is ordinary income,
  possibly self-employment income, at fair market value when received
  [IRS-crypto]. Desktop should show each payout with its USD value at receipt.
  This is not legal advice.

**Sybil friction (Nick's OIDC + binary-id idea):**

- An OIDC-verified Ditto account plus a per-install key give per-account caps
  and revocation.
- **Apple offers no device attestation to Mac apps.**
  `DCAppAttestService.isSupported` "is false" for any app running on a Mac
  [Apple-AppAttest]. A binary id is therefore self-reported, and an owner who
  patches the binary can report anything (T4).
- This is friction, not a defense. Payout and placement math assume sybils
  (T3).

### 5.7 What validators check once payouts exist

1. **Storage:**
   - Merkle challenges against roots Platform publishes.
   - Leaf indices come from public randomness. Responses are signed by the
     miner's hotkey and carry deadlines.
   - Cheap availability samples (§3.3).
   - Validators need no secrets and see only ciphertext.
2. **Compute:**
   - Platform-signed receipts of verified units per Mac (pilot) or per pool
     (later), with the same oracle trust as today's Platform.
   - Cross-checks of deterministic-redundancy hashes (§4.5).
   - Validators cannot decrypt results, by design.
3. **Weights:**
   - Under Null, only the elected validator writes them. Its row is: the
     competition recipients at their track shares, plus each Mac or pool at
     verified units × a reference price capped by the infra track's share,
     plus **the burn destination for the surplus** (§6).
   - The other validators still run the storage and compute checks and
     publish any disagreement. They become auditors rather than weight-setters.
   - Validators stay stateless. Placement, challenge schedules, and the ledger
     live in Platform (`CLAUDE.md`).

## 6. Economics

### 6.1 What SN118 spends on miners today

- **Emission rate:** SN118 emits 1 alpha per block. Subtensor routes 41% of it
  to miners [I-treasury] [E-chain]. That is the historical default; since Etao,
  owners may set miners anywhere from 33% upward [BT-emissions].
  - Fully released, that is **2,952 alpha per day**.
  - At the pool's spot price of 0.002962 TAO per alpha (finalized block
    9,487,564, ignoring slippage), that is **8.74 TAO per day**, about
    **262 TAO per month**.
  - At $215.67 per TAO (CoinGecko, 2026-11-12 02:39 UTC) [TAO-price], that is
    about **$1,890 per day, or $56.6k per month**. TAO traded near $305 in
    early October, so treat the dollar figure as a moving target.
  - The pool holds 4,096 TAO of liquidity, so a day's miner emission moves
    the price by well under 1%.
- **Where it goes now** (public API, 2026-11-12) [I-status]:
  - 70% to the memory-agent king-of-the-hill board, which pays five hotkeys:
    35.0%, 14.0%, 9.1%, 7.0%, and 4.9% of the miner pool.
  - 30% to the generator track.
  - 0% burned.
- **This is a budget, not Ditto cash.** Emissions are inflation, and whatever
  miners sell lands on the SN118 pool. The question is what the budget buys.

### 6.2 The market value of the work

**Storage** (list prices, retrieved 2026-11-11):

| Provider | Price per month | Notes |
|---|---|---|
| AWS S3 Standard (us-east-1) | $0.023/GB (first 50 TB), ≈ $23/TB [S3-price] | Egress extra |
| Google Cloud Storage Standard (us-central1) | $0.020/GiB, ≈ $20.5/TiB [GCS-price] | Egress extra |
| Cloudflare R2 | $0.015/GB, ≈ $15/TB [R2-price] | Free egress |
| Backblaze B2 | $6.95/TB [B2-price] | Free egress up to 3× stored |
| Hippius | $6/TB pay-as-you-go, $4.65–5.30/TB on plans [Hippius] | No egress fees. Where SN-49 backups already go. |
| Hetzner Storage Box (BX11) | €3.20 for 1 TB [Hetzner] | Unlimited traffic; one site, no cross-site coding |
| Sia, renter side | ≈ $3/TB including 3× redundancy [Sia-renterd] | Host median is $1.05/TB |
| Storj, **paid to node operators** | $1.35/TB stored, $1.00/TB egress (since 2026-09-01) [Storj-payout] | What a decentralized network pays home nodes |

**Worked example.** Ditto places 10 TB of encrypted objects at RS 10-of-30, so
miners store 30 TB:

| Comparison | Monthly cost |
|---|---|
| The same 10 TB as durable storage elsewhere | $60 (Hippius) to about $230 (S3) |
| Paying miners Storj's operator rate for 30 TB | $40.50, or $81 at a 2× premium |
| SN118 miner emissions | about $56,600 |

Either way, useful storage absorbs **0.07–0.4% of the miner pool**. Ten
terabytes is also generous for encrypted memories, artifacts, and backups.
Ditto's real number is an open input (§8, Q10).

**Compute.** One M4 Pro Mac mini runs the encrypted adapter at 47 queries per
second, about 4M per day. Ditto's plaintext cost for those same 4M adapter
calls is about 80 CPU-seconds, a fraction of a cent. With Ditto holding the
keys, encrypted compute has **no resource value to Ditto**. Its value is the
user-held-key capability (§4.2), or simply being verifiable work.

**The volunteer's side.**

- Power: a Mac mini M4 draws 4 W at idle and up to 65 W. An M4 Pro draws 5 W
  and up to 140 W, measured at the wall [Apple-macmini-power].
- Electricity: 18.16 ¢/kWh, the US residential average for August 2026
  [EIA].
- The two cost lines below are derived from Apple's idle and max figures, not
  measured:
  - **Storage-only work** adds a few watts: about $0.25–0.65 per month at
    2–5 W.
  - **A compute lane** at a quarter of the performance cores adds roughly
    15–35 W: about **$2–5 per month**.
- At Storj's operator rate, 1 TB contributed earns $1.35 per month, about
  **4.5¢ per day**. At Sia's median host price it earns about 3.5¢.

"Small daily payouts" is literally true. A compute lane paid at market value
can cost the volunteer more in electricity than it earns, so compute must be
priced above its power cost or left out of the payout story.

### 6.3 Paying for useful work, not for existing

- **Price each verified unit at a market reference with a stated premium**,
  for example 2× Storj's operator rate. Units: TB-days stored and proven,
  GB retrieved within its deadline, and compute jobs verified.
- **Cap the track's share of the miner pool, and burn the surplus.** SN118
  already has a burn setting.
- **Never pay for capacity Ditto does not use.** Paying for proofs of space
  over filler data turns the track into proof-of-space with Ditto branding. It
  invites cheap sybil disks and buys nothing.

### 6.4 Versus the agent competition, and alongside it

| | Agent competition (today) | Encrypted infra track (proposed) |
|---|---|---|
| What emissions buy | Memory-harness R&D measured by DittoBench | Verified storage and compute units for Ditto-owned encrypted data |
| Value to Ditto | Harness improvements; transfer to the product is uncertain | Commodity storage worth well under 1% of the budget; compute worth about $0 unless user-held keys ship |
| Who earns | Five KOTH hotkeys plus the generator track | Potentially hundreds of Mac owners: one UID each under Null during the pilot, pools later |
| How it is gamed | Benchmark overfitting, fought with bench design | Sybils, lazy or withholding miners, fought with cryptography and verification |
| Ditto's operating load | Screeners, validators, Backroom review | Gateway bandwidth, repair, consumer support, and legal (paying consumers in crypto) |
| Distribution | None | Desktop installs and engaged users |

**Verdict:**

- **On useful work per unit of emission, the infra track does not beat the
  competition.** At fair prices it can absorb only a sliver of the budget.
- It wins on **breadth**: many small earners, and a reason to install Ditto
  Desktop.
- If user-held-key search ships, it also enables a **product capability**.
- Those are strategic choices for Peyton, not engineering results.

**It does not have to be either/or.** SN118's allocation is already a set of
tracks with operator-set shares. The infra track is one more destination, so
the agent and data-generation competitions can be turned on, off, or
re-weighted independently:

| Track | Share (example only; operator setting) |
|---|---|
| Memory KOTH | x% |
| Generator | y% |
| Mac infra (verified units, capped) | z% |
| Burn (surplus) | the rest |

The design must keep three properties:

- **One mechanism, many tracks.** Adding a second on-chain mechanism halves
  capacity (2,500 → 1,250 under Null). It also resets the split whenever the
  count changes. Use it only if a track needs its own weight row.
- **Registration stays open to competition miners.** Pace Mac enrollment and
  keep UID headroom (§5.6).
- **Null is subnet-wide.** If SN118 switches, the competitions also settle
  through the elected validator's row. That trust change needs an explicit
  decision (T14, §8 Q2).

**Recommendation:**

- **Do not replace the competitions.** Run Phases 1–2 unpaid alongside them.
- If Phase 1 meets its metrics, give the infra track a small, capped share,
  with the surplus burned. Judge it on Desktop engagement as well as on stored
  bytes.

### 6.5 What this section could not measure

- **Ditto's actual hosting bill.** That covers storage GB, egress, and the CPU
  used to serve memories. No billing data was read for this doc, and none is
  in the repo.
- **Home-node uptime and churn.** The durability math in §3.2 and §3.4 swings
  on these numbers.

The first is a five-minute ops pull. The second is Phase 1's job.

## 7. Phased pilot plan

Each phase extends Omar's loopback pattern [I-pilot]:

- a versioned wire contract;
- a reference miner;
- a Ditto-side client with verification, fallback, and a ledger that reports
  `payouts: "none"`;
- a proof harness of positive and negative scenarios;
- a bench whose economic claims are `"none"`.

The phase gates are:

- Every phase is synthetic and unpaid until Peyton approves the next one.
- No real user data before a privacy review.
- No payouts before a legal review.

### Phase 0: this document (done)

- **Delivered:** threat model, designs, and measured feasibility on a Mac mini
  [E-*].
- **Exit:** Peyton answers §8, Q1–Q5.

### Phase 1: encrypted, erasure-coded storage on volunteer Macs (synthetic, unpaid)

| Step | Scope | Runs on |
|---|---|---|
| **1a. Loopback** | `storage/v1` contract (put, get, challenge, release). Reference miner in Go. Client: pad, AES-256-GCM, RS 10-of-30, Merkle roots, placement, challenges, lazy repair. Ledger. Proof harness. Bench. Placement follows the query-adapter pilot: `research/<name>/` with its own release component and path-filtered CI. | One machine, CI |
| **1b. Team Macs** | The same helper on 5–10 team machines over real home links, through an outbound-only gateway. Gateway placement is §8, Q9. No infrastructure is provisioned by this doc. | 2 weeks |
| **1c. Volunteer Macs** | 20–50 opted-in volunteers on an internal Desktop build that bundles the helper. Synthetic objects only, labeled as such. | 30 days |

The 1a proof harness must cover these negative scenarios:

- a miner withholds shards;
- a shard is corrupted;
- one object's shard is served as another's;
- an old version is replayed;
- a miner outsources storage and answers slowly;
- a sybil owns many holders of one object;
- a churn storm;
- the miner's disk fills;
- a canary scan finds no plaintext and no key material on the miner.

**Success metrics** (Phase 1c, over 30 days):

| Metric | Target |
|---|---|
| Median node uptime | ≥ 80% |
| p10 node uptime | ≥ 50% |
| Object read availability | ≥ 99.9% within 10 s for a 64 MiB object |
| Objects lost | 0 |
| Challenge pass rate, honest nodes | ≥ 99.5% |
| Repair reads | ≤ 2% of stored bytes per day |
| Retrieval p95 for 64 MiB | ≤ 20 s |
| Average helper CPU | < 5% |
| Over-cap incidents (disk or bandwidth) | 0 |
| Volunteer-reported disruptions | 0 |
| Volunteers still enrolled at day 30 | ≥ 70% |

**Kill criteria:** any one of these ends the storage track.

- Median uptime below 60% over 2 weeks. RS overhead and repair then outgrow
  the value.
- Repair above 10% of stored bytes per day at RS 10-of-30.
- **Any** plaintext or key material found on a miner.
- Sybil resistance turns out to need KYC before payouts can work at all.
- More than 1 in 10 volunteers reports a real disruption: heat, noise,
  bandwidth, or disk.

### Phase 2: CKKS compute lane (synthetic, unpaid)

- **`query-adapter/v2-ckks`:** an encrypted query goes in, and an encrypted
  `q + α·Δ` comes back. Steps:
  1. Synthetic plaintext weights first, then encrypted weights.
  2. Then an encrypted-similarity lane over synthetic corpora.
  3. Then, if Q1 picks it, a user-held-key prototype in which a test client,
     not Ditto, decrypts.
- **Verification:** Ditto checks 100% of results while it holds plaintext.
  Canaries. Cross-Mac determinism of the redundancy hash. Aggregate per-epoch
  verdicts only (T9).

**Success metrics:**

| Metric | Target |
|---|---|
| End-to-end p95 over home links (adapter) | ≤ 1 s |
| End-to-end p95 over home links (10k-vector similarity) | ≤ 3 s |
| Parity pass rate | 100% |
| Fault-injection detection (the pilot's `perturb`, `swap-tenant`, `nan` faults) | 100% |
| Evaluation-key traffic | ≤ 1 GB per miner per month |
| Result hashes across two Macs | Bit-identical |

**Kill criteria:**

- Adapter p95 above 2 s.
- Key distribution above 5 GB per miner per month.
- Non-deterministic results across Macs, which breaks cheap redundancy.
- No committed product consumer: neither user-held-key search nor any other
  user of encrypted compute.

### Phase 3: only by explicit decision

- **Null rehearsal first, on testnet.** Run a test subnet in Null mode with
  the real fold: competition recipients, synthetic Mac UIDs, and burn in one
  raw-u16 row. Exercise:
  - enrollment pacing under the 1.26× burn multiplier, and PoW registration;
  - eviction pressure on competition miners;
  - elected-validator failover and stale-row alarms;
  - the bounded trim back to Yuma.

  Only then decide whether SN118 itself switches (Q2).
- **Real data:** Ditto-owned encrypted objects first, starting with the SN-49
  backup class beside Hippius. Then opt-in user blobs after a privacy review.
- **Payouts:**
  - one UID per Mac under Null, up to the capacity left after the competition
    miners;
  - compute and storage pools beyond that;
  - always at a capped track share with the surplus burned (§6.3–6.4).
- **Validators:** storage challenges and compute receipts. Under Null, the
  elected validator writes the row and the others audit it (§5.7).
- **What stays unchanged until then:** `bench_version`, scoring, emissions,
  and the competitions' existing shares.

**Rough effort** (engineering weeks; to be re-estimated after Phase 0 answers):

| Phase | Effort |
|---|---|
| 1a | 3–4 |
| 1b | 1–2 |
| 1c | 2–3, plus the Desktop helper packaging in ditto-app |
| 2 | 4–6 |
| 3 | Not estimated |

## 8. Open questions for Peyton and Omar

| # | Decision | Options | Recommendation |
|---|---|---|---|
| **Q1** | **Who holds the decryption key?** | (a) Ditto's servers, the SN-472 framing. (b) The user's device: true E2EE graphs, with miners computing for users and Ditto blind. | (a) for Phase 1 storage. Evaluate (b) in Phase 2. Only (b) makes encrypted compute worth more than its overhead (§4.2). |
| **Q2** | **Move SN118 to Null consensus for per-Mac UIDs?** And who runs the elected validator? | Stay on Yuma, with pools from day one; switch to Null for the payout phase; switch only after a testnet rehearsal | Rehearse on testnet first (Phase 3). Switch only if Ditto runs, or tightly trusts, the elected validator. Null is subnet-wide, so it changes how the competitions settle too (T14). Pools remain the long-term path beyond 2,500 UIDs. |
| **Q3** | **Track shares beside the competitions** | Replace the competitions; alongside them with a capped share; research only | Alongside at 0% during the pilot. Then a small capped share, surplus burned. Keep one mechanism and keep the agent and generator competitions switchable (§6.4). |
| **Q4** | **First real data** | Ditto's own encrypted backups (SN-49 class); user attachments and artifacts; memory segments | Ditto-owned backups first. User data only after a privacy review and an opt-in. |
| **Q5** | **Sybil and identity policy** | OIDC account plus per-install key; PoW registration on or off; a deposit per pool later; KYC for payouts | Account caps, burn registration with paced enrollment, and PoW off until sybil data exists. KYC only if counsel requires it. App Attest is unavailable on Macs, so no hardware attestation (§5.6). |
| Q6 | **Build or buy storage** | Build a Mac-native network; pay Hippius, which already runs RS 10-of-30 with proofs at $6/TB, and focus on compute and E2EE | Decide on what Hippius lacks: Mac-native miners in Desktop, the compute lane, and the Desktop audience. Storage alone does not justify a new network (§1.2). |
| Q7 | **Legal** | — | Paying consumers in crypto (US: ordinary or self-employment income at fair market value [IRS-crypto]; sanctions screening) and hosting opaque encrypted data both need counsel before payouts. |
| Q8 | **Contracts (Omar)** | — | Do `storage/v1` and `query-adapter/v2-ckks` live in `research/` next to the pilot? Should the client, verification, and ledger be shared, or copied per pilot? |
| Q9 | **Gateway** | Hetzner (traffic included) or GCP (egress per GB) | Hetzner, for the initial placement and repair-traffic budget. |
| Q10 | **Real hosting numbers** | — | What do Ditto's storage GB, egress, and memory-serving CPU cost per month, and how fast are they growing? Needed to turn §6 from an upper bound into a business case. |
| Q11 | **Product** | — | Is a 0.3–0.5 s encrypted similarity path acceptable for sealed graphs? Would iOS clients hold CKKS keys as well as Desktop? |
| Q12 | **Pools (long term)** | Ditto-run pools only; third-party pools; custodial or non-custodial | Non-custodial. Allow third-party pools once verification receipts are public. |


## 9. Sources

Every crypto or performance number above cites one of these entries, or our
own measurements in `experiments/decentralized-encrypted-infra/results/`.
External pages were retrieved 2026-11-11 or 2026-11-12 (UTC).

### Internal

- **[I-pilot]** Omar Barazanji, query-adapter pilot stack:
  [#3483](https://github.com/ditto-assistant/ditto-subnet/pull/3483),
  [#3484](https://github.com/ditto-assistant/ditto-subnet/pull/3484),
  [#3485](https://github.com/ditto-assistant/ditto-subnet/pull/3485); runbook
  `research/query-adapter-pilot/docs/runbook.md` ("Future work").
- **[I-pilot-measure]** `research/query-adapter-pilot/docs/measurements.md`
  (Apple M5 Pro, 2026-11-10).
- **[I-adapter]** backend#1360, Seed Memories v4 per-user query adapter.
- **[I-memcrypto]** `ditto-assistant/backend`,
  `docs/roadmap/2026-09-memory-encryption.md`. Cited sections:
  - §3.1: three promises;
  - §3.3: key hierarchy;
  - §3.4: blind index rejected;
  - §3.5: rotation rejected;
  - §3.8: residual metadata;
  - §3.9: encrypted-embedding costs.
- **[I-E2EE]** backend#2874, `docs/roadmap/2026-09-e2ee-feature-matrix.md`:
  13 capabilities need a server principal or an enclave.
- **[I-app-encryption]** ditto-app#3275 ("At-rest protection, not end-to-end
  encryption") and ditto-app#3257 (the private search pilot decrypts on the
  server). Jira DITTO-5.
- **[I-SN49]** Jira SN-49: nightly `age`-encrypted `pg_dump` to Hippius.
- **[I-treasury]** `docs/sn118-treasury-operator-runbook.md`: 1 alpha per
  block, 41% to miners.
- **[I-status]** `.agents/skills/subnet-status/scripts/subnet_status.py`
  against `https://dittobench.ai/api/v1/public`, 2026-11-12: emission split,
  KOTH curve.
- **[I-desktop]** `ditto-assistant/ditto-app`:
  - `desktop/entitlements.mac.plist` and `electron-builder.yml`;
  - `.github/workflows/desktop-release.yml`;
  - `desktop/identity/tailnet.ts` and `desktop/identity/register.ts`;
  - the `ditto-desktop-releases` README.
- **Related, not relied on:**
  - DITTO-2023 (KubeTEE PoC);
  - DITTO-2543 and ditto-subnet#3431 (verifiable bench execution);
  - ditto-app#1427 (vibe-mining);
  - backend#2059 (privacy-first feedback).

### Our measurements

Host: Mac mini Mac16,11, Apple M4 Pro (10P + 4E cores), 64 GB, macOS 27.2.

- **[E-ckks]** `results/ckks-adapter-*.json` and `results/ckks-similarity-*.json`
  (Go, Lattigo v6.2.0).
- **[E-tfhe]** `results/tfhe-adapter-concrete-6bit.json` (Concrete ML 1.9.0,
  Python 3.11).
- **[E-storage]** `results/storage.json` (Go, `crypto/aes`,
  klauspost/reedsolomon v1.14.2, SHA-256).
- **[E-durability]** `results/durability.json` (closed-form binomial model).
- **[E-chain]** `results/sn118-chain-snapshot.json`: read-only Finney RPC,
  finalized block 9,487,564.

### Homomorphic encryption

- **[CKKS-2017]** Cheon, Kim, Kim, Song. "Homomorphic Encryption for Arithmetic
  of Approximate Numbers." ASIACRYPT 2017. https://eprint.iacr.org/2016/421
- **[TFHE-2020]** Chillotti, Gama, Georgieva, Izabachène. "TFHE: Fast Fully
  Homomorphic Encryption over the Torus." J. Cryptology 33(1), 2020.
  https://eprint.iacr.org/2018/421
- **[HES-2018]** Albrecht et al. "Homomorphic Encryption Standard" v1.1, 2018.
  Table 1: log q ≤ 438 at N = 2¹⁴ and ≤ 218 at N = 2¹³ for 128-bit security
  with ternary secrets.
  https://homomorphicencryption.org/wp-content/uploads/2018/11/HomomorphicEncryptionStandardv1.1.pdf
- **[TFHE-rs-bench]** Zama. TFHE-rs CPU benchmarks: PBS at p_fail 2⁻¹²⁸ is
  5.64 ms at 4 bits, 52.9 ms at 6 bits, and 773 ms at 8 bits, on 2× AMD EPYC
  9R45 (192 cores). https://docs.zama.org/tfhe-rs/get-started/benchmarks/cpu
- **[Apple-HE-2024]** Apple Machine Learning Research. "Combining Machine
  Learning and Homomorphic Encryption in the Apple Ecosystem," 2024-10-24.
  https://machinelearning.apple.com/research/homomorphic-encryption
- **[Apple-swift-HE]** apple/swift-homomorphic-encryption README: BFV "does not
  provide IND-CCA security."
  https://github.com/apple/swift-homomorphic-encryption
- **[Wally-2024]** Asi et al. "Wally: Batched Private Nearest Neighbor Search at
  Scale." arXiv:2406.06761. https://arxiv.org/abs/2406.06761
- **[Li-Micciancio-2021]** Li, Micciancio. "On the Security of Homomorphic
  Encryption on Approximate Numbers." EUROCRYPT 2021.
  https://eprint.iacr.org/2020/1533
- **[LMSS-2022]** Li, Micciancio, Schultz, Sorrell. "Securing Approximate
  Homomorphic Encryption Using Differential Privacy." CRYPTO 2022.
  https://eprint.iacr.org/2022/816
- **[Chenal-Tang-2014]** Chenal, Tang. "On Key Recovery Attacks against
  Existing Somewhat Homomorphic Encryption Schemes." LATINCRYPT 2014.
  https://eprint.iacr.org/2014/535
- **[GW-2013]** Gennaro, Wichs. "Fully Homomorphic Message Authenticators."
  ASIACRYPT 2013. https://doi.org/10.1007/978-3-642-42045-0_16
- **[CF-2013]** Catalano, Fiore. "Practical Homomorphic MACs for Arithmetic
  Circuits." EUROCRYPT 2013. https://doi.org/10.1007/978-3-642-38348-9_21
- **[VKH-2023]** Viand, Knabenhans, Hithnawi. "Verifiable Fully Homomorphic
  Encryption." arXiv:2301.07041, Table 2. https://arxiv.org/abs/2301.07041
- **[TW-2025]** Tremblay Thibault, Walter. "Towards Verifiable FHE in Practice:
  Proving Correct Execution of TFHE's Bootstrapping using plonky2." ACM CCS
  2025. https://eprint.iacr.org/2024/451

### MPC and private inference

- **[Beaver-1991]** Beaver. "Efficient Multiparty Protocols Using Circuit
  Randomization." CRYPTO '91. https://doi.org/10.1007/3-540-46766-1_34
- **[SPDZ-2012]** Damgård, Pastro, Smart, Zakarias. "Multiparty Computation
  from Somewhat Homomorphic Encryption." CRYPTO 2012.
  https://eprint.iacr.org/2011/535
- **[Cheetah-2022]** Huang, Lu, Hong, Ding. "Cheetah: Lean and Fast Secure
  Two-Party Deep Neural Network Inference." USENIX Security 2022, Table 8.
  https://www.usenix.org/system/files/sec22-huang-zhicong.pdf
- **[BOLT-2024]** Pang, Zhu, Möllering, Zheng, Schneider. "BOLT:
  Privacy-Preserving, Accurate and Efficient Inference for Transformers." IEEE
  S&P 2024. https://eprint.iacr.org/2023/1893
- **[BumbleBee-2025]** Lu et al. "BumbleBee: Secure Two-party Inference
  Framework for Large Transformers." NDSS 2025, Tables IV–V.
  https://eprint.iacr.org/2023/1678
- **[SIGMA-2024]** Gupta et al. "SIGMA: Secure GPT Inference with Function
  Secret Sharing." PoPETs 2024(4), Table 5.
  https://eprint.iacr.org/2023/1269
- **[Krawczyk-1993]** Krawczyk. "Secret Sharing Made Short." CRYPTO '93.
  https://doi.org/10.1007/3-540-48329-2_12

### Embeddings, obfuscation and inversion

- **[Morris-2023]** Morris, Kuleshov, Shmatikov, Rush. "Text Embeddings Reveal
  (Almost) As Much As Text." EMNLP 2023, Tables 1 and 3.
  https://arxiv.org/abs/2310.06816
- **[Huang-2024]** Huang et al. "Transferable Embedding Inversion Attack." ACL
  2024. https://aclanthology.org/2024.acl-long.230/
- **[vec2vec-2025]** Jha, Zhang, Shmatikov, Morris. "Harnessing the Universal
  Geometry of Embeddings." arXiv:2505.12540. https://arxiv.org/abs/2505.12540
- **[LGK-2006]** Liu, Giannella, Kargupta. "An Attacker's View of Distance
  Preserving Maps for Privacy Preserving Data Mining." PKDD 2006.
  https://doi.org/10.1007/11871637_30
- **[LKR-2006]** Liu, Kargupta, Ryan. "Random Projection-Based Multiplicative
  Data Perturbation for Privacy Preserving Distributed Data Mining." IEEE TKDE
  18(1), 2006. https://doi.org/10.1109/TKDE.2006.14
- **[He-2019]** He, Zhang, Lee. "Model Inversion Attacks Against Collaborative
  Inference." ACSAC 2019. https://doi.org/10.1145/3359789.3359824

### Encrypted search, PIR and ORAM

- **[IKK-2012]** Islam, Kuzu, Kantarcioglu. "Access Pattern Disclosure on
  Searchable Encryption." NDSS 2012.
  https://www.ndss-symposium.org/wp-content/uploads/2017/09/06_1.pdf
- **[CGPR-2015]** Cash, Grubbs, Perry, Ristenpart. "Leakage-Abuse Attacks
  Against Searchable Encryption." ACM CCS 2015.
  https://doi.org/10.1145/2810103.2813700
- **[ZKP-2016]** Zhang, Katz, Papamanthou. "All Your Queries Are Belong to Us:
  The Power of File-Injection Attacks on Searchable Encryption." USENIX
  Security 2016. https://files.usenix.org/s3fs-public/cdn/sec16_paper_zhang.pdf
- **[NKW-2015]** Naveed, Kamara, Wright. "Inference Attacks on
  Property-Preserving Encrypted Databases." ACM CCS 2015.
  https://doi.org/10.1145/2810103.2813651
- **[KKNO-2016]** Kellaris, Kollios, Nissim, O'Neill. "Generic Attacks on
  Secure Outsourced Databases." ACM CCS 2016.
  https://doi.org/10.1145/2976749.2978386
- **[PathORAM-2013]** Stefanov et al. "Path ORAM: An Extremely Simple Oblivious
  RAM Protocol." ACM CCS 2013. https://eprint.iacr.org/2013/280
- **[RingORAM-2015]** Ren et al. "Constants Count: Practical Improvements to
  Oblivious RAM." USENIX Security 2015, Table 1.
  https://www.usenix.org/conference/usenixsecurity15/technical-sessions/presentation/ren-ling
- **[SimplePIR-2023]** Henzinger, Hong, Corrigan-Gibbs, Meiklejohn,
  Vaikuntanathan. "One Server for the Price of Two: Simple and Fast
  Single-Server Private Information Retrieval." USENIX Security 2023.
  https://www.usenix.org/conference/usenixsecurity23/presentation/henzinger
- **[YPIR-2024]** Menon, Wu. "YPIR: High-Throughput Single-Server PIR with
  Silent Preprocessing." USENIX Security 2024.
  https://www.usenix.org/conference/usenixsecurity24/presentation/menon
- **[Compass-2025]** Zhu, Patel, Zaharia, Popa. "Compass: Encrypted Semantic
  Search with High Accuracy." OSDI 2025.
  https://www.usenix.org/conference/osdi25/presentation/zhu-jinhao
- **[Pacmann-2025]** Zhou, Shi, Fanti. "Pacmann: Efficient Private Approximate
  Nearest Neighbor Search." ICLR 2025. https://openreview.net/forum?id=yQcFniousM

### Proofs of storage and availability

- **[PDP-2007]** Ateniese et al. "Provable Data Possession at Untrusted
  Stores." ACM CCS 2007 (460 blocks for 1% at 99%).
  https://eprint.iacr.org/2007/202
- **[Juels-Kaliski-2007]** Juels, Kaliski. "PORs: Proofs of Retrievability for
  Large Files." ACM CCS 2007. https://doi.org/10.1145/1315245.1315317
- **[Shacham-Waters-2008]** Shacham, Waters. "Compact Proofs of
  Retrievability." ASIACRYPT 2008. https://eprint.iacr.org/2008/073
- **[DAS-2018]** Al-Bassam, Sonnino, Buterin (and Khoffi in the FC version).
  "Fraud and Data Availability Proofs." arXiv:1809.09044; FC 2021.
  https://arxiv.org/abs/1809.09044

### Storage networks and prices

- **[Storj-WP]** Storj Labs. "Storj: A Decentralized Cloud Storage Network
  Framework" v3. https://static.storj.io/storjv3.pdf
- **[Storj-code]** storj/storj `satellite/metainfo/config.go`, release default
  `29/35/80/110`.
  https://github.com/storj/storj/blob/main/satellite/metainfo/config.go
- **[Storj-payout]** Storj. Node payouts as of 2026-09-01.
  https://storj.dev/node/payouts
- **[Sia-renterd]**
  - SiaFoundation/renterd `api/setting.go` (MinShards 10, TotalShards 30);
  - Sia docs on host storage proofs;
  - sia.tech/provide-storage (median host $1.05/TB-month, 2026-11-12);
  - Sia renting docs (about $3/TB-month including 3× redundancy).

  https://github.com/SiaFoundation/renterd/blob/master/api/setting.go
- **[Hippius]**
  - Hippius docs, "How Arion stores your data": RS k = 10, m = 20; warden
    challenges every 30 s.
  - "Arion Miner Setup": Ubuntu 22.04+.
  - hippius.com/pricing: $6/TB-month.

  https://docs.hippius.com/learn/storage-systems
- **[Filecoin-PoRep]** Filecoin docs, sealing pipeline and storage proving;
  Lotus hardware requirements.
  https://docs.filecoin.io/storage-providers/architecture/sealing-pipeline
- **[S3-price]** AWS S3 price file, 2026-11-09: Standard us-east-1
  $0.023/GB-month for the first 50 TB. https://aws.amazon.com/s3/pricing/
- **[GCS-price]** Google Cloud Storage pricing: Standard us-central1
  $0.020/GiB-month. https://cloud.google.com/storage/pricing
- **[R2-price]** Cloudflare R2 pricing: $0.015/GB-month, free egress.
  https://developers.cloudflare.com/r2/pricing/
- **[B2-price]** Backblaze B2 pricing: $6.95/TB-month.
  https://www.backblaze.com/cloud-storage/pricing
- **[Hetzner]** Hetzner Storage Box BX11: 1 TB for €3.20/month.
  https://www.hetzner.com/storage/storage-box/

### Apple platform

- **[Apple-PSG]** Apple Platform Security guide, "Secure Enclave" (published
  2024-12-19): "a dedicated secure subsystem" that boots only a signed sepOS.
  https://support.apple.com/guide/security/secure-enclave-sec59b0b31ff/web
- **[Apple-PCC]** Apple Developer, "Private Cloud Compute" (June 2026): apps
  may use Apple Foundation Models on PCC; no developer code or models are
  deployed. https://developer.apple.com/private-cloud-compute/
- **[Apple-notarization]** "Notarizing macOS software before distribution."
  https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution
- **[Apple-SIP]** "Disabling and Enabling System Integrity Protection"; and
  "Hardened Runtime."
  https://developer.apple.com/documentation/security/disabling-and-enabling-system-integrity-protection
- **[Apple-DTS]** Apple Developer Forums, thread 674706 (Apple DTS).
  https://developer.apple.com/forums/thread/674706
- **[Apple-AppAttest]** `DCAppAttestService.isSupported`: false for apps
  running on a Mac.
  https://developer.apple.com/documentation/devicecheck/dcappattestservice/issupported
- **[Apple-CryptoKit]** CryptoKit `SecureEnclave`: P256, MLKEM768/1024 and
  MLDSA65/87 only. https://developer.apple.com/documentation/cryptokit/secureenclave
- **[Apple-SMAppService]** `SMAppService` (macOS 13+).
  https://developer.apple.com/documentation/servicemanagement/smappservice
- **[Apple-launchd]** `launchd.plist(5)`, `ProcessType` (macOS 27.2 man page).
- **[Apple-QoS]** "Tuning your code's performance for Apple silicon."
  https://developer.apple.com/documentation/apple-silicon/tuning-your-code-s-performance-for-apple-silicon
- **[Apple-thermal]** `ProcessInfo.ThermalState`.
  https://developer.apple.com/documentation/foundation/processinfo/thermalstate-swift.enum
- **[Apple-LPM]** `ProcessInfo.isLowPowerModeEnabled`.
  https://developer.apple.com/documentation/foundation/processinfo/islowpowermodeenabled
- **[Apple-macmini-power]** Mac mini power consumption, published 2026-09-21.
  https://support.apple.com/en-us/103253
- **[GoFetch-2024]** Chen et al. "GoFetch: Breaking Constant-Time
  Cryptographic Implementations Using Data Memory-Dependent Prefetchers."
  USENIX Security 2024. https://gofetch.fail/
- **[TEE-2025]** Physical interposer attacks on server TEEs:
  - De Meulemeester, Oswald, Verbauwhede, Van Bulck. "Battering RAM: Low-Cost
    Interposer Attacks on Confidential Computing via Dynamic Memory Aliasing."
    IEEE S&P 2026. https://doi.org/10.1109/SP63933.2026.00052
  - Seto et al. "WireTap: Breaking Server SGX via DRAM Bus Interposition." ACM
    CCS 2025. https://wiretap.fail/
  - Chuang et al. "TEE.fail: Breaking Trusted Execution Environments via DDR5
    Memory Bus Interposition." IEEE S&P 2026. https://tee.fail/
  - Vendor advisories: Intel 2025-10-28-001; AMD-SB-3024 and AMD-SB-3040.

### Bittensor

- **[BT-null]** RaoFoundation/subtensor, `docs/guides/null-consensus.mdx` (at
  `d967d13`): the elected highest-stake validator, capacity
  `floor(2500 / mechanism_count)`, raw u16 weights, PoW registration, and
  bounded trimming. Bittensor docs "Running a subnet": at most 2 mechanisms;
  UIDs are shared across mechanisms.
  https://github.com/RaoFoundation/subtensor/blob/d967d13588834f4f5aaa8ef4c8fc1a865d581462/docs/guides/null-consensus.mdx
- **[BT-docs]** Bittensor docs, "Mining": pruning evicts the lowest
  emission-based score among non-immune UIDs.
  https://www.bittensor.com/docs/guides/mining
- **[BT-burn]** Bittensor docs, registration burn: ×1.26 per registration,
  half-life 360 blocks, clamp 0.0005–100 TAO.
  https://www.bittensor.com/docs/guides/mining/burn
- **[BT-emissions]** Bittensor docs, "Emissions" and "Etao": an 18/41/41
  default, owner-settable within bounds since runtime v480.
  https://www.bittensor.com/docs/concepts/emissions
- **[BT-fees]** RaoFoundation/subtensor PR #3183 (spec 467):
  `transfer_keep_alive` costs 0.000083 TAO. The runtime includes
  `Utility.batch`. https://github.com/RaoFoundation/subtensor/pull/3183
- **[TAO-price]** CoinGecko simple price API: $215.67 at 2026-11-12 02:39:53
  UTC. https://api.coingecko.com/api/v3/simple/price?ids=bittensor&vs_currencies=usd

### Networks, energy and tax

- **[Tailscale-NAT]** Anderson. "How NAT traversal works." Tailscale, 2020.
  https://tailscale.com/blog/how-nat-traversal-works
- **[DCUtR-2026]** Trautwein et al. "Large-Scale Measurement of NAT Traversal
  for the Decentralized Web: A Case Study of DCUtR in IPFS." ACM IMC 2026,
  arXiv:2604.12484. https://arxiv.org/abs/2604.12484
- **[Xfinity-cap]** Xfinity. "Xfinity Internet 1.2 TB data plan FAQs."
  https://www.xfinity.com/support/articles/data-usage-plan
- **[Ookla-2026]** Ookla Speedtest Global Index, US median fixed upload
  72.62 Mbit/s for September 2026, via Allconnect (secondary source).
  https://www.allconnect.com/blog/us-internet-speeds-globally
- **[EIA]** EIA *Electric Power Monthly*, Table 5.6.A: US residential average
  18.16 ¢/kWh, August 2026.
  https://www.eia.gov/electricity/monthly/epm_table_grapher.php?t=epmt_5_6_a
- **[IRS-crypto]** IRS. "Frequently asked questions on virtual currency
  transactions," Q9–Q12.
  https://www.irs.gov/individuals/international-taxpayers/frequently-asked-questions-on-virtual-currency-transactions
