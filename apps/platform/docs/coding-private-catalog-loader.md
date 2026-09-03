# Private coding-catalog record loader

Platform can optionally resolve one selected DittoBench Coding record from a
separately credentialed S3-compatible store. The loader is internal and
shadow-only: it exposes no route, enumerates no catalog, issues no ticket, and
never changes score or emissions.

The selected production object provider is Hippius. The provider-specific
secrecy, encryption, credential, exact-read, evidence-mediation, and canary
requirements are fixed by
[`docs/coding-private-hippius-data-plane.md`](../../../docs/coding-private-hippius-data-plane.md).
This existing loader is not proof that those requirements are implemented or
active.

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

The catalog identity must use the narrowest bucket-specific Hippius reader
scope available. The reviewed provider scope may also permit bucket listing,
so provider role names alone do not satisfy the application contract. Platform
may issue only exact `GetObject` operations under `coding-catalog/v1/*` and
must never list, write, delete, alter ACLs, or access another bucket. Audit and
the provider canary must prove that behavior. The identity must be distinct
from upload, avatar, trace, curator, and evidence identities.

The Ansible role supplies it to the Python Platform API through
`DITTO_CODING_CATALOG_STORAGE_*`; the shared PM2 environment explicitly clears
it for the Go model-relay slots, and validators and miners never receive it.
The current app VM service account remains a host trust boundary: stronger
per-process secret isolation requires a separate service account or
secret-delivery service. Client-side encryption, credential custody, provider
capability verification, and curator upload are separate operator-reviewed
controls and do not become active merely by merging this code.

Offline publication uses a separate bucket-scoped read-write identity stored as
`platform-coding-catalog-curator-access-key` and
`platform-coding-catalog-curator-secret-key`. Terraform owns those containers
but intentionally grants neither one to the Platform app service account. The
curator identity is for the protected upload workflow only; it must never be
rendered into Platform, Backroom, relay, validator, or miner runtime settings.

The reviewed activation sequence is:

```yaml
# infra/ansible/host_vars/ditto-platform-<environment>.yml
platform_coding_catalog_enabled: true
platform_coding_catalog_endpoint: "https://s3.hippius.com"
platform_coding_catalog_region: "decentralized"
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

That sequence remains blocked until the Hippius-only contract's client-side
encryption, exact-object verification, non-human credential custody, and live
provider canary are implemented. Do not activate this loader with a personal
token or an existing avatar, trace, or general account credential.

The provider canary begins with the confirmation-gated
`scripts/probe_hippius_coding_storage.py` operator tool. Its successful receipt
proves only the synthetic capability checks recorded there; it neither enables
this loader nor certifies private-data secrecy.

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

Backroom MCP exposes the append-only release ledger through
`get_coding_catalog_releases`, `register_coding_catalog_release`,
`supersede_coding_catalog_release`, and `retire_coding_catalog_release`.
Supersession is one Platform transaction: it appends the signed replacement and
the predecessor's retirement tombstone together, or appends neither. It never
updates or deletes a release, retirement, exposure, or private problem object.

`scripts/prepare_hippius_private_inputs.py` consumes the same commitment and
records, recomputes that plan, and prepares the encrypted local transport. It
accepts only a public wrapping key and performs no Hippius request. Its
digest-bound manifest is not registration or activation authority.

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
