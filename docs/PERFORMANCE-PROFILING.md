# Performance profiling

The subnet has complementary sampling profilers for its Go and Python service
planes. Use profiles to choose work from measured live stacks, then compare a
fresh profile after deployment; source inspection or a local benchmark alone
does not establish production activation.

## Go pprof

Every long-lived Go HTTP process in `services/model-relay` and
`services/dittobench-api` starts a best-effort `net/http/pprof` listener on
`127.0.0.1` at `main port + 3000`. The listener is intentionally separate from
the request server. Its host is not configurable, no profiler port is published
by Docker or the cloud firewall, and public Caddy explicitly returns 404 for
`/debug/pprof` and its descendants. Collection enters through IAP SSH and the
checked-in operator tooling only.

Known production mappings:

| service | main | pprof | access on target VM |
| --- | ---: | ---: | --- |
| Platform relay 1 | 8010 | 11010 | host loopback |
| Platform relay 2 | 8011 | 11011 | host loopback |
| DittoBench API | 8000 | 11000 | `sandbox-docker` network namespace |
| DittoBench model relay | 11434 | 14434 | host loopback |

From `services/model-relay`, the operator CLI wraps GCP IAP SSH, loopback
`curl`, profile storage, and `go tool pprof`:

```sh
go run ./cmd/pprofctl list --probe
go run ./cmd/pprofctl top --service platform-relay-1 --profile profile --seconds 15
go run ./cmd/pprofctl top --service platform-relay-1 --profile heap
go run ./cmd/pprofctl goroutines --service platform-relay-2
go run ./cmd/pprofctl svg --service platform-relay-1 --profile allocs
go run ./cmd/pprofctl web --service platform-relay-1 --profile heap

# Save before a deploy, then compare after it is active.
go run ./cmd/pprofctl fetch --service platform-relay-1 --profile heap \
  --out .tmp/pprof/baseline.pb.gz
go run ./cmd/pprofctl diff --service platform-relay-1 --profile heap \
  --base .tmp/pprof/baseline.pb.gz
```

The defaults target `ditto-platform-prod` in `ditto-app-dev/us-central1-a`.
Use `--project`, `--zone`, and `--instance` for another GCE host. A service not
in the known map can be inspected with a safe label plus `--main-port`. For a
process in a Compose network namespace, also pass `--container-service`; the
CLI resolves the running container by its stable Compose service label rather
than assuming a checkout-dependent container name. The known `dittobench-api`
target already selects `sandbox-docker` automatically.

`PPROF_PORT` overrides the derived port and `PPROF_DISABLE=true` disables the
listener. Do not set one shared `PPROF_PORT` for both Platform relay slots: it
would make them contend for one listener. CPU, heap, allocation, goroutine, and
trace profiles work by default. Block and mutex profiles require
`PPROF_BLOCK_RATE` or `PPROF_MUTEX_FRACTION` and a process restart; leave them
off unless contention is the specific investigation because sampling adds
overhead.

Profiles are operational artifacts. They land under `.tmp/pprof/`, are ignored
by Git, and should not be attached to public issues without review.

## Python py-spy

The Platform Ansible role installs a pinned `py-spy` as an isolated uv tool at
`/usr/local/bin/py-spy`. It is not part of the API virtualenv and cannot change
the application's dependency graph. On a Platform VM:

```sh
cd /opt/ditto-subnet/apps/platform
./scripts/profile-python.sh --seconds 30
./scripts/profile-python.sh --app ditto-api --seconds 60 \
  --output /tmp/ditto-api-before.speedscope.json
```

The wrapper resolves the PID from the deploy user's PM2 state, requires a
bounded 1-300 second capture, refuses to overwrite an existing artifact, and
uses `sudo` only for the ptrace operation. The output opens in
[Speedscope](https://www.speedscope.app/). It may still disclose internal code
paths, so copy it off the host only to an approved private location and remove
stale `/tmp` profiles as part of incident cleanup.

CPU observed while py-spy is attached includes `ptrace` sampling overhead. Use
a separate post-detach CPU window when deciding whether the application itself
is saturated.

## Comparison loop

1. Capture a representative profile while the target route is under normal or
   reproduced load, recording the exact process commit.
2. Tie the hot stack to request counts/latency, successful versus failed status,
   request-key reuse, and database or upstream volume. Keep raw profile and
   access-log artifacts private; report aggregate evidence.
3. Change one bounded cost center and validate semantic parity plus the intended
   structural reduction locally.
4. After merge, release, deployment, and rollout are separately confirmed,
   capture the same workload again and compare profiles and route latency.
