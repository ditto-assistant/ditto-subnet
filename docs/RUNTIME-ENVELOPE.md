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
| Memory | **863 MiB** (published) | `--memory`, with `--memory-swap` set equal, so no swap. Files in `/tmp` are tmpfs pages and count against this cap: a full 512 MiB `/tmp` leaves 351 MiB for everything else. The cap in force is `sandbox_memory_limit_mib` on `GET /api/v1/public/validators` |
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

The published memory cap was lowered from 8192 MiB to 863 MiB (SN-488). It is
the highest cap at which the smallest validator host (7,934 MiB) still has the
memory for all 8 of its slots. Eight is the most any one validator runs, so
the fleet can run at most 8 times the number of validators. Memory is one limit; the
operator's per-validator slot cap (below) is another, and a validator runs the
smaller of the two. Keep harnesses lightweight: an out-of-memory kill at your
own cap is a failed run.

The operator switches the live setting separately, after a release reaches the
fleet; until then, leases are stamped with the previous 8192 MiB. Platform
stamps the live value on every lease, so a lease issued before a change keeps
the cap it was issued with.

## Who pays for a resource failure

The scorer decides from physical evidence alone, before it removes the
container. While the container runs, it samples the cgroup and host memory
every 2 s, and every 0.5 s once usage is within 15% of the cap. The last
samples taken before the kernel removes a killed cgroup are still available.

| Code | Charged to | Rule |
|---|---|---|
| `harness_oom` | **Agent**: a failed agent run (attempt consumed, 6 h cooldown), never retried as infrastructure | The kernel recorded an OOM kill (`OOMKilled`, or `memory.events` `oom_kill > 0`) and one of two proofs holds. **Proven:** `memory.events` counted an `oom`, which the kernel records only when the harness's own cgroup reached its own cap. How much memory the host had left does not matter. **Inferred:** `memory.peak` ≥ cap − 64 MiB, and the lowest host `MemAvailable` in the samples before the kill stayed above 1024 MiB. Sent as terminal `sandbox_failure`, so the wire kind matches the charge |
| `host_memory_pressure` | Validator (no fault; retried; slot blocked 10 min) | An OOM kill with neither proof: the harness's own cgroup never reached its cap, and its peak was below the cap or the host was short of memory (or unobserved) at the kill. This is the host's OOM killer, not the harness's cap |
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

Running `N` slots takes at least `N × cap + reserve` of host memory. One slot
needs `cap + 1024`. With the 863 MiB cap and the 1024 MiB reserve:

| Slots | Host memory needed |
|---|---|
| 1 | 1,887 MiB |
| 4 | 4,476 MiB |
| 8 | 7,928 MiB |

A nominal 8 GiB host reports about 7,934 MiB, so memory admits all 8 of its
slots. The operator's per-validator slot cap (Backroom slot settings) is a
separate limit, and dispatch funds the smaller of the two per validator.

The worker logs the narrowing on every change, with the memory it would need
for one slot (when none fit) and for every configured slot. A host that fits no
harness logs at error level. The heartbeat reply carries the live cap, so a host
re-sizes as soon as the operator changes it, without waiting for a lease.

The heartbeat's `healthy_slots`, and with it `serviceable_slots` in Backroom
`get_validator_capacity`, reflect it. The public `/validators` view and the
operations dashboard show each host's `memory_admitted_slots`,
`memory_required_mib` and a `memory_note` with that fix path, and mark the
slots past it `Memory-limited`. A host whose memory admits no slot reads
`critical` there.

## Practising against the envelope

`ditto practice` runs the harness at the live case concurrency (8). It pins the
harness to 2 CPUs where the OS allows that (Linux). It samples the harness's
resident memory plus its store, then prints the peak as a share of the cap. A
peak at or above the cap fails the rehearsal as `harness_oom (local)`, and a
peak above 85% prints a warning.

A host process is not a cgroup, so treat the number as an estimate. Leave
headroom.
