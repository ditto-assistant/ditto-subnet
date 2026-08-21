# DittoBench Coding contract vectors

This directory is the language-neutral public authority for DittoBench Coding
wire examples and canonical digest vectors. Contract v1 is shadow-only and
hard-codes `weight_eligible=false`.

Consumers verify the vectors they are authorized to see. Python and Go verify
run manifests, evidence, grader plans, resource profiles, and ordered execution
receipts. The Rust miner verifies only miner-facing seed/run and memory vectors;
it must never receive grader plans or receipts. A contract change is incomplete
until every affected consumer passes the same relevant vectors and boundaries.

`coding_catalog_v1.json` freezes the public known-field catalog commitment,
signing-message digest, and a synthetic private-exposure projection. The
exposure contains only opaque IDs and content digests; it is a contract vector,
not a usable task, repository, memory bundle, grader, or Merkle proof.

`coding_selection_v1.json` freezes the synthetic finalized anchor and
future-height assignment,
position-bound catalog membership proof, deterministic selected probe, private
task-set identity, shared public run manifest, Platform run authority, and
exposure projection. It contains digest-only synthetic records and no usable
private corpus material.

`coding_artifact_capability_v1.json` freezes the single-capability delivery
wire shared by Platform and the trusted Go artifact fetcher. It covers every
allowed authoring/grading phase, kind, audience, and size-policy combination.
Its URLs use the reserved `.invalid` domain and synthetic signatures. They are
transport examples, never identity, credentials, or usable capabilities. Rust
and miner-facing code must not consume this validator-only vector.

`coding_authoring_freeze_v1.json` freezes the validator-only authoring evidence
digest, content-addressed evidence references, signed phase-transition message,
and accepted response. It contains no patch or transcript bytes and grants no
grader capability.

`coding_grading_lease_v1.json` freezes the signed request and freeze-bound
grading response shared by Platform and the validator client. The response
contains exactly visible, resource, and grader capabilities; memory is
structurally absent. Its URLs use reserved `.invalid` transport examples and
are never usable credentials.

The vectors contain only synthetic identifiers, digests, policies, and reserved
domain transport examples. Private catalog records, repository bundles, hidden
tests, policy labels, provider credentials, signing keys, and usable bearer
capabilities must never enter this package.

Canonical JSON uses lexicographically sorted object keys, compact separators,
UTF-8, no unpaired surrogate escapes, at most 32 nesting levels, escaped
U+2028/U+2029 separators, and one trailing newline. Evidence
roots additionally require the selected run manifest and validator lease ticket;
raw task or run evidence is not independently signable.
