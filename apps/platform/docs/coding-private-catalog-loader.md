# Private coding-catalog record loader

Platform can optionally resolve one selected DittoBench Coding record from a
separately credentialed S3-compatible store. The loader is internal and
shadow-only: it exposes no route, enumerates no catalog, issues no ticket, and
never changes score or emissions.

## Storage boundary

The loader is disabled unless every `DITTO_CODING_CATALOG_STORAGE_*` credential
is configured. It never falls back to the miner-upload `STORAGE_*` bucket or
credentials. Production endpoints must use HTTPS; plaintext is accepted only
for loopback development. Credentials, endpoints, bucket names, and object keys
are omitted from the configuration representation and from typed loader errors.

One record is addressed only by the registered commitment digest and selected
zero-based index:

```text
coding-catalog/v1/<commitment-sha256>/records/<six-digit-index>.json
```

The caller cannot supply a URL, prefix, bucket, or arbitrary object key. The
source has no list operation and reads at most one bounded object per selector
probe. Catalog bytes are held in memory only; this layer creates no plaintext
disk cache.

## Record contract

Each object uses `dittobench-coding-private-catalog-record-v1` and contains:

```json
{
  "schema": "dittobench-coding-private-catalog-record-v1",
  "catalog_commitment_sha256": "...",
  "task_version": {},
  "membership_proof": {},
  "issue": {},
  "runtime_policy": {},
  "budgets": {},
  "runner_plan": {},
  "grader_plan": {},
  "grader_resource_profile": {}
}
```

Input is byte- and depth-bounded. Duplicate fields and non-finite JSON values
are rejected. Unknown fields are ignored for rolling compatibility and never
become digest authority. The existing task and proof models revalidate their
known fields and canonical digests. The issue, model-visible runtime policy,
and model/tool/wall budgets are hydrated in the same envelope and must
reproduce the three digests committed by the selected task leaf.

The task payload additionally commits `runner_plan_sha256`. The record must
reproduce that authoring preimage, the selected task's existing
`grader_plan_sha256`, and `resource_profile_sha256`. Cross-phase validation
binds the runner plan to the model-visible path/command IDs and binds both plans
to the same case, visible bundle, base tree, candidate limits, and compiled
grader contract. The loader returns the complete record internally; phase
delivery endpoints project only the authoring or grading subset.

The default and hard record bound is 2 MiB, and every command argv is
independently capped at 8 KiB. A curator must keep the complete projected
record within that operational envelope; an otherwise field-valid oversized
plan is deliberately undeliverable. Operators cannot expand the in-memory
transport budget further. Tabs, newlines, and carriage returns remain valid
issue text; other control characters are rejected so a contract-valid maximum
cannot expand sixfold during JSON encoding.

Before returning a record, the loader independently checks the exact registered
commitment digest, contract version, release, catalog index, Merkle root, task
count, task commitment, proof digest, issue digest, runtime-policy digest,
budget digest, and position-bound Merkle membership. The selector repeats the
authority and membership checks before constructing a run.

Missing objects, object-store failures, and timeouts are retryable unavailable
errors. Oversized, malformed, digest-drifted, or non-member records are
non-retryable integrity errors. Task cancellation is preserved rather than
translated into either domain.

## Activation boundary

The API lifespan constructs this source only when its separate configuration is
present. The single-run reconciler and ticket-bound task-lease builder may
consume it only when explicitly invoked; there is no background or fleet
caller. Automatic scheduling, HTTP task delivery, Luna inference grants,
validator execution, scoring, deployment, and emissions require later reviewed
layers. Coding contract v1 remains permanently `weight_eligible=false`.
