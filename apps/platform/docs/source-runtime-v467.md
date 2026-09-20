# Source attribution runtime audit: v467

The source-emission collector stopped at block 9095775 because the next runtime
fingerprint was unknown. This update admits only the downloaded, verified v467
compressed artifact. Unknown fingerprints still fail closed; the v466-to-v467
transition still invalidates existing provenance. Disclosure policy is unchanged.

## Artifact identity

Official [v467 release](https://github.com/RaoFoundation/subtensor/releases/tag/v467)
and [digest](https://github.com/RaoFoundation/subtensor/releases/download/v467/subtensor-digest.json):
source commit `c6bcb4a7400764c94c1d1b1938514c6c2dd3d33b`.
Downloaded `subtensor.wasm` on 2026-09-20 UTC and independently hashed its bytes:

- Size: 2541439 bytes
- BLAKE2b-256: `2f175dcc64196ec8a6b9235f8d7cfd84efef6c68bb925c4455949591cef9f6d2`
- SHA-256: `5a4218a3198cf276bf531643ca6813438781b72dc9fac57fa83a3ffe49f9a81a`

The BLAKE2b value matches the live collector's rejected fingerprint. This is
artifact identity plus a source audit, not an independent reproducible WASM build.

## Scope of semantic review

Compared v466 (`cdffbe2f7ab0c37ab07884387bfbd6443dca178d`) to v467. Fetched
both versions of the four payout-critical files and compared their bytes:

| File under `pallets/subtensor/src/` | Unchanged v467 SHA-256 |
| --- | --- |
| `coinbase/reveal_commits.rs` | `9b7ed58144a1f002e62073772ee2fc7c416a383a67b410d04114b9a023a3b2c5` |
| `coinbase/block_step.rs` | `a6f63baa625ed50299d7488ed2e2c8c3c3d79056cd6b42a2a3e65f8a7cd0c71c` |
| `epoch/run_epoch.rs` | `222c57244ad24ebaffe79430c4e6b1897d87d2710f591bff3f2c284bd4a2f0a3` |
| `subnets/weights.rs` | `db9e5c8d324f501db8b2d8e1668ab5a0fa44f84ed1857516a8b67423e9ff831e` |

Reviewed every production source change between the release commits. Changes
are confined to root-basket staking claims and flush accounting, transaction-fee
discount and wrapper logic, runtime wiring for those fees, generated SDK/docs,
and tests. They do not change successful reveal events, commit identity,
non-root weight writes, consumed-weight epoch computation, or miner-emission
receipt semantics used by the collector. Stake remains read from the chain for
each payout; no static stake is assumed.

The upgrade boundary is intentionally ineligible even for this known runtime.
Tests cover the v466-to-v467 transition, stable-v467 successful reveal, and
unknown-runtime rejection without weakening the existing provenance controls.
