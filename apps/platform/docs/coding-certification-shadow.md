# Shadow coding capability-certification persistence

Platform stores validator-signed DittoBench Coding capability receipts without
changing the ordinary tool/memory score, queue, rank, weight, or emissions.
Coding contract v1 remains permanently `weight_eligible=false`.

## Write authority

`POST /api/v1/validator/agent/{agent_id}/coding-certification` accepts one
receipt only when all of these match:

- a permitted validator hotkey and valid sr25519 signature;
- an open canonical scoring ticket for the same agent, validator, benchmark
  version, and exact lease deadline;
- a representable receipt timestamp no earlier than the ticket issuance (with
  five minutes of clock skew), no later than its deadline, and not expired;
- the agent's immutable source-artifact SHA-256;
- the agent's current screened-image SHA-256;
- the receipt's known-field canonical digest.

The signature binds the validator, agent, benchmark version, lease deadline,
screened image, and receipt digest. Exact retries are idempotent. Reusing the
same `(agent, validator, coding_contract_version, certification_id)` for
different evidence returns `409`.

## Storage and invalidation

`coding_capability_certifications` is append-only. Rows are never updated to
look current. Active state is derived at read time and requires:

```text
status = certified
receipt not expired
stored artifact == current agent artifact
stored screened image == current screened image
```

A new upload or rebuilt screened image therefore invalidates prior evidence
without deleting audit history. The table stores only content-addressed
transcript/frozen-submission keys and the bounded receipt JSON, never repository
contents, grader tests, provider credentials, or signing keys.

## Operator visibility

`GET /api/v1/admin/agents/{agent_id}/coding-certifications` returns newest-first
history, active/stale reasons, and current support/certification summaries.
Backroom exposes the same read through `get_agent_coding_certifications` under
`backroom:read`.

## Activation boundary

This persistence is evidence only. No ordinary scoring or lease path reads it.
Shadow core qualification is recorded separately. The separate shadow coding
ledger may bind a future validator-specific coding lease to this exact receipt,
but it has no production issuer and remains permanently weight-ineligible.
Coding emissions still require a new contract version, calibration, and owner
approval.
