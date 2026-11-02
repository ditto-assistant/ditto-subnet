# Public treasury receipt contract

The public dashboard Activity page has a separate Treasury spending section.
`GET /api/v1/public/treasury-activity` returns cursor-paginated rows from
`treasury_public_events`. The existing admin activity feed records requests and
policy intent; it must never be presented as proof of a payment.

Each treasury row represents a finalized chain receipt or a later GM provider
reconciliation for one payment ID. A GM token deposit is labeled
`gm_token_deposit`; it becomes a confirmed `gm_credit_purchase` only after GM
credit reconciliation. The reconciled row references its finalized event.
Those stages share `payment_id` and count as one payment. A maintenance bounty
is a finalized chain transfer, with no GM credit stage.

The block hash, extrinsic index, and event index identify the exact public
transfer. Policy and burn revisions, effective burn share in millionths, both
purpose allocations, selected denominator, allocated alpha budget, actual
source alpha, route, chain deposit asset and amount, optional GM credited USD
nanos, public sender and recipient, authenticated public actor ID and role,
and verification source are explicit columns. Atomic amounts are decimal
strings in public JSON so JavaScript cannot round 64-bit values. The response
has no arbitrary JSON field. PostgreSQL rejects updates and deletes and
enforces unique payment-state and chain-event-state pairs.

A bounty row additionally requires a public award ID and accepted-work
reference. The publisher must verify both against the approved bounty record;
a chain transfer by itself does not prove the bounty purpose. GM credit rows
must repeat every immutable economic and route field from the finalized token
deposit, while adding the reconciled credit amount and actor.

There is deliberately no operator-write HTTP endpoint or automatic importer.
The signer currently accepts self-attested allocation and reviewer data, and
no bounty executor exists. Before enabling spends, implement a writer that
independently verifies finalized chain receipts against the exact public
sender/recipient, checks the active policy and burn revisions, proves the
allocation from finalized emissions, and uses authenticated actor identity.
Publish a `reconciled` GM credit event only after provider credit is independently
confirmed and linked to the finalized event. Use a public actor alias derived
from authenticated identity, never a caller supplied name. Never store GM
account references, API keys, private Billing rows,
signer journal payloads, or free-form reasons in this public table. A missing
row means **no verified spend is recorded**, not proof that no transfer occurred.
