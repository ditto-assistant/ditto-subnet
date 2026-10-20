# Source attribution runtime audit: v468-v473

The source-emission collector stopped at block 9117747 because the next runtime
fingerprint was unknown (#2703, same class as #2231). This update admits the
downloaded, verified compressed artifacts for v468, v469, v470, v471 and v473
in one audit pass. Unknown fingerprints still fail closed; runtime-change
boundaries still reset provenance. Disclosure policy is unchanged.

## Artifact identity

Official releases and srtool digests, downloaded 2026-10-05 UTC and
independently hashed (SHA-256 then BLAKE2b-256 of the artifact bytes):

| Release | Source commit | Size | SHA-256 (prefix) | BLAKE2b-256 (live fingerprint) |
| --- | --- | --- | --- | --- |
| [v468](https://github.com/RaoFoundation/subtensor/releases/tag/v468) | `30c70d90f8a3708d85cf95ae992b7a3fe30d2c4c` | 2550095 | `5110d665…` | `0x899a87a4e4610587d81d9237adeb3e420ea524cd39b397c7a9b4c811b0e7af1d` |
| [v469](https://github.com/RaoFoundation/subtensor/releases/tag/v469) | `370bac46fa8cf602c4f8283a0635b3a8b4675394` | 2553157 | `ecf82a02…` | `0x8858cf3545c90255e5f865dec13713fa1d76cac2be6b8f1070256ee572280abe` |
| [v470](https://github.com/RaoFoundation/subtensor/releases/tag/v470) | `923fd1fa7d6eadad3ec16f3941826b86c9c3aa1d` | 2556358 | `e5abec69…` | `0x5675b684d69a07f6f224c2ba9cabef719804911fba40fbe1a2295198c9cb7c47` |
| [v471](https://github.com/RaoFoundation/subtensor/releases/tag/v471) | `c004cebf360f4088187ee49d851dfb1a1eaaf710` | 2559437 | `04385dd7…` | `0x5b0168d2878c1fdcdc424dddca29111b8fe8960bd169742d1c14ce16b22ae381` |
| [v473](https://github.com/RaoFoundation/subtensor/releases/tag/v473) | `f87cada631f81d11683e715a9f059f693992e64a` | 2593406 | `cbd9f5b4…` | `0x7773f5c0a6d6e9ea9ff347edcc491246eec08a5cf441d964ee96f40d7fa65a08` |

Every independently computed SHA-256 matches its release digest exactly, and
the v468 BLAKE2b-256 matches the collector's rejected fingerprint. v472 has no
tag or release; its runtime was the v471 artifact, already covered above and by
the treasury-side review in `docs/audits/collector-finney-v473/`. This is
artifact identity plus a scoped source audit, not an independent reproducible
WASM build.

## Scope of semantic review

Compared the four payout-critical files at each release commit. All are
byte-identical from v467 through v471 (SHA-256):

| File under `pallets/subtensor/src/` | Unchanged SHA-256 |
| --- | --- |
| `coinbase/reveal_commits.rs` | `9b7ed58144a1f002e62073772ee2fc7c416a383a67b410d04114b9a023a3b2c5` |
| `coinbase/block_step.rs` | `a6f63baa625ed50299d7488ed2e2c8c3c3d79056cd6b42a2a3e65f8a7cd0c71c` |
| `epoch/run_epoch.rs` | `222c57244ad24ebaffe79430c4e6b1897d87d2710f591bff3f2c284bd4a2f0a3` |
| `subnets/weights.rs` | `db9e5c8d324f501db8b2d8e1668ab5a0fa44f84ed1857516a8b67423e9ff831e` |

Reviewed every production change between the v467 and v471 release commits:
v468 adds root-basket trading and root dust-row skipping on claims; v469 adds
failed-EVM-call fee refunds and CI gating; v470/v471 are bug fixes in the same
surfaces. They do not change successful reveal events, commit identity,
non-root weight writes, consumed-weight epoch computation, or miner-emission
receipt semantics used by the collector. Stake remains read from the chain for
each payout; no static stake is assumed.

The v471-to-v473 boundary changes exactly one of the four files:
`block_step.rs` drops a call to `populate_root_coldkey_staking_maps()` in
favor of its v2 replacement — root-network coldkey bookkeeping that the
collector does not consume. `reveal_commits.rs`, `run_epoch.rs` and
`subnets/weights.rs` stay byte-identical. The same boundary was independently
reviewed for the treasury collector in `docs/audits/collector-finney-v473/`,
which also records the finalized on-chain boundary (block 9217264).

Runtime-change boundaries between any two audited fingerprints remain
ineligible: a block whose parent runtime differs resets provenance
(`runtime_changed`), exactly as in v466→v467 and v467→v468.

## Read-only chain replay (2026-10-05 UTC)

The live collector failed at block 9117747 with
`runtime fingerprint is not audited: 0x899a87a4…`, matching the v468 artifact
above. The v473 treasury audit pinned the v472→v473 upgrade at block 9217264.
Historical blocks replay with their at-the-time audited fingerprints, so the
entire 2026-09-20 → 2026-10-03 span is attributable once deployed: the cursor
passes 9117747, the 8 pending payouts resolve, and kings crowned since
09-20 gain `emission_confirmed_at` retroactively through the existing
`record_emission_confirmed` late-arriving-proof path.

After deployment, verify with Backroom's source release policy: the cursor
advances past 9117747, `pending_kings` drains, and only qualifying exact
submissions become downloadable. This read-only audit made no production
writes.
