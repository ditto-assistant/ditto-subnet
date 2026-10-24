# Collector Finney v483/v484 — interface admission, 2026-10-24

Custody stopped correctly again. Finney upgraded from audited v482
(`0x37d83888…5d8b62`) to an untagged v483 (`0x99856ec6…9952d2`) at block
9342487, then to v484 (`0x23041f50…097760`) at block 9348410. Every Gamma manual
transfer was blocked with `runtime_changed` ("custody audit and offline approval
required").

The official [v484 artifact](https://github.com/RaoFoundation/subtensor/releases/tag/v484)
(source `5749dbfbe4a78ccd3938d46ffae45e059488feb7`, srtool 0.18.3 / rustc 1.89.0)
hashes to the finalized Finney `:code`: blake2b
`0x23041f50d3529b359e9be3b90f3ca40474678e834e97f49fb401d0c691097760`, sha256
`8ba79280…881e681`. v483 has no release; its upgrade manifest names it only as
`specVersionBefore`. The [manifest](manifest.json) records the measurements.

## What changed in custody

Signing no longer needs a new cold signature for every runtime upgrade. Custody
now fingerprints `COLLECTOR_SIGNER_SURFACE`
(`ditto_screening_protocol.collector_receipts`) twice. It reads the exact
metadata at a finalized block running the signed runtime
(`AUDITED_COLLECTOR_REFERENCE_BLOCK`) and at the live block. If the two match,
custody reads and signs as the signed runtime under the existing policy.

The live policy is revision 9 and names v483 (see
[collector-finney-v483](../collector-finney-v483/README.md)). The reference
block is 9346596, so v484 is compared against v483. v483 never got an official
release artifact. Current signing now happens on v484, whose official artifact
is verified above; v483's hash is only the signature anchor and the reader for
blocks 9342487–9348409.

The surface covers:

- every storage item, event and call custody reads, decodes or signs;
- the SubtensorModule pallet index and the `AmountTooLow` error index that the
  retry proof matches by number;
- the `InitialMinTransfer` constant type;
- the signed extensions custody encodes.

Both fingerprints are computed in the signer's own process. No digest from
Platform or another library version is trusted.

The live runtime must also keep the signed runtime's `transactionVersion`
(1 at v482, v483 and v484) and must not have a lower `specVersion`. Runtimes bump
`transactionVersion` when a dispatchable's encoding or meaning changes for
signed transactions.

Any change to that surface still refuses with `runtime_changed`. That case still
needs an audit, a policy naming the new runtime and the usual offline bundle.
The live proxy filter allowlist and Yuma consensus checks still run on every
guard, and the policy must still name the audited v482 artifact.

What this does not cover:

- Behaviour behind an unchanged shape, such as fee or semantics changes inside
  `transfer_stake`. The chain still enforces stake locks and balances, and
  custody's fee cap and amount checks still apply.
- Runtime API result shapes: stake info, availability, alpha price and payment
  info. Custody validates those field by field where it reads them.

## Measured on 2026-10-24

- `COLLECTOR_SIGNER_SURFACE` at v483 and at v484 equals v482's. This was checked
  with the custody code itself, SDK 10.5.0, against `archive.chain.opentensor.ai`.
  It held at the current head and at blocks 9342486, 9342487, 9345000, 9348409
  and 9348410.
- Platform's treasury receipt interface (`9e0d036e…`) and source-emission
  interface (`1ef98cc8…`) are unchanged.
- Every per-call digest in every pallet is identical to v482.
- The reference block is pruned on `entrypoint-finney`. Custody already reads
  through the archive network, which keeps it.
