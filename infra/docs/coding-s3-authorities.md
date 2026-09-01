# Coding S3-compatible authority rollout

The `gcp-platform` stack defines two non-interchangeable coding byte
authorities using Google Cloud Storage's S3-compatible XML API:

- private inputs: immutable catalog records and task artifacts;
- sealed evidence: transcripts, frozen submissions, and exact publication
  request and acknowledgement bytes.

They are absent by default. Merging the Terraform does not create a bucket,
identity, HMAC key, secret version, IAM binding, or audit configuration.

## Identity boundary

Each environment receives three dedicated service accounts:

| Identity | Private inputs | Sealed evidence | Object list/delete |
| --- | --- | --- | --- |
| input curator | create only | none | denied by omission |
| input reader | exact-object get only | none | denied by omission |
| evidence finalizer | none | create and exact-object get | denied by omission |

The accounts receive no project role. Bucket bindings use either
`roles/storage.objectCreator`, which cannot view, delete, or overwrite an
object, or repository-owned custom roles containing only
`storage.objects.get` and, for evidence, `storage.objects.create`.

HMAC keys exist only to support the repository's established S3-compatible
client surface at `https://storage.googleapis.com`. Terraform writes each
one-time key secret into a dedicated Secret Manager container. A second,
false-by-default Platform-binding gate may grant only the input-reader and
evidence-finalizer secrets to the dedicated server-side Platform identity. The
curator key remains outside the runtime under every gate combination.

Validators, executors, candidate containers, and model processes receive no
bucket IAM and no long-lived credential. They may later receive only a bounded
method- and object-specific capability after Platform authorization.

## Storage controls

Both buckets enforce uniform bucket-level access, public-access prevention,
versioning, a minimum retention policy, and `prevent_destroy`. Runtime
identities cannot overwrite or delete, so content-addressed names remain
create-once. Incomplete multipart uploads are aborted after one day; no rule
expires current objects.

Cloud Storage's XML API permits unencrypted HTTP by default. Enabling the
authorities therefore also enforces the project-wide
`storage.secureHttpTransport` organization policy. The protected plan must
check every existing XML/S3 client in the project before apply; all checked-in
Platform storage endpoints already render HTTPS.

The initial policies are intentionally not locked. Locking a Cloud Storage
retention policy is irreversible. Review the actual protected plan, prove the
full upload/finalization/recovery flow, and approve a separate lock operation
after the retention values are final.

When enabled, Terraform also manages Data Read and Data Write audit logging for
`storage.googleapis.com`. That is project-wide and can increase log volume, so
the first plan must check existing audit configuration and expected cost.

## First protected plan

The authority-rollout source intent sets
`enable_coding_s3_authorities = true` while keeping
`enable_coding_storage_platform_binding = false`. Merge records intent only;
it does not run Terraform.

A read-only preflight on 2026-09-01 returned `NOT_FOUND` for all four proposed
bucket names. That observation does not reserve a globally unique name, so the
protected plan/apply operator must recheck immediately before apply. The same
developer identity lacked permission to inspect effective org-policy and audit
configuration; no conclusion was drawn from that gap.

1. Confirm the plan source is the exact current `main` SHA.
2. Recheck the four proposed names and confirm they remain globally available.
3. Confirm the EU location and 30-day input / 90-day evidence minimums.
4. Inspect existing project Cloud Storage audit and organization-policy
   configuration for conflicts, and confirm every XML client uses HTTPS.
5. Confirm Platform secret binding and both application gates remain false.
6. Run the protected `gcp-platform` plan; never apply from an application
   release workflow.
7. Verify the plan creates exactly two buckets and three identities per
   environment, with no public, list, overwrite, delete, or Platform grant.
8. Apply only after owner approval, then record the exact Terraform revision
   and resource identities.

## Platform binding

The follow-on access-rollout source sets
`enable_coding_storage_platform_binding = true`, but that PR must remain draft
and unmerged until the authority-only plan has been applied and independently
verified. Merging it earlier would collapse two separately reviewed authority
transitions into one Terraform plan.

Once the prerequisite is recorded, its protected plan may grant the dedicated
Platform API identity access only to the private-input reader and evidence-
finalizer secret containers. The plan must contain no curator grant, bucket IAM
change, application config, Ansible converge, or retention lock. Both Platform
application integrations remain false after apply.

Before an Ansible converge, set `PLATFORM_TARGET_ENV=dev|prod` and source
`infra/ansible/scripts/platform-app-env.sh`. The script selects only that
environment's bucket names and non-secret HMAC access IDs. Ansible reads the
matching secret halves under `no_log` only when the private-input or evidence
gate is explicitly enabled. It rejects missing, shared, non-TLS, or ordinary
upload/avatar credentials before rendering `.env`. Relay processes explicitly
clear every private-input and evidence storage variable.

Even after apply, coding remains inactive. Platform configuration, secret
access, capability issuance, worker activation, scoring, and weights require
separate reviews.

## Post-apply control verification

After each protected apply, run the inert control-plane verifier before moving
to the next phase:

```sh
python3 infra/ansible/scripts/verify_coding_storage_authorities.py \
  --project ditto-app-dev \
  --environment prod \
  --phase authorities \
  --source-sha <exact-applied-main-sha> \
  --output coding-storage-authorities-receipt.json
```

Use `--phase platform-access` only after the separate Platform-binding apply.
The command issues only `gcloud` describe/list/IAM-policy reads. It never reads
a Secret Manager payload or performs an object operation. It verifies bucket
location, class, uniform access, public-access prevention, versioning,
retention, exact bucket IAM, service-account identities, one active HMAC key
per identity, secure HTTP transport, Data Read/Write audit logging, and the
phase-specific Platform secret bindings. Any curator access, direct Platform
bucket IAM, broad storage role, public member, missing audit mode, or drifted
retention fails closed.

Run it with the protected infrastructure plan identity or a dedicated audit
identity that can read bucket descriptions/IAM, service-account and HMAC
metadata, Secret Manager IAM policies, effective organization policy, and the
project audit configuration. It needs no `secretmanager.versions.access`,
object-data, bucket-mutation, IAM-mutation, or Terraform-state write permission.
An identity missing any required control-plane read fails the verification; do
not reinterpret that as a passing or absent policy.

The output is mode `0600`, creation-only, URL-free, and contains no HMAC access
ID or secret value. Its canonical payload receives a SHA-256 receipt digest.
This receipt proves control-plane posture only. A later data-plane canary must
separately prove exact private-input GET and sealed-evidence PUT/HEAD/full-hash
behavior before either Platform application gate is enabled.

## Data-plane canary

After both control-plane receipts pass, use the separate confirmation-gated
canary modes. First, an authorized curator operator writes the deterministic
private-input canary using a temporary owner-only secret file:

```sh
cd apps/platform
umask 077
uv run python scripts/verify_coding_storage_data_plane.py seed-private-input \
  --project ditto-app-dev \
  --environment prod \
  --source-sha <exact-applied-main-sha> \
  --curator-access-key <non-secret-terraform-output> \
  --curator-secret-file <mode-0600-temporary-file> \
  --confirm 'SEED CODING STORAGE PRIVATE INPUT CANARY' \
  --output coding-storage-curator-seed-receipt.json
```

The curator command performs one create-only write. It never reads, lists,
overwrites, or deletes an object. The fixed content-addressed key means a
second seed cannot silently replace the first object. The operator must remove
the temporary curator secret file immediately after the command; neither the
receipt nor the Platform verification path contains that credential.

Then run Platform verification on the GCE host carrying the exact
`ditto-platform-api` service account:

```sh
cd /opt/ditto-subnet/apps/platform
uv run python scripts/verify_coding_storage_data_plane.py verify-platform \
  --project ditto-app-dev \
  --environment prod \
  --source-sha <exact-deployed-main-sha> \
  --private-input-access-key <non-secret-terraform-output> \
  --evidence-access-key <non-secret-terraform-output> \
  --confirm 'RUN CODING STORAGE DATA PLANE CANARY' \
  --output coding-storage-data-plane-receipt.json
```

This mode rejects any other attached identity, proves the curator secret is
denied, and fetches only the reader/finalizer secret values into process
memory. It verifies the exact private-input bytes and SHA-256; reader list,
write, delete, and cross-authority operations must be denied. It then uses the
production sealed-evidence capability minter for a checksum/metadata-bound PUT,
HEAD, and full-download SHA-256 verification; finalizer list, delete, and
cross-authority operations must be denied. A matching immutable evidence object
is reused without a second PUT.

Both receipts are URL-free, secret-free, creation-only mode `0600`, and
SHA-256 bound. The commands are never automatic and intentionally leave their
small retained canary objects in place. Do not enable either Platform
application integration until the control-plane and data-plane receipt digests
are reviewed together.

## Development Platform activation

Only after all four control/data-plane receipt digests are reviewed may the
development host override both role defaults:

```yaml
platform_coding_catalog_enabled: true
platform_coding_evidence_enabled: true
platform_coding_storage_readiness_enabled: true
```

Production pins both values to `false`. Merge does not deploy or converge an
infra-only host-var change. Before converge, verify the `dev` branch contains
the exact reviewed Platform storage implementation and canary commits; the
development host intentionally checks out `dev`, not `main`. The operator must
then select the development Terraform outputs and converge only the development
VM:

```sh
export GCP_OSLOGIN_USER=<operator-os-login-user>
export PLATFORM_TARGET_ENV=dev
source infra/ansible/scripts/platform-app-env.sh
ansible-playbook -i infra/ansible/inventory/gcp.yml \
  infra/ansible/playbooks/gcp-platform-app.yml \
  --limit ditto-platform-dev
```

The role fails before Secret Manager access or `.env` rendering if either
environment-scoped bucket/access ID is absent or if the two authorities reuse a
bucket, HMAC identity, miner-upload authority, or avatar authority. After
converge, verify the exact dev checkout/config SHA and API health, confirm the
private catalog and evidence minters load only in the Python Platform role, and
recheck that the production `.env` was not modified.

This activation still creates no coding run or ticket, starts no worker or
executor, invokes no model or grader, and affects no score, rank, weight, or
emission. Rollback is both dev flags back to `false` followed by the same
development-only converge.

## Runtime readiness visibility

When the development-only readiness gate is enabled, an authenticated admin
may request `GET /api/v1/admin/coding-storage/readiness`. The Platform process
performs only exact-key reads against the two retained data-plane canaries: a
bounded private-input GET and sealed-evidence HEAD plus full-object SHA-256.
The response contains the expected digest, size, status, deployed source SHA,
and environment, but no bucket, object key, endpoint, access ID, secret, or
presigned URL. Missing, drifted, timed-out, or unavailable objects fail the
combined readiness result closed.

Backroom exposes the same snapshot as a read-only MCP operation. This status
is operator evidence only: `read_only=true` and `weight_eligible=false`, with
no database mutation, task issuance, worker activation, score, rank, weight,
or emission effect. Production and the role default keep the readiness gate
false, and relay processes explicitly scrub it.
