"""Local loopback measurements for the query adapter pilot.

Measures, on one machine:

* an **equivalent local baseline**: Ditto applying the adapter itself, either
  warm (weights already materialized in memory) or cold (decode the adapter
  from its wire bytes, verify its digest, then apply);
* the **reference miner over loopback HTTP** (in-process ``start_miner``, raw
  ``query-adapter/v1`` messages, no pilot client): warm (cache hits), cold
  (round-robin over more tenants than the cache holds, so misses), and
  multi-tenant round-robin at the requested concurrency.

It reports latency percentiles, miner ``compute_us`` versus end-to-end time,
throughput, CPU time per request, and serving-cost **inputs** (adapter bytes,
resident adapters per GiB, wire bytes). It makes no economic-savings claim:
loopback numbers on one host say nothing about a remote, paid, or attested
deployment.

    python -m query_adapter_pilot.bench --json /tmp/qa-bench.json
    python -m query_adapter_pilot.bench --local-only

Synthetic-only: seeded adapters and queries, ``syn-`` tenants, loopback.
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import importlib
import json
import os
import platform
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlsplit

import numpy as np

from .contract import (
    CONTRACT_VERSION,
    DIM,
    HIDDEN,
    PARITY_MIN_COSINE,
    ROUTE_ADAPT,
    ROUTE_HEALTH,
    ROUTE_PROVISION,
    TOKEN_HEADER,
    AdapterRef,
    AdapterWeights,
    AdaptRequest,
    AdaptResponse,
    F32Array,
    ProvisionRequest,
    make_request_id,
)
from .reference import apply, parity, synthetic_adapter, synthetic_query

BENCH_KIND = "pilot-bench"
NOTE = (
    "Local loopback measurements on one host with synthetic adapters. These are "
    "serving-cost inputs, not economic savings, and say nothing about a remote, "
    "paid, or attested deployment."
)
ADAPTER_SEED_BASE = 900_000
QUERY_SEED_BASE = 950_000
RUN_ID = "bench"
GIB = 2**30


@dataclass(frozen=True)
class BenchConfig:
    requests: int = 200
    tenants: int = 16
    cache_capacity: int = 4
    concurrency: int = 4
    timeout_s: float = 10.0


@dataclass
class Sample:
    latency_ns: int
    compute_us: int | None = None
    cache: str | None = None
    vector: F32Array | None = None
    tenant: int = 0
    query: int = 0
    error: str | None = None


def _pct(values: Sequence[float]) -> dict[str, float | None]:
    if not values:
        return {"p50": None, "p95": None, "p99": None, "mean": None, "max": None}
    arr = np.asarray(values, dtype=np.float64)
    p50, p95, p99 = np.percentile(arr, [50, 95, 99])
    return {
        "p50": round(float(p50), 4),
        "p95": round(float(p95), 4),
        "p99": round(float(p99), 4),
        "mean": round(float(arr.mean()), 4),
        "max": round(float(arr.max()), 4),
    }


def phase_stats(
    samples: Sequence[Sample], wall_ns: int, cpu_ns: int, concurrency: int
) -> dict[str, Any]:
    """Summarize one measured phase. ``cpu_ns`` is a ``time.process_time_ns``
    delta, so for in-process miner phases it covers client AND miner threads."""
    good = [s for s in samples if s.error is None]
    computes = [float(s.compute_us) for s in good if s.compute_us is not None]
    n = len(samples)
    out: dict[str, Any] = {
        "requests": n,
        "errors": dict(Counter(s.error for s in samples if s.error is not None)),
        "concurrency": concurrency,
        "latency_ms": _pct([s.latency_ns / 1e6 for s in good]),
        "throughput_rps": round(n / (wall_ns / 1e9), 2) if wall_ns > 0 else None,
        "cpu_us_per_request": round(cpu_ns / 1e3 / n, 2) if n else None,
        "wall_s": round(wall_ns / 1e9, 4),
    }
    if computes:
        out["miner_compute_us"] = _pct(computes)
        e2e_us = [s.latency_ns / 1e3 for s in good if s.compute_us is not None]
        out["overhead_us_p50"] = round(float(np.median(e2e_us)) - float(np.median(computes)), 2)
    caches = Counter(s.cache for s in good if s.cache is not None)
    if caches:
        out["cache"] = {"hit": caches.get("hit", 0), "miss": caches.get("miss", 0)}
    return out


def _timed(
    jobs: Sequence[tuple[int, int]], fn: Callable[[int, int], Sample], concurrency: int
) -> tuple[list[Sample], int, int]:
    """Run ``fn(tenant, query)`` over ``jobs``; return samples, wall ns, CPU ns."""
    cpu0, wall0 = time.process_time_ns(), time.perf_counter_ns()
    if concurrency <= 1:
        samples = [fn(t, q) for t, q in jobs]
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            samples = list(pool.map(lambda job: fn(*job), jobs))
    return samples, time.perf_counter_ns() - wall0, time.process_time_ns() - cpu0


@dataclass
class Fleet:
    """Synthetic tenants, their adapters, and a pool of queries."""

    refs: list[AdapterRef]
    weights: list[AdapterWeights]
    provision_bodies: list[bytes]
    queries: list[F32Array]

    @classmethod
    def build(cls, tenants: int, queries: int) -> Fleet:
        refs, weights, bodies = [], [], []
        for t in range(tenants):
            w = synthetic_adapter(ADAPTER_SEED_BASE + t)
            ref = AdapterRef(f"syn-bench-t{t:04d}", 1, w.digest())
            refs.append(ref)
            weights.append(w)
            bodies.append(json.dumps(ProvisionRequest(ref, w).to_wire()).encode())
        qs = [synthetic_query(QUERY_SEED_BASE + i) for i in range(queries)]
        return cls(refs, weights, bodies, qs)


def round_robin(n: int, tenants: Sequence[int]) -> list[tuple[int, int]]:
    return [(tenants[i % len(tenants)], i) for i in range(n)]


def min_parity(fleet: Fleet, samples: Sequence[Sample]) -> float | None:
    """Worst cosine of returned vectors against the reference (checked after
    timing so verification cost does not pollute the measured phase)."""
    worst: float | None = None
    for s in samples:
        if s.vector is None:
            continue
        cos, _ = parity(s.vector, apply(fleet.weights[s.tenant], fleet.queries[s.query]))
        worst = cos if worst is None else min(worst, cos)
    return worst


# ─── Local baseline ──────────────────────────────────────────────────────────


def bench_local(cfg: BenchConfig, fleet: Fleet) -> dict[str, Any]:
    jobs = round_robin(cfg.requests, list(range(cfg.tenants)))

    def warm(t: int, q: int) -> Sample:
        t0 = time.perf_counter_ns()
        vec = apply(fleet.weights[t], fleet.queries[q])
        return Sample(time.perf_counter_ns() - t0, vector=vec, tenant=t, query=q)

    def cold(t: int, q: int) -> Sample:
        t0 = time.perf_counter_ns()
        # decode from wire bytes; from_wire re-derives and checks the digest
        weights = ProvisionRequest.from_wire(json.loads(fleet.provision_bodies[t])).weights
        vec = apply(weights, fleet.queries[q])
        return Sample(time.perf_counter_ns() - t0, vector=vec, tenant=t, query=q)

    out: dict[str, Any] = {}
    for name, fn, conc in (
        ("warm", warm, 1),
        ("cold", cold, 1),
        ("warm_concurrent", warm, cfg.concurrency),
    ):
        samples, wall, cpu = _timed(jobs, fn, conc)
        out[name] = phase_stats(samples, wall, cpu, conc) | {
            "min_reference_cos": min_parity(fleet, samples)
        }
    return out


# ─── Miner over loopback ─────────────────────────────────────────────────────


class LoopbackClient:
    """Minimal raw-contract HTTP client: one persistent connection per thread."""

    def __init__(self, url: str, token: str, timeout_s: float) -> None:
        parts = urlsplit(url)
        if parts.hostname not in ("127.0.0.1", "localhost", "::1") or parts.port is None:
            raise ValueError(f"bench only talks to a loopback miner, got {url!r}")
        self.host, self.port = parts.hostname, parts.port
        self.token = token
        self.timeout_s = timeout_s
        self._local = threading.local()
        self._seq = 0
        self._lock = threading.Lock()

    def _conn(self) -> http.client.HTTPConnection:
        conn: http.client.HTTPConnection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout_s)
            self._local.conn = conn
        return conn

    def call(self, method: str, route: str, body: bytes | None) -> tuple[int, bytes]:
        headers = {TOKEN_HEADER: self.token}
        if body is not None:
            headers["Content-Type"] = "application/json"
        for attempt in (0, 1):
            conn = self._conn()
            try:
                conn.request(method, route, body=body, headers=headers)
                resp = conn.getresponse()
                return resp.status, resp.read()
            except (http.client.HTTPException, ConnectionError):
                conn.close()
                self._local.conn = None
                if attempt:
                    raise
        raise AssertionError("unreachable")

    def next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq


def _adapt(client: LoopbackClient, fleet: Fleet, t: int, q: int) -> tuple[Sample, int, int]:
    """One adapt round trip; returns the sample and request/response byte sizes.
    Timing covers encode, HTTP, and response parsing (what Ditto would pay)."""
    ref = fleet.refs[t]
    t0 = time.perf_counter_ns()
    rid = make_request_id(RUN_ID, ref.tenant_id, client.next_seq())
    body = json.dumps(AdaptRequest(rid, ref, fleet.queries[q]).to_wire()).encode()
    try:
        status, raw = client.call("POST", ROUTE_ADAPT, body)
    except (OSError, http.client.HTTPException) as exc:
        return Sample(time.perf_counter_ns() - t0, error=type(exc).__name__), len(body), 0
    if status != 200:
        code = f"HTTP_{status}"
        with contextlib.suppress(ValueError, KeyError, TypeError):
            code = str(json.loads(raw)["error"]["code"])
        return Sample(time.perf_counter_ns() - t0, error=code), len(body), len(raw)
    resp = AdaptResponse.from_wire(json.loads(raw))
    elapsed = time.perf_counter_ns() - t0
    error = None if resp.request_id == rid and resp.ref == ref else "ECHO_MISMATCH"
    sample = Sample(elapsed, resp.compute_us, resp.cache, resp.vector, t, q, error)
    return sample, len(body), len(raw)


def load_miner() -> Any:
    """Import the reference miner slice lazily (it may be absent in a
    contract-only checkout)."""
    return importlib.import_module("query_adapter_pilot.miner")


def bench_miner(cfg: BenchConfig, fleet: Fleet, miner: Any) -> dict[str, Any]:
    token = "syn-bench-" + os.urandom(8).hex()
    config = miner.MinerConfig(
        host="127.0.0.1",
        port=0,
        token=token,
        cache_capacity=cfg.cache_capacity,
        max_tenants=max(1024, cfg.tenants),
        fault="none",
        slow_ms=0,
        fault_seed=0,
    )
    out: dict[str, Any] = {}
    sizes: dict[str, list[int]] = {"adapt_request": [], "adapt_response": []}
    with miner.start_miner(config) as running:
        client = LoopbackClient(str(running.url), token, cfg.timeout_s)

        prov_ns, prov_errors = [], 0
        for body in fleet.provision_bodies:
            t0 = time.perf_counter_ns()
            status, _ = client.call("PUT", ROUTE_PROVISION, body)
            prov_ns.append(time.perf_counter_ns() - t0)
            prov_errors += status != 200
        out["provision"] = {
            "tenants": len(fleet.provision_bodies),
            "errors": prov_errors,
            "latency_ms": _pct([n / 1e6 for n in prov_ns]),
        }

        def adapt(t: int, q: int) -> Sample:
            sample, req_bytes, resp_bytes = _adapt(client, fleet, t, q)
            sizes["adapt_request"].append(req_bytes)
            sizes["adapt_response"].append(resp_bytes)
            return sample

        hot = list(range(max(1, min(cfg.tenants, cfg.cache_capacity // 2 or 1))))
        for t in hot:  # prime the hot set so the warm phase measures hits
            adapt(t, 0)
        everyone = list(range(cfg.tenants))
        for name, tenants, conc in (
            ("warm", hot, 1),
            ("cold", everyone, 1),
            ("multi_tenant", everyone, cfg.concurrency),
        ):
            samples, wall, cpu = _timed(round_robin(cfg.requests, tenants), adapt, conc)
            out[name] = phase_stats(samples, wall, cpu, conc) | {
                "tenants": len(tenants),
                "min_reference_cos": min_parity(fleet, samples),
            }
        out["cold"]["cache_capacity"] = cfg.cache_capacity
        out["cold"]["forces_misses"] = cfg.tenants > cfg.cache_capacity
        status, raw = client.call("GET", ROUTE_HEALTH, None)
        out["health"] = json.loads(raw) if status == 200 else {"status_code": status}
    out["wire_bytes"] = {key: int(np.median(vals)) if vals else None for key, vals in sizes.items()}
    return out


# ─── Report ──────────────────────────────────────────────────────────────────


def serving_inputs(fleet: Fleet) -> dict[str, Any]:
    w = fleet.weights[0]
    params = DIM * HIDDEN + HIDDEN + HIDDEN * DIM + DIM
    return {
        "params_per_adapter": params,
        "bytes_per_adapter_f32": w.nbytes,
        "resident_adapters_per_gib": GIB // w.nbytes,
        "provision_wire_bytes": len(fleet.provision_bodies[0]),
        "adapt_request_wire_bytes": None,  # filled from the miner phase
        "adapt_response_wire_bytes": None,
        "query_bytes_f32": DIM * 4,
    }


def machine() -> dict[str, Any]:
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "numpy": np.__version__,
        "cpu_count": os.cpu_count(),
    }


def run(cfg: BenchConfig, local_only: bool = False, miner: Any = None) -> dict[str, Any]:
    if cfg.requests < 1 or cfg.tenants < 1 or cfg.cache_capacity < 1 or cfg.concurrency < 1:
        raise ValueError("requests, tenants, cache_capacity, concurrency must be >= 1")
    fleet = Fleet.build(cfg.tenants, cfg.requests)
    inputs = serving_inputs(fleet)
    doc: dict[str, Any] = {
        "contract": CONTRACT_VERSION,
        "kind": BENCH_KIND,
        "note": NOTE,
        "economic_claims": "none",
        "machine": machine(),
        "config": asdict(cfg),
        "parity_min_cosine": PARITY_MIN_COSINE,
        "serving_inputs": inputs,
        "local": bench_local(cfg, fleet),
        "miner": None,
    }
    if not local_only:
        result = bench_miner(cfg, fleet, miner if miner is not None else load_miner())
        inputs["adapt_request_wire_bytes"] = result["wire_bytes"]["adapt_request"]
        inputs["adapt_response_wire_bytes"] = result["wire_bytes"]["adapt_response"]
        doc["miner"] = result
    doc["cpu_us_per_request"] = {
        f"{side}_{phase}": stats["cpu_us_per_request"]
        for side in ("local", "miner")
        for phase, stats in (doc[side] or {}).items()
        if isinstance(stats, dict) and "cpu_us_per_request" in stats
    }
    return doc


def summary_lines(doc: dict[str, Any]) -> list[str]:
    lines = [NOTE, ""]
    lines.append(f"{'phase':24} {'p50 ms':>9} {'p95 ms':>9} {'p99 ms':>9} {'rps':>9} {'cpu us':>9}")
    for side in ("local", "miner"):
        for phase, stats in (doc.get(side) or {}).items():
            if not isinstance(stats, dict) or "latency_ms" not in stats or phase == "provision":
                continue
            lat = stats["latency_ms"]
            lines.append(
                f"{side + '.' + phase:24} {lat['p50']!s:>9} {lat['p95']!s:>9} {lat['p99']!s:>9}"
                f" {stats['throughput_rps']!s:>9} {stats['cpu_us_per_request']!s:>9}"
            )
    si = doc["serving_inputs"]
    lines.append("")
    lines.append(
        f"adapter {si['bytes_per_adapter_f32']} B f32, {si['resident_adapters_per_gib']} per GiB,"
        f" provision {si['provision_wire_bytes']} B, adapt req/resp"
        f" {si['adapt_request_wire_bytes']}/{si['adapt_response_wire_bytes']} B"
    )
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m query_adapter_pilot.bench",
        description="Local loopback measurements (inputs only, no savings claim).",
    )
    defaults = BenchConfig()
    parser.add_argument("--requests", type=int, default=defaults.requests)
    parser.add_argument("--tenants", type=int, default=defaults.tenants)
    parser.add_argument("--cache-capacity", type=int, default=defaults.cache_capacity)
    parser.add_argument("--concurrency", type=int, default=defaults.concurrency)
    parser.add_argument("--json", metavar="OUT", help="write the full measurement JSON here")
    parser.add_argument(
        "--local-only", action="store_true", help="skip the miner (local baseline only)"
    )
    args = parser.parse_args(argv)
    cfg = BenchConfig(args.requests, args.tenants, args.cache_capacity, args.concurrency)
    try:
        doc = run(cfg, local_only=args.local_only)
    except ModuleNotFoundError as exc:
        print(f"miner slice unavailable ({exc}); rerun with --local-only", file=sys.stderr)
        return 2
    except ValueError as exc:
        parser.error(str(exc))
    if args.json:
        with open(args.json, "w") as f:
            json.dump(doc, f, indent=2)
            f.write("\n")
    print("\n".join(summary_lines(doc)))
    worst = [
        stats.get("min_reference_cos")
        for side in ("local", "miner")
        for stats in (doc.get(side) or {}).values()
        if isinstance(stats, dict) and "min_reference_cos" in stats
    ]
    if any(w is None or w < PARITY_MIN_COSINE for w in worst):
        print("parity below threshold in a measured phase", file=sys.stderr)
        return 1
    errors = sum(
        sum(stats.get("errors", {}).values())
        for stats in (doc.get("miner") or {}).values()
        if isinstance(stats, dict) and isinstance(stats.get("errors"), dict)
    )
    errors += int((doc.get("miner") or {}).get("provision", {}).get("errors", 0))
    if errors:
        print(f"{errors} miner request errors during measurement", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
