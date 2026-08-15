---
name: ditto-subnet-runtime-profiling
description: Capture, inspect, compare, and interpret Ditto subnet Python py-spy and Go pprof profiles without exposing profiler endpoints. Use for current or live runtime profiles, CPU or allocation hot spots, goroutine or contention diagnosis, before-and-after performance evidence, or choosing a measured Python optimization to port to Go across Platform relays and DittoBench services.
---

# Ditto Subnet Runtime Profiling

Use sampling evidence from the exact running revision to identify a bounded cost
center. Keep capture read-only and private, then separate the measured finding,
the proposed code change, and eventual deployment proof.

Read [`docs/PERFORMANCE-PROFILING.md`](../../../docs/PERFORMANCE-PROFILING.md)
before capturing. It owns the current service map, ports, and operator commands.

## Establish the capture contract

Record these fields before interpreting a profile:

- target host, process/service, runtime, and profile type;
- UTC start time, duration, and workload or route active during the window;
- exact deployed binary or checkout revision reported by that process or host;
- capture command, artifact path, byte size, SHA-256, and profiler errors;
- relevant request count, latency, CPU, memory, database, or upstream metrics.

Never substitute the local branch SHA, a release tag, or a green PR for the
running revision. An idle profile answers only what the process does while idle.
Do not generate production load unless the user explicitly authorizes it.

## Capture Go with pprof

Run the checked-in CLI from `services/model-relay`. It reaches loopback through
GCP IAP SSH; for the scorer namespace it enters the container from that SSH
session.

```bash
go run ./cmd/pprofctl list --probe
go run ./cmd/pprofctl top --service platform-relay-1 \
  --profile profile --seconds 30
go run ./cmd/pprofctl top --service platform-relay-1 \
  --profile heap --sample-index inuse_space
go run ./cmd/pprofctl top --service platform-relay-1 \
  --profile allocs --sample-index alloc_space
go run ./cmd/pprofctl goroutines --service platform-relay-2
```

Use CPU `profile` for on-CPU work, `allocs` with `alloc_space` for allocation
pressure, `heap` with `inuse_space` for retained memory, and goroutines for
blocked or leaked work. Use block or mutex profiles only when the investigation
requires them: enabling those samplers changes runtime overhead and needs a
restart.

For before/after evidence, save the baseline and compare the same profile type,
sample index, duration, target, and representative workload:

```bash
go run ./cmd/pprofctl fetch --service platform-relay-1 \
  --profile profile --seconds 30 --out .tmp/pprof/before.pb.gz
go run ./cmd/pprofctl diff --service platform-relay-1 \
  --profile profile --seconds 30 --base .tmp/pprof/before.pb.gz
```

Interpret `flat` as direct sampled cost and `cum` as cost in a function plus its
descendants. Start from high-flat application frames, then walk their cumulative
callers to the route or worker responsible. A high cumulative wrapper is not by
itself the implementation to optimize.

## Capture Python with py-spy

Enter the Platform VM through GCP IAP SSH, record the deployed checkout SHA,
then run the bounded wrapper from the deployed checkout:

```bash
gcloud compute ssh ditto-platform-prod \
  --project ditto-app-dev --zone us-central1-a \
  --tunnel-through-iap --quiet

cd /opt/ditto-subnet
git rev-parse HEAD
cd apps/platform
./scripts/profile-python.sh --app ditto-api --seconds 30
```

Copy the named artifact only through the approved SSH/IAP path into
`.tmp/pprof/`:

```bash
gcloud compute scp --tunnel-through-iap \
  --project ditto-app-dev --zone us-central1-a \
  ditto-platform-prod:/tmp/ditto-api-UTC.speedscope.json .tmp/pprof/
```

Preserve py-spy's sample/error summary from the capture. If the deployed
revision does not yet contain the wrapper, report that activation gap; do not
add a public profiler endpoint as a workaround.

Treat CPU measured while py-spy is attached as profiler-affected. `ptrace`
sampling can add substantial system CPU, so collect a clean PID/host CPU window
after detach before claiming the application itself is pegged or recovered.

Use a local Speedscope viewer for private flame-chart inspection; do not upload
the artifact to the public hosted viewer. For a deterministic top-frame summary
of either speedscope JSON or py-spy's collapsed raw format, run:

```bash
python3 .agents/skills/ditto-subnet-runtime-profiling/scripts/summarize_python_profile.py \
  .tmp/pprof/ditto-api.speedscope.json --limit 25
```

Compare two private artifacts by normalized stack share, even when capture
durations differ. Speedscope source line numbers are ignored during comparison
so an unchanged function still matches after nearby edits:

```bash
python3 .agents/skills/ditto-subnet-runtime-profiling/scripts/summarize_python_profile.py \
  .tmp/pprof/after.speedscope.json \
  --base .tmp/pprof/before.speedscope.json --limit 25
```

Treat `self` as samples ending in the frame and `inclusive` as samples containing
the frame anywhere in the stack. Use inclusive callers to attribute a leaf cost
to an endpoint. Sampling proportions are not request latency percentiles and do
not establish per-request cost without traffic volume and route attribution.

## Correlate routes without publishing access logs

Export only a bounded Caddy JSONL window through the approved IAP path, then
aggregate it locally. Match explicit route families rather than grouping raw
dynamic paths, and keep the source log private:

```bash
python3 .agents/skills/ditto-subnet-runtime-profiling/scripts/summarize_caddy_access.py \
  .tmp/pprof/caddy-window.jsonl \
  --since 2026-08-15T13:50:00Z --until 2026-08-15T14:05:00Z \
  --match 'agent-pipeline=^/api/v1/agent/[^/]+/pipeline$'
```

The summary reports request/status counts, successful-request p50/p95/max
latency, exact URI cardinality, and requests per URI without printing URIs.
When the checked-in script is present on the target host, prefer invoking it
through IAP against `/opt/ditto/logs/caddy/platform.log` so only the aggregate
crosses SSH. Use an explicit UTC window that contains the profile capture.
Use status-specific latency so fast proxy failures do not make a slow route look
healthy. Low requests-per-URI reuse is evidence against whole-response caching;
it does not rule out caching or reusing a repeated internal query or projection.

## Choose the next performance change

1. Group frames into operational cost centers such as query construction,
   database wait/decode, JSON encoding, chain fetch/translation, provider I/O,
   cryptography, logging, or garbage collection.
2. Correlate the leading group with route traffic and latency. Compare a peer or
   a second window before calling a transient stack systemic.
3. Identify the smallest repeated unit of work. Prefer removing, caching,
   batching, narrowing, or sharing it within one authoritative request snapshot
   before rewriting a broad subsystem. Measure cache-key reuse before proposing
   a cross-request response cache.
4. Port work to Go only when the hot cost is CPU/allocation or concurrency work
   the Go request plane can own while preserving wire, transaction, auth,
   accounting, and failure semantics. Database wait usually calls for query,
   index, round-trip, or payload work first.
5. Microbenchmark the isolated repeated unit when possible, and pin semantic
   parity plus structural behavior such as one authoritative load instead of
   two. Avoid flaky wall-clock assertions in the correctness suite.
6. State the activation boundary. After deployment, capture the same profile
   again; local benchmarks and CI do not prove the live win.

Report the top stacks with sample counts and percentages, the request path they
belong to, the evidence against alternatives, the recommended bounded change,
and what a comparable post-deploy profile must show.

## Security and artifact handling

- Never bind pprof outside `127.0.0.1`, publish its ports, proxy its paths, add a
  firewall exception, or create a public tunnel. Use `pprofctl` and IAP SSH.
- Never upload profiles to public issues or third-party viewers. Profiles can
  disclose function names, paths, dependencies, and runtime behavior.
- Treat raw access-log windows as private too. They may contain identifiers or
  query values; publish only aggregate route labels, counts, and latency.
- Keep artifacts under ignored `.tmp/pprof/` or an approved private location.
  Hash before moving, redact reports, and remove stale remote `/tmp` artifacts.
- Do not dump environment variables, process memory, request bodies, tokens, or
  secrets. Sampling stacks are sufficient for this workflow.
- Stop and report a missing SSH/IAP permission, absent profiler, revision
  mismatch, or unrepresentative workload instead of weakening the boundary.
