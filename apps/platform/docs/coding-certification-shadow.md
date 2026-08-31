# Shadow coding capability-certification persistence

Platform stores validator-signed DittoBench Coding capability receipts without
changing the ordinary tool/memory score, queue, rank, weight, or emissions.
Coding contract v1 remains permanently `weight_eligible=false`.

## Write authority

`POST /api/v1/validator/agent/{agent_id}/coding-certification` accepts one
receipt only when all of these match:

- a permitted validator hotkey and valid sr25519 signature;
- a claimed certification lease for the same validator, agent, artifact,
  screened image, and benchmark version;
- a representable receipt timestamp no earlier than the lease issuance (with
  five minutes of clock skew), no later than its deadline, and not expired;
- the agent's immutable source-artifact SHA-256;
- the agent's current screened-image SHA-256;
- the receipt's known-field canonical digest;
- for invoked model evidence (`complete` or `provider_failure`), the claimed
  lease's `coding_certification_inference_requests` ledger: no `started` or
  `unsettled` rows, grant accounting equal to the receipt, and
  `provider_receipt_set_sha256` equal to the reconstructed settlement digest;
- a terminal (`revoked` or `exhausted`) canary grant and immutable binding
  fields on the receipt row recording its generation, inference-grant digest,
  and settlement-set digest.

Unused-inference failed receipts (`coding_inference_not_observed`, no invoked
model evidence) persist without a settlement row. A settlement on that lease
makes that unused-inference claim a `409`.

The signature binds the validator, agent, benchmark version, lease ID,
screened image, and receipt digest. Exact retries are idempotent. Reusing the
same `(agent, validator, coding_contract_version, certification_id)` or the
same lease for different evidence returns `409`. A scoring ticket is not
authority for this write.

## Storage and invalidation

`coding_capability_certifications` is append-only. Rows are never updated to
look current. Active state is derived at read time and requires:

```text
status = certified
receipt not expired
stored artifact == current agent artifact
stored screened image == current screened image
immutable terminal-ledger binding present
```

A new upload or rebuilt screened image therefore invalidates prior evidence
without deleting audit history. The table stores only content-addressed
transcript/frozen-submission keys and the bounded receipt JSON, never repository
contents, grader tests, provider credentials, or signing keys.

Legacy rows without the immutable binding remain visible for audit but are not
active certification authority. They cannot create private assignments, tickets,
claims, task leases, or coding inference grants.

## Operator visibility

`GET /api/v1/admin/agents/{agent_id}/coding-certifications` returns newest-first
history, active/stale reasons, and current support/certification summaries.
Backroom exposes the same read through `get_agent_coding_certifications` under
`backroom:read`.

## Activation boundary

This persistence is evidence only. No ordinary scoring or weight path reads it.
Shadow core qualification is recorded separately. New receipts bind a claimed
certification lease for the same artifact and remain permanently
weight-ineligible. Coding emissions still require a new contract version,
calibration, and owner approval.
