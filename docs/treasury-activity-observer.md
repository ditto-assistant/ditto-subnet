# Verified treasury activity ingress and observer

This code prepares receipt observation. It does not enable weights, a signer,
payments, provider credits, an OAuth grant or a production observer.

## What is recorded

`POST /api/v1/admin/treasury-receipts`, exposed through the authenticated public
Backroom `record_treasury_receipt` tool, accepts bounded chain selectors. It
independently verifies the immutable enclosing epoch ledger digest, V2 offline
policy signature, exact historical settings checksum and destinations, finalized
Finney block hashes and the reviewed v472 runtime. It then verifies actual
extrinsic bytes and scoped successful chain effects. Caller finality, actors,
balances, journal assertions and provider-credit fields are not authority.

For collector distributions, the reader binds the approved anchor UID to the
source and dispatch identities, reads actual liquid initialization credits,
checks the collector auto-stake route, and applies the shared integer split.
Gross miner incentives and principal balances are not spendable source proof.
Each source block/bucket can produce only one distribution receipt. Linked alpha
payments cannot consume more than that verified distribution.

TAO holding-wallet-to-configured-payee transfers are independently useful chain
facts. They require the exact historical epoch and payee rule but no fabricated
earning or parent receipt. They are explicitly `vendor_payment`, with funding
and conversion attribution unproved. No event is a provider credit purchase.
`provider_credit` ingress refuses until an independent provider verifier exists;
SN28 effect decoding is not implemented.

Historical publication-off receipts remain in append-only private history.
Publication uses the historical bucket policy, not the latest proposal. The
public projection allowlists fields and omits private billing references, audit
reason, signed transactions, delegate metadata and credentials. Public stages
separate holding distribution, vendor payment and provider reconciliation.

## Automatic observation, default off

`scripts/treasury_activity_observer.py` reads an immutable public configuration
file and checks its supplied SHA-256. `enabled` defaults to false. Disabled
execution opens no chain, queue, OAuth client or signer.

An enabled implementation reads finalized holding-wallet payments and optionally
an existing transfer journal. The journal is opened read-only, with private-file
and policy/role checks. Only finalized operation coordinates are exported; old
journals without exact extrinsic hash/index require independent recovery.
Unresolved earlier transfers prevent advancing past a later operation.

The separate observer SQLite queue is private, locked and policy-pinned. It
atomically queues selectors before advancing a finalized hash checkpoint. At
most 32 blocks and 100 deliveries are processed per tick. Large payment blocks
retain an event offset for bounded continuation. Admission pauses before the
1000-pending bound; existing work still drains, including old over-capacity
queues. Unknown delivery remains pending and redelivery uses Platform's
idempotent chain-effect identity. An acknowledgment must match the queued
epoch, source, block/hash, extrinsic/hash, bucket, amount and policy.

Public MCP transport outages reconnect with 15–300 second bounded backoff.
Explicit receipt refusal, invalid policy, authentication expiry, identity or
finalized-hash drift halts for independent recovery. No token refresh, credential
discovery, transaction retry, signing or broadcast is implemented. Chain reads
remain read-only and refuse unaudited runtime/schema transitions.

## Activation blockers

The staged `infra/systemd/sn118-treasury-activity-observer.service` supplies an
already separately approved credential with systemd `LoadCredential` and
`--token-file`. Private ownership/mode, regular-file/no-follow and content bounds
are enforced. No token appears in the command or activation.env. The unit is
not installed/enabled by release, has no signing identity/journal mount and
does not restart after semantic/auth refusal. See
[gamma-activation-packet.md](gamma-activation-packet.md) for installation gates
and the unresolved cross-host distribution-selector handoff. Disabled config
reads no credential or network in the CLI, even with a supplied token-file path.
systemd credential copying occurs before launch, so an absent activation.env is
the unit's default-off boundary; creating that marker requires separate review.

The observer is **not activated and has no live OAuth binding**. Existing
`backroom:write` is broad; a client's local allowlist cannot constrain a stolen
token. Never provision that broad grant for this observer. The dedicated
`backroom:treasury:observe` implementation requires exclusive authorization and
explicit write-level staff consent. Its server exposes only exact historical
settings read and verified receipt ingestion, with no general read floor.
Requests for other tools/resources/downloads and JSON-RPC batches refuse.
Live staff level/session expiry are checked at consent, issuance, refresh and
every request; ordinary grant replacement and revocation still apply.

Startup discovers exactly the two allowed tools and refuses a broader token
catalog. The settings call selects the approved policy's exact revision even
when it is older than the latest 200 history entries. A missing, corrupt or
oversized row refuses without default substitution. The complete raw JSON
checksum and all configured destinations remain independently checked.
Code, tests and release do not authorize a grant: approved credentials must be
obtained through the normal browser consent flow, separately from deployment.

Runtime adoption, separately authorized immutable configuration and the scoped
OAuth binding, deployment/supervision and a bounded accepted receipt remain
separate gates. No production installation, secret, IAM or policy binding is
performed by this change.

The repository has no OUR pruning/deregistration dispatcher wired to this
observer. Receipt code supplies no chain immunity or pruning protection claim.
The previously reviewed weight adapter and collector custody gates remain
independent.
