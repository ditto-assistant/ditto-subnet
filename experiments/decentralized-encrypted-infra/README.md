# SN-473 feasibility experiments (throwaway, not production)

> **These are experiments, not production code.** They exist only to put
> measured numbers behind
> [`docs/design/decentralized-encrypted-infra.md`](../../docs/design/decentralized-encrypted-infra.md).
> Nothing here is wired into a release, a validator, Platform, or Ditto
> Desktop. Every input is synthetic. No real user data, real embeddings, or
> trained adapters are used anywhere.

The release graph ignores `experiments/**` (`release/components.toml`). These
files belong to no component and ship nowhere.

## What runs

| Script | Question | Synthetic input |
|---|---|---|
| `python/export_adapter_fixture.py` | Exports the pilot's synthetic adapter and queries, plus `reference.apply` outputs | seeded generators in `research/query-adapter-pilot` (`reference.synthetic_adapter`, `synthetic_query`) |
| `go/cmd/ckks adapter` | Can a Mac mini run the Personal Query Adapter (768→128→768, GELU) on an **encrypted query**, with weights in the clear or **encrypted** too? Latency, key and ciphertext sizes, and parity against the pilot bar | the fixture above |
| `go/cmd/ckks similarity` | Encrypted query × 4,096-vector blocks of 768-d unit vectors, against a **public** corpus held in the clear or a **Ditto-encrypted** corpus | seeded random unit vectors with 10 planted near-duplicates |
| `python/tfhe_adapter_concrete.py` | The same adapter as a **TFHE** circuit (Zama Concrete ML 1.9.0) | the fixture above |
| `go/cmd/storage` | Client-side cost of the storage track: AES-256-GCM, Reed–Solomon at public-network parameters, SHA-256 Merkle storage proofs | 64 MiB of seeded pseudorandom bytes |
| `python/durability.py` | Availability, durability, repair traffic, sybil withholding, and challenge counts for replication vs erasure coding | closed-form binomial math, no data |
| `python/sn118_chain_snapshot.py` | SN118 UID cap, registration burn, and pool price (read-only, public Finney RPC, no wallet) | public chain state |

The CKKS code uses [Lattigo v6.2.0](https://github.com/tuneinsight/lattigo)
(pure Go, Apache-2.0) because Ditto's backend is Go and the PyPI `openfhe`
wheel failed on this host: `openfhe==1.6.1.0.24.4` is tagged `py3-none-any`
but ships only a Linux x86_64 extension. Concrete ML 1.9.0 installs from
wheels only on Python 3.11; on 3.12, `onnxoptimizer==0.3.13` has no macOS
arm64 wheel and needs `cmake` to build.

## Run

```bash
cd experiments/decentralized-encrypted-infra
./run_all.sh                 # CKKS, storage, durability (~2 minutes)
./run_all.sh --with-tfhe     # + Concrete ML TFHE (Python 3.11, ~1 GB of wheels, ~2 minutes)
./run_all.sh --with-chain    # + read-only SN118 chain snapshot
OUT_DIR=/tmp/rerun ./run_all.sh   # write elsewhere instead of overwriting results/
```

Requirements: Go ≥ 1.24 and `uv`. Each Python script declares its own
dependencies inline (PEP 723), so `uv run --script` installs them into an
ephemeral environment.

## Host

All numbers below come from one machine on 2026-11-12 (UTC). The committed JSON
is the first run. A second end-to-end `run_all.sh` run, written to a scratch
`OUT_DIR`, reproduced it:

- CKKS latencies and throughput within ±2%, with identical parity;
- AES-GCM within 2%;
- Reed–Solomon encode within about 15%, since it is goroutine-parallel;
- TFHE within about 20% (19.5–24.2 s per query), failing parity both times;
- identical durability math.

| Field | Value |
|---|---|
| Machine | **Mac mini** (Mac16,11), Apple **M4 Pro**, 14-core CPU (10 performance + 4 efficiency), 64 GB |
| OS | macOS 27.2 (26B123) |
| Go / Lattigo / reedsolomon | go1.27.3 / v6.2.0 / klauspost/reedsolomon v1.14.2 |
| Python / Concrete ML | 3.11.15 / 1.9.0 |

A base Mac mini M4 has 4 performance cores, so expect roughly 40% of the
10-worker throughput below. Single-core latencies should be similar.

## Results

### 1. Query adapter on an encrypted query (CKKS, N = 2^14, 128-bit)

Circuit: Ditto encrypts the 768-d query, padded to 1,024 slots. The miner runs:

- Layer 1 as a 128-diagonal baby-step/giant-step transform plus an inner sum,
  so its output comes back replicated;
- GELU as a Chebyshev polynomial on `[-B, B]`. `B` bounds every pre-activation
  exactly, because queries are unit norm: `|z_i| ≤ ‖W1[:,i]‖₂ + |b1_i|`;
- Layer 2 as another 128-diagonal transform;
- the residual add.

Ditto decrypts and normalizes. The change of basis is folded into W1, and α is
folded into W2. Every parameter set satisfies the HE-standard bound log QP ≤ 438
for N = 2^14.

| Config | Depth | log QP | Miner p50 / p95 (1 core) | Layer 1 / GELU / Layer 2 | 10 workers | Galois keys | Query in | Result out | Min cos / max abs vs pilot `reference.apply` |
|---|---|---|---|---|---|---|---|---|---|
| plaintext weights, GELU deg 7 | 5 | 341 | 140.4 / 143.6 ms | 97.3 / 23.3 / 19.1 ms | 56.9 q/s | 27 keys, 284 MiB | 1.50 MiB | 256 KiB | 0.9999999976 / 8.0e-6 |
| **plaintext weights, GELU deg 15** | 6 | 386 | **183.7 / 190.9 ms** | 123.5 / 40.3 / 19.7 ms | **47.4 q/s** | 27 keys, **378 MiB** | 1.75 MiB | 256 KiB | 0.9999999999999986 / 2.3e-8 |
| plaintext weights, GELU deg 31 | 7 | 431 | 248.4 / 256.0 ms | 150.2 / 78.2 / 19.8 ms | 34.7 q/s | 27 keys, 486 MiB | 2.00 MiB | 256 KiB | 0.9999999999999989 / 2.3e-8 |
| **encrypted weights**, GELU deg 15 | 6 | 386 | **339.9 / 350.7 ms** | 249.2 / 39.6 / 50.1 ms | **24.1 q/s** | 26 keys, 364 MiB | 1.75 MiB | 256 KiB | 0.9999999999999982 / 2.3e-8 |

- **Every configuration passes the pilot parity bar** (cos ≥ 1 − 1e-6,
  max abs ≤ 1e-5). Degree 15 clears it by about 400× on max abs. Degree 7
  passes with only 1.25× headroom.
- **Ditto side:** about 2.3 ms to encrypt a query and 0.13 ms to decrypt,
  decode and normalize a result. Key generation takes 0.35–0.62 s.
  Relinearization keys are 11–18 MiB.
- **Encrypted weights cost 290 MiB per adapter version on the miner.**
  That is 376× the 771.5 KiB of float32 weights.
- **For scale:**
  - The same adapter in plaintext takes 0.0175 ms p50 in the pilot's numpy
    bench. That was `local.warm` on an Apple M5 Pro; see
    `research/query-adapter-pilot/docs/measurements.md`.
  - A naive float64 Go loop on this host takes about 0.15 ms.
  - So the CKKS miner path is about 10,000× the pilot's plaintext compute,
    before any network.

### 2. Encrypted embedding similarity (CKKS, N = 2^13, depth 1, 128-bit)

The query is padded to 1,024 and replicated 4×. Each block is a 4,096 × 1,024
matrix applied with 1,024 generalized diagonals. One result ciphertext holds
4,096 scores.

| Corpus held by the miner | Per 4,096-vector block, 1 core (p50 / p95) | Per 10k vectors, 1 core | 10 workers | Miner holds per block | Expansion vs float32 | Query / scores ciphertext | Max score error | Top-10 |
|---|---|---|---|---|---|---|---|---|
| public corpus in the clear (encoded) | 45.7 / 46.1 ms | 111 ms | 772k vectors/s | 192 MiB | 16.0× | 256 / 128 KiB | 7.7e-9 | 10/10 |
| **Ditto-encrypted corpus** | 119.7 / 135.0 ms | **292 ms** | 260k vectors/s | 256 MiB | **21.4×** | 256 / 128 KiB | 8.7e-9 | 10/10 |

Galois keys total 47 MiB at this depth. Ditto encrypts a query in 0.38 ms and
decrypts a block of scores in 0.12 ms.

### 3. The same adapter under TFHE (Concrete ML 1.9.0, 6-bit)

| Measure | Value |
|---|---|
| Programmable bootstraps per query | 1,664 |
| Execute latency (all 14 cores) | **19.5 s, 20.3 s** (2 queries); the second run measured 21.2 s and 24.2 s |
| Bootstrap + key-switch keys | 448.5 MiB + 336.8 MiB |
| Encrypted output | 48.0 MiB per query |
| Accuracy vs pilot (executed) | min cos **0.99892**, max abs **4.7e-3**. **Fails** the pilot bar by about 470× on max abs |
| Accuracy (simulated, 32 queries) | min cos 0.99877, max abs 5.1e-3 |

TFHE needs low-bit quantization, so it cannot meet a float parity bar here.
Raising the bit width raises the cost of every bootstrap steeply. For this
float workload, CKKS on one core (184 ms) is about 100× faster in wall-clock
time than TFHE on all fourteen cores (19.5 s).

### 4. Storage track client cost (Go, arm64 crypto extensions)

| Operation | 1 goroutine | 10 goroutines |
|---|---|---|
| AES-256-GCM seal (1 MiB chunks) | 5,548 MiB/s | 40,493 MiB/s |
| AES-256-GCM open | 7,539 MiB/s | 37,373 MiB/s |

| Reed–Solomon (k of n) | Expansion | Encode (input MiB/s) | Reconstruct with n−k erasures (MiB/s) |
|---|---|---|---|
| 4 of 12 | 3.00× | 8,196 | 13,660 |
| 10 of 30 (Sia-like) | 3.00× | 11,444 | 11,176 |
| 16 of 32 | 2.00× | 13,310 | 12,773 |
| 29 of 80 (Storj-like) | 2.76× | 5,826 | 5,898 |

Reed–Solomon numbers use the library's default internal parallelism.

The committed `results/storage.json` predates a shard-size rounding fix made
in review: its `shard_kib` truncates 6553.6 and 2259.9 KiB shards to 6553 and
2259. `go/cmd/storage` now rounds up, and the throughput figures are unaffected.

SHA-256 Merkle tree over a 16 MiB shard with 4 KiB leaves:

- build: 7.3 ms (2,189 MiB/s);
- one storage-proof response: the 4 KiB leaf plus 384 B of siblings;
- prove: 0.14 µs; verify: 2.8 µs.

**Takeaway:** client-side encryption, coding and proofs run at GB/s. A home
uplink is roughly 1,000× slower, so bandwidth, not CPU, is the constraint.

### 5. Durability math (independent nodes; correlated failures make it worse)

From `results/durability.json`. "Nines" is −log10(probability). Failure probabilities are summed directly from the lower binomial tail, so tiny values do not cancel to zero. Anything above about 10 nines describes the independence model, not real homes.

| Each node online | 3 copies | 5 copies | RS 10-of-30 (3×) | RS 29-of-80 (2.76×) |
|---|---|---|---|---|
| 50% | 0.90 | 1.51 | 1.67 | 2.32 |
| 70% | 1.57 | 2.61 | 5.14 | 9.95 |
| 80% | 2.10 | 3.49 | 8.35 | 17.53 |
| 90% | 3.00 | 5.00 | 14.24 | 31.79 |

These are availability nines, the share of time an object can be read.

Probability that an adversary holding a fraction β of randomly placed pieces
can make an object unrecoverable by withholding:

| β | 5 copies | RS 10-of-30 | RS 29-of-80 |
|---|---|---|---|
| 10% | 1.0e-5 | 5.8e-15 | 1.6e-32 |
| 33% | 4.1e-3 | 4.4e-5 | 7.1e-9 |
| 50% | 3.1e-2 | 2.1e-2 | 4.8e-3 |

Random challenges needed to catch a holder missing 1% of its pieces: 459 at
99% confidence, 1,375 at 1 − 1e-6.

### 6. SN118 chain snapshot (finalized block 9,487,564)

| Item | Value |
|---|---|
| Consensus | `SubnetEpochConsensus` Yuma, `MechanismCountCurrent` 1, no `MechanismEmissionSplit` |
| UIDs | 256 of 256 in use (`MaxAllowedUids` 256, `MaxAllowedValidators` 64). Under Null consensus, capacity would be `floor(2500 / mechanisms)`. |
| Registration | Burn route on at 0.0005 TAO (the `MinBurn` floor). PoW route off. Each registration multiplies the burn by 1.26, which halves every 360 blocks. |
| Immunity period | 300 blocks (~1 hour) |
| Pool spot price | 0.002962 TAO per alpha (`SubnetTAO / SubnetAlphaIn`) |
| Alpha emission | 1 alpha/block. 41% to miners is 2,952 alpha/day if fully released, ≈ 8.74 TAO/day at spot before slippage. |

## Caveats

- Two runs on one host. CKKS repeats within ±2% and Reed–Solomon within about
  15%. Treat differences under about 10% as noise, and other Macs as untested.
- Every number is local compute. None includes a network round trip, TLS, or
  a home uplink.
- Encrypted payloads include:
  - a 1.75 MiB query and a 256 KiB result per adapter call;
  - 378 MiB of evaluation keys, shipped once per miner per key rotation.

  On a 20 Mbit/s uplink, the result alone takes about 0.1 s.
- The parity bar and adapter shape come from the pilot's synthetic adapter.
  A trained adapter can have larger `‖W1[:,i]‖`. That widens the GELU interval
  and may need a higher polynomial degree. The cost is on the
  degree-7/15/31 rows above.
- The HE security bound is the HomomorphicEncryption.org standard table for
  ternary secrets at 128 bits. No custom security estimate was run.
