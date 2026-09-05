# Hosted Coding validator transport v2

Status: opt-in HTTP adapter above the signed hosted control contract. It is not
imported by the validator worker. The corresponding Platform route remains
disabled without explicit trusted-runtime signer injection.

`HostedCodingTransport` accepts an operator-configured canonical HTTPS origin and
independently provisioned Platform verification keys. It posts only the signed
known-field request projection to the reserved
`/api/v1/validator/coding-hosted/control` path. No caller-supplied task URL,
Hippius credential, private input, grader or artifact download is supported.

Each exchange checks the request against the expected assignment and request
digest before network I/O. The client disables environment proxies and redirects,
uses normal TLS certificate verification, requests identity encoding and
`Cache-Control: no-store`, and applies a total deadline bounded by 30 seconds and
the outgoing request expiry. It never automatically retries an operation.

Only HTTP 200 with JSON, no-store and uncompressed content can carry a terminal
receipt. Both declared and streamed body lengths are bounded to 8192 bytes, and
the response signature, trusted signer, assignment, attempt and expiry must pass
the shared verifier before a result is returned. HTTP 202 requires a separately
signed `HostedCodingStatus` and returns that distinct type, never terminal evidence.
A status body under HTTP 200 or a terminal result under HTTP 202 is rejected.
Other HTTP statuses are redacted transport errors. Future orchestration must handle
durable admission and status polling separately, without rerolling an evaluation.

Transport and verification failures have fixed redacted messages. They are
control-plane failures, not evidence that a candidate failed a coding problem.
No response body or remote error details are logged or exposed by this module.

Mock-transport tests cover signatures, redirects, header restrictions, actual
stream size, timeout cancellation, preflight assignment mismatch and safe errors.
They do not prove a deployed endpoint or independent execution. Server admission
uses the durable replay ledger, but host key provisioning, terminal finalization
and worker wiring remain separate reviewed steps. All accepted contract receipts
are shadow-only and non-weightable.
