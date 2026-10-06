# Platform PostgreSQL backup and restore

SN-49 protects `ditto-pg-platform` in `ditto-app-dev/us-central1-a`. PGDATA
and WAL remain on its existing boot disk; the raw 50 GB data disk is untouched.
Snapshots are crash-consistent (`guest_flush=false`): recovery replays WAL as
after power loss. The online dump never stops/restarts PostgreSQL.

## Activation gates and evidence

Source/CI, merge/application release, protected Terraform apply, secret custody,
host convergence, and live backup/restore are separate stages. Token-creation
authorization does not authorize IAM changes, a merge, or production convergence.

1. Review the exact PR head and merge only with owner authorization. Application
   semantic releases can deploy Platform and Backroom after merge, but reader
   config and the DB timer are disabled until explicitly converged.
2. Dispatch `infra-plan-apply.yml` with root `gcp-platform`, operation `plan`,
   and these two targets (one comma-separated value):

   ```
   google_compute_resource_policy.platform_postgres_daily,module.pg_vm.google_compute_disk_resource_policy_attachment.boot_snapshot
   ```

   The first dedicated plan checker reports **2 creates, 0 updates, 0 deletes**.
   If that apply created the exact policy before an attachment failure, a fresh
   plan may report **1 create, 0 updates, 0 deletes** with that policy unchanged.
   The checker verifies the exact policy name, target disk/project/zone, daily
   schedule and retention in both cases.
   Paste its real output, exact main SHA, run id and sealed plan checksum into
   the PR. A local mocked-provider plan is not this evidence. Reject any VM
   replacement, disk recreation, or unrelated mutation. Apply only the reviewed
   binary plan through protected `infra-apply`, reusing the exact target string.
3. Provision recovery custody in the existing **`ditto-subnet`** project using
   [`subnet-recovery-project.md`](subnet-recovery-project.md). Its separate
   owner bootstrap links billing/enables APIs/delegates metadata administration;
   the protected `gcp-subnet-recovery` root creates six empty containers and the
   isolated restore identity. The old `gcp-platform` root owns only snapshot
   metadata IAM and two now-obsolete narrow state grants from the initial
   bootstrap. Recovery CI, federation, state and plan storage now have dedicated
   resources in `ditto-subnet`; their migration removes the shared CI grants
   that were reachable through inherited backend impersonation. Each plan has its own
   review/approval; never fold these into the two-resource snapshot plan.
   Verify the live app uses the dedicated Platform API identity. The DB VM has no attached
   service account (read-only inspection, 2026-10-04). The role fetches its three
   secrets on the authorized Ansible controller and copies root-owned files;
   the VM needs no cloud identity. Do not attach an identity to the DB VM (that
   could restart PostgreSQL). Do not create IAM or secrets out of band to bypass this
   reviewed Terraform phase.
4. Before storing the age identity, inspect **effective** Secret Manager IAM,
   including project/folder/org grants. The DB VM must have no path to the
   identity; neither Platform nor deploy/probe/worker identities may read it.
   Terraform creates containers only, never secret versions. Do not store any
   backup credential or age identity in `ditto-app-dev`: an audit on 2026-10-04
   found unconditional
   `roles/secretmanager.secretAccessor` grants to the default compute,
   Cloud Build, backend, GitHub Action and staging backend service accounts.
   These grants are not copied into `ditto-subnet`. The new project's initial
   project policy contained only Peyton's Owner grant on 2026-10-05, with no
   folder and no service-account members in the inspected organization policy.
   Recheck after apply, including indirect impersonation. CI apply has IAM
   administration and could change secret policies, so protected human review
   remains a custody boundary even without direct payload-read permissions.
   Keep the private-key secret empty until this effective-access review passes.
5. In Chrome Hippius Console, verify `ditto-platform-pg-backups` stays private.
   Create distinct single-bucket writer and reader sub-tokens, using the
   owner-approved lifetime (initially 30 days). No all-bucket grant and no reuse
   of avatars, traces or Coding credentials. The writer needs write/delete;
   the reader must not write/delete. Capture one-time pairs directly into these
   Secret Manager versions **in `ditto-subnet`** without printing,
   screenshotting, returning or logging their bytes:

   | Credential | Secret |
   | --- | --- |
   | Writer access ID | platform-pg-backup-hippius-access-key-id |
   | Writer secret | platform-pg-backup-hippius-secret-access-key |
   | Reader access ID | platform-pg-backup-reader-access-key-id |
   | Reader secret | platform-pg-backup-reader-secret-access-key |

   Use a protected file/clipboard consumer and `gcloud secrets versions add
   --project=ditto-subnet --data-file=<protected-file>`, never command arguments
   containing a payload.
   Delete protected staging copies and clear the clipboard after capture.
   Verify anonymous GET/HEAD/LIST refusal, exact-bucket writer operations,
   reader write/delete refusal and cross-bucket refusal with synthetic encrypted
   objects before binding either token to production. Fail closed on a scope
   mismatch; do not broaden the token to make a probe pass.
6. The owner generates an age identity **offline**. Store the public recipient
   in `ditto-subnet/platform-pg-backup-age-recipient` and the private identity in
   `ditto-subnet/platform-pg-backup-age-identity`. Keep an independently
   protected human recovery copy. The DB VM receives only the public recipient. Restrict private
   identity reads to humans and the dedicated restore service account after
   checking inherited IAM. No private key in Terraform state, host config or PR.
7. Set protected prod environment variables from Terraform outputs:
   `GCP_PG_RESTORE_SERVICE_ACCOUNT` and
   `GCP_PG_RESTORE_WORKLOAD_IDENTITY_PROVIDER`. The isolated WIF provider admits
   only this main workflow, on schedule or dispatch, in prod. It has no SSH,
   production DB, provider writer, state bucket, or project-wide secret access.
   Install and read back `infra/github/platform-pg-backup-ruleset.json` with
   separately authorized repository-admin access before adding the private
   identity. It protects the workflow, every imported restore file, dependency
   lock and custody configuration with independent `admin` team review.
8. With separate host-convergence authorization, run the DB playbook with
   `postgres_backup_enabled=true`. Persist that explicit intent in the reviewed
   host/group configuration after qualification. Do not rely on a one-time flag
   as a permanent source of truth.
   Run from an authorized controller whose `gcloud` identity may read only the
   needed writer pair and public recipient for this convergence. Secret fetches
   use `delegate_to: localhost`, `become: false` and `no_log: true`; do not use
   `--diff`, verbose secret debugging, fact caching or a delegated master token.
   The role installs exact Debian 13 amd64 pins:
   [age 1.2.1-1+b5](https://packages.debian.org/trixie/age),
   [awscli 2.23.6-1](https://packages.debian.org/trixie/awscli), and
   [python3-boto3 1.37.9-1](https://packages.debian.org/trixie/python3-boto3), plus
   [python3-requests 2.32.3+dfsg-5+deb13u1](https://packages.debian.org/trixie/python3-requests).
   Refresh pins through review if Debian archives change; do not silently float.
9. Enable the Platform reader with
   `platform_database_backup_reader_enabled=true` in its separately authorized
   app convergence. Platform receives only the reader pair, never the writer or
   private age identity. Verify served API/Backroom SHA and authenticated
   `get_database_backup_status`; `disabled` or `unavailable` is not freshness.
10. On the DB VM, assert the three files in `/etc/ditto-pg-backup` are root-owned,
    single-link mode 0600; directory mode 0700; no age identity exists there.
    Assert `systemctl is-enabled ditto-postgres-backup.timer` and inspect
    `systemctl list-timers ditto-postgres-backup.timer`. Manually start only
    `ditto-postgres-backup.service` (never Postgres). Inspect its exit status,
    journal, remote encrypted objects and `last_success` marker.
11. Dispatch the real restore drill against the actual backup; inspect the run's
    exact SHA and result. Record two consecutive **unattended** 06:30 UTC timer
    successes, then the first 06:00 UTC GCE snapshot within 24 hours of apply.
    A successful manual run is not the two-night acceptance criterion.

## Backup contract and monitoring

Each run holds one read-only exported PostgreSQL snapshot while collecting the
migration marker and counts for `agents`, `screening_attempts`, `scores` and
running `pg_dump --snapshot`. The source counts therefore describe the archive,
not a moving live database. Roles/password hashes from `pg_dumpall --globals-only`
are streamed through age separately. No plaintext archive is staged or uploaded.

Staging is a private directory on `/var/tmp`, never the VM's RAM-backed `/tmp`.
The guard requires database size plus max(10 GiB, 20% of filesystem capacity).
The service uses flock, Nice=10 and idle I/O scheduling. Its failure unit emits
the fixed error marker `ditto-pg-backup: FAILED`. Catching SIGTERM unwinds staging
cleanup; a power loss/SIGKILL cannot run a trap, so review orphan encrypted local
directories after abnormal shutdown rather than claiming cleanup happened.

Multipart presigned requests avoid the existing provider direct-SDK signature
problem. Dumps and encrypted globals are uploaded and HEAD-size-verified before
the manifest commit marker. Retention runs only after those commits succeed:
30 newest complete daily groups and the latest first-day dump in each of the
12 newest months. Only exact date/timestamp/type-matching keys in daily/monthly
are eligible for deletion. Unknown keys and incomplete groups are untouched.

A successful run atomically writes `/var/lib/ditto-pg-backup/last_success`
(UTC epoch and manifest SHA-256). Backroom reports newest object metadata and the
validated remote manifest commit, hours since its completion, freshness based
on the dump's start (stale after 36 h), and independently observed GCE snapshot
metadata. Remote commit freshness does not prove that a later retention operation
or the local service exit succeeded; inspect the local marker/journal for that.
The read reports missing/unavailable explicitly. It neither returns database
contents nor grants write authority. The weekly Sunday 08:17 UTC restore drill
fails on stale backups, mismatched encrypted SHA-256/size, wrong PostgreSQL major,
failed decryption/restore, different migration marker, or empty/core counts
outside 5%. It also restores the globals in the isolated database. The restore drill
and metadata reader allow at most five minutes of future clock skew; the
36-hour recovery-point age limit remains unchanged. The container
has no network or published ports; the runner shreds the key and DB files in an
always-run cleanup step. No decrypted artifact is uploaded.

## Restore from a GCE snapshot

This is an authorized recovery procedure, not part of online backup convergence.

1. Read and record the newest READY snapshot's exact source disk and timestamp.
   Prefer recovery into a new isolated VM first; keep the original disk intact.
2. Create a new disk from that exact snapshot in the intended zone. Attach it
   to the recovery VM, inspect filesystems, then boot/recover PostgreSQL and
   verify schema, roles and core counts. Never format the restored disk.
3. For a production swap, schedule a separately approved outage: stop the
   database/instance, detach the old boot disk without deleting it, attach the
   restored boot disk, and boot. Verify PostgreSQL recovery and both Platform
   environments before traffic restoration. Record Terraform drift and reconcile
   it without replacing the recovered VM/disk.
4. Roll back by stopping the replacement, reattaching the retained original
   boot disk, and booting it. Keep snapshots and original disk until acceptance.

## Restore from Hippius

Use the exact reviewed restore-drill script as the rehearsal. A manifest is the
commit marker; never select a dump without its matching encrypted globals and
manifest. Check backup age, both encrypted SHA-256 values and sizes before
decrypting. Use the matching PostgreSQL major plus pgvector/extensions.

For an authorized human recovery, download only encrypted objects into a private
mode-0700 directory, consume the separately protected identity with
`age -d -i <identity-file> <encrypted-file>`, and pipe directly into
`pg_restore --exit-on-error --no-owner --no-privileges` against an isolated
target. Restore globals into that isolated target first (review collisions with
the target's initial superuser). Verify the Alembic row and all three counts,
then approve cutover separately. Never restore directly over production as a
test. Shred any plaintext staging copies; remove the disposable DB and identity
copies on success and failure. Preserve encrypted evidence for recovery.

## Rotation and rollback

Rotate Hippius before expiry (alert at seven days remaining). Create a same-scope
replacement, capture new Secret Manager versions, reconverge the protected files
without restarting PostgreSQL, prove writer/reader/anonymous/cross-bucket limits,
run a backup and real restore drill, then revoke the old token and disable its
superseded secret versions. Keep the old pair enabled until qualification.

For age rotation, generate offline, retain old identities for as long as any
retained backup needs them, and add the new identity to the restore identity
file before switching the DB's public recipient. Test both an old and a new
backup with the combined restore identity file. Delete no old key until its
last encrypted backup has aged out and human recovery custody is confirmed.

To pause uploads after authorization, stop/disable only
`ditto-postgres-backup.timer`; do not stop PostgreSQL or delete retained objects.
Keep snapshots active. Re-enable/restart the timer to resume. Removing the
snapshot attachment/policy or revoking IAM is a separate reviewed Terraform
rollback; `KEEP_AUTO_SNAPSHOTS` preserves existing recovery snapshots.

## Local validation

```sh
terraform -chdir=infra/terraform/stacks/gcp-platform init -backend=false -input=false -lockfile=readonly
terraform -chdir=infra/terraform/stacks/gcp-platform validate
terraform -chdir=infra/tests/compute-snapshot init -backend=false -input=false -lockfile=readonly
terraform -chdir=infra/tests/compute-snapshot test
python3 infra/ansible/tests/test_postgres_backup.py
python3 infra/scripts/test-platform-pg-backup-workflow.py
(cd infra/ansible && uvx --from ansible-core==2.21.2 ansible-playbook --check -i localhost, tests/postgres-backup.yml)
(cd apps/platform && make lint lint-copy typecheck test)
(cd apps/backroom && scripts/platform-contract/generate.sh && pnpm check && pnpm test && pnpm build)
```

Render/check-mode assertions prove configured modes/enabling, not actual host
files or timer execution. Mocked providers and fixtures do not prove the live
Terraform plan, private bucket permissions, encrypted production upload,
restore, or two-night unattended run.
