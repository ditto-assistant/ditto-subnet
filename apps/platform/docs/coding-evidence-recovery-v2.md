# Native reserved evidence recovery

This operator-only command resumes publication of already captured ciphertext for
one native inference, authoring or terminal reservation. It is not a worker retry,
host reconciler, decryption tool or scoring activation. It does not create a
reservation, grading claim, provider settlement, capability or new attempt.

Stop and reconcile the exact owning process and its containers first. Recovery
requires the original spool's exclusive existing lock and the original owner UID.
The mode-0700 spool must be outside Git, with unchanged owner-only immutable files.
No missing lock is created. A read-only spool rejects stores and does not delete,
repair or enumerate unrelated partial captures. The selected complete capture must
match the existing database identity, expected SHA-256, worker, evaluation and
attempt. Inference additionally requires its exact request UUID.

## Configuration and invocation

Run from the reviewed Platform installation as the original spool owner. Place
configuration files outside Git in an owner-only mode-0700 directory; each must be
a single-link, nonsymlink mode-0600 regular file. Paths must be absolute. Example
identifiers below are placeholders, not an authorized recovery target:

```json
{
  "schema": "dittobench-coding-evidence-recovery-config-v2",
  "shadow_only": true,
  "weight_eligible": false,
  "target": {
    "phase": "inference",
    "worker_id": "00000000-0000-0000-0000-000000000001",
    "evaluation_id": "00000000-0000-0000-0000-000000000002",
    "attempt_id": "00000000-0000-0000-0000-000000000003",
    "request_id": "00000000-0000-0000-0000-000000000004",
    "identity_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  },
  "postgres_environment_file": "/protected/coding/postgres.json",
  "hippius_environment_file": "/protected/coding/evidence-storage.json",
  "probe_receipt_file": "/protected/coding/evidence-probe.json",
  "spool_root": "/protected/coding/original-spool",
  "max_bytes": 2147483648,
  "max_objects": 4096
}
```

For `authoring` or `terminal`, omit `request_id`. Obtain the identity digest and
IDs from the exact trusted reservation, not an inferred directory name. The
PostgreSQL file uses the existing runtime JSON list of `POSTGRES_NAME=value`
entries. Only host, port, user, password, database, command timeout and pool-size
settings are accepted; the first five are required. No process environment is
used to supply these credentials.

The separate Hippius JSON object accepts only string values for
`DITTO_CODING_HIPPIUS_ENDPOINT_URL`, `DITTO_CODING_HIPPIUS_SEALED_EVIDENCE_BUCKET`,
`DITTO_CODING_HIPPIUS_EVIDENCE_MEDIATOR_ACCESS_KEY`,
`DITTO_CODING_HIPPIUS_EVIDENCE_MEDIATOR_SECRET_KEY`,
`DITTO_CODING_HIPPIUS_REGION` and `DITTO_CODING_HIPPIUS_TIMEOUT_SECONDS`.
Use the existing sealed-evidence parser's required values and bounds. Do not
supply private-input reader, curator, unwrap, signing or provider credentials.

```bash
uv run python -m ditto.coding_evidence_recovery --inspect \
  --config /protected/coding/recovery.json

uv run python -m ditto.coding_evidence_recovery --resume-reserved \
  --config /protected/coding/recovery.json \
  --confirm 'RECOVER RESERVED CODING EVIDENCE'
```

There is no default action. Inspection reads only protected configuration,
PostgreSQL and selected local ciphertext. It does not load Hippius credentials or
the probe receipt, contact storage or write database rows. Its `prepared` state
means local capture and reservation consistency, not current publication authority.

## Publication and refusal boundaries

Publication requires a fresh matching Hippius probe, the same immutable storage
domain and the existing publication deadline. A new matching probe can renew the
storage check but cannot extend that deadline. Every selected local blob is
validated before provider I/O. Recovery performs exact-key reads, uploads only on
an explicit not-found response, and verifies downloaded ciphertext again. It does
not list objects, add semantic metadata, overwrite an observed mismatch, re-encrypt
or fall back to another provider. Deterministic keys and existing append-only
reservations retain the normal mediator's authority model; no additional provider
immutability guarantee is claimed.

After all readbacks, recovery locks and rechecks ownership, the identity and the
database clock before inserting only the existing phase's finalization row.
Terminal finalization closes the matching private task in the same transaction;
it does not rerun grading or require its plaintext result body. Authoring recovery
does not start grading. Inference recovery does not redispatch the provider.

The bounded JSON receipt contains phase, evaluation/attempt IDs, identity digest,
counts and state, with `reexecuted=false`, `shadow_only=true` and
`weight_eligible=false`. `already_finalized` acknowledges an existing database
finalization after validating the local capture; it makes no fresh provider
durability claim. Errors produce a fixed redacted diagnostic and nonzero exit.

Missing or partial selected captures, absent reservations, mismatched targets,
corrupt remote bytes, changed storage domains, expired publication windows and
ambiguous execution without durable evidence remain exact-resource operator
reconciliation cases. Do not delete their tombstones or reuse their attempts.
The command does not enable a service, provision credentials, decrypt evidence,
prove container cleanup or qualify a live private canary.

## Process-death qualification regressions

`test_coding_evidence_process_recovery.py` exercises a real separate writer
process and kernel file lock, terminating only that test-owned process with
SIGKILL before capture, after directory creation, after sealed-byte persistence,
and after the complete store returns. The parent proves that a live owner blocks
recovery, process death releases the original lock without replacing it, partial
captures remain unmodified and unusable, committed bytes survive unchanged, and
read-only recovery cannot create new captures. A writer restart also refuses
retained partial state rather than repairing it automatically.

These are public synthetic byte/lock tests on the test host, not power-loss,
native-host, encryption, PostgreSQL reservation or Hippius durability evidence.
The existing database-backed recovery suite separately verifies reservation
identity, lost acknowledgements, expiry, corrupt remote bytes, and phase-specific
finalization without candidate/inference/grading re-execution. Actual operational
gate 4 still requires approved fault drills on the intended native host with the
real encrypted spool, ledger and exact Hippius readback. Keep that evidence
separate from CI and reconcile the exact owning process/containers before any
operational recovery invocation.
