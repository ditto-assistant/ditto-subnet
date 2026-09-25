# Public treasury receipt contract

The public dashboard Activity page has a separate Treasury spending section.
`GET /api/v1/public/treasury-activity` returns cursor-paginated rows from
`treasury_public_events`. The existing admin activity feed records requests and
policy intent; it must never be presented as proof of a payment.

Each treasury row represents a finalized chain receipt or a later reconciliation
for one payment ID. The chain block hash, extrinsic index, and event index
identify its exact public transfer. Two states for one payment are linked by
`payment_id`, so a finalized transfer and its reconciliation must be counted
once, not as two purchases. Policy revision, burn revision, denominator,
allocation bps, allocated alpha rao, route, gross and realized amounts, asset,
public sender and recipient, actor provenance, and verification source are
explicit columns. Atomic amounts are decimal strings in the public JSON so
JavaScript cannot round 64-bit values. The response has no arbitrary JSON field. The table
rejects updates and deletes in PostgreSQL and enforces unique payment-state
and chain-event-state pairs.

There is deliberately no operator-write HTTP endpoint or automatic importer.
The signer currently accepts self-attested allocation and reviewer data, and
no bounty executor exists. Before enabling spends, implement a writer that
independently verifies finalized chain receipts against the exact public
sender/recipient, checks the active policy and burn revisions, proves the
allocation from finalized emissions, and uses authenticated actor identity.
Publish a `reconciled` GM event only after provider credit is independently
confirmed. Never store GM account references, API keys, private Billing rows,
signer journal payloads, or free-form reasons in this public table. A missing
row means **no verified spend is recorded**, not proof that no transfer occurred.
