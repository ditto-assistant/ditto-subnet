# Source attribution runtime audit: v475–v478

The deployed source verifier admits artifacts only through v473. At finalized
Finney block 9277846 (`0xab3b3175db31481154ace5e223242cdb9e618c2b30e9e815e141f72d422ec252`),
SN118 used Yuma and runtime 478. The unmodified reader rejected its exact code
hash with `runtime fingerprint is not audited`. This is a separate hard stop
from the archive capacity backlog in #2850: a working archive cannot replay
through these runtimes until their attribution semantics are admitted.

## Artifact identity

The official compressed artifacts were downloaded on 2026-10-14 UTC. Each
independently computed SHA-256 and BLAKE2b-256 matches its srtool digest, and
the digest source commit matches the pinned release commit. Full identities
and the payout-critical file hashes are in
[source-runtime-v475-artifacts.json](source-runtime-v475-artifacts.json).

- [v475](https://github.com/RaoFoundation/subtensor/releases/tag/v475):
  source `d1718c99c34cf96abbf2bf09c0e9e48b945c76f5`;
  code `0x557634c8c31bc639ea6552e297dcfb781cd7d9bdd491a5352c151305db33d3a0`.
- [v476](https://github.com/RaoFoundation/subtensor/releases/tag/v476):
  source `d6dd557a7e425701c8237d7e0e0cae84c25b682b`;
  code `0x12a5ec0b0d4d1f72281a4e41a926ccce6c4cfda6134adf40eeb13847b6a8cd12`.
- [v477](https://github.com/RaoFoundation/subtensor/releases/tag/v477):
  source `5c6e83e2f4936f257234f246957f75684c35227c`;
  code `0x18b52484d9c5d7523f704c86b17b2ab959fffbc6edb6766a77e9877f0ad19387`.
- [v478](https://github.com/RaoFoundation/subtensor/releases/tag/v478):
  source `850f2755bb05a3851f948489f0ab27b0645cebce`;
  code `0x7e722b3aa5c9192c98665a5d65fa91364bf9055e21fc90efce6d59bf4ef414e1`.
  This matches the observed finalized-chain fingerprint.

There is no official v474 tag or release in the inspected release history.
This change adds no guessed v474 fingerprint. Unrecognized artifacts continue
to fail closed. This establishes artifact identity and a scoped source audit;
it is not an independent reproducible WASM build.

## Attribution semantics

Reviewed the [v473…v475 diff](https://github.com/RaoFoundation/subtensor/compare/v473...v475)
and each following release through v478. The seven payout-critical files
recorded in the manifest are byte-identical from v475 through v478.

`coinbase/block_step.rs` is also byte-identical to v473. Initialization still
reveals eligible commitments before running coinbase. The only change in
`coinbase/reveal_commits.rs` uses key iteration to prune expired epochs instead
of decoding each key/value pair; the removed epochs, successful reveals and
event ordering are unchanged.

The newer runtime has two reward algorithms. `SubnetEpochConsensus` is a
ValueQuery enum with Yuma at index 0 and Null at index 1. Null elects a sole
validator, uses a different commitment queue and computes integer reward
allocations separately. The existing >2/3 effective-stake source proof is
audited for Yuma. A fingerprint admission therefore requires an explicit
decoded `Yuma` at **both** parent and current block hashes. Missing, malformed,
Null or unfamiliar values fail closed; these reads use the same finalized-state
cache as the other controls. Legacy fingerprints do not query a storage entry
their metadata lacks.

Any SN118 `EpochConsensusSet` event invalidates all source provenance for that
block, including an intra-block Null-to-Yuma round trip whose endpoint modes
both read Yuma. This setter can change validator permits after initialization;
its post-state must not be used to attribute the initialization payout. The
same guard applies when UID mappings also change. Events for other subnets do
not reset SN118 provenance.

In `epoch/run_epoch.rs`, the Yuma branch retains permitted/active stake
selection, row normalization, weighted-median clipping and rank computation.
The new exact integer apportionment and sole-validator input apply only to
Null. The bond cutoff now prefers the last Yuma epoch, with the previous
last-mechanism fallback for an uninitialized value; for continuously Yuma
subnets it refers to the same successful epoch. The source proof reads the
actual consumed vectors, voting stake and maximum positive miner payout.

`subnets/mechanism.rs` keeps Yuma's aggregate emission budget and split; the
separate funded slices are used by Null. `coinbase/run_coinbase.rs` can reduce
the shared scheduler cap when another subnet has a due Null epoch. Attribution
continues to use observed successful distribution blocks and events rather
than assuming a fixed tempo. Its new owner-credit helper falls through to the
existing stake-credit path for Yuma. Owner-associated exclusions and successful
distribution evidence remain required.

The new weight admission limits do not change successful Yuma reveal identity.
Unknown/manual writes still invalidate provenance. The v478 idle migration
repairs staking-hotkey association lists; it does not rewrite the consumed
weight matrix, successful distribution events or signed receipt identity.

All existing boundaries remain: a runtime transition resets provenance,
unrecognized fingerprints never advance, direct weights and multiple
mechanisms remain unsupported, and a public release still needs the exact
agent UUID/SHA, finalized commit inclusion, immutable ledger pin, conservative
>2/3 effective stake, maximal positive miner payout and configured embargo.

## Read-only reproduction and rollout

Before this patch, the actual reader rejected the live v478 fingerprint. With
the patch, it read finalized block 9277867
(`0x5aa1689409bbccd3cb2ee66c6befb905583c84fcfe269c544fe6de46246c4b56`)
successfully: Yuma, no payout, no updates and no reset. No production state was
written. This proves the parser accepts the new runtime; it does not prove a
champion's payout has been attributed.

Tests cover each verified artifact, normal reveals, upgrade invalidation,
both consensus boundaries, malformed consensus values, legacy storage
compatibility and the existing payout attribution invariants.

After merge and deployment, Backroom `get_source_release_policy` reports
`audited_runtime_code_hashes` from the running verifier; absence means unknown
on older releases. Compare the rejected or collected hash with that served
list. Then verify sustained cursor progress, exact-agent payout attribution
and the public artifact endpoint. Archive access/quota and missing signed
validator receipts remain separate recovery conditions. Never reset the cursor,
synthesize a payout or substitute crown age for the required proof.
