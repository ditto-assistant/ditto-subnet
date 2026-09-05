# Hosted Coding request and result contract v2

Status: validation primitives only; HTTP routing, durable replay admission,
task selection, execution, key provisioning and reward activation are pending.

Platform and validator Python consumers use identical model and verifier
sources, guarded by a source parity test and frozen signing vectors in
`packages/dittobench-coding-contract/testdata/coding_hosted_control_v2.json`.
This follows the proposed Platform-hosted private execution boundary.

Requests act on a Platform-assigned evaluation UUID with `evaluate`, `status`
or `acknowledge`. They bind the validator hotkey, candidate artifact digest,
assignment and policy digests, a nonce and a maximum 120-second validity
window. An acknowledgement additionally binds the exact unsigned result digest.
Server admission must verify the handle belongs to this validator and artifact,
and enforce durable nonce and attempt idempotency before doing work. Signature
validation alone does not establish those database facts.

Terminal results bind the request digest and the complete expected evaluation,
attempt, validator audience, Platform signing identity, artifact, assignment,
policy, execution/grading profiles and sealed evidence commitment. The maximum
validity window is one hour; a verifier accepts only a currently valid result.
The future status API must renew expired receipts for the same durable terminal
record without rerunning candidate execution.

Only `completed`, `candidate_failure`, `infrastructure_failure` or
`integrity_failure` leave Platform in this first result profile. `completed`
means an execution has a terminal outcome and finalized evidence; it does not
mean its patch resolved the task. Detailed task scores remain Platform-private.
No task IDs, condition labels, tests, logs, patches, URLs, credentials or raw
evidence fields are part of these models. All receipts remain shadow-only and
weight-ineligible.

Signatures cover the known fields except `signature`, encoded as sorted compact
UTF-8 JSON plus one newline. The schema is included for domain separation.
Signature verification is supplied by the configured identity provider; tests
exercise Bittensor keypairs. Platform signing identities must be provisioned
independently and must not be inferred from a result's claimed signer.
The signature is lowercase hex. A digest identifies the same unsigned message
regardless of signature representation.

Request parsing ignores unknown fields but never forwards them. Result byte
verification requires a canonical known-field projection within 8192 bytes;
unknown result fields, duplicated JSON fields, malformed bytes and private
markers in extra fields fail with a fixed error message. The HTTP adapter must
enforce the byte bound while streaming, authenticated TLS, no redirects and
`Cache-Control: no-store` before passing bytes to this verifier.

These checks establish identity and message integrity. A valid Platform
signature is not independent proof of correct execution. Several validators
verifying the same attempt must not be counted as independent executions.
