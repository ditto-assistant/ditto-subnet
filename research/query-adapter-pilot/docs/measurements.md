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
| Commit | integrated tip e1c98d857 (contract, miner, contract fix, client, and proof slices) |
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

| Phase | p50 | p95 | p99 | Throughput (req/s) | CPU µs/req | Miner `compute_us` p50 / p95 | Overhead µs p50 | Cache hit/miss |
|---|---|---|---|---|---|---|---|---|
| `local.warm` (weights in memory) | 0.0121 | 0.0145 | 0.0176 | 73,971 | 13.54 | n/a | n/a | n/a |
| `local.cold` (parse JSON, decode base64, check digest, apply) | 1.602 | 1.734 | 1.838 | 619 | 1,612.01 | n/a | n/a | n/a |
| `local.warm_concurrent` (4 threads) | 0.0338 | 0.5178 | 1.081 | 24,762 | 66.94 | n/a | n/a | n/a |
| `miner.warm` (2 hot tenants, cache hits) | 0.1698 | 0.2047 | 0.2228 | 5,754 | 158.97 | 13 / 18 | 156.81 | 200 / 0 |
| `miner.cold` (16 tenants, cache 4) | 0.4673 | 0.5294 | 0.5563 | 2,114 | 469.19 | 14 / 19 | 453.33 | 2 / 198 |
| `miner.multi_tenant` (16 tenants, 4 threads) | 1.3279 | 1.8422 | 2.1692 | 2,891 | 772.58 | 32 / 202.75 | 1,295.88 | 0 / 200 |
| `miner.provision` (per tenant, 16 tenants) | 2.0629 | 2.6829 | 3.6202 | n/a | n/a | n/a | n/a | n/a |

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
| Wire bytes per adapt request | `serving_inputs.adapt_request_wire_bytes` | 4,353 |
| Wire bytes per adapt response | `serving_inputs.adapt_response_wire_bytes` | 4,372 |
| Query vector bytes, float32 | `serving_inputs.query_bytes_f32` | 3,072 |
| Miner cache after run (capacity / entries / hits / misses / evictions) | `miner.health.cache` | 4 / 4 / 202 / 400 / 396 |
| Miner adapt requests served / errors | `miner.health.requests` | 602 / 0 |

## Proof run

This is `scripts/prove.py` at integrated tip e1c98d857. It went through the
miner CLI and the `python -m query_adapter_pilot run` entry point. 14/14
scenarios passed. Latencies are the report's end-to-end p50/p95 in ms.

| Scenario | Outcomes | Sources | Accepted | Dupes | Min cos | p50/p95 ms | Result |
|---|---|---|---|---|---|---|---|
| `happy` | ok=30 | miner=30 | 30 | 0 | 1.0000000 | 0.66/1.18 | PASS |
| `incorrect` | rejected_incorrect=30 | fallback_local=30 | 0 | 0 | 1.0000000 | 0.73/1.24 | PASS |
| `invalid-nan` | rejected_invalid=3 | fallback_local=3 | 0 | 0 | 1.0000000 | 1.04/1.49 | PASS |
| `invalid-bad-norm` | rejected_invalid=3 | fallback_local=3 | 0 | 0 | 1.0000000 | 0.87/1.41 | PASS |
| `invalid-wrong-echo` | rejected_invalid=3 | fallback_local=3 | 0 | 0 | 1.0000000 | 0.88/1.44 | PASS |
| `unavailable` | unavailable=3 | fallback_local=3 | 0 | 0 | 1.0000000 | 0.44/0.55 | PASS |
| `timeout` | timeout=3 | fallback_local=3 | 0 | 0 | 1.0000000 | 201.95/202.06 | PASS |
| `stale` | rejected_stale=4 | fallback_local=4 | 0 | 0 | 1.0000000 | 0.91/1.02 | PASS |
| `missing` | rejected_missing=4 | fallback_local=4 | 0 | 0 | 1.0000000 | 0.72/1.77 | PASS |
| `unauthorized` | ok=4, rejected_unauthorized=4 | fallback_identity=4, miner=4 | 4 | 0 | 1.0000000 | 0.40/1.25 | PASS |
| `isolation` | ok=18 | miner=18 | 18 | 0 | 1.0000000 | 0.72/1.19 | PASS |
| `isolation-swap` | rejected_incorrect=18 | fallback_local=18 | 0 | 0 | 1.0000000 | 1.08/1.46 | PASS |
| `replay` | duplicate=4, ok=6 | miner=10 | 6 | 4 | 1.0000000 | 0.73/1.40 | PASS |
| `multi-tenant` | ok=80 | miner=80 | 80 | 0 | 1.0000000 | 0.96/1.08 | PASS |

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
- No economic, pricing, or savings conclusion follows from these numbers.
