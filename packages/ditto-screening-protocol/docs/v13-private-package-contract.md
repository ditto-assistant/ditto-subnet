# V13 sealed private-package preflight

`v13_private_package.py` defines a versioned, content-addressed handoff for the
private metamorphic checks required by policy V13. It contains no challenge
prompts, expected answers, seed values, or executable case runner. The fixed
profile requires 20 pairs in each of three transformation classes, balanced
as 10 pairs per class in each of two hidden seed rotations (60 pairs total).
Applicable tool catalogs add 20 alias/reorder pairs, again balanced between
seeds (80 pairs total). This matches the published minimum while making
same-direction replication observable. The later statistical test may still
be inconclusive if the sample cannot establish the required effect.

The trusted Platform registry must bind agent UUID, screening attempt UUID,
committed archive SHA, verified image SHA, profile SHA, manifest SHA, and
registration time. Registration must follow the artifact commitment. The
manifest must be generated after the commitment and no later than registration.
Only an isolated runner can read the sealed manifest and payload store; the
Backroom and public API should receive a sanitized summary or digest, never
case bytes. Preparation checks every content digest and enforces finite size
and time bounds. The store must serve immutable content-addressed bytes, and
the runner must recheck each digest at execution to avoid a preflight-to-run
swap. The runner interface accepts the prepared identity-bound package, not a
bare manifest. Any mismatch leaves the check incomplete.

This contract is **not** a completed private verification: no trusted registry
implementation, sealed package, independent seed generation, semantic
equivalence review, case execution, statistical analysis, or artifact replay is
included here. A future runner must perform paired clean controls through
equivalent infrastructure and implement the predeclared correction, confidence,
materiality, and replication analysis. A robustness result alone cannot prove
misconduct. No receipt from this module authorizes a V13 CLEAR or REJECT.
