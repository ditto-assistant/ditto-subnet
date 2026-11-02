# Finney v472 collector contract audit, 2026-10-03

The collector now pins finalized compressed `:code` Blake2-256
`0x43bc67be9df30636d7e948e7bdb1ed065f2fb92029458cc939abf89d76d8ada3`.
SHA-256 is `5e4ff64971b4baa31738215b4f79b0db3aaee1833d0a4f3ca62ff36de50f394f`,
2,561,862 bytes, spec472/transaction1 on the already-pinned Finney genesis.
Observation hash, metadata hash and all source/build fingerprints are in
[manifest.json](manifest.json). This is evidence for that exact runtime and
collector contract, not an upstream release assertion, runtime upgrade,
collector policy signature or production activation.

## Source reconstruction and bounded build

No official v472 tagged artifact/source binding was available during this
check. Start from the official public [v471 source](https://github.com/RaoFoundation/subtensor/tree/c004cebf360f4088187ee49d851dfb1a1eaaf710).
Apply [reconstructed-source.patch](reconstructed-source.patch): the six escrow
production paths from upstream [47742598](https://github.com/RaoFoundation/subtensor/commit/47742598185b9adc900fe5fcae4da96902f19b6c),
plus the already-deployed spec472 number. The resulting Git tree is
`8594b96810d83e1538745c863cfaf1b7108fa114`. No AlphaV2 migration source from the
larger release473 branch is included. No upstream code was pushed or upgraded.

A fresh BuildKit builder on verified Debby built the v471 checked-in srtool
Dockerfile against srtool v0.18.3 advertised tag and exact checksum
`0a446889c5e60abe92a41e426377276f5c7295e6`, rustc1.89.0, production profile,
`--features=metadata-hash`. Builder/runtime bounds were four CPUs/12GiB;
runtime compilation completed with exit0. The candidate unmodified compressed
WASM is 2,562,172 bytes and **does not have the live byte hash**.

## Exactly isolated nondeterministic difference

Every section except code is byte-identical, including data, metadata-bearing
constants, names, imports, exports, runtime version and APIs. Every code body
except `<wasmi_collections::hash::RandomStateImpl as Default>::default` is
byte-identical. Wabt1.0.42 (asset SHA-256
`84895407a6bbb80e918f33b16b2fb2206021c150b6bc9ff6f761263a745ab131`) disassembly
shows exactly 22 `i64.const` operands differ within that one function; all its
other instructions, loads/stores/calls/control flow and instruction order are
identical. The complete independently produced function text and comparison
reports are included here. `verify_rebuild.py` checks both full expanded WASM
and full WAT, rejecting other body/section/instruction changes.

The lock's ahash0.8.12 `get_fixed_seeds()` with `compile-time-rng` obtains its
constants from const-random-macro0.1.16. That macro's `span.rs` uses build-time
`getrandom` unless `CONST_RANDOM_SEED` was provided. Both published crate hashes
match the source lock. The differing function initializes the Wasmi interpreter
collection hasher; it is not the collector registration/transfer/coinbase path.
This documented source binding has an isolated random-constant exception; it
is **not** a claim of an unmodified byte-for-byte reproducible build. The live
runtime gate still compares its exact compressed hash, with no normalization,
version-only acceptance or general seed-function exemption at runtime.

## Collector scope and changes from the prior audit

Ten critical source files were compared byte-for-byte to audited v470
`923fd1fa7d6eadad3ec16f3941826b86c9c3aa1d`; exact paths/digests are in the manifest:
registration and burn-limit path, proxy dispatch/allowlists, same-hotkey stake
transfer, collateral lock/account paths, coinbase liquid credit and StakeInfo.
The v471 changes add wrapper walking for basket-trade dust checks and a basket
minimum; the allowed collector calls do not enter that trade path. The v472
backport changes subnet-creation escrow settlement/cancellation; it does not
change registering a neuron on the existing SN118 network. One stake-helper
change removes stale staking-hotkey tracking when unstaking to zero; availability
must still come from the unchanged aggregate lock API.

The reviewed contracts remain: native Registration permits only
`register/register_limit/burned_register`; Transfer permits the three Balances
transfers plus `transfer_stake/transfer_stake_and_hotkey`. Application signing
still narrows this to bounded `register_limit` and same-SN118/same-hotkey
`transfer_stake`. The burn cap is enforced before registration payment. Exact
outer/inner success, payer/tip/fees, UID/Owner/Keys, and paired alpha effects
remain required. Gross incentives are not spendable proof; self-directed
`AutoStakeAdded` after collateral capture authorizes only the liquid amount.
Deprecated alpha shares and StakeInfo's hardcoded `locked=0` are not spendable
balance substitutes.

Live SDK10.5.0 read-only QA is a separate compatibility check. No signer secret,
key, transaction or production routing is acquired by this audit.
