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
