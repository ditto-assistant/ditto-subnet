"""scripts/prove.py expectation logic against hand-built reports.

These run without the miner or client slices: each scenario gets a report a
correct client would write, which must pass, and targeted corruptions of it,
which must fail. The process half of prove.py is covered by test_e2e_*.
"""

from __future__ import annotations

import contextlib
import copy
import importlib.util
import json
import os
import socket
import stat
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from query_adapter_pilot.contract import (
    CONTRACT_VERSION,
    OUTCOMES,
    REQUEST_ID_RE,
    Outcome,
    Source,
    make_request_id,
)
from query_adapter_pilot.workload import Workload

ROOT = Path(__file__).resolve().parents[1]


def _load_prove() -> ModuleType:
    if "qa_prove" in sys.modules:
        return sys.modules["qa_prove"]
    spec = importlib.util.spec_from_file_location("qa_prove", ROOT / "scripts" / "prove.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["qa_prove"] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop("qa_prove", None)  # never leave a half-initialized module
        raise
    return module


prove = _load_prove()
NAMES = [s.name for s in prove.SCENARIOS]

EXPECTED: dict[str, tuple[str, str]] = {
    "happy": (Outcome.OK, Source.MINER),
    "isolation": (Outcome.OK, Source.MINER),
    "multi-tenant": (Outcome.OK, Source.MINER),
    "incorrect": (Outcome.REJECTED_INCORRECT, Source.FALLBACK_LOCAL),
    "isolation-swap": (Outcome.REJECTED_INCORRECT, Source.FALLBACK_LOCAL),
    "invalid-nan": (Outcome.REJECTED_INVALID, Source.FALLBACK_LOCAL),
    "invalid-bad-norm": (Outcome.REJECTED_INVALID, Source.FALLBACK_LOCAL),
    "invalid-wrong-echo": (Outcome.REJECTED_INVALID, Source.FALLBACK_LOCAL),
    "unavailable": (Outcome.UNAVAILABLE, Source.FALLBACK_LOCAL),
    "timeout": (Outcome.TIMEOUT, Source.FALLBACK_LOCAL),
    "stale": (Outcome.REJECTED_STALE, Source.FALLBACK_LOCAL),
    "missing": (Outcome.REJECTED_MISSING, Source.FALLBACK_LOCAL),
}


def good_report(name: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The report and health a correct client and miner would produce."""
    scn = prove.scenario(name)
    wl = Workload.load(scn.workload_path)
    rows: list[dict[str, Any]] = []
    for r in wl.requests:
        user = wl.user(r.user_ref)
        seq = r.index if r.replay_of is None else r.replay_of
        if name == "unauthorized":
            outcome, source = (
                (Outcome.OK, Source.MINER)
                if user.authorized_version is not None
                else (Outcome.REJECTED_UNAUTHORIZED, Source.FALLBACK_IDENTITY)
            )
        elif name == "replay":
            outcome = Outcome.OK if r.replay_of is None else Outcome.DUPLICATE
            source = Source.MINER
        else:
            outcome, source = EXPECTED[name]
        routed = outcome != Outcome.REJECTED_UNAUTHORIZED
        rows.append(
            {
                "index": r.index,
                "request_id": make_request_id(scn.run_id, user.user_ref, seq),
                "user_ref": user.user_ref,
                "tenant_id": user.user_ref,
                "adapter_version": user.authorized_version,
                "outcome": outcome,
                "source": source,
                "miner_code": None,
                "latency_ms": 1.0 + r.index / 100,
                "miner_compute_us": 50 if source == Source.MINER else None,
                "cache": ("hit" if r.index % 3 else "miss") if source == Source.MINER else None,
                "verified": outcome in (Outcome.OK, Outcome.REJECTED_INCORRECT, Outcome.DUPLICATE),
                "reference_cos": 1.0 if routed else None,
                "reference_max_abs": 0.0 if routed else None,
            }
        )
    accepted = sum(1 for row in rows if row["outcome"] == Outcome.OK)
    dupes = sum(1 for row in rows if row["outcome"] == Outcome.DUPLICATE)
    report = {
        "contract": CONTRACT_VERSION,
        "kind": "pilot-report",
        "run_id": scn.run_id,
        "workload": wl.name,
        "miner_url": "http://127.0.0.1:1",
        "config": {"verify_rate": 1.0, "fallback": "local", "timeout_ms": scn.timeout_ms},
        "provision": [],
        "requests": rows,
        "summary": {
            "total": len(rows),
            "by_outcome": {},
            "by_source": {},
            "latency_ms": {"end_to_end_p50": 1.2, "end_to_end_p95": 1.9},
            "parity": {"min_reference_cos": 1.0, "max_reference_abs": 0.0},
        },
        "ledger": {
            "accepted_units": accepted,
            "duplicates_suppressed": dupes,
            "rejected": len(rows) - accepted - dupes,
            "by_outcome": {},
            "payouts": "none",
        },
    }
    if scn.no_miner:
        return report, None
    routed_n = sum(1 for row in rows if row["outcome"] != Outcome.REJECTED_UNAUTHORIZED)
    health = {
        "contract": CONTRACT_VERSION,
        "status": "ok",
        "fault": scn.fault,
        "cache": {
            "capacity": scn.cache_capacity,
            "entries": min(scn.cache_capacity, len(wl.users)),
            "hits": 16,
            "misses": 64,
            "evictions": 56,
        },
        "requests": {"adapt_ok": routed_n, "errors": {}},
    }
    return report, health


@pytest.mark.parametrize("name", NAMES)
def test_correct_report_passes(name: str) -> None:
    report, health = good_report(name)
    assert prove.evaluate(prove.scenario(name), report, health) == []


def _fails(name: str, report: dict[str, Any], health: dict[str, Any] | None, needle: str) -> None:
    failures = prove.evaluate(prove.scenario(name), report, health)
    assert failures, f"{name}: corruption not detected"
    assert any(needle in f for f in failures), failures


@pytest.mark.parametrize(
    "name", ["incorrect", "isolation-swap", "invalid-nan", "stale", "missing", "timeout"]
)
def test_accepting_a_bad_vector_fails(name: str) -> None:
    report, health = good_report(name)
    report["requests"][0] |= {"outcome": Outcome.OK, "source": Source.MINER}
    report["ledger"]["accepted_units"] = 1
    _fails(name, report, health, "request 0")


def test_happy_detects_rejection_parity_and_extra_traffic() -> None:
    report, health = good_report("happy")
    bad = copy.deepcopy(report)
    bad["requests"][3]["outcome"] = Outcome.REJECTED_INCORRECT
    _fails("happy", bad, health, "request 3")
    bad = copy.deepcopy(report)
    bad["summary"]["parity"]["min_reference_cos"] = 0.999
    _fails("happy", bad, health, "min_reference_cos")
    bad = copy.deepcopy(report)
    bad["requests"][5]["verified"] = False
    _fails("happy", bad, health, "not verified")
    assert health is not None
    noisy = copy.deepcopy(health)
    noisy["requests"]["adapt_ok"] += 2
    _fails("happy", report, noisy, "adapt traffic")
    _fails("happy", report, None, "health unavailable")


def test_fault_mode_must_match() -> None:
    report, health = good_report("incorrect")
    assert health is not None
    _fails("incorrect", report, health | {"fault": "none"}, "fault")


def test_unauthorized_must_not_reach_miner() -> None:
    report, health = good_report("unauthorized")
    assert health is not None
    leaked = copy.deepcopy(health)
    leaked["requests"]["errors"] = {"MISSING_VERSION": 1}
    _fails("unauthorized", report, leaked, "adapt traffic")
    bad = copy.deepcopy(report)
    victim = next(r for r in bad["requests"] if r["outcome"] == Outcome.REJECTED_UNAUTHORIZED)
    victim["miner_code"] = "MISSING_VERSION"
    _fails("unauthorized", bad, health, "miner fields")
    bad = copy.deepcopy(report)
    victim = next(r for r in bad["requests"] if r["outcome"] == Outcome.REJECTED_UNAUTHORIZED)
    victim["source"] = Source.FALLBACK_LOCAL
    _fails("unauthorized", bad, health, victim["user_ref"])


def test_replay_ledger_and_ids() -> None:
    report, health = good_report("replay")
    bad = copy.deepcopy(report)
    bad["ledger"]["accepted_units"] = 10
    _fails("replay", bad, health, "accepted_units")
    bad = copy.deepcopy(report)
    bad["ledger"]["duplicates_suppressed"] = 0
    _fails("replay", bad, health, "duplicates_suppressed")
    bad = copy.deepcopy(report)
    replay = next(r for r in bad["requests"] if r["outcome"] == Outcome.DUPLICATE)
    replay["request_id"] = replay["request_id"][:-1] + "9"
    _fails("replay", bad, health, "request_id differs")


def test_isolation_detects_shared_tenant_and_low_tenant_parity() -> None:
    report, health = good_report("isolation")
    bad = copy.deepcopy(report)
    first = bad["requests"][0]["tenant_id"]
    for r in bad["requests"]:
        r["tenant_id"] = first
    _fails("isolation", bad, health, "share a tenant id")
    bad = copy.deepcopy(report)
    bad["requests"][1]["reference_cos"] = 0.5
    _fails("isolation", bad, health, "per-tenant min cos")


def test_multi_tenant_requires_cache_churn() -> None:
    report, health = good_report("multi-tenant")
    assert health is not None
    for key in ("evictions", "misses", "hits"):
        cold = copy.deepcopy(health)
        cold["cache"][key] = 0
        _fails("multi-tenant", report, cold, f"cache.{key}")
    bad = copy.deepcopy(report)
    for r in bad["requests"]:
        r["cache"] = "miss"
    _fails("multi-tenant", bad, health, "cache markers")


def test_report_shape_is_enforced() -> None:
    report, health = good_report("happy")
    for mutate, needle in (
        (lambda r: r.update(contract="query-adapter/v0"), "contract"),
        (lambda r: r.update(kind="other"), "kind"),
        (lambda r: r["requests"].pop(), "requests"),
        (lambda r: r["ledger"].update(payouts="tao"), "payouts"),
        (lambda r: r["requests"][0].update(outcome="made_up"), "unknown outcomes"),
        (lambda r: r["requests"][0].update(user_ref="real-user"), "non-synthetic"),
    ):
        bad = copy.deepcopy(report)
        mutate(bad)
        _fails("happy", bad, health, needle)


def test_scenarios_cover_the_negative_case_matrix() -> None:
    assert len(set(NAMES)) == len(NAMES)
    outcomes = {EXPECTED[n][0] for n in EXPECTED} | {
        Outcome.REJECTED_UNAUTHORIZED,
        Outcome.DUPLICATE,
    }
    assert outcomes <= OUTCOMES
    assert outcomes >= OUTCOMES - {Outcome.MINER_ERROR}
    faults = {s.fault for s in prove.SCENARIOS}
    assert faults == {"none", "perturb", "nan", "bad-norm", "wrong-echo", "slow", "swap-tenant"}
    for s in prove.SCENARIOS:
        assert s.workload_path.is_file()
        assert REQUEST_ID_RE.fullmatch(make_request_id(s.run_id, "syn-x", 0))
    timeout = prove.scenario("timeout")
    assert timeout.slow_ms > timeout.timeout_ms
    assert prove.scenario("multi-tenant").cache_capacity == 8


def test_table_and_rows() -> None:
    results = []
    for name in ("happy", "replay"):
        report, health = good_report(name)
        res = prove.Result(prove.scenario(name), report=report, health=health)
        results.append(res)
    results.append(prove.Result(prove.scenario("timeout"), failures=["boom"]))
    text = prove.table(results)
    lines = text.splitlines()
    assert lines[0].split()[:2] == ["scenario", "outcomes"]
    assert "ok=30" in lines[2] and "miner=30" in lines[2] and lines[2].endswith("PASS")
    assert "duplicate=4,ok=6" in lines[3]
    assert lines[4].startswith("timeout") and lines[4].endswith("FAIL")


def test_process_helpers(tmp_path: Path) -> None:
    path, token = prove.write_token(tmp_path)
    assert path.read_text() == token and token.startswith("syn-pilot-")
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with prove.refusing_loopback_url() as url:
        assert url.startswith("http://127.0.0.1:")
        assert prove.fetch_health(url, token) is None


def _attempt(port: int) -> None:
    """One HTTP-shaped attempt; raises whatever the connection fails with."""
    with socket.create_connection(("127.0.0.1", port), timeout=2) as conn:
        conn.sendall(b"GET /v1/health HTTP/1.1\r\nHost: x\r\n\r\n")
        if not conn.recv(1):
            raise ConnectionResetError("closed without a response")


def test_unavailable_port_stays_held_while_in_use() -> None:
    """For the whole block every connection fails fast (refused or reset, not
    a timeout) and no other socket can take the port (no bind-then-release
    race); the port is released afterwards."""
    with prove.refusing_loopback_url() as url:
        port = int(url.rsplit(":", 1)[1])
        for _ in range(3):
            started = time.monotonic()
            with pytest.raises((ConnectionRefusedError, ConnectionResetError)):
                _attempt(port)
            assert time.monotonic() - started < 1.0
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as rival, pytest.raises(OSError):
            rival.bind(("127.0.0.1", port))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as later:
        later.bind(("127.0.0.1", port))


def test_stale_artifacts_are_cleared_before_launch(tmp_path: Path) -> None:
    """A reused --out-dir must not feed an earlier run's ready file, report,
    or health snapshot into this run."""
    workdir = tmp_path / "happy"
    workdir.mkdir()
    stale_report, stale_health = good_report("happy")
    (workdir / "report.json").write_text(json.dumps(stale_report))
    (workdir / "health.json").write_text(json.dumps(stale_health))
    (workdir / "miner-ready.json").write_text(
        json.dumps({"contract": CONTRACT_VERSION, "url": "http://127.0.0.1:9", "pid": 1})
    )
    present_at_launch: dict[str, bool] = {}

    @contextlib.contextmanager
    def recording_launcher(_scn: Any, wd: Path, _token: Path) -> Iterator[Any]:
        present_at_launch.update({n: (wd / n).exists() for n in prove.SCENARIO_ARTIFACTS})
        raise RuntimeError("stop after inspecting the workdir")
        yield  # pragma: no cover

    res = prove.run_scenario(prove.scenario("happy"), tmp_path, recording_launcher)
    assert present_at_launch["miner-ready.json"] is False
    assert present_at_launch["report.json"] is False
    assert present_at_launch["health.json"] is False
    assert present_at_launch["pilot-token"] is True  # freshly written for this run
    assert res.report is None and not res.ok
    assert not (workdir / "report.json").exists()


def test_harness_reports_missing_client_cleanly(tmp_path: Path) -> None:
    """unavailable needs no miner; whatever the client slice's state, the
    harness returns a Result instead of raising."""
    res = prove.run_scenario(prove.scenario("unavailable"), tmp_path)
    assert isinstance(res, prove.Result)
    if importlib.util.find_spec("query_adapter_pilot.client") is None:
        assert not res.ok and any("client exited" in f for f in res.failures)


def test_relative_out_dir_is_resolved_before_spawning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The miner and client run with a different cwd, so every path handed to
    them must already be absolute even when --out-dir is relative."""
    monkeypatch.chdir(tmp_path)
    seen: list[Path] = []

    @contextlib.contextmanager
    def recording_launcher(_scn: Any, workdir: Path, token_file: Path) -> Iterator[Any]:
        seen.extend([workdir, token_file])
        raise RuntimeError("stop after recording paths")
        yield  # pragma: no cover

    res = prove.run_scenario(prove.scenario("happy"), Path("rel-out"), recording_launcher)
    assert seen and all(p.is_absolute() for p in seen)
    assert seen[0] == tmp_path.resolve() / "rel-out" / "happy"
    assert res.report_path is not None and res.report_path.is_absolute()
    assert (tmp_path / "rel-out" / "happy" / "pilot-token").is_file()
