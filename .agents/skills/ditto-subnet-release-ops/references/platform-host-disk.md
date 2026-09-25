# Platform app VM disk

`ditto-platform-prod` and `ditto-platform-dev` boot from a single pd-balanced
disk sized by `app_boot_disk_gb` in `infra/terraform/stacks/gcp-platform`.
`/tmp` is tmpfs: emptying it does not free `/`.

## Symptom

`deploy_platform / deploy` fails during `git fetch` / `git unpack-objects`:

```
fatal: unable to write loose object file: No space left on device
```

That is host disk, not a code defect in the release SHA. Scoring can stay up
while deploys cannot land.

## Inspect (read-only)

```bash
.agents/skills/gcloud-ditto-readonly/scripts/inspect_platform_disk.sh
.agents/skills/gcloud-ditto-readonly/scripts/inspect_platform_disk.sh ditto-platform-dev
```

## Reclaim caches (mutating, confirmation required)

```bash
.agents/skills/ditto-subnet-release-ops/scripts/reclaim_platform_disk_caches.sh \
  "RECLAIM PLATFORM DISK CACHES"
```

Removes apt archives, archived journals down to 80M, `/home/deploy/.cache/uv`,
npm cacache, and `git gc` on `/opt/ditto-subnet`. Do not inherit the SSH user's
`uv.toml` when running as `deploy`.

## Claimable after caches (separate authorization)

| Bucket | Typical | Safe? |
|---|---|---|
| Live pm2 logs `/opt/ditto-subnet/apps/platform/logs` | multi-GB, no `max_size` | `pm2 flush` only with operator OK; current process holds the fd |
| Pre-cutover `/opt/ditto-platform/logs` | stale if pm2 cwd is the monorepo | leftover logs, not live |
| Relay spool `/opt/ditto-platform-relay/traces/ready` | multi-GB | **not** cache; wait until shipped to Hippius |
| Docker | one live image | `docker system df` reclaimable 0% while the sidecar runs |
| Live `.venv` / checkout | needed | no |

Never delete Postgres data, `.env`, `.env.deploy`, trace `open/` files, or the
running git working tree.

## Rightsize

30G filled production: live working set was already ~26G after cache reclaim,
uv cache returns ~3G on the next `uv sync`, traces and unbounded pm2 logs keep
growing. Target is **100G**, pinned in `prod.auto.tfvars` and the variable
default. Screener prod is already 160G for the same class of growth.

On google provider 6.50 (`~> 6.0`),
`boot_disk.initialize_params.size` is **ForceNew**. A Terraform size change
plans replacement of both app VMs; `lifecycle.prevent_destroy` and
`deletion_protection` fail that apply. Grow **live first**, then let Terraform
refresh to 100G as a no-op pin for new VMs.

```bash
gcloud compute disks resize ditto-platform-prod --size=100GB \
  --zone=us-central1-a --project=ditto-app-dev
gcloud compute disks resize ditto-platform-dev --size=100GB \
  --zone=us-central1-a --project=ditto-app-dev
```

Then IAP to each host, confirm the layout, grow the partition and ext4 (do not
assume `/dev/sda1` without `lsblk`):

```bash
lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT
findmnt -n -o SOURCE,FSTYPE /
# Debian needs cloud-guest-utils for growpart
sudo growpart /dev/sda 1
sudo resize2fs /dev/sda1
df -h /
```

Protected plan/apply after the live size already matches. If a plan still
shows instance replacement, abort.

## Dedicated Platform PostgreSQL root disk

The September 2026 incident filled `ditto-pg-platform`'s 30 GB root filesystem.
The live PostgreSQL 17 main cluster was at `/var/lib/postgresql/17/main` on
`/dev/sda1`, not on the separately attached data volume. It completed WAL redo,
then failed to write init files and `current_logfiles.tmp` with ENOSPC.

Run `inspect_platform_postgres.sh` first. The manual
`Recover Platform PostgreSQL disk capacity` workflow is restricted to main and
requires `GROW PLATFORM POSTGRES BOOT DISK TO 100GB`. Its infra-apply job validates
the exact disk name, attachment, and current size (30 or already 100 GB), then
grows that disk in place. Its dependent prod job verifies the guest's device,
filesystem, cloud capacity, and cluster directory before expanding the partition
and ext4. It restarts the exact PostgreSQL cluster only if readiness remains
unavailable after restoring disk headroom. No log, database, or WAL file is deleted.

`pg_boot_disk_gb=100` pins this capacity in Terraform. Refresh and review the
protected Terraform plan after the live grow; never apply a plan that replaces
the database VM. This workflow does not migrate the cluster to the attached data
disk or change persistent IAM.

## Collector log retention (prevention, not recovery)

Growing the boot disk bought headroom; it did not stop the growth. PostgreSQL's
`log_rotation_age` / `log_rotation_size` only open a new file — the cluster never
deletes one. `roles/postgres` therefore installs a bounded reclaimer, the same
shape as `screener_cache_gc_*` on the screener hosts:

| Variable | Default | Meaning |
|---|---|---|
| `postgres_log_gc_enabled` | `true` | `false` stops and disables the timer |
| `postgres_log_gc_retention_days` | `7` | age bound; matches the incident recovery |
| `postgres_log_gc_max_total_mb` | `6144` | hard ceiling, oldest-first |
| `postgres_log_gc_on_calendar` | `hourly` | `ditto-postgres-log-gc.timer` cadence |
| `postgres_log_gc_dry_run` | `false` | `true` logs the full plan, deletes nothing |

`ditto-postgres-log-gc` runs as `postgres`, writes only the collector directory,
and never deletes the live file (`current_logfiles` plus the newest file by
mtime). At the measured 635 MB/day, 7 days is ~4.4 GB; the ceiling is the hard
bound a slow-query burst cannot outrun. `log_min_duration_statement` stays at
500 ms deliberately — that log is the only evidence for the slow statements in
#1745 that still need a fix.

Role tasks fail closed rather than silently reclaim nothing: the policy must be
1–30 days and 256–20480 MB, and `postgres_log_gc_dir` / `_glob` must describe the
same files as `postgres_tuning.log_directory` / `log_filename`.

Make it live (protected path, operator-run):

```bash
export DITTO_PG_PASSWORD=…   # only for a first provision; day two reuses the file
GCP_OSLOGIN_USER=… ansible-playbook -i infra/ansible/inventory/gcp.yml \
  infra/ansible/playbooks/gcp-platform-pg.yml --check --diff
# then the same command without --check
```

Verify on the host: `systemctl list-timers ditto-postgres-log-gc.timer`,
`systemd-analyze verify ditto-postgres-log-gc.service`,
`journalctl -u ditto-postgres-log-gc -n 50`, `du -sh /opt/ditto/logs/postgresql`.
First run on prod reclaims everything older than 7 days in one pass. To rehearse,
converge with `-e postgres_log_gc_dry_run=true` first and read the plan in the
journal.

## The attached 50 GB data disk: decision and runbook (NOT executed)

`ditto-pg-platform-data` (50 GB pd-balanced, `prevent_destroy`) has been attached
since the stack was written, with **no filesystem and no mount**. PGDATA, WAL,
and the collector logs all still live on the boot disk.

**Decision: do not migrate now.** With the boot disk at 100 GB and collector logs
bounded, the live working set (11 GB data + 2 GB WAL + ≤6 GB logs) is under 20%
of the boot disk. A cluster relocation is an offline, non-atomic change to the
one stateful component in SN118 whose loss is unrecoverable from the chain; the
capacity argument for doing it under time pressure is gone. Revisit when the
cluster approaches ~50 GB, when WAL and data need separate IOPS, or when a
boot-disk replacement is needed for another reason — the data disk surviving
instance replacement is its real value.

What is already representable, default off: `roles/postgres/tasks/data_disk.yml`
mounts the volume (only) behind two independent gates. Nothing runs until an
operator sets both:

```bash
# 1. Mount an already-formatted disk:
#    postgres_data_disk_enabled=true      (group_vars/role_platform_postgres.yml)
# 2. First mount of the still-empty disk additionally needs, once:
#    -e postgres_data_disk_allow_format=true
GCP_OSLOGIN_USER=… ansible-playbook -i infra/ansible/inventory/gcp.yml \
  infra/ansible/playbooks/gcp-platform-pg.yml --check --diff \
  -e postgres_data_disk_enabled=true -e postgres_data_disk_allow_format=true
```

It asserts `/dev/disk/by-id/google-ditto-pg-platform-data` exists, refuses a disk
carrying a foreign filesystem, never reformats one that already has ext4, mounts
at `/opt/ditto/pgdata` with `nofail`, and stops there. It does **not** move
PGDATA, WAL, or the logs, and flipping the flag back to `false` does not unmount
(skipped tasks, not a teardown) — unmount by hand, deliberately.

Migration runbook, if the decision above is ever reversed. Maintenance window,
Platform API returns 500s throughout, never run it ad hoc:

1. Announce the window. Stop the Platform API on both app VMs (`pm2 stop`), so
   the cluster is not being written during the copy.
2. `pg_dumpall` (or a GCE disk snapshot of the boot disk) to a location that is
   not the boot disk, and verify the artifact before touching anything.
3. `sudo systemctl stop postgresql` and confirm with `pg_lsclusters` that the
   cluster is down. A copy from a running cluster is silently corrupt.
4. Converge with the two gates above to format and mount the volume, or do it by
   hand: `mkfs.ext4 /dev/disk/by-id/google-ditto-pg-platform-data`,
   `mount /opt/ditto/pgdata`, fstab entry by `/dev/disk/by-id/...` with `nofail`
   (never `/dev/sdb` — device order is not stable).
5. `rsync -aHAX --numeric-ids /var/lib/postgresql/17/main/ /opt/ditto/pgdata/main/`
   then compare sizes and file counts. Keep the original directory; do not delete
   it in the same window.
6. Point the cluster at the new location — `data_directory` in
   `/etc/postgresql/17/main/postgresql.conf` — and ensure `postgres:postgres`
   ownership and mode `0700`. The Ansible role does not own `data_directory`
   today; adding it is part of this change, not a manual edit to be re-applied by
   hand afterwards.
7. Start PostgreSQL, then verify: `pg_isready`, `SHOW data_directory`,
   `SELECT pg_is_in_recovery()`, row counts on `agents` and
   `public_activity_scores`, and Platform `/health` plus the leaderboard.
8. Restart the Platform API. Only after a clean day, and a fresh backup, reclaim
   the old directory on the boot disk.

Risks to weigh before choosing that path: an ENOSPC or ownership mistake mid-copy
leaves a cluster that starts against a partial directory; a `/dev/sdX` fstab entry
plus `nofail` can silently boot with the disk absent and PGDATA pointing at an
empty mountpoint; the 50 GB volume becomes the new unmonitored bound (there is
still no >80% disk alert — the remaining #1745 follow-up); and the migration must
be represented in Ansible, or the next converge fights the hand-edited config.
