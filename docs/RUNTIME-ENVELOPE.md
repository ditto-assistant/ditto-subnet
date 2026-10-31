# SN118 harness runtime envelope

Every scored run puts the miner harness inside the same envelope. Screening
probes it under that envelope, and `ditto practice` reports against it. A failure
is charged to the agent only when the harness broke a limit published here.

The machine-readable source is
[`packages/ditto-screening-protocol/ditto_screening_protocol/data/runtime_envelope.json`](../packages/ditto-screening-protocol/ditto_screening_protocol/data/runtime_envelope.json).
These consumers read it:

| Consumer | Reads |
|---|---|
| Scorer sandbox (`services/dittobench-api/internal/sandbox`, byte-identical embedded copy with a drift test) | CPUs, PIDs, tmpfs, start and health deadlines, OOM thresholds |
| Scorer runner | The test pins `/seed` and per-case deadlines to the envelope |
| Validator worker | Run wall clock and the host memory reserve |
| Screener (`workers/screener`) | Memory fallback, CPUs, PIDs, image and source caps |
| `ditto practice` (`miners/dittobench-starter-kit/scripts/local-rehearsal.py`) | Memory cap, CPUs, case concurrency |

Two values are live Platform settings, not release constants:

- the harness memory cap (Backroom inference concurrency settings,
  `benchmark_runtime.sandbox_memory_limit_mib`);
- the case concurrency (`benchmark_runtime.case_concurrency`).

Platform stamps both on every scoring lease. It also stamps the memory cap on
every screening claim, so the screener probes at the same cap as the scored
run. The JSON carries the published value, and consumers use it only when the
stamp is absent.

## Limits

| Dimension | Limit | Notes |
|---|---|---|
| Memory | **8192 MiB** (live) | `--memory`, with `--memory-swap` set equal, so no swap. Files in `/tmp` are tmpfs pages and count against this cap |
| CPU | 2 | `--cpus 2` |
| PIDs | 512 | Fork-bomb bound |
| `/tmp` | 512 MiB tmpfs, `noexec` | The only writable path. The root filesystem is read-only |
| Open files | 1024 | `nofile` |
| Container start | 120 s | Image unpack and `docker run` |
| Healthy | 180 s | `/health` must answer |
| `/seed` | 900 s per call | `seed_deadline_exceeded` |
| Per case (`/run`) | 300 s | A timeout scores that case 0 and the run continues |
| Per run | 9,900 s wall clock | Bounded also by the 180-minute lease, less a 120 s report margin |
| Concurrency | **8** (live) overlapping `/run` calls | The harness must be safe and inside the memory cap with 8 cases in flight |
| Screened image | 8 GiB | Source upload: 64 MiB unpacked |
| Chat requests | **16,384** per ticket (live) | `inference_allowance_exhausted` |
| Chat tokens | **75,000,000** per ticket (live) | Settled usage only |
| Chat concurrency / rate | 32 in flight, 1,920 per minute per ticket (live) | |
| Per request | `max_tokens` clamped to 32,768; 4 MiB body; 90 s upstream | |

The scorer applies the envelope's 64 MiB peak tolerance and 1024 MiB host
floor when it judges an OOM kill. Validators hold back a 1024 MiB host
reserve (`VALIDATOR_HOST_MEMORY_RESERVE_MIB`) from harness caps.

## Who pays for a resource failure

The scorer decides from physical evidence alone, before it removes the
container. While the container runs, it samples the cgroup and host memory
every 2 s, and every 0.5 s once usage is within 15% of the cap. The last
samples taken before the kernel removes a killed cgroup are still available.

| Code | Charged to | Rule |
|---|---|---|
| `harness_oom` | **Agent** (attempt consumed, 6 h cooldown) | Three conditions must all hold. (1) The kernel recorded an OOM kill: `OOMKilled`, or `memory.events` `oom_kill > 0`. (2) The harness reached its own cap: `memory.events` `oom > 0`, or `memory.peak` ≥ cap − 64 MiB. (3) The lowest host `MemAvailable` in the samples before the kill stayed above 1024 MiB. Sent as terminal `sandbox_failure`, so the wire kind matches the charge |
| `host_memory_pressure` | Validator (no fault; slot blocked 10 min) | An OOM kill where condition 2 or 3 fails, or the evidence is missing |
| `harness_killed_137` | Validator on the first validator. Agent if a second validator reports the same | Exit 137 (SIGKILL) with no OOM flag. Nothing shows who sent the signal. It never overrides a verdict the scored path already reached |
| `sandbox_tmpfs_exhausted` | Validator (no fault) | `/tmp` reached its declared size |
| `inference_allowance_exhausted` | Agent | The harness spent its request or token wall |
| `seed_deadline_exceeded` | Not attributed; retried inside the ordinary budget | A loaded host and a slow harness look the same |

Platform charges a `sandbox_oom` hand-back only when its detail is
`harness_oom`. A validator that predates this rule cannot prove the
conditions, so its plain `sandbox_oom` is parked as `infrastructure`.

## Host-memory admission

A validator offers at most this many benchmark slots:

```
(host RAM − VALIDATOR_HOST_MEMORY_RESERVE_MIB) // live harness memory cap
```

A host with at least the cap plus 1024 MiB (`single_slot_headroom_mib`) always
keeps one slot, whatever the reserve. That slot is safe because a kill there
that the harness did not cause is classified `host_memory_pressure`, which is
no-fault. Platform also never lets the generator-round reservation take a
host's only healthy slot: the reservation is sized against
`min(allowed, healthy) − 1`, so a one-slot host serves ordinary work and keeps
the canonical quorum at three distinct validators.

With the 8192 MiB cap and the 1024 MiB reserve:

| Host RAM | Slots |
|---|---|
| 8 GiB | 0 |
| 16 GiB | 1 |
| 32 GiB | 3 |
| 64 GiB | 7 |

The worker logs the narrowing. The heartbeat's `healthy_slots`, and with it
`serviceable_slots` in Backroom `get_validator_capacity`, reflect it.

## Practising against the envelope

`ditto practice` runs the harness at the live case concurrency (8). It pins the
harness to 2 CPUs where the OS allows that (Linux). It samples the harness's
resident memory plus its store, then prints the peak as a share of the cap. A
peak at or above the cap fails the rehearsal as `harness_oom (local)`, and a
peak above 85% prints a warning.

A host process is not a cgroup, so treat the number as an estimate. Leave
headroom.
