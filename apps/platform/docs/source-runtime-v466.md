# Source attribution runtime audit: v466

The source-emission collector stopped at block 9091160 because the next runtime
fingerprint was unknown. This update admits only the downloaded, verified v466
compressed artifact. Unknown fingerprints still fail closed; runtime transitions
still invalidate all existing provenance. Disclosure policy is unchanged.

## Artifact identity

Official [v466 release](https://github.com/RaoFoundation/subtensor/releases/tag/v466)
and [digest](https://github.com/RaoFoundation/subtensor/releases/download/v466/subtensor-digest.json):
source commit `cdffbe2f7ab0c37ab07884387bfbd6443dca178d`.
Downloaded `subtensor.wasm` on 2026-09-18 UTC and independently hashed its bytes:

- Size: 2542037 bytes
- BLAKE2b-256: `ff4ba0da10fb8ac26fab3e446f23413ef7f91de4a604802097ece0b928d53a8e`
- SHA-256: `9573425dc2231d46e73e62d2977e62ae5076c9edbcdeb05e50a2a4771988a363`

The BLAKE2b value matches the live collector's rejected fingerprint. This is
artifact identity plus a source audit, not an independent reproducible WASM build.

## Scope of semantic review

Compared v464 (`5cd66b8597b3ce5f9f2bade2b11c91af57df923d`) to v466.
Fetched both versions of the four critical files and compared their bytes:

| File under `pallets/subtensor/src/` | Unchanged v466 SHA-256 |
| --- | --- |
| `coinbase/reveal_commits.rs` | `9b7ed58144a1f002e62073772ee2fc7c416a383a67b410d04114b9a023a3b2c5` |
| `coinbase/block_step.rs` | `a6f63baa625ed50299d7488ed2e2c8c3c3d79056cd6b42a2a3e65f8a7cd0c71c` |
| `epoch/run_epoch.rs` | `222c57244ad24ebaffe79430c4e6b1897d87d2710f591bff3f2c284bd4a2f0a3` |
| `subnets/weights.rs` | `db9e5c8d324f501db8b2d8e1668ab5a0fa44f84ed1857516a8b67423e9ff831e` |

Reviewed surrounding production changes: root basket dividend retention and
liquidity accounting, bounded root claims and unstaking fees, proxy restriction
hardening, retryable root-weight removal and share-pool reconciliation migrations,
transaction payment fee discounts, and runtime migration wiring. These do not
change the successful reveal events, commit identity, non-root weight writes,
consumed-weight epoch computation, or miner-emission receipt semantics used here.
Stake remains read from the chain for each payout; no static stake is assumed.

The upgrade boundary is intentionally ineligible even for this known runtime.
Existing tests retain ambiguous-write, unknown-runtime and quorum rejection.
Explicit v464-to-v466 transition and stable-v466 successful reveal tests cover
admission without weakening provenance reset. Same-block payout weight updates
remain rejected pending a separate ordering audit and implementation.
