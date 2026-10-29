# Collector Finney v483 recovery — 2026-10-23

Custody stopped correctly. Finney upgraded from audited v482
(`0x37d83888085e2f1b7c9c36c1341cfb6c0d6689f8efd6e0c6128e3666eb5d8b62`) to v483 at
block 9342487, and the worker reports `runtime_changed`. Finalized Finney `:code`
is `0x99856ec64a61eb8011999a803bd336979512275214a173fea583abd5c89952d2`.
Reviewed source is
[db3fdeb98b](https://github.com/RaoFoundation/subtensor/commit/db3fdeb98b)
(release-483, PR #3280), diffed against v482
[8cf229f8ef](https://github.com/RaoFoundation/subtensor/tree/8cf229f8ef969389d054a4c161b53ffd164a041f).

**Superseded on 2026-10-24.** Finney moved on to v484, whose official artifact
is verified in [collector-finney-v484](../collector-finney-v484/README.md).
Custody signs there under this revision-9 policy because the signer surface is
unchanged. The original note follows.

**Artifact verification is pending.** On 2026-10-23 upstream has published no
v483 release: its release-train run for db3fdeb98b was cancelled, and its own
mainnet watcher fails with HTTP 404 looking for the release. Until an official
srtool WASM whose blake2b-256 equals the live hash is recorded in the
[manifest](manifest.json), do not merge this change or activate signing. The
offline approvals may be signed in advance because they name only this exact
hash; they authorize nothing on any other runtime.

## Scoped review since v482

- **The changed runtime surface is fee-free wrapper validation and EVM
  precompiles.** `SubtensorTransactionExtension::validate` now calls
  `check_fee_free_wrapped_calls` only when `info.pays_fee == Pays::No`, and
  `fee_free_wrapped_weight` returns zero for any fee-paying call. Our
  `Proxy.proxy(Some(Transfer), transfer_stake)` pays a fee (`transfer_stake` is
  `Pays::Yes`), so neither applies. The per-call checks moved into
  `check_call_guards` unchanged. The PoW `provides` tag moved to unsigned
  origins only, and the rate-limit tags are unchanged.
- **The proxy pallet, call indices and encoding are unchanged.**
  `pallets/proxy`, `pallets/subtensor/src/macros/{dispatches,events,errors}.rs`
  and storage migrations do not appear in the diff. `transfer_stake` is still
  7/86 and `register_limit` 7/134, and `transactionVersion` is still 1.
  The other changed files are `precompiles/` and the vendored Frontier dispatch
  precompile (EVM only), docs, JS tests, CI and the website.
- **Metadata pins are identical.** Regenerating
  `audited_runtime_interfaces.py` at block 9346596 yields the same
  source-emission (`1ef98cc8…`) and treasury-receipt (`9e0d036e…`) digests and
  the same per-call digests as v482. Only the baseline label moves to v483.

## Signing boundary

The code admits v483 for current signing only under a newly cold-signed policy
naming this exact hash, and only by the transition v482 → v483 at revision + 1.
Older runtimes cannot skip straight to v483. The only financial change is
revision 8 → 9 plus the v482 → v483 fingerprint. Caps, fees, destinations,
start/cooldown and isolated signer identities are unchanged, and recurrence
stays off. The offline bundle has the same four signatures as v482: the new
policy, the exact quiesced journal migration manifest, the emission-policy
rebind, and the manual 2,500-alpha range approval.

No journal reset, new key, recurring timer, weight change or test payment is
authorized.
