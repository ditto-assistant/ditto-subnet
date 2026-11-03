# Source attribution across runtime upgrades

The source-emission collector proves that a king earned completed winner
emissions before its source is released. It used to read a block only when the
runtime's WASM `:code` hash matched a hand-audited release artifact
(`AUDITED_RUNTIME_CODE_HASHES`). Every Finney upgrade therefore froze source
release until someone audited the new artifact: v467 (#2231), v468 (#2703),
v475 (#2941), and v472, which stalled the cursor at block 9197055 from
2026-10-02 because v472 was an untagged deploy with no release to audit
(its artifact is `0x43bc67be…`, not v471's as `source-runtime-v468.md` assumed).
v479 is untagged too.

## Interface admission

`admit_runtime` (`ditto/chain/source_emission_verifier.py`) still admits the
audited artifacts directly. Any other runtime is admitted when its **consumed
interface** matches an audited runtime's:

- `ditto/chain/runtime_interface.py` reads the runtime's own metadata
  (`init_runtime(block_hash=…)`) and hashes the SCALE shape of exactly the
  storage items, event variants and calls the verifier, its catch-up range
  planners and the payout receipt reader decode (`SOURCE_EMISSION_SURFACE`).
  Storage modifiers, hashers and defaults are included; docs and type names
  are not. `System.Events` is not hashed whole, since it embeds every pallet's
  events; the consumed variants are hashed by name instead. The outer
  `RuntimeCall`/`RuntimeEvent` enums are hashed by path only for the same
  reason.
- `ditto/chain/audited_runtime_interfaces.py` pins the audited digests,
  derived from live metadata of audited v473 (no epoch consensus) and v478
  (with `SubnetEpochConsensus`).
- A runtime that has `SubnetEpochConsensus` is read only while it is `Yuma`,
  the same rule the v475 audit set for its artifacts.
- Admissions are cached per `:code` hash, which fixes the metadata. A refused
  or unreadable interface is not cached, so a transient metadata failure
  retries as a `ChainConnectionError`.

Measured on Finney 2026-10-17, the consumed interface is identical for every
runtime from v448 to v479 except at v475, where `SubnetEpochConsensus` was
added. That is the only upgrade in that window that needed a human audit.
On 2026-10-20 the planners' storage (`BlockAtRegistration`,
`LastHotkeySwapOnNetuid`, `RevealPeriodEpochs`, `Tempo`,
`OwnerHyperparamRateLimit`, `WeightCommits`) joined the surface, and every
runtime from block 9,100,000 to 9,327,306 was still admitted, including the
three admitted on their interface alone (`0x43bc67be…`, `0x44e952fd…`,
`0x5fb504c1…`). The planners also read the `HotkeySwapOnSubnetInterval`
constant, which is not pinned: they check its value against each range.

## When the collector stops

`collector_blocked_reason` on Backroom `get_source_release_policy` names the
runtime and the consumed entries that moved, for example:

```text
runtime fingerprint is not audited: 0x… (interface changed since v478:
event:SubtensorModule.IncentiveAlphaEmittedToMiners)
```

Backroom `get_runtime_interface_report` shows the same verdict for any
finalized block before the collector reaches it.

Audit only the named entries against the reader's assumptions. If the reader
still decodes them correctly, add the release and a block that ran it to
`PINS` in `scripts/pin_runtime_interfaces.py` and regenerate:

```bash
uv run python scripts/pin_runtime_interfaces.py --write
```

If the reader's logic must change, change it and the surface together.

## Treasury receipts

Platform's treasury receipt reader (`ditto/chain/treasury_receipts.py`) uses
the same admission over its own `TREASURY_RECEIPT_SURFACE`: collector
identity storage, stake/transfer/proxy events and the `Proxy.proxy`,
`Balances.transfer_keep_alive` and `SubtensorModule.transfer_stake` calls,
pinned as `TREASURY_RECEIPT_INTERFACES` from audited v473 and v478. Exact
collector artifacts are still admitted directly. When either side of a
receipt block is interface-admitted, the chain's spec versions order the
upgrade, so a reverse change still refuses. The interface is identical for
v472–v479 except v475 (`SubnetEpochConsensus`).

This admits **reading** only. The validator collector signer
(`ditto/treasury/collector_chain.py`) still signs only on the runtime its
cold-signed policy binds and halts on any other, by design: an upgrade can
change what a proxy may dispatch without changing any type the reader
decodes. `get_runtime_interface_report` lists, under `treasury_signer`,
every `Pallet.call` added, removed or reshaped since that runtime (pinned as
`TREASURY_CALL_BASELINE`). That list is what a new cold approval has to
review. For v479 it is one call,
`SubtensorModule.set_reject_staking_hotkey_growth`.

## Residual risk

An upgrade that changes payout semantics without changing any consumed type
(for example epoch math inside the WASM) is admitted. The payout gate still
requires strictly more than 2/3 of effective stake backing the exact
submission's committed vector, and the backed hotkey must receive the maximal
miner payout in the chain's own `IncentiveAlphaEmittedToMiners` event, so a
semantic drift that changes who is paid fails the gate rather than releasing
source.

## Decoder isolation

Once a session had loaded a newer runtime, decoding an *extrinsic* from an
older-runtime block failed inside cyscale's `GenericCall` with
`'scale_info::N' object has no attribute 'index'`. Commit verification
decodes old commit blocks after the scan has read new ones, so payout
resolution failed on every sweep. cyscale 0.4.0 has the bug and 0.8.0 does
not; async-substrate-interface 2.2.1 alone is not enough, since it still
resolves cyscale 0.4.0 from the lock. Events decode correctly either way,
which is why an events-only reproduction looks fixed. That error, raised by
the payout resolver in the scan's failure path, also replaced the scan's own
"not audited" refusal in `collector_blocked_reason`; the collector now logs a
resolver failure instead.

## Metadata at an upgrade block

`init_runtime(block_hash=…)` decodes a block with its **parent's** runtime,
which is right for that block's extrinsics and events but wrong for
fingerprinting the runtime whose `:code` the block stores. At v475's upgrade
block 9233781 it reports v473's interface. Admission, the treasury reader,
the report and the pin script therefore read `metadata_at(substrate, hash)`
(`state_getMetadata` at that exact state, decoded with a fresh configuration)
and never `init_runtime`. Otherwise an upgrade that changed a consumed type
would be admitted, and cached by code hash, on its predecessor's interface.

The reverse applies to the *storage* at that block. Its state was written by
the previous runtime (the new one's migrations run in the next block), so the
library decodes it with the previous metadata, and an item added by the
upgrade is "not found" there. `SubnetEpochConsensus` reads are therefore gated
by the runtime that **wrote** the state, the code stored at its parent
(`_epoch_consensus_at`). A writer from before v475 runs Yuma only. Before this
fix the collector stopped at block 9233782 on 2026-10-18, the first block
whose parent is v475's upgrade block 9233781. The treasury receipt reader
gates the same way.
