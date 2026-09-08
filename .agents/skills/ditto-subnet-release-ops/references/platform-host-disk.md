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
