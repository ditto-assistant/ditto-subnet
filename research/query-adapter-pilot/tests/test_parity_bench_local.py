"""The local-baseline half of bench.py: correct, bounded, and claim-free."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from query_adapter_pilot import bench
from query_adapter_pilot.contract import CONTRACT_VERSION, PARITY_MIN_COSINE, ROUTE_HEALTH


def test_timeout_drops_the_connection() -> None:
    """A socket TimeoutError (an OSError, not a ConnectionError) must close
    and reset the per-thread connection rather than leave it half-used."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(8)  # completes the TCP handshake, never answers
        client = bench.LoopbackClient(f"http://127.0.0.1:{srv.getsockname()[1]}", "t", 0.2)
        with pytest.raises(TimeoutError):
            client.call("GET", ROUTE_HEALTH, None)
        assert getattr(client._local, "conn", None) is None


def test_wire_sizes_count_successful_round_trips_only() -> None:
    sizes: dict[str, list[int]] = {"adapt_request": [], "adapt_response": []}
    bench.record_wire_sizes(sizes, bench.Sample(1, error="MISSING_VERSION"), 4353, 120)
    bench.record_wire_sizes(sizes, bench.Sample(1, error="TimeoutError"), 4353, 0)
    assert sizes == {"adapt_request": [], "adapt_response": []}
    bench.record_wire_sizes(sizes, bench.Sample(1, compute_us=10, cache="hit"), 4353, 4372)
    assert sizes == {"adapt_request": [4353], "adapt_response": [4372]}


def test_local_baseline_is_at_parity_and_counts_inputs() -> None:
    cfg = bench.BenchConfig(requests=12, tenants=3, cache_capacity=2, concurrency=2)
    doc = bench.run(cfg, local_only=True)
    assert doc["contract"] == CONTRACT_VERSION and doc["kind"] == bench.BENCH_KIND
    assert doc["economic_claims"] == "none" and "not economic savings" in doc["note"]
    assert doc["miner"] is None
    for phase in ("warm", "cold", "warm_concurrent"):
        stats = doc["local"][phase]
        assert stats["requests"] == 12 and stats["errors"] == {}
        assert stats["min_reference_cos"] >= PARITY_MIN_COSINE
        lat = stats["latency_ms"]
        assert 0 < lat["p50"] <= lat["p95"] <= lat["p99"] <= lat["max"]
        assert stats["throughput_rps"] > 0 and stats["cpu_us_per_request"] >= 0
    assert doc["local"]["warm_concurrent"]["concurrency"] == 2
    si = doc["serving_inputs"]
    assert si["params_per_adapter"] == 768 * 128 + 128 + 128 * 768 + 768
    assert si["bytes_per_adapter_f32"] == 4 * si["params_per_adapter"] == 790_016
    assert si["resident_adapters_per_gib"] == 2**30 // 790_016
    assert si["provision_wire_bytes"] > si["bytes_per_adapter_f32"]  # base64 + JSON
    assert set(doc["cpu_us_per_request"]) == {"local_warm", "local_cold", "local_warm_concurrent"}


def test_phase_stats_math() -> None:
    samples = [bench.Sample(latency_ns=n * 1_000_000, compute_us=n * 100) for n in (1, 2, 3, 4)]
    samples.append(bench.Sample(latency_ns=9_000_000, error="TIMEOUT"))
    stats = bench.phase_stats(samples, wall_ns=1_000_000_000, cpu_ns=5_000_000, concurrency=1)
    assert stats["requests"] == 5 and stats["errors"] == {"TIMEOUT": 1}
    assert stats["latency_ms"]["p50"] == 2.5 and stats["latency_ms"]["max"] == 4.0
    assert stats["throughput_rps"] == 5.0 and stats["cpu_us_per_request"] == 1000.0
    assert stats["miner_compute_us"]["p50"] == 250.0
    assert stats["overhead_us_p50"] == 2500.0 - 250.0


def test_loopback_client_refuses_remote_hosts() -> None:
    with pytest.raises(ValueError):
        bench.LoopbackClient("http://10.0.0.5:8080", "t", 1.0)
    with pytest.raises(ValueError):
        bench.LoopbackClient("http://127.0.0.1", "t", 1.0)
    bench.LoopbackClient("http://127.0.0.1:9", "t", 1.0)


def test_rejects_degenerate_config() -> None:
    with pytest.raises(ValueError):
        bench.run(bench.BenchConfig(requests=0), local_only=True)


def test_cli_local_only_writes_json(tmp_path: Path) -> None:
    out = tmp_path / "bench.json"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "query_adapter_pilot.bench",
            "--local-only",
            "--requests",
            "8",
            "--tenants",
            "2",
            "--json",
            str(out),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "not economic savings" in proc.stdout
    doc = json.loads(out.read_text())
    assert doc["config"]["requests"] == 8 and doc["local"]["cold"]["requests"] == 8
