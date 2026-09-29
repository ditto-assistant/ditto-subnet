---
name: ditto-subnet-release-ops
description: Design, implement, audit, or operate ditto-subnet semantic releases, affected-component CI, container builds, automatic application deployments, validator rollouts, screener autoscaling and trusted builds, Hetzner primary capacity with GCE overflow, GCP IAM/WIF, Cloudflare Workers, Terraform, Ansible, and rollback. Use for release or live-runtime work where exact SHA, credentials, provider safety, or activation boundaries matter.
---

# Ditto Subnet Release Ops

Preserve one exact monorepo release identity while keeping application deployment and infrastructure authority separate.

## Orient

```bash
python3 .agents/skills/ditto-subnet-context/scripts/lookup-context.py \
  --max-topics 4 "$ARGUMENTS"
```

Pass the user's task text verbatim. If it is empty, omit the query and begin
from the monorepo overview rather than injecting every release owner.

Read [`references/release-ops-index.md`](references/release-ops-index.md), then inspect the exact workflow, Terraform stack, or live runtime involved.

## Work from evidence

1. Resolve the requested PR/commit/release and current `main` independently.
2. Inspect the affected-component plan before predicting builds or deploys.
3. Validate workflow permissions, immutable action refs, environment protection, WIF subject, and rollback path.
4. For a live claim, verify deployed SHA/image digest, service health, and client-visible behavior.
5. State what is implemented, merged, released, deployed, applied, and activated separately.

## Authority and secrets

- Automatic application deploys may follow a semantic release from `main`.
- Terraform always uses reviewed plan and protected apply; application workflows do not apply infrastructure.
- Never read or print provider secrets. Use Secret Manager indirection and tests that consume values without returning them.
- Never place cloud, GitHub, Platform, or provider credentials in untrusted build/runtime environments.
- Do not create service accounts or IAM bindings out of band merely to bypass an unapplied Terraform bootstrap.

## Platform app VM disk

`deploy_platform` `No space left on device` during `git fetch` is a full boot
disk on `ditto-platform-prod`, not a bad release SHA. Inspect, reclaim caches
only with confirmation, then grow via Terraform. Playbook:
[`references/platform-host-disk.md`](references/platform-host-disk.md).

```bash
.agents/skills/gcloud-ditto-readonly/scripts/inspect_platform_disk.sh
.agents/skills/ditto-subnet-release-ops/scripts/reclaim_platform_disk_caches.sh \
  "RECLAIM PLATFORM DISK CACHES"
```

Do not delete `/opt/ditto-platform-relay/traces`. A 30G boot disk is too small;
`app_boot_disk_gb` is 100. Provider 6.50 treats boot-disk size as ForceNew —
grow with `gcloud compute disks resize` then `growpart`/`resize2fs` **before**
Terraform. Protected apply must not replace the VMs.

## Capacity invariants

The enrolled Hetzner worker is primary. GCE normally targets zero and supplies bounded backlog or outage capacity. The controller must be fenced and count pending workers; an independent GCP watchdog may add fallback capacity only when backlog exists, controller authority is missing, expired, or unready, and current Platform policy confirms positive primary admission. Fail closed when provider isolation cannot be proven.

GCE scale-in re-reads `/controller/nodes` after the fenced renew. Both zero and partial scale-in currently defer: the 180-second controller lease cannot fence new legacy claims for the full duration of a physical GCE deletion, even when all members are idle at the reread. Zero scale-in also requires a known zero legacy running-attempt count and every running managed-group member ready and idle; partial scale-in needs a per-instance claim fence. A failed read or unknown running-attempt count also defers (`GCE_SCALE_IN_DEFERRED`, not a provider failure). A deferral keeps publishing the lower desired target, so it does not reopen claims while the controller lease is fresh. Physical excess capacity remains until a durable claim fence is implemented or an operator safely drains it.

`SCREENING=0` on the primary is an operator closure and a global full stop (`HETZNER_PRIMARY_ADMISSION_CLOSED`): GCE does not overflow it and the watchdog does not activate fallback for it, whatever the backlog, `gce_overflow_enabled`, provider order, or the host's readiness and heartbeat. Reopening needs a deliberate `screening_concurrency >= 1` activation. A primary the controller cannot vouch for (failed node read, omitted row, missing admission setting) fails closed as `HETZNER_PRIMARY_UNKNOWN`; only a known-open, unready primary overflows. A stale routing revision that still names Targon first honors the same closed/unknown stop; its `RETIRED_PROVIDER_ROUTING` GCE fallback applies only to a known-open primary.

A failed Platform routing or node-inventory read holds the current GCE target for `--inventory-failure-hold-passes` passes (default 4) with `PLATFORM_INVENTORY_UNAVAILABLE`. A routing read failure keeps the controller's physical target unchanged until fresh policy returns, including after that hold expires. After the hold, the controller marks itself unready so the independent watchdog can serve an open primary's backlog under current Platform policy; closed or unknown admission remains stopped. The controller reuses the last good routing revision cached in its state file; only a controller without one publishes the synthetic, unclaimable revision 0 (`PROVIDER_ROUTING_UNAVAILABLE`). Each capacity event is sent once, on the fenced first renew of the pass.
