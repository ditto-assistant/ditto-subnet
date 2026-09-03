# Ticket-bound S3 artifact connectivity canary

The artifact canary is a separate mode of the attested
`dittobench-coding-executor-scorer` binary. It proves one real, ticket-bound S3
delivery through the existing hardened `internal/codingartifacts` fetcher
without constructing the coding host or executing candidate code.

## Input boundary

The operator pre-positions one root-owned mode-`0400`
`artifact-capability.json`. Systemd passes it through `LoadCredential`; Ansible
never reads, copies, templates, logs, or stores its presigned bearer URL.

The canary accepts only:

- delivery phase `authoring`;
- artifact kind `visible-bundle`;
- audience `workspace-materializer`;
- a nonzero ticket UUID and active ticket deadline;
- a short-lived S3 v2/v4 URL whose path, encoded expiry, exact size, and SHA-256
  agree with the capability.

Memory, resource, grader, grading-phase, expired, redirected, private-network,
oversized, short, and digest-mismatched deliveries fail without a receipt.

## Execution boundary

The one-shot unit starts the same provenance-verified scorer binary with
`--artifact-canary`. That mode constructs only the artifact fetcher and canary.
It does not construct `codinghost`, Docker executors, the supervisor, the
publication journal, inference relay, Platform client, validator keypair, or a
candidate workspace. The verified file is streamed to `io.Discard` and removed
when its reader closes.

The service has no Docker socket, scorer control socket, supplementary group,
listener, wallet, Platform/provider credential, or writable path outside its
fixed mode-`0700` canary directory.

## Diagnostic receipt

Success atomically writes a mode-`0600` receipt containing:

- the artifact kind, exact SHA-256, and byte size;
- a SHA-256 of the ticket UUID rather than the raw ticket;
- capability expiry and local start/completion times;
- explicit `ticket_authority_used=true`, `s3_accessed=true`,
  `platform_contacted=false`, and `candidate_executed=false` fields.

The receipt never contains the presigned URL, S3 key, raw ticket UUID, file
path, certificate, or credential. It is unsigned operator-local diagnostic
evidence and cannot authorize assignment, certification, scoring, weights, or
emissions.

## Activation boundary

`coding_executor_artifact_canary_enabled` is false. The operator must first
obtain and review the validator connectivity receipt from the preceding layer,
then explicitly pre-position one active capability and enable this one-shot
control. Merge, release, and ordinary host convergence perform no S3 request.
