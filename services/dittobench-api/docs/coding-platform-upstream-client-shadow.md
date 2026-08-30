# Shadow coding Platform upstream client

`internal/codingplatform` implements the validator-side `codingrelay.Upstream`
port. It converts one already-journaled locked Luna attempt into a signed,
bounded request for the coding-specific Platform/model-relay route. The package
does not mount that route and never receives an OpenRouter credential.

## Dispatch authority

Construction deep-owns the exact coding grant exchange result:

- ticket, case, profile capability, grant UUID, generation, and policy digest;
- authoring issuance time, deadline, and effective request/token budgets;
- the opaque Platform bearer;
- the validator-owned Ed25519 private key and matching exchanged public key;
- the exact HTTPS route ending in
  `/api/v1/inference/coding/chat/completions`.

The client rejects plaintext, userinfo, query, fragment, alternate-port, escaped
path, typed-nil transport, authority drift, over-policy budgets, invalid
lifetime, bearer drift, or broker-key mismatch. Its default transport disables
environment proxy routing and redirects. Caller-supplied transports are a
test/integration port and remain trusted validator dependencies.

Each call validates and recomputes the locked-request digest, request UUID,
global/logical sequence, retry attempt, and exact deadline. It then emits one
`dittobench-coding-inference-dispatch-v1` JSON envelope and signs the complete
bytes with the existing domain-separated DPoP message:

```text
ditto-inference:v1:
  grant_id:generation:nonce:requested_at:sha256(dispatch_body)
```

The six grant/proof headers, `Content-Type: application/json`, and
`Cache-Control: no-store` are fixed by the client. Miner headers, provider
routing overrides, referers, OpenRouter titles, raw URLs, and credentials are
never forwarded. The client performs exactly one HTTP attempt; receipt-free
model retries remain ordered and journaled by `codingrelay`.

## Trusted result boundary

The response is a
`dittobench-coding-inference-dispatch-result-v1` envelope containing:

- the global dispatch sequence;
- one canonical `dittobench-coding-provider-settlement-v1` projection;
- a nullable base64 exact normalized-response projection;
- a nullable base64 canonical failure-response projection.

The client requires HTTP 200, JSON, `Cache-Control: no-store`, bounded bytes,
duplicate-free JSON, required known authority, and exact ticket/case/profile,
grant/generation, request/attempt, and locked-request identity. It independently
recomputes the normalized or failure projection digest before returning a
deep-owned `codingrelay.UpstreamResult`. HTTP errors, redirects, malformed
responses, and missing settlements are generic unsettled failures; provider or
task text is never included in an error.

Bearer and private-key buffers are zeroed on close and on clock, expiry, or
nonce-generator integrity failure. Config, capability, client, and private
result types reject JSON diagnostics and provide redacted string/log values.

## Current activation boundary

This package is not constructed by the validator. Its model-relay target now
exists but is independently disabled by default and has no deployment
configuration. `internal/codinggateway` composes this client with the relay and
journal; the private runtime adapter now supplies the source-bound publisher
and exact revocation-only client. No production composition root invokes that
gateway. Ticket claiming, evidence publication, deployment, scoring, and
weights remain separate later reviews.

Validation:

```bash
cd services/dittobench-api
go test -race ./internal/codingplatform ./internal/codingrelay ./internal/codingrelayjournal
go vet ./internal/codingplatform ./internal/codingrelay ./internal/codingrelayjournal
```
