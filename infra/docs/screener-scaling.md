# Federated screener capacity

The normal production path is one fixed-cost 64 GB Hetzner node named
`subnet-screener-1`, with the existing GCE MIG retained at zero as outage and
backlog-overflow capacity. Platform owns admission and leases; Backroom owns
the revisioned provider and per-node concurrency settings. See
[`docs/hetzner-screener-fleet.md`](../../docs/hetzner-screener-fleet.md) for the
default-Debian install and Ansible runbook.

## Provider order and safety gate

For each reconciliation the
controller:

1. reads the current Platform demand and renews its fenced lease;
2. reads the audited Backroom provider revision;
3. treats the configured Hetzner node heartbeat as the primary availability
   signal;
4. keeps GCE at zero while the primary is ready and unclaimed backlog is at or
   below `max(min_backlog, screening_concurrency * backlog_multiplier)`;
5. adds only residual GCE capacity above that threshold, or full bounded GCE
   capacity when the primary is not ready;
6. scales GCE down only after GCE-owned leases finish.

Scale-in re-reads the node inventory after the fenced renew and immediately
before any GCE mutation, because a GCE worker may claim after the first read.
Scaling to zero resizes only when `legacy_gcp_running_attempts` is zero and every
running managed-group member has a fresh idle heartbeat. The renew has already
published target zero, which
blocks new legacy claims on a Hetzner-primary route when the snapshot is ready.
A GCP-first route or unready snapshot cannot establish that fence, so zero-target
scale-in defers there. Partial scale-in also defers:
an idle heartbeat cannot stop that instance from claiming before deletion, and
the shared legacy hotkey has no per-instance claim fence. The controller still
checks attribution and current managed-group members before reporting that
deferral. A deferral leaves the managed
group unchanged but keeps publishing the lower target, so it never reopens
claims that the renew withdrew. It records a `gce_scale_in_deferred` event with
`GCE_SCALE_IN_DEFERRED` and is not a provider failure; the next pass retries.

`SCREENING=0` (`screening_concurrency=0`) on the primary is an operator closure,
not an outage: it is a global full stop recorded as
`HETZNER_PRIMARY_ADMISSION_CLOSED`, and GCE does not overflow it regardless of
backlog, `gce_overflow_enabled`, or the host's readiness and heartbeat, so a
host health failure cannot reopen screening. Reopening needs a deliberate
`screening_concurrency >= 1` activation on the primary. Explicit GCP-first
provider routing is a separate operator decision, takes precedence, and is the
only outage failover for a closed primary. A primary the controller cannot vouch
for -- a failed node-inventory read, an omitted primary row, or a row without
its admission setting -- also fails closed (`HETZNER_PRIMARY_UNKNOWN`), so an
inventory outage cannot bypass an operator stop. Only a primary known to be
open but unready is a host failure that overflows to GCE. A stale routing
revision that still names the retired Targon provider first honors the same
closed and unknown stops; its GCE fallback (`RETIRED_PROVIDER_ROUTING`) applies
only to a primary known to be open.

A Platform deploy or transient 5xx on the routing or node-inventory read must
not flap the GCE MIG. The controller holds the current GCE target, in both
directions, for `--inventory-failure-hold-passes` consecutive failing passes
(default 4, about two minutes) and reports `PLATFORM_INVENTORY_UNAVAILABLE`. The
hold never adds capacity. A routing read failure reuses the last good revision
cached in the controller state file, so running GCE workers still match the
Platform claim check. Without a cached revision, the controller publishes an
unready revision 0 (`PROVIDER_ROUTING_UNAVAILABLE`) and preserves the current
MIG size until an authoritative routing read succeeds; it neither adds
capacity nor deletes workers on an unknown route. A cached revision also
preserves the current MIG size for the full routing outage, including after
the transient hold expires. Node-inventory failures still follow the normal
rules after the hold: an unknown primary fails closed. The first successfully
fenced failing pass records a
`platform_inventory_unavailable` event and the expiry records
`platform_inventory_hold_expired`. A failed pre-event read or first fenced
renew leaves the transition pending for the next pass.

Production uses `['hetzner', 'gcp']` for build, runtime smoke, and source review.
The second entry means that separate GCE workers may claim still-unclaimed
submissions when the capacity policy activates them. It does not mean a failed
Hetzner lane is retried on GCE.

- **Hetzner primary** (`['hetzner', 'gcp']`): one full worker process starts the
  canary under one enrolled node identity. After the canary, Platform initially
  admits two build/smoke KVM slots and two review slots on the 64 GB host, with
  a matching two local worker processes.
- **GCE-only** (`['gcp']`): an audited emergency posture in which GCE workers
  run the whole build, smoke, and review pipeline locally.


Within one submission, static execution-safety preflight runs first, followed
by build, runtime smoke, general source review, and verdict. General review is
never leased before the exact attempt has both a successful build and smoke.
Different submissions move through those stages concurrently.

A revisioned write requires compare-and-swap, an audit reason, and an exact
confirmation string covering all three lists and the overflow policy. Node
screening, shared sandbox, build, runtime, and review ceilings have a separate
append-only control. New nodes default to zero capacity.

## Capacity event retention

`screener_capacity_events` is an append-only audit table, so Platform prunes it
instead of letting reconciliation and provider lifecycle events accumulate.

- **Window:** 30 days by default, set with
  `SCREENER_CAPACITY_EVENT_RETENTION_DAYS`. `0` keeps every event and disables
  pruning; any other value must be at least 7, so a typo cannot erase incident
  history. The window in effect is returned as `event_retention_days` by
  `GET /api/v1/admin/screener-capacity` (and therefore `get_screener_capacity`),
  with `null` meaning pruning is off.
- **Mechanism:** a Platform-role janitor sweeps hourly. Each sweep is **one
  transaction** that takes a transaction-scoped advisory lock, lists the
  environments holding events once, then runs up to 20 batches inside it, each
  deleting at most 1,000 expired events **per environment** against the
  `(environment, created_at)` index. Because the lock lives as long as that one
  transaction, another Platform replica cannot start a sweep of its own between
  batches, so replicas share a single deletion budget per sweep. Every
  environment has its own budget, so a backlog in one cannot starve another, and
  an environment stops being visited once a batch comes back short. Readers keep
  seeing recent history while it runs (MVCC). An event exactly at the cutoff is
  kept. A failure rolls back the whole sweep, so nothing is half-deleted, and the
  next sweep retries it.
- **Signals:** `ditto_screener_capacity_event_janitor_runs_total{outcome}`
  (`deleted`, `busy`, `error`), `ditto_screener_capacity_event_janitor_deleted_total`
  and `ditto_screener_capacity_event_janitor_duration_seconds`. The deleted
  counter moves only after the sweep commits, so it never counts rows that a
  failed sweep rolled back; a `busy` outcome means another replica owns the
  sweep. Alert on a sustained `error` rate; failures are also logged as
  `screener capacity event janitor sweep failed`.

## Stand-up order

No repository merge deploys or mutates production. Keep the existing GCE MIG
available at zero, merge and deploy Platform/Backroom/controller support, then
follow the dedicated-host runbook. Enrollment alone grants zero capacity.

First rehearse the public host role on Terraform's optional
`subnet-screener-dev-1` (`n2-standard-16`, nested KVM, no runtime secrets), then
return `enable_screener_fleet_dev_host` to false and prove the disposable VM is
absent. The dedicated-host runbook contains the exact protected workflow and
Ansible commands.

After `subnet-screener-1` is converged, use Backroom to:

1. verify its node ID, Hetzner resource ID, exact release SHA, image digest,
   heartbeat, and zero effective limits;
2. keep existing provider routes and every node limit at zero while host-local
   cold build, smoke, failed-build/no-review, and failed-smoke/no-review probes
   pass (shadow mode);
3. append the one-lane canary setting
   `SCREENING=1 SANDBOX=1 BUILD=1 RUNTIME=1 SOURCE_REVIEW=1`;
4. set all three provider lists to `['hetzner', 'gcp']` and enable overflow for
   `subnet-screener-1` at multiplier 3, minimum backlog 12, maximum 6;
5. prove one production build -> smoke -> source-review sequence and one
   build failure that never obtains a review lease;
6. raise the 64 GB node to
   `SCREENING=2 SANDBOX=2 BUILD=2 RUNTIME=2 SOURCE_REVIEW=2`, set the private
   inventory to two worker processes, and prove two simultaneous cold
   build/smoke lanes without memory or disk pressure; raise to three only after
   measured sandbox-plus-review memory leaves safe host margin;
7. exercise one controlled stale-heartbeat event and one above-threshold queue,
   proving GCE claims new work, preserves active leases, and returns to zero;
8. drain retired nested-Docker Targon worker nodes. Do not re-enable them.

The exact Debian, inventory, vault, Ansible, activation, verification, and drain
commands live in [`docs/hetzner-screener-fleet.md`](../../docs/hetzner-screener-fleet.md).

## BuildKit cache cleanup on dedicated screener hosts

The `screener_worker` role installs `ditto-screener-cache-gc.timer` and its
oneshot service. The active `hetzner_screener_fleet` role now includes the
same cache-only tasks for its persistent full screening workers, using their
rootless executor socket and `ditto-screener` service identity. The disposable
KVM build guests keep isolated per-job caches; this timer does not enter those
guests or touch the host's rootful Docker daemon. It backs up the rootless
executor's own builder GC
(`workers/screener/deploy/rootless-daemon.json`, `defaultKeepStorage` 40GB) by
running `docker builder prune --force --keep-storage <budget> [--filter
until=<age>]` against the rootless executor socket only. BuildKit skips records
used by an in-flight build, so a timer firing mid-build does not interrupt it.
The job never uses `--all`, `docker system prune`, or image/volume/container
pruning.

| Variable | Default | Meaning |
|---|---|---|
| `screener_cache_gc_enabled` | `true` | `false` stops and disables the timer |
| `screener_cache_gc_on_calendar` | `hourly` | systemd cadence (plus `screener_cache_gc_randomized_delay: 5min`) |
| `screener_cache_gc_keep_storage` | `40GB` | retained cache budget; keep equal to the daemon budget |
| `screener_cache_gc_min_age` | `1h` | records younger than this are never pruned; empty (`""`) means no age floor, by design |
| `screener_cache_gc_df_path` | executor home | filesystem logged with `df -h`; the executor home (`/var/lib/ditto-screener-docker`) holds the daemon's data root, so it is the cache's mount. `df` of the home itself works for the unit user despite mode 0700; a path beneath it would not |
| `screener_cache_gc_dry_run` | `false` | log policy and disk state, skip the prune |

This timer is the second pass, not the only one: `update-screener.sh`
(`maintain_cache`, ~line 313) already runs `docker builder prune --keep-storage`
against the same rootless executor, and the daemon's builder GC is the
continuous limit. The keep-storage budget therefore lives in THREE places
(`rootless-daemon.json`, the updater's `SCREENER_CACHE_KEEP_STORAGE`, and
`screener_cache_gc_keep_storage`) and must stay equal. The age floor is small
on purpose: `update-screener.sh` (~line 324) uses no age filter because a floor
exempts burst-created cache, which is exactly what overruns the budget; 1h only
protects records from the current burst's in-flight builds, and `""` removes it.
The timer's unit orders `After=` the executor but never `Wants=` it, so it can
never start the daemon; an unreachable executor fails the run visibly.

The 40GB budget deliberately preserves warm layers so requeued and resubmitted
builds stay fast (cold builds exceed nine minutes, see #429); lowering it trades
throughput for headroom. The host disk must leave room above the budget. Each
run logs `docker system df` and `df -h` before and after to journald under
`ditto-screener-cache-gc`; a failed prune or unreachable executor exits nonzero,
leaving the unit in `failed` state.

On Hetzner, `screener_fleet_cache_gc_*` controls this same policy. It is enabled
only when `screener_fleet_runtime_enabled` is true, so a disposable rehearsal
host never starts the timer. The persistent full workers use a 100GB budget in
both the rootless daemon and the timer. A changed daemon budget restarts the
rootless Docker service during converge, so drain builds first. A warm-cache
speedup must be measured from actual full-worker build durations afterward.

Inspect and operate manually (as an operator, against the rootless socket):

```bash
systemctl list-timers ditto-screener-cache-gc.timer
systemctl status ditto-screener-cache-gc.service
journalctl -u ditto-screener-cache-gc.service --since -1d
export DOCKER_HOST=unix:///run/ditto-screener-docker/docker.sock
docker system df; docker system df -v | sed -n '/Build cache/,$p'; df -h /
sudo systemctl start ditto-screener-cache-gc.service   # run the bounded policy now
docker builder prune --keep-storage 40GB --filter until=1h  # manual, same policy (asks for confirmation)
```

Emergency reclaim (interrupts warm-cache performance, never while a build is
active: check `docker ps` first): `docker builder prune --all --force`.

Coverage and UNVERIFIED items. CI runs the script's unit tests (fake `docker`),
renders the units, and runs `tasks/cache_gc.yml` in `--check` mode for the
enabled and disabled policy. That is not an idempotency or systemd test; a real
idempotent second apply cannot be exercised in CI. Nothing here is proven on a
real host. Before #541 can close, a dev host must confirm: `docker builder prune
--keep-storage ... --filter until=...` behavior on the executor's Docker
version; `systemd-analyze verify` on the units; the unit running as the deploy
user with `SupplementaryGroups` under the hardened sandbox; that editing the
cadence variables re-arms the running timer (a handler restarts it); and the
before/after `docker system df`, `df -h`, timer status, and heartbeat health.

Rollout: converge the host with the existing screener Ansible path (protected
workflow, `--check --diff` first). Production verification records, before and
after: `docker system df`, `df -h`, `systemctl status
ditto-screener-cache-gc.timer`, and the screener heartbeat health in Backroom.

## Rollback

Apply all three lanes as GCE-only to restore the GCE screening path after an
audited Backroom revision. Stop the controller unit only after explicitly
setting the GCE MIG to a safe nonzero target or confirming the queue is empty. The
`ONLY_SCALE_OUT` watchdog is intentionally incapable of deleting workers during
a controller outage.

Drain `subnet-screener-1` before host maintenance. Removing the node, GCE
resources, or any deletion protection remains a separate reviewed operator
action.
