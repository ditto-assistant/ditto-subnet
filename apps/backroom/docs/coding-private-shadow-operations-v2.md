# Coding private shadow operations v2

Status: implemented Backroom visibility and guarded operator entry points. A
merge or deployment alone does not register a release, prepare a run, issue a
ticket, start a validator, or authorize private task access.

Authorized operators need release-level visibility into private Coding shadow
operation without seeing private corpus material. The Backroom
`/coding-control` surface and its MCP counterparts expose:

```text
opaque release status and audit completeness
provider-profile and publication-receipt freshness
selection/quorum progress and infrastructure failures
aggregate p0-p4, monotone score, confidence, and evidence counts
candidate timeout/OOM/integrity counts
quarantine and retirement status
```

The initial control surface uses existing Platform authorities:

```text
signed native private-v2 registration, quarantine and retirement
exact contract-v1 artifact reconciliation into a future-height run
fixed, sorted, unique k=3 contract-v1 validator ticket issuance
exact artifact-bound assignment, run, ticket and result inspection
```

Private-v2 registration stays `selectable=false`; it is not a contract-v2
launch switch. Contract-v1 reconciliation does not issue tickets. Ticket
issuance does not execute a container: each validator still claims and runs
under the existing certification, lease and evidence boundaries. These are
separate UI sections and separate MCP tools with exact confirmation phrases.

It MUST NOT expose task IDs, source repositories, issue text, memory records,
condition labels, hidden tests, patches, object keys, bucket names, endpoint
origins, credentials, wrapped keys, or unredacted provider receipts.

All values remain shadow diagnostics. Backroom may show `weight_eligible=false`
but may not toggle it. Release registration, selection, credential lifecycle,
private publication, and activation retain separate owner-authorized controls.
Every write requires a live write-level Backroom account, OAuth
`backroom:write` for MCP, same-origin protection for the browser, and forwards
the signed-in operator email to Platform. No private signing key or provider
credential is accepted by any control.
