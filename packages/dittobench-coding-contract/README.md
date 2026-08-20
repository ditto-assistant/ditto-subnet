# DittoBench Coding contract vectors

This directory is the language-neutral public authority for DittoBench Coding
wire examples and canonical digest vectors. Contract v1 is shadow-only and
hard-codes `weight_eligible=false`.

Python, Go, and Rust consumers must parse the same files under `testdata/` and
produce the recorded canonical SHA-256 roots. A contract change is incomplete
until all three consumers pass the same vectors and boundary cases.

The vectors contain synthetic identifiers and digests only. Private catalog
records, repository bundles, hidden tests, policy labels, provider credentials,
and signing keys must never enter this package.

Canonical JSON uses lexicographically sorted object keys, compact separators,
UTF-8, escaped U+2028/U+2029 separators, and one trailing newline. Evidence
roots additionally require the selected run manifest and validator lease ticket;
raw task or run evidence is not independently signable.
