"""End-to-end pilot scenarios through the real miner and client slices.

Skipped until both ``query_adapter_pilot.miner`` and
``query_adapter_pilot.client`` exist. Then every proof scenario runs twice:
once with an in-process ``start_miner`` (fast, precise failures) and once
through ``scripts/prove.py``, which spawns the miner CLI exactly as an operator
would. The client always runs through the normal ``python -m
query_adapter_pilot run`` entry point.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

miner = pytest.importorskip("query_adapter_pilot.miner")
pytest.importorskip("query_adapter_pilot.client")

from query_adapter_pilot import bench  # noqa: E402
from query_adapter_pilot.contract import CONTRACT_VERSION, PARITY_MIN_COSINE  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _load_prove() -> ModuleType:
    if "qa_prove" in sys.modules:
        return sys.modules["qa_prove"]
    spec = importlib.util.spec_from_file_location("qa_prove", ROOT / "scripts" / "prove.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["qa_prove"] = module
    spec.loader.exec_module(module)
    return module


prove = _load_prove()


@contextlib.contextmanager
def inprocess_miner(scn: object, _workdir: Path, token_file: Path) -> Iterator[object]:
    config = miner.MinerConfig(
        host="127.0.0.1",
        port=0,
        token=token_file.read_text(),
        cache_capacity=scn.cache_capacity,  # type: ignore[attr-defined]
        max_tenants=scn.max_tenants,  # type: ignore[attr-defined]
        fault=scn.fault,  # type: ignore[attr-defined]
        slow_ms=scn.slow_ms,  # type: ignore[attr-defined]
        fault_seed=0,
    )
    with miner.start_miner(config) as running:
        yield prove.MinerHandle(url=str(running.url))


@pytest.mark.parametrize("name", [s.name for s in prove.SCENARIOS])
def test_scenario_in_process_miner(name: str, tmp_path: Path) -> None:
    result = prove.run_scenario(prove.scenario(name), tmp_path, inprocess_miner)
    assert result.ok, "\n".join(result.failures)


def test_prove_script_through_miner_cli(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "prove.py"), "--out-dir", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]
    n = len(prove.SCENARIOS)
    assert f"{n}/{n} scenarios passed" in proc.stdout
    assert all((tmp_path / s.name / "report.json").is_file() for s in prove.SCENARIOS)


def test_prove_relative_out_dir_through_miner_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative --out-dir must work: the spawned miner and client have a
    different cwd and must still find the token, ready file and report."""
    monkeypatch.chdir(tmp_path)
    assert prove.main(["--only", "invalid-nan", "--out-dir", "rel-out"]) == 0
    workdir = tmp_path / "rel-out" / "invalid-nan"
    assert (workdir / "report.json").is_file() and (workdir / "health.json").is_file()


def test_reused_out_dir_ignores_stale_ready_file_and_report(tmp_path: Path) -> None:
    """A ready file pointing at a dead port and a stale report from an earlier
    run must not be used: the harness waits for the new miner and evaluates
    the new report."""
    workdir = tmp_path / "invalid-nan"
    workdir.mkdir()
    with prove.refusing_loopback_url() as dead:
        (workdir / "miner-ready.json").write_text(
            json.dumps({"contract": CONTRACT_VERSION, "url": dead, "pid": 1})
        )
        (workdir / "report.json").write_text(json.dumps({"stale": True}))
        result = prove.run_scenario(prove.scenario("invalid-nan"), tmp_path)
        assert result.ok, "\n".join(result.failures)
    assert result.report is not None and "stale" not in result.report
    assert result.report["miner_url"] != dead


def test_bench_against_in_process_miner() -> None:
    cfg = bench.BenchConfig(requests=24, tenants=6, cache_capacity=2, concurrency=3)
    doc = bench.run(cfg)
    m = doc["miner"]
    assert m["provision"]["errors"] == 0 and m["provision"]["tenants"] == 6
    for phase in ("warm", "cold", "multi_tenant"):
        assert m[phase]["errors"] == {}, (phase, m[phase]["errors"])
        assert m[phase]["min_reference_cos"] >= PARITY_MIN_COSINE
        assert m[phase]["miner_compute_us"]["p50"] is not None
    assert m["warm"]["cache"]["hit"] == cfg.requests
    assert m["cold"]["cache"]["miss"] > 0 and m["cold"]["forces_misses"]
    assert m["multi_tenant"]["concurrency"] == 3
    assert m["health"]["cache"]["evictions"] > 0
    si = doc["serving_inputs"]
    assert si["adapt_request_wire_bytes"] > si["query_bytes_f32"]
    assert si["adapt_response_wire_bytes"] > si["query_bytes_f32"]
