# Hosted Coding request and result contract v2

Status: signed contracts plus default-off HTTP admission and durable replay
protection. Task selection, execution, terminal finalization, key provisioning
and reward activation remain separate, unactivated work.

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

Pending receipts use a separate `dittobench-coding-hosted-status-v2` schema,
with `assigned`, `admitted` or `started` state and a maximum 120-second lifetime.
They bind the same request, audience, attempt and profile identities but contain
no outcome or evidence commitment. `started` reports a durable start marker,
not worker liveness or successful execution. A pending receipt must never be
accepted by the terminal-result verifier or counted as scoring evidence.

The default-off `POST /api/v1/validator/coding-hosted/control` route returns
HTTP 202 signed pending receipts for `evaluate` and `status`. It requires an
explicit trusted-runtime `HostedCodingControl` signer injection; standard
startup does not provision one. Without it, the route returns 503 before parsing
the body. Enabled requests are capped at 8192 streamed bytes and ten seconds
before parsing, then thirty seconds for authentication and admission. All route
responses are `no-store`; validation and service failures omit input values.

Admission verifies the signature, current chain permit and pre-approved durable
assignment. It consumes a global nonce in the same transaction as admission;
fresh nonces cannot create another attempt. Status never admits or starts work.
Platform signs and self-verifies the allowlisted pending projection before
committing; a signing failure rolls back both admission and nonce consumption.
An HTTP response lost after commit requires an explicit fresh-nonce status check,
not an automatic execution retry. `acknowledge` remains unavailable until durable
terminal finalization is implemented. This route cannot create assignments,
retrieve/decrypt private objects, launch candidates or grant inference access.

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
