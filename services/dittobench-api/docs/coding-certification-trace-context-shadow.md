# Privacy-scoped coding certification trace context

## Status

This contract adapts the inference tracing introduced by merged PRs #1079 and
#1081 to the shadow coding-certification path. It is documentation-only:
there is no route, sink configuration, worker execution, scoring input, or
weight activation in this change.

## Trusted producer

Only the validator-owned coding relay may stamp a coding trace context on its
upstream model-relay request. A miner request, coding harness, workspace tool,
or provider response must never supply or override that header.

The future header remains advisory to generic trace capture, but its values are
authoritative only after the coding relay reconciles them with its durable
dispatch journal and provider receipts.

## Allowed context

The canonical v1 object contains only opaque authority and ordering fields:

```json
{
  "schema": "ditto.coding.certification.trace-context.v1",
  "coding_contract_version": 1,
  "phase": "certification",
  "lease_id": "opaque-lease-id",
  "agent_artifact_sha256": "lowercase-sha256",
  "screened_image_sha256": "lowercase-sha256",
  "dispatch_sequence": 1
}
```

The later lease implementation supplies `lease_id`; until then this context is
not emitted. `dispatch_sequence` is allocated before the provider-visible
request and is never inferred from completion order.

## Forbidden data

The context must never carry:

- ticket or workspace capability URLs, bearer tokens, provider credentials, or
  headers;
- repository paths, repository contents, patches, test output, hidden tests,
  grader commands, or frozen-submission bytes;
- memory contents, user/profile identifiers, issue text, prompts, tool
  arguments/results, model completions, or provider raw bodies; or
- a coding task ID, private catalog identity, score, reward, or weight.

Raw inference bodies remain governed by the private trace-sink policy from
#1079. Coding dispatch must stay disabled until sink retention, access, and
deletion controls explicitly cover private coding traffic.

## Reconciliation

At certification termination, the coding relay must produce an ordered trace
receipt root over every admitted dispatch. Each receipt binds the context,
locked request digest, trusted provider settlement/receipt identity, and the
previous receipt digest. Missing, duplicate, reordered, foreign-lease, or
unreceipted dispatches fail closed as validator infrastructure evidence; they
must not become a miner failure or a clean rerun.

The terminal coding certification record may retain only the receipt root and
bounded trace-object identities. It must not copy trace bodies into Platform,
the public contract package, the coding outbox, or miner-visible responses.

## Implementation order

1. Add a strict Go trace-context parser and relay-side header stamper in
   `internal/codingrelay`, backed by its durable dispatch journal.
2. Teach model-relay to retain this context only on the private coding trace
   stream and reject/strip it from any miner-originated request.
3. Reconcile ordered trace receipts into coding relay evidence and the existing
   certification outbox.
4. Enable a coding trace sink only after separate retention and access review.

All stages remain shadow-only and `weight_eligible=false`.
