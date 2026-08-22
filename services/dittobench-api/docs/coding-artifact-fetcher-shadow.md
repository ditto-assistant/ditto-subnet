# Verified coding artifact fetcher

`internal/codingartifacts` is the trusted transport consumer for one
Platform-minted shadow coding artifact capability. It downloads opaque bytes
into a private temporary file and returns a fresh `io.ReadCloser` compatible
with `codingcertifier.BundleOpener`.

The fetcher does not call Platform, enumerate storage, extract an archive,
parse a memory or resource profile, start a workspace, run candidate code, or
grade a patch.

## Capability boundary

Every request binds:

- one nonzero ticket UUID and its deadline;
- exactly one contract-v1 artifact kind and trusted audience;
- lowercase SHA-256 and exact positive byte size;
- a short-lived S3 v2 or v4 bearer URL whose encoded expiry matches the
  capability;
- the fixed content-addressed suffix
  `coding-artifacts/v1/<kind>/sha256/<digest>`.

The only valid audience mapping is:

```text
visible-bundle   -> workspace-materializer
memory-bundle    -> memory-seed-projector
resource-profile -> resource-supervisor
grader-bundle    -> protected-grader
```

The shared delivery contract additionally permits:

```text
authoring -> visible-bundle, memory-bundle, resource-profile
grading   -> visible-bundle, resource-profile, grader-bundle
```

The Go wire DTO validates the shared vector and converts into the fetcher's
non-serializable internal capability. Unknown wire fields are ignored for
rolling compatibility, while duplicate or missing known fields, invalid UTF-8,
unpaired surrogates, excessive nesting, and trailing content fail closed.

The future orchestrator remains responsible for phase authority. In
particular, it must not project the grader capability until authoring is frozen
and must never place any bearer URL in the miner harness or model context.

## Fetch guarantees

- HTTPS is mandatory outside an explicit loopback-only test/development gate.
- The existing `netguard` validates public destinations and rechecks the
  connected address against DNS rebinding; DNS validation is inside the
  request timeout.
- Redirects, ambient proxy inheritance, and transparent content decoding are
  disabled so a bearer URL is sent only to its validated storage destination.
- Request timeout, capability expiry, ticket deadline, response headers, and
  the response stream are bounded.
- The stream must match both the exact declared size and full SHA-256.
- Partial files are removed on every failure; successful files use mode `0600`
  and are removed idempotently on close.
- Errors expose typed invalid, expired, unavailable, and integrity domains but
  never wrap or reproduce the signed URL.
- Ordinary, Go-syntax, and structured logging use a redacted capability
  projection, while JSON serialization of the internal capability type fails
  closed.
- Each opener call downloads fresh bytes; there is no cross-ticket cache.

Archive extraction and tree verification remain owned by `codingrunner` and
`codinggrader` after transport verification.

## Activation boundary

This package has no endpoint or composition-root wiring. It does not claim a
validator job, deliver bytes to a miner, change a score, deploy a service, or
make coding contract v1 weight-eligible.
