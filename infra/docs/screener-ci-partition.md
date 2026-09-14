# Screener partition and rootless Docker

The sibling CI guest is managed in `ditto-assistant/infra`. This repository owns
the primary screening runtime. The primary host budget is split between:

| Slice | Work | CPU ceiling | Memory ceiling |
| --- | --- | --- | --- |
| `dittoscreener.slice` | One build/smoke VM, one source review, worker and lane agent | 8 CPUs | 16 GiB |
| `user-1005.slice` | Rootless Docker owned by `ditto-builder` | 8 CPUs | 14 GiB |

Keep `user@1005.service` under its native
`/user.slice/user-1005.slice/user@1005.service` hierarchy. Moving that user manager
directly into `dittoscreener.slice` was followed by every fake-gateway container
failing with `unable to apply cgroup configuration: Interactive authentication
required`. Restoring the native hierarchy and restarting the drained user manager
made the same constrained container start successfully. `docker info` remained
successful throughout the outage and is insufficient as a readiness check.

Converge with the partition playbook and `screener_partition_activate=true`.
Activation serializes against the updater, drains workers before the lane agent,
refuses to stop rootless Docker if workload inventory fails or work remains, then
starts a constrained `/bin/true` container from the trusted preinstalled analyzer
image before restarting screening. No miner image is used for this check.

Require a real screening attempt to advance past gateway startup afterward.
Use Backroom's exact-attempt `retry_failed_screening_now` for affected parked
submissions after verifying artifact, score count and latest attempt. Preserve
policy, scores and failure history; infrastructure recovery does not authorize
an enforcement verdict.
