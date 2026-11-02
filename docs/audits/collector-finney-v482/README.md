# Collector Finney v482 recovery — 2026-10-22

Custody stopped correctly. Finney upgraded from audited v481
(`0x5fb504c1c793a3ddb5456f189ddef98b5146853aea6ff998501f1b6c6eb720bc`) to v482 at
block 9334380, and the worker reports `runtime_changed`. The official v482
compressed WASM, its srtool digest and finalized Finney `:code` all match:
`0x37d83888085e2f1b7c9c36c1341cfb6c0d6689f8efd6e0c6128e3666eb5d8b62`.
Source is [8cf229f8ef969389d054a4c161b53ffd164a041f](https://github.com/RaoFoundation/subtensor/tree/8cf229f8ef969389d054a4c161b53ffd164a041f).
The [manifest](manifest.json) records the downloaded byte hashes, critical
source hashes and live observations. This is scoped source review plus official
artifact verification, not an independent reproducible build.

## Scoped review since v481

- **`Proxy.proxy` changed only in its `force_proxy_type` enum.** `ProxyType`
  gained `Validate = 19` and `Weights = 20` at the end. `Registration = 9` and
  `Transfer = 10` are unchanged, so `Some(Transfer)` still encodes `01 0a`.
  Pallet 16/call 0, `transfer_stake` 7/86 and `register_limit` 7/134 are
  unchanged, and `transactionVersion` is still 1. `pallets/proxy/src/lib.rs` is
  byte-identical, and the `transfer_stake`/`register_limit` dispatch blocks
  hash identically. The other changed Proxy calls change only through the same
  enum. Platform's receipt interface moves from v478 `5d44ccf6…` to
  `9e0d036e…`, and `call:Proxy.proxy` is its only changed entry.
- **Native filters are unchanged.** Transfer still allows five calls and
  Registration four. The new `Validate`/`Weights` types admit only weights,
  serving, EVM-key and commitment calls, and neither is a superset of Transfer
  or Registration.
- **Consumed events and errors are unchanged.** `macros/events.rs` is
  identical, and `AmountTooLow` is still error 82, so the retry check holds.
  New errors are appended at 188–189. There are no storage migrations.
- **The fee-payer wrapper now follows up to three proxy levels.** The payer is
  unchanged for our single level. Declared weight rises by one DB read, and
  the existing `max_fee_rao` check still bounds the fee.
- **Yuma miner payout is now split from the funded mining budget.** Earnings
  must still be the exact finalized Initialization incentive with a matching
  liquid `AutoStakeAdded`. Amounts may differ in size; the event shapes do not.

## New third-party risks (not blockers)

- **Destination reservation.** `announce_coldkey_swap_v2` lets a payer reserve
  any non-hotkey account as a swap destination. While the reservation is live,
  a cross-coldkey `transfer_stake` into it fails with
  `ColdkeySwapDestinationReserved`. Custody would record a finalized failure
  and spend its fee; no funds are lost. The failed-dispatch fence stays closed
  until custody proves the result. A pre-sign reservation read is follow-up
  hardening and not part of this change. At observation, the GM destination
  had no reservation.
- **Coldkey merge without consent.** A swap into an already-used coldkey now
  merges instead of refusing. Extra stake, locks or hotkeys on the collector
  coldkey can only shrink `available`. AutoStakeDestination, Proxies and SN118
  ownership checks are unaffected.

## Signing boundary

The code admits v482 for current signing only under a newly cold-signed policy
naming this exact hash. The only financial diff is revision 7→8 plus the
v481→v482 fingerprint. Keep caps, fees, destinations, start/cooldown and
isolated signer identities unchanged, and keep recurrence off. The offline
bundle needs four signatures, all verified before activation:

- the new policy
- the exact quiesced journal migration manifest
- the emission-policy rebind to the new custody digest
- the manual 2,500-alpha range approval, which binds the custody digest

No journal reset, new key, recurring timer, weight change or test payment is
authorized.
