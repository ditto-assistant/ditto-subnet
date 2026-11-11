# Query adapter pilot measurements

> **The numbers below are local loopback measurements, not economic savings.**
> They come from synthetic adapters on one host, with the client and the
> reference miner talking over 127.0.0.1. They are inputs for a later
> serving-cost model. They say nothing about cost, price, or savings on a
> remote, paid, or attested deployment, and should not be quoted as such.

Produced by:

```bash
uv run python -m query_adapter_pilot.bench --json "$TMPDIR/qa-bench.json"
```

## Run metadata

| Field | Value |
|---|---|
| Date | TBD |
| Commit | TBD |
| Host (`machine.platform`, `machine.machine`) | TBD |
| CPU count (`machine.cpu_count`) | TBD |
| Python / numpy | TBD |
| `config.requests` | TBD |
| `config.tenants` | TBD |
| `config.cache_capacity` | TBD |
| `config.concurrency` | TBD |

## Latency and throughput

All latencies are in ms. "CPU µs/req" is a `time.process_time` delta divided
by requests. For miner phases it includes both client and miner threads,
because they share one process.

| Phase | p50 | p95 | p99 | Throughput (req/s) | CPU µs/req | Miner `compute_us` p50 | Overhead µs p50 | Cache hit/miss | Min reference cos |
|---|---|---|---|---|---|---|---|---|---|
| `local.warm` (weights in memory) | TBD | TBD | TBD | TBD | TBD | n/a | n/a | n/a | TBD |
| `local.cold` (decode wire, verify digest, apply) | TBD | TBD | TBD | TBD | TBD | n/a | n/a | n/a | TBD |
| `local.warm_concurrent` | TBD | TBD | TBD | TBD | TBD | n/a | n/a | n/a | TBD |
| `miner.warm` (cache hits) | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD |
| `miner.cold` (tenants > cache) | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD |
| `miner.multi_tenant` (round-robin, concurrent) | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD |
| `miner.provision` (per tenant) | TBD | TBD | TBD | n/a | n/a | n/a | n/a | n/a | n/a |

## Serving-cost inputs

| Input | JSON field | Value |
|---|---|---|
| Parameters per adapter (W1+b1+W2+b2) | `serving_inputs.params_per_adapter` | TBD (expected 197,504) |
| Bytes per adapter, float32 | `serving_inputs.bytes_per_adapter_f32` | TBD (expected 790,016) |
| Resident adapters per GiB (weights only) | `serving_inputs.resident_adapters_per_gib` | TBD (expected 1,359) |
| Wire bytes per provision | `serving_inputs.provision_wire_bytes` | TBD |
| Wire bytes per adapt request | `serving_inputs.adapt_request_wire_bytes` | TBD |
| Wire bytes per adapt response | `serving_inputs.adapt_response_wire_bytes` | TBD |
| Query vector bytes, float32 | `serving_inputs.query_bytes_f32` | 3,072 |
| Miner cache after run (hits / misses / evictions) | `miner.health.cache` | TBD |

## Proof run

Fill this in from `uv run python scripts/prove.py` at the same commit.

| Scenario | Outcomes | Sources | Accepted | Dupes | Min cos | p50/p95 ms | Result |
|---|---|---|---|---|---|---|---|
| (paste the prove.py table) | TBD | TBD | TBD | TBD | TBD | TBD | TBD |

## Caveats

- Loopback HTTP has no real network latency, TLS, or attestation overhead.
- Resident adapters per GiB counts raw float32 weights only. It excludes the
  decoded-cache overhead, the Python object overhead, and the process baseline.
- `local.cold` decodes a full provision body (JSON and base64) and re-derives
  the digest. That is an upper bound on what Ditto would pay to materialize an
  adapter it does not hold in memory.
- No economic, pricing, or savings conclusion follows from these numbers alone.
