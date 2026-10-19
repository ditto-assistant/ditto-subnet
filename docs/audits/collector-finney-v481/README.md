# Collector Finney v481 recovery — 2026-10-19

SN-273 supersedes the unresolved v479 artifact mismatch for **current signing**.
The official v481 compressed WASM, its srtool source/digest and finalized
Finney `:code` all match exactly:
`0x5fb504c1c793a3ddb5456f189ddef98b5146853aea6ff998501f1b6c6eb720bc`.
Source is [6a972693ae9580541225bd37696ac2fee26b8ae9](https://github.com/RaoFoundation/subtensor/tree/6a972693ae9580541225bd37696ac2fee26b8ae9).
The [manifest](manifest.json) records downloaded byte hashes, critical source
hashes, public Backroom interface reports, exact finalized native filters and
collector/recipient observations. This is scoped source and official artifact
verification, not an independent reproducible build.

## Scoped review since v478

- `common/src/proxy.rs`, the proxy pallet, same-subnet stake transfer and
  bounded registration source are unchanged. Native Registration still admits
  four calls, Transfer five, without constraints. The application still signs
  only `register_limit` or bounded same-hotkey/SN118 `transfer_stake`, inside the
  exact role's proxy. No new delegate, arbitrary/nested call or fee authority.
- v479 added recipient staking-list growth refusal. The common transfer
  validation now checks that preference before allowing a new hotkey entry.
  Existing destinations remain admissible. At the recorded finalized state,
  GM's recipient flags were zero and its staking list already contained the
  collector hotkey. This observation cannot replace pre-sign chain checks.
- v481 adds `[burn, recycle, owner, validator, miner]` emission weights and pays
  root's baseline share from the validator slot. These change upstream reward
  calculation, not the treasury withdrawal limit. SN118's observed vector was
  the default 0/0/18/41/41 split. No emission-weight write is part of recovery.
  Earnings must still be the actual finalized Initialization incentive and
  exact liquid `AutoStakeAdded` credit after collateral/routing; neither the
  nominal split nor a stake-balance delta authorizes spending.
- Lock changes concern retained positions during hotkey swaps. This worker
  does not swap hotkeys, remove stake, buy TAO or alter lock preferences. Its
  existing availability, collateral, fee reserve and retained-alpha checks
  remain. Both observation sides must still be explicit Yuma.
- New calls `set_reject_staking_hotkey_growth` and
  `sudo_set_subnet_emission_weights` are outside Registration/Transfer's
  unchanged inventories. The new admin call cannot be dispatched by custody.
  Known transfer/earning events retain their shapes; new events append.

## Historical v479 reading is not v479 signing

The earlier official v479 artifact does **not** match the deployed v479
executable, including after decompression. Nothing here claims otherwise.
Public Backroom's merged runtime-interface verifier independently measured the
deployed v479 receipt surface as exactly audited v478's:
`5d44ccf618519ace1ce8c2fb4c3652ea1f07950439e883281de922202bcb3ec4`,
with no moved consumed entries. That immutable v479 hash is admitted only for
historical effect reading, while exact native filter and Yuma checks still run.
Platform continues to label it **interface-admitted**, never artifact-audited.
Unknown hashes and reverse transitions refuse; no v474/v480 hash is guessed.
An implementation change behind an identical interface is a residual reader
risk, already present in Platform's interface admission. Exact events, liquid
credits, historical identities and immutable epoch policies remain mandatory.

## Signing bundle and rollout

The sole base custody change is revision6→7 plus v478→v481 fingerprint. Keep the
100-alpha automatic distribution cap, fees, destinations, start/cooldown and
isolated signer identities unchanged. A separate reusable manual approval
authorizes positive amounts through 2,500 alpha, up to512 proved sources.
Rebind the existing 0–100% GM emission range to the new custody digest; this
does not change effective operator allocation. Latest observed settings rev5
deliberately set Gamma0 on October14. Preserve that decision and recurrence off.

The offline bundle must include the new policy, exact quiesced journal migration
manifest, emission rebind and manual range approval. Verify all four public
signatures before activation. No live journal pin rewrite, reset, older-journal
restore, new key, recurring timer, weight change or test payment is authorized.
Old signed operations must retain their history and reconcile their original
bytes. Backroom's usable fresh readiness, sufficient proved aggregate earnings
and final user transfer confirmation are separate post-signature gates.
