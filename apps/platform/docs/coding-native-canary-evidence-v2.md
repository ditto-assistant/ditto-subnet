# Native canary evidence acceptance checks

This is a read-only acceptance prerequisite for one already completed full-path
native canary. It does not select a task, admit/start an assignment, launch a
worker, invoke inference, grade, publish missing evidence, acknowledge a result,
repair retained state, or authorize rollout. It is not a new public control plane.

## What it verifies

The verifier requires an independently selected worker/assignment and Platform
signing identity, plus one exact signed result and terminal commitment. It checks:

1. The existing canonical, bounded, unexpired SR25519 result envelope against
   its expected evaluation, attempt, validator, artifact, assignment, policies,
   execution/grading profiles and request digest. The outcome must be completed.
2. The matching persisted result delivery and validator acknowledgement, an
   admitted/started assignment, and a frozen private task closed as completed.
3. Finalized terminal and authoring identities, their grading-claim linkage,
   common source, and exact frozen patch identity/size.
4. A revoked inference grant and at least one settled inference request, with
   every request's finalized evidence and the ordered evidence-set digest bound
   into authoring. At most 256 requests are admitted. A canary that skipped
   inference cannot demonstrate that path.
5. Complete matching local ciphertext captures under the original spool locks,
   a fresh matching Hippius probe/storage domain, and fresh exact-key reads for
   every inference, authoring chunk/manifest, and terminal blob. Existing envelope
   framing and ciphertext digests are verified without loading unwrap keys.
6. The same immutable target set after readback, with the signed result still
   valid. Database snapshots use repeatable-read, read-only transactions.

A missing/corrupt remote object fails. There is no upload, fallback, repair or
re-execution. Unlike a historical finalization acknowledgement, success requires
fresh remote reads. Readback freshness does not prove that wrapping private keys
remain available; this tool never decrypts evidence or recomputes the grade.

## Protected operator input

First stop and reconcile the exact owning runtime and its containers. Run from
the approved Platform installation as the original spool owner. Configurations
and referenced files use the existing protected-file rules: canonical absolute
paths, single-link owner-owned mode-0600 files and private directories. Spools
must remain owner-only mode 0700 outside Git, with their original existing lock
and immutable captures. No missing directory/lock is created or repaired.

The config schema is `dittobench-coding-canary-evidence-config-v2`, with explicit
`shadow_only=true` and `weight_eligible=false`. Required fields are:

| Field | Input |
|---|---|
| `expected` | All fields of `HostedResultExpectation`: evaluation/attempt UUIDs, validator/Platform hotkeys, artifact/assignment/policy/execution-profile/grading-profile/request digests |
| `worker_id` | Exact original native worker UUID |
| `result_sha256` | Existing `hosted_message_digest` of the selected verified signed response; not a hash of its file or signature bytes |
| `terminal_sha256` | Independently selected terminal identity commitment |
| `signed_result_file` | Original canonical signed response bytes, at most 8192 bytes |
| `postgres_environment_file` | Existing protected allowlisted `POSTGRES_*=value` JSON list |
| `hippius_environment_file` | Protected sealed-evidence configuration only |
| `probe_receipt_file` | Fresh matching Hippius authority probe receipt |
| `spool_roots` | Exactly `inference`, `authoring`, `terminal` mapped to the original spool paths |

Select the expected Platform key independently; do not trust whatever key a
response names. Unknown config fields grant no additional authority. Invalid
signed responses are rejected before database/storage credential files are read.

The Hippius file accepts only `DITTO_CODING_HIPPIUS_` settings for `ENDPOINT_URL`,
`SEALED_EVIDENCE_BUCKET`, `EVIDENCE_MEDIATOR_ACCESS_KEY`,
`EVIDENCE_MEDIATOR_SECRET_KEY`, `REGION`, and `TIMEOUT_SECONDS`. Although the
existing mediator credential type is reused, this path performs GETs only.
Private-input, provider, curator, unwrap and signing credentials are not loaded.
Each spool is bounded to 2 GiB / 4096 objects for this inspection profile.

```text
<approved-platform-python> -B -I -m ditto.coding_canary_acceptance \
  --verify-evidence --config /ABS/PRIVATE/canary-evidence.json
```

The command prints a source-free JSON report only after all checks pass and
handles close. Preserve it through an approved private evidence path; it is not
a signed validator response. Errors exit nonzero and report only a fixed safe
stage (result binding, ledger, or phase readback), never underlying exception
details, paths or credentials. Configuration failures remain generic. The
report includes source commitments, phase readback counts/digests and current
readback timestamps, not plaintext, storage keys, provider credentials or scores.

## Acceptance remains separate

`canary_evidence_verified=true` proves only the checks above. It is not proof of
physical container cleanup, calibration quality, host integrity, key custody,
native-matrix acceptance, or rollback. The report explicitly lists those pending
acceptance decisions and preserves `rollout_approved=false`, `shadow_only=true`,
and `weight_eligible=false`. Review actual host, calibration, custody and rollback
evidence before separately approving a bounded shadow rollout.

Tests use real PostgreSQL, synthetic signed responses, real encrypted local
captures and a fake Hippius transport. They check GET-only readback, database
read-only enforcement, repeated fresh verification, and failure on wrong targets,
untrusted signers, missing/corrupt evidence and malformed CLI requests. They do
not constitute a production canary or real Hippius durability qualification.
