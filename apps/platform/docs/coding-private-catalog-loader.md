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

## Deployment activation

The Platform Ansible role renders this source only when
`platform_coding_catalog_enabled: true`. Its defaults keep the feature off. An
operator must first create a distinct private S3-compatible bucket, then add
the catalog access-key and secret-key versions to Secret Manager. Terraform
creates only the empty secret containers
`platform-coding-catalog-access-key` and
`platform-coding-catalog-secret-key`; it never receives a credential value or
creates a corpus object.

The catalog identity must have only the permissions required by Platform:
`GetObject` under `coding-catalog/v1/*`, with no list, write, delete, public
read, or miner-upload-bucket access. It must be distinct from both the upload
and avatar identities. The Ansible role supplies it to the Python Platform API
through `DITTO_CODING_CATALOG_STORAGE_*`; the shared PM2 environment explicitly
clears it for the Go model-relay slots, and validators and miners never receive
it. The current app VM service account remains a host trust boundary: stronger
per-process cloud-secret isolation requires a separate service account or
secret-delivery service. Bucket policy, encryption, retention, and curator
upload are separate operator-reviewed controls and do not become active merely
by merging this code.

Developers who need the same bounded read-only identity for local Coding work
are granted access to the two catalog-specific Secret Manager containers via
the Terraform `coding_catalog_secret_users` allowlist. That allowlist grants
no curator credential, bucket write/delete/list permission, or access to the
upload, avatar, or trace credential pairs. Retrieve the values directly into
the local S3 client's credential mechanism; do not print them or commit them to
an env file.

The reviewed activation sequence is:

```yaml
# infra/ansible/host_vars/ditto-platform-<environment>.yml
platform_coding_catalog_enabled: true
platform_coding_catalog_endpoint: "https://<private-s3-origin>"
platform_coding_catalog_region: "<region>"
platform_coding_catalog_bucket: "<private-coding-catalog-bucket>"
```

After Terraform has created the two empty Secret Manager containers, the
operator adds the dedicated read-only key values out of band, converges the
Platform app role, and reruns `scripts/start.sh` or restarts/reloads the Platform
PM2 process with `--update-env` so it receives the new `.env`. The role rejects
empty catalog secret versions and rejects catalog identities or key material
that reuse upload HMAC or avatar Hippius credentials. Leaving the flag false is
the rollback; it omits every catalog variable from `.env` and leaves the source
unavailable after rerunning `scripts/start.sh` or restarting/reloading with
`--update-env`.

## Curator publication preflight

The public repository contains a no-upload curator preflight:

```bash
cd apps/platform
uv run python scripts/plan_coding_catalog_publication.py \
  --commitment /protected/catalog/commitment.json \
  --records-dir /protected/catalog/records \
  --output /protected/catalog/publication.json
```

It accepts only canonical external files named `000000.json` through the exact
committed task count. Every record is revalidated against the commitment,
position-bound Merkle proof, task version, runner plan, grader plan, and
resource profile before the tool emits its only permitted object key. The
output is a protected upload plan containing object hashes and opaque task
identities, not task bytes or credentials. It does not contact S3, write a
catalog object, register the commitment, or sign an admin request; those remain
separate offline curator operations.

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
