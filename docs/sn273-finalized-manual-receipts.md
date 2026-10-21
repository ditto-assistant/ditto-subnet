# Finalized manual wallet receipts (SN-273)

A collector-to-service transfer may finalize even when one or more source epochs
have no immutable Platform ledger snapshot. A custody report alone is not chain
proof. Missing snapshots must not be reconstructed from adjacent or current
settings merely to publish a source-attributed distribution.

The manual audit loop therefore verifies and publishes a **wallet movement**
separately, using the existing canonical chain decoder and the explicit first
source epoch's signed wallet identity/publication anchor. It checks finality,
audited runtime, canonical hashes, exact proxy sender, destination, amount and
successful events. The stored request/report digests must match, and batch
requests retain their cold-signed manual range check. Publication-off stays off.
No new signing, transaction submission, receipt-source reservation, allocation
balance, custody journal mutation or automatic transfer is involved.

The public `/api/v1/public/treasury-activity/manual-movements` feed returns only
verified movement fields, marked `source_attribution: incomplete`, with no actor,
audit reason, billing identity or free-form proof. Gamma displays this status and
the Taostats transaction link. GM credits remain unproven. Backroom continues to
show `audit_pending` and blocks another request until the full source audit is
resolved. A public wallet receipt is not an approved source distribution.

Full ingress still requires every immutable source epoch, independent finalized
source events, historical signed allocations and cumulative consumption bounds.
It now checks missing epochs before archive reads. Successful full ingress
atomically replaces the temporary wallet-movement projection with the ordinary
fully attributed receipt; conflicts and unverifiable data remain refused.

## Existing 500-alpha request

Request `9f7a0893-0629-44e4-9cc2-a0c94e346583` finalized once at block 9329324,
extrinsic 5, transaction
`0xa92ee5ef395d6862fa84580d5ac5c7be83c20058e9d62858aeb0bf2255e809b9`.
Only 18 of its 37 source epochs had immutable ledger snapshots at diagnosis.
The original generic custody ValueError no longer reproduced; no exact original
exception message is asserted. Recovery is publication-only. Never create a
replacement transfer, clear a delivery fence or manufacture missing snapshots to
repair this receipt.
