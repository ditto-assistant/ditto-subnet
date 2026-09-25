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

## Validator control command

`python -m ditto.validator.coding_hosted_control` drives one Platform-hosted
shadow assignment from the validator host. It isn't imported by the validator
worker, and it is off unless the validator environment sets both:

| Variable | Meaning |
|---|---|
| `VALIDATOR_CODING_HOSTED_CONTROL_ENABLED` | `true` to allow the command |
| `VALIDATOR_CODING_HOSTED_PLATFORM_HOTKEY` | The trusted Platform control signer address. It is a separate online key, never the offline curator key, and never taken from a command argument. |

Run it inside the validator container, so the configured hotkey wallet is used
in place through `load_validator_keypair`. The command touches only the key's
public address and `sign`. It never reads, prints or exports seed material.
`--validator-hotkey` pins the expected address. The loaded key,
`VALIDATOR_HOTKEY` and the assignment must all equal it. Before using it,
confirm from a live read that the pinned hotkey is still the validator's
configured signer.

The assignment file is the exact `authority` object from the admin assignment
preview/create response. `--assignment-sha256` is its `assignment_sha256`. The
command recomputes Platform's canonical projection digest and refuses a
mismatch, unknown or missing fields, duplicate keys, symlinks, or
`weight_eligible` other than `false`. A golden vector pins parity with
`HostedAssignmentAuthority`. The Platform origin is the validator's configured
`VALIDATOR_PLATFORM_API_URL`.

```text
docker compose run --rm --no-deps \
  -e VALIDATOR_CODING_HOSTED_CONTROL_ENABLED \
  -e VALIDATOR_CODING_HOSTED_PLATFORM_HOTKEY \
  -v /var/lib/ditto-validator-hosted-control:/hosted \
  ditto-subnet uv run --no-sync python -m ditto.validator.coding_hosted_control \
  evaluate --validator-hotkey <pinned> \
  --assignment /hosted/assignment.json --assignment-sha256 <digest>
```

`docker compose run` passes only the service's declared environment, so both
variables need `-e` until the service environment declares them. The host
directory is root-owned mode `0700`.

Clocks: requests are backdated 30 s and expire 60 s after signing, keeping the
signed window within Platform's 120 s bound. The validator accepts Platform
receipts within 30 s of clock skew in either direction; larger skew is refused.

- `evaluate [--result-out …]` signs one admission request and refuses an expired
  assignment before signing. Admission is idempotent on Platform: a repeat
  returns the current signed status, or the signed terminal result once the
  attempt has finished. That result is saved only if `--result-out` is given.
- `status --result-out /hosted/result.json [--wait-seconds N]` checks the output
  path first (absolute, absent, in a caller-owned `0700` directory), then signs a
  fresh status request per poll. It sleeps 20 s between polls and stops at N
  seconds (at most 3600), or as soon as an attempt that never started passes its
  deadline. A verified terminal result is written to a new `0600` file; a failed
  write leaves no partial file. Transport or verification failures stop
  immediately.
- `acknowledge --result /hosted/result.json` re-verifies that exact result file
  and acknowledges its digest. Only an empty `no-store` HTTP 204 counts. A result
  is signed for one hour; after that, fetch a fresh result with `status` and
  acknowledge that one instead.

Output is one JSON line with operation, state or outcome, digests,
`shadow_only=true` and `weight_eligible=false`. Refusals print a fixed message
and exit 70. Using the command also requires a validator image built from a
release that contains it, and the Platform control signer being enabled.
