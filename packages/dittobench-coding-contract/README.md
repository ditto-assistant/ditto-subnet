# DittoBench Coding contract vectors

This directory is the language-neutral public authority for DittoBench Coding
wire examples and canonical digest vectors. Contract v1 is shadow-only and
hard-codes `weight_eligible=false`.

Consumers verify the vectors they are authorized to see. Python and Go verify
run manifests, evidence, grader plans, resource profiles, and ordered execution
receipts. The Rust miner verifies only miner-facing seed/run and memory vectors;
it must never receive grader plans or receipts. A contract change is incomplete
until every affected consumer passes the same relevant vectors and boundaries.

The vectors contain synthetic identifiers and digests only. Private catalog
records, repository bundles, hidden tests, policy labels, provider credentials,
and signing keys must never enter this package.

Canonical JSON uses lexicographically sorted object keys, compact separators,
UTF-8, no unpaired surrogate escapes, at most 32 nesting levels, escaped
U+2028/U+2029 separators, and one trailing newline. Evidence
roots additionally require the selected run manifest and validator lease ticket;
raw task or run evidence is not independently signable.
