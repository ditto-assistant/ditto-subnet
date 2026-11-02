# Gamma Beta shadow ledger pin

The shared `TreasuryLedgerPin` contract records an immutable treasury policy and
collector identity observation in the existing epoch ledger. V1 accepts only
`mode: shadow`. It grants no weight, transfer, registration or spending authority.

## Compatibility and binding

When no treasury pin is present, Platform omits the field from the ledger wire and
the served context. Existing epoch digests remain unchanged. Root and Platform
import the same wire model from `ditto-screening-protocol`; Backroom's generated
OpenAPI contract carries the same known fields.

The policy digest covers canonical known fields, including the policy revision,
genesis hash, netuid 118, collector hotkey/coldkey, public collector-policy digest,
and bucket IDs, holding coldkeys and basis-point allocations. Combined service
allocation is at most 10000 bps. Duplicate destinations and a collector holding
destination are refused. Unknown JSON fields are ignored and cannot add authority
or alter the policy digest.

The identity observation contains a finalized block/hash, the hotkey-to-UID read,
the reciprocal UID-to-hotkey read, the owning coldkey and the subnet-owner coldkey.
Validation requires matching chain/subnet/policy identity, reciprocal UID binding,
and a coldkey distinct from the subnet owner. The observation must belong to the
pinned epoch; an observation at the ledger's block must match its block hash.
Address validation checks syntax only, not SS58 checksum or chain ownership.

Platform freezes the known fields under `context.served.treasury_pin` and includes
them in the existing ledger digest. Replay reads that stored policy rather than
current settings, checks its identity binding and verifies the enclosing digest.
Malformed or changed treasury evidence raises an error instead of disappearing
from the response. Historical pins without treasury evidence retain their existing
replay behavior.

## Default-off producer and follow-up gates

The served snapshot field defaults to `None`. This patch provides no resolver,
settings write, chain reader or automatic producer. Shape validation does not
attest that recorded observations came from finalized chain state, and the public
collector-policy digest does not verify an offline signature. Synthetic fixtures
exercise those contracts without claiming a real collector registration.

Before weight enforcement is possible, follow-up work must add a reviewed
finalized-state producer, independently verified collector-policy binding,
capability negotiation and adoption by every served validator, the actual
service-before-burn weight adapter, and public readiness diagnostics. The collector
must remain outside competitive payout at zero or paused service allocation.
Live identity must be revalidated before weights or sweeps; UID reuse, owner drift,
missing evidence or policy drift must halt the relevant operation. OUR pruning
exclusion is not chain immunity. Activity ingestion and payment observation remain
separate integration work.

No live policy, wallet, signer, credential, proxy, registration, service allocation,
burn allocation or admission setting is changed by this contract patch.
