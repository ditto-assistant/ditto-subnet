# Query adapter pilot measurements

> **The numbers below are local loopback measurements, not economic savings.**
> They come from synthetic adapters on one host, with the client and the
> reference miner talking over 127.0.0.1. They are inputs for a later
> serving-cost model. They say nothing about cost, price, or savings on a
> remote, paid, or attested deployment, and should not be quoted as such.

Produced by:

```bash
uv run python -m query_adapter_pilot.bench --json "$TMPDIR/qa-bench.json"
uv run python scripts/prove.py
```

## Run metadata

| Field | Value |
|---|---|
| Date | 2026-11-10 |
| Commit | integrated tip 10d057370 + this fix (bench reports bad responses as errors and primes concurrent workers before timing) |
| Host | Apple M5 Pro, `macOS-26.6.2-arm64-arm-64bit` (`arm64`) |
| CPU count (`machine.cpu_count`) | 18 |
| Python / numpy | 3.12.11 / 2.5.4 |
| `config.requests` | 200 |
| `config.tenants` | 16 |
| `config.cache_capacity` | 4 |
| `config.concurrency` | 4 |

## Latency and throughput

All latencies are in ms. "CPU µs/req" is a `time.process_time` delta divided
by requests. For miner phases it includes both client and miner threads,
because they share one process. No phase had request errors. Every phase's
minimum reference cosine was 0.9999999999999998, which is at or above
`PARITY_MIN_COSINE` (0.999999).

Concurrent phases start every worker thread before timing begins. For the
miner, each worker also opens its keep-alive connection with a health call
before timing. So no measured request pays thread start-up or a TCP connect.

| Phase | p50 | p95 | p99 | Throughput (req/s) | CPU µs/req | Miner `compute_us` p50 / p95 | Overhead µs p50 | Cache hit/miss |
|---|---|---|---|---|---|---|---|---|
| `local.warm` (weights in memory) | 0.0175 | 0.0406 | 0.0815 | 45,625 | 21.91 | n/a | n/a | n/a |
| `local.cold` (parse JSON, decode base64, check digest, apply) | 1.6392 | 2.0322 | 2.9236 | 587 | 1,657.48 | n/a | n/a | n/a |
| `local.warm_concurrent` (4 threads) | 0.104 | 0.5764 | 1.0699 | 19,124 | 80.67 | n/a | n/a | n/a |
| `miner.warm` (2 hot tenants, cache hits) | 0.1704 | 0.2261 | 0.2616 | 5,603 | 165.03 | 14 / 20.05 | 156.38 | 200 / 0 |
| `miner.cold` (16 tenants, cache 4) | 0.5231 | 0.8073 | 1.1461 | 1,779 | 546.40 | 17 / 44.25 | 506.15 | 2 / 198 |
| `miner.multi_tenant` (16 tenants, 4 threads) | 1.3996 | 2.0271 | 2.0952 | 2,735 | 822.62 | 35 / 227.1 | 1,364.58 | 0 / 200 |
| `miner.provision` (per tenant, 16 tenants) | 2.3856 | 2.9143 | 3.6541 | n/a | n/a | n/a | n/a | n/a |

"Overhead µs p50" is the end-to-end p50 minus the miner-reported
`compute_us` p50. It covers client encoding, the loopback HTTP round trip, and
response parsing.

`miner.cold` shows 2 hits because the first two round-robin tenants were still
cached from the warm phase.

## Serving-cost inputs

| Input | JSON field | Value |
|---|---|---|
| Parameters per adapter (W1+b1+W2+b2) | `serving_inputs.params_per_adapter` | 197,504 |
| Bytes per adapter, float32 | `serving_inputs.bytes_per_adapter_f32` | 790,016 |
| Resident adapters per GiB (weights only) | `serving_inputs.resident_adapters_per_gib` | 1,359 |
| Wire bytes per provision | `serving_inputs.provision_wire_bytes` | 1,053,620 |
| Wire bytes per adapt request (successful round trips only) | `serving_inputs.adapt_request_wire_bytes` | 4,353 |
| Wire bytes per adapt response (successful round trips only) | `serving_inputs.adapt_response_wire_bytes` | 4,372 |
| Query vector bytes, float32 | `serving_inputs.query_bytes_f32` | 3,072 |
| Miner cache after run (capacity / entries / hits / misses / evictions) | `miner.health.cache` | 4 / 4 / 202 / 400 / 396 |
| Miner adapt requests served / errors | `miner.health.requests` | 602 / 0 |

## Proof run

This is `scripts/prove.py` at integrated tip 10d057370 + this fix. It went
through the miner CLI and the `python -m query_adapter_pilot run` entry point.
14/14 scenarios passed. Latencies are the report's end-to-end p50/p95 in ms.

| Scenario | Outcomes | Sources | Accepted | Dupes | Min cos | p50/p95 ms | Result |
|---|---|---|---|---|---|---|---|
| `happy` | ok=30 | miner=30 | 30 | 0 | 1.0000000 | 0.84/1.46 | PASS |
| `incorrect` | rejected_incorrect=30 | fallback_local=30 | 0 | 0 | 1.0000000 | 1.01/1.85 | PASS |
| `invalid-nan` | rejected_invalid=3 | fallback_local=3 | 0 | 0 | 1.0000000 | 1.11/1.80 | PASS |
| `invalid-bad-norm` | rejected_invalid=3 | fallback_local=3 | 0 | 0 | 1.0000000 | 0.97/1.96 | PASS |
| `invalid-wrong-echo` | rejected_invalid=3 | fallback_local=3 | 0 | 0 | 1.0000000 | 0.90/1.33 | PASS |
| `unavailable` | unavailable=3 | fallback_local=3 | 0 | 0 | 1.0000000 | 0.58/0.80 | PASS |
| `timeout` | timeout=3 | fallback_local=3 | 0 | 0 | 1.0000000 | 202.18/202.42 | PASS |
| `stale` | rejected_stale=4 | fallback_local=4 | 0 | 0 | 1.0000000 | 0.99/1.02 | PASS |
| `missing` | rejected_missing=4 | fallback_local=4 | 0 | 0 | 1.0000000 | 0.80/1.89 | PASS |
| `unauthorized` | ok=4, rejected_unauthorized=4 | fallback_identity=4, miner=4 | 4 | 0 | 1.0000000 | 0.53/1.67 | PASS |
| `isolation` | ok=18 | miner=18 | 18 | 0 | 1.0000000 | 0.80/1.23 | PASS |
| `isolation-swap` | rejected_incorrect=18 | fallback_local=18 | 0 | 0 | 1.0000000 | 1.25/1.90 | PASS |
| `replay` | duplicate=4, ok=6 | miner=10 | 6 | 4 | 1.0000000 | 0.87/1.50 | PASS |
| `multi-tenant` | ok=80 | miner=80 | 80 | 0 | 1.0000000 | 1.09/1.66 | PASS |

In `multi-tenant`, the miner cache ended at capacity 8, 8 entries, 12 hits,
68 misses, and 60 evictions.

## Caveats

- **`local.cold` and `miner.cold` measure different work. Do not read either
  as faster than the other.**
  - `local.cold` covers JSON parsing and base64 decoding of the whole
    provision payload, the digest check, and apply.
  - `miner.cold` covers decoding from the miner's stored raw bytes, the digest
    check, apply, and the loopback round trip.
- Loopback HTTP has no real network latency, TLS, or attestation overhead.
- Resident adapters per GiB counts raw float32 weights only. It excludes the
  decoded-cache overhead, the Python object overhead, and the process baseline.
- These are single runs on one host. They show no run-to-run variance.
  Because variance was not measured, differences from the earlier e1c98d857
  run (for example `local.warm` throughput) are not evidence of a regression
  or an improvement.
- No economic, pricing, or savings conclusion follows from these numbers.
