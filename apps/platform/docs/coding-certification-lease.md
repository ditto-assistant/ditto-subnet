# Qualified coding-certification leases

Platform mints one shadow-only public-canary lease after it re-checks current
core qualification for the exact agent artifact. The lease is not a scoring
ticket, not a private coding assignment, and remains `weight_eligible=false`.

## Write authority

`POST /api/v1/validator/coding-certification-leases` issues a lease only when
all of these hold in one transaction:

- a permitted validator hotkey and valid sr25519 signature;
- a complete, content-addressed screened image on the agent;
- a configured shadow core-qualification policy for the requested benchmark;
- the latest complete observation is `qualified` and binds the same agent,
  source artifact, screened image, benchmark, and current policy checksum;
- no `issued` or `claimed` lease already exists for that identity;
- the committed public `certification/v1` canary identity can be loaded.

Issuance and policy revision share a per-benchmark transaction lock, so a
lease cannot bind an observation that is already stale at commit. A later
incomplete observation does not hide an earlier complete qualified wave.

An absent policy, incomplete wave, unqualified observation, missing image, or
stale artifact is not an error against the normal submission. The endpoint
returns `404`.

`POST /api/v1/validator/coding-certification-leases/{lease_id}/claim` is
exclusive to the named validator. Exact signed retries of an already-issued,
already-claimed, or already-aborted lease authenticate and return the stored
row. The request nonce is recorded on first success; a replay of that nonce
is accepted only when the mutation is idempotent. `POST .../abort` is allowed
only while the lease is still `issued`. A claimed lease cannot be aborted, so
a restart cannot create a clean rerun. Expiry is committed before the
endpoint returns `404`.

## Storage

`coding_certification_leases` stores the frozen authority JSON plus the
screened-image identity (digest, config id, reference, upload id). Rows are
never rewritten to look current except for the one-way status transitions
`issued → claimed`, `issued → aborted`, and `issued → expired`. Coding
contract v1 stays `weight_eligible=false`.

## Activation boundary

The validator public-canary worker claims this lease, drives
`codingcertifier` through the scorer control plane, and submits the terminal
receipt against the claimed lease. Private-task admission remains a later
reviewed step. Ordinary Tool + Memory scoring, weights, and emissions do not
read this table.
