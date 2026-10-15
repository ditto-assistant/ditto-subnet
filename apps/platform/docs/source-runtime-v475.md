# Source attribution runtime audit: v475, v477, v478

The source-emission collector stops at block 9233781, where Finney moved from
the audited v473 runtime to v475. Later boundaries are v477 at 9267632, v478 at
9276925 and v479 at 9286202. This update admits v475, v477 and v478 **only
while SN118's `SubnetEpochConsensus` is Yuma**. v479 has no published release
yet and stays unaudited, so the collector still fails closed at 9286202.
Disclosure policy is unchanged.

## Artifact identity

Official releases, downloaded 2026-10-15 UTC and independently hashed
(SHA-256, then BLAKE2b-256 of the artifact bytes). Each SHA-256 appears in its
release's `subtensor-digest.json`, and each BLAKE2b-256 equals the live
`state_getStorageHash(":code")` fingerprint after its upgrade block.

| Release | Source commit | Size | SHA-256 (prefix) | BLAKE2b-256 (live fingerprint) | First block |
| --- | --- | --- | --- | --- | --- |
| [v475](https://github.com/RaoFoundation/subtensor/releases/tag/v475) | `d1718c99c34cf96abbf2bf09c0e9e48b945c76f5` | 2632482 | `00ef6bf4…` | `0x557634c8c31bc639ea6552e297dcfb781cd7d9bdd491a5352c151305db33d3a0` | 9233781 |
| [v477](https://github.com/RaoFoundation/subtensor/releases/tag/v477) | `5c6e83e2f4936f257234f246957f75684c35227c` | 2630752 | `e226dd03…` | `0x18b52484d9c5d7523f704c86b17b2ab959fffbc6edb6766a77e9877f0ad19387` | 9267632 |
| [v478](https://github.com/RaoFoundation/subtensor/releases/tag/v478) | `850f2755bb05a3851f948489f0ab27b0645cebce` | 2634898 | `1d64a44f…` | `0x7e722b3aa5c9192c98665a5d65fa91364bf9055e21fc90efce6d59bf4ef414e1` | 9276925 |

v476 was a prerelease that never ran on Finney. This is artifact identity plus
a scoped source audit, not an independent reproducible WASM build.

## Scope of semantic review

Payout-critical files under `pallets/subtensor/src/` (SHA-256 prefixes). v475,
v477 and v478 are byte-identical to each other in every file below, so only
the v473 → v475 boundary needs review:

| File | v473 | v475 = v477 = v478 |
| --- | --- | --- |
| `coinbase/reveal_commits.rs` | `9b7ed58144` | `33098bed26` |
| `coinbase/block_step.rs` | `2286c0bc60` | `2286c0bc60` (unchanged) |
| `epoch/run_epoch.rs` | `222c57244a` | `59e3a71468` |
| `subnets/weights.rs` | `db9e5c8d32` | `6dd0d7831c` |
| `macros/events.rs` | `d310e05848` | `95c2b3554f` |
| `coinbase/run_coinbase.rs` | `09cce211f3` | `18c68fb642` |

v475 introduces `SubnetEpochConsensus` (`Yuma`, the default, or `Null`, which
uses only the largest-stake validator's weights and stake-proportional
dividends). Findings, from the whitespace-insensitive diffs:

- **`reveal_commits.rs`.** The only change is expired-commit cleanup iterating
  `iter_key_prefix` instead of `iter_prefix`. It removes the same entries, so
  commit identity and successful reveal events are unchanged.
- **`weights.rs`.** Every behavioral change is gated on `EpochConsensus::Null`:
  the commit-time permit check, raw instead of max-upscaled weights, the
  max-weight test, `check_validator_permit`, and Null commit queueing. The
  rest is dispatch-weight accounting. Under Yuma, `internal_set_weights`
  stores and emits `WeightsSet` exactly as in v473.
- **`run_epoch.rs`.** About 500 changed lines, most of them the Yuma body
  re-indented into an `else` branch. Under Yuma, consensus, clipping, trust,
  incentive and the `server_emission` computation are unchanged. The bond
  calls change formatting only. The bond-registration cutoff now prefers
  `LastYumaStepBlock`, which only differs after a Null-to-Yuma switch and
  affects validator bonds, not miner incentive. `IncentiveAlphaEmittedToMiners`
  keeps its fields and placement.
- **`run_coinbase.rs` and `subnets/mechanism.rs`.**
  `epoch_with_mechanism_budgets` recombines the same three pools into the
  previous total for Yuma and splits it exactly as before; the per-pool
  budgets are used only for Null. `LastMechansimStepBlock` is still written on
  every epoch, and `LastYumaStepBlock` is added beside it. Owner-cut crediting
  moves into a helper, and the collector excludes owner hotkeys either way.
- **`events.rs`.** Adds `EpochConsensusSet` and `NullUidsPruningProgress`. No
  existing event changes shape.

`SubnetEpochConsensus(118)` read `Yuma` after each upgrade boundary and at the
head on 2026-10-15.

## Enforcement

- `read_source_emission_block` reads `SubnetEpochConsensus` at the parent and
  the block for these runtimes, and raises `unsupported epoch consensus`
  unless both are `Yuma`. Pre-v475 runtimes never read the storage, which
  does not exist there.
- The sparse-replay planner requires `Yuma` at both range endpoints, or else
  requests the full scan. It also flags `EpochConsensusSet` (and the new
  pruning events) as structural, so a switch inside a range reaches the
  fail-closed reader.
- Runtime-change boundaries between audited fingerprints still reset
  provenance (`runtime_changed`).
