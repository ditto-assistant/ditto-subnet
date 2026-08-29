# Shadow coding task lease core

Platform can reconstruct one ticket-bound private task lease from durable run
authority and one refetched catalog record. The builder is internal and mints
no URL or capability itself. A separate internal minter may derive the four
ticket-bounded artifact capabilities described in
`coding-artifact-capabilities.md` from this verified lease core.

The builder verifies the ticket, current artifact certification, finalized
issuance, immutable assignment, registered catalog commitment, selected private
record, position-bound Merkle proof, run manifest, private task-set manifest,
and irreversible exposure projection. It rejects an expired ticket or any
stored/refetched digest drift.

The resulting lease core contains the validator ticket identity and deadline,
the identical shared run manifest, the private task-set manifest, repository
epoch, issue, model-visible runtime policy, and budgets. It contains no gold
patch, hidden test, grader bytes, source URL, catalog coordinate, storage URL,
workspace capability, or inference grant capability.

## Activation boundary

There is no HTTP endpoint, validator claim, task/capsule delivery, workspace
capability, Luna relay grant, execution, scoring, deployment, or emissions
effect. Presigned artifact URLs exist only as an unexposed internal projection.
Coding contract v1 remains permanently `weight_eligible=false`.
