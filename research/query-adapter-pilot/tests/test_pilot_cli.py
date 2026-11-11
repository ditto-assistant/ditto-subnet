"""``python -m query_adapter_pilot run`` end to end against the fake miner."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from test_client_fake_miner import TOKEN, FakeMiner, closed_port_url, user, workload_json

from query_adapter_pilot import pilot
from query_adapter_pilot.contract import CONTRACT_VERSION, PARITY_MIN_COSINE, Outcome, Source

REPORT_KEYS = {
    "contract",
    "kind",
    "run_id",
    "workload",
    "miner_url",
    "config",
    "provision",
    "requests",
    "summary",
    "ledger",
}
CONFIG_KEYS = {"verify_rate", "fallback", "timeout_ms"}
PROVISION_KEYS = {"user_ref", "tenant_id", "adapter_version"}
REQUEST_KEYS = {
    "index",
    "request_id",
    "user_ref",
    "tenant_id",
    "adapter_version",
    "outcome",
    "source",
    "miner_code",
    "latency_ms",
    "miner_compute_us",
    "cache",
    "verified",
    "reference_cos",
    "reference_max_abs",
}
SUMMARY_KEYS = {"total", "by_outcome", "by_source", "latency_ms", "parity"}
LATENCY_KEYS = {"miner_ok_p50", "miner_ok_p95", "end_to_end_p50", "end_to_end_p95"}
PARITY_KEYS = {"min_reference_cos", "max_reference_abs"}
LEDGER_KEYS = {"accepted_units", "duplicates_suppressed", "rejected", "by_outcome", "payouts"}

MIXED_WORKLOAD = workload_json(
    [
        user("syn-alice"),
        user("syn-bob", provision=[1, 2], authorized=2),
        user("syn-stale", provision=[1, 2], authorized=1),
        user("syn-missing", provision=[1], authorized=2),
        user("syn-nobody", provision=[1], authorized=None),
    ],
    [
        {"user_ref": "syn-alice", "query_seed": 1},
        {"user_ref": "syn-bob", "query_seed": 2},
        {"user_ref": "syn-alice", "query_seed": 3},
        {"replay_of": 0},
        {"user_ref": "syn-stale", "query_seed": 4},
        {"user_ref": "syn-missing", "query_seed": 5},
        {"user_ref": "syn-nobody", "query_seed": 6},
    ],
    name="mixed",
)


@pytest.fixture
def files(tmp_path: Path) -> dict[str, Path]:
    wl = tmp_path / "workload.json"
    wl.write_text(json.dumps(MIXED_WORKLOAD))
    token = tmp_path / "token"
    token.write_text(TOKEN + "\n")
    return {"workload": wl, "token": token, "out": tmp_path / "out" / "report.json"}


def _args(files: dict[str, Path], url: str, *extra: str) -> list[str]:
    return [
        "run",
        "--workload",
        str(files["workload"]),
        "--miner-url",
        url,
        "--token-file",
        str(files["token"]),
        "--out",
        str(files["out"]),
        *extra,
    ]


def _check_shape(report: dict[str, Any]) -> None:
    assert set(report) == REPORT_KEYS
    assert set(report["config"]) == CONFIG_KEYS
    for entry in report["provision"]:
        extra = set(entry) - PROVISION_KEYS
        assert set(entry) >= PROVISION_KEYS and extra in ({"status"}, {"code"})
    for row in report["requests"]:
        assert set(row) == REQUEST_KEYS
        assert isinstance(row["latency_ms"], float)
        assert isinstance(row["verified"], bool)
        assert row["cache"] in ("hit", "miss", None)
    assert set(report["summary"]) == SUMMARY_KEYS
    assert set(report["summary"]["latency_ms"]) == LATENCY_KEYS
    assert set(report["summary"]["parity"]) == PARITY_KEYS
    assert set(report["ledger"]) == LEDGER_KEYS


def test_module_run_end_to_end_writes_pinned_report(files: dict[str, Path]) -> None:
    src = Path(__file__).resolve().parents[1] / "src"
    env = {**os.environ, "PYTHONPATH": str(src)}
    with FakeMiner() as miner:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "query_adapter_pilot",
                *_args(files, miner.url, "--run-id", "e2e"),
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
            check=False,
        )
    assert proc.returncode == 0, proc.stderr
    assert len(proc.stdout.strip().splitlines()) == 1
    assert "payouts=none" in proc.stdout
    report = json.loads(files["out"].read_text())
    _check_shape(report)
    assert report["contract"] == CONTRACT_VERSION
    assert report["kind"] == "pilot-report"
    assert report["run_id"] == "e2e"
    assert report["workload"] == "mixed"
    assert report["miner_url"] == miner.url
    assert report["config"] == {"verify_rate": 1.0, "fallback": "local", "timeout_ms": 250.0}
    assert [e.get("status") for e in report["provision"]] == [200] * 7
    outcomes = [r["outcome"] for r in report["requests"]]
    assert outcomes == [
        Outcome.OK,
        Outcome.OK,
        Outcome.OK,
        Outcome.DUPLICATE,
        Outcome.REJECTED_STALE,
        Outcome.REJECTED_MISSING,
        Outcome.REJECTED_UNAUTHORIZED,
    ]
    rows = report["requests"]
    assert rows[3]["request_id"] == rows[0]["request_id"]
    assert rows[6]["source"] == Source.FALLBACK_IDENTITY and rows[6]["adapter_version"] is None
    assert rows[4]["miner_code"] == "STALE_VERSION"
    s = report["summary"]
    assert s["total"] == 7
    assert s["by_source"] == {
        Source.FALLBACK_IDENTITY: 1,
        Source.FALLBACK_LOCAL: 2,
        Source.MINER: 4,
    }
    assert all(v is not None for v in s["latency_ms"].values())
    assert s["parity"]["min_reference_cos"] >= PARITY_MIN_COSINE
    assert report["ledger"] == {
        "accepted_units": 3,
        "duplicates_suppressed": 1,
        "rejected": 3,
        "by_outcome": {
            Outcome.DUPLICATE: 1,
            Outcome.OK: 3,
            Outcome.REJECTED_MISSING: 1,
            Outcome.REJECTED_STALE: 1,
            Outcome.REJECTED_UNAUTHORIZED: 1,
        },
        "payouts": "none",
    }


def test_run_completes_with_exit_0_when_everything_falls_back(
    files: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    code = pilot.main(
        _args(files, closed_port_url(), "--fallback", "identity", "--timeout-ms", "50")
    )
    assert code == 0
    report = json.loads(files["out"].read_text())
    _check_shape(report)
    assert {e["code"] for e in report["provision"]} == {Outcome.UNAVAILABLE}
    assert set(report["summary"]["by_source"]) == {Source.FALLBACK_IDENTITY}
    assert report["summary"]["latency_ms"]["miner_ok_p50"] is None
    assert report["summary"]["latency_ms"]["miner_ok_p95"] is None
    assert report["ledger"]["accepted_units"] == 0
    assert report["config"]["timeout_ms"] == 50.0
    assert pilot.RUN_ID_RE.fullmatch(report["run_id"])
    assert len(capsys.readouterr().out.strip().splitlines()) == 1


def test_default_run_ids_are_random_and_valid() -> None:
    ids = {pilot.new_run_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(pilot.RUN_ID_RE.fullmatch(i) for i in ids)


@pytest.mark.parametrize(
    "case",
    ["non_loopback", "missing_token", "empty_token", "bad_workload", "missing_workload", "run_id"],
)
def test_usage_errors_exit_2(
    files: dict[str, Path], case: str, capsys: pytest.CaptureFixture[str]
) -> None:
    url = "http://127.0.0.1:9"
    extra: list[str] = []
    if case == "non_loopback":
        url = "http://192.0.2.10:8080"
    elif case == "missing_token":
        files["token"].unlink()
    elif case == "empty_token":
        files["token"].write_text("\n")
    elif case == "bad_workload":
        files["workload"].write_text(json.dumps({**MIXED_WORKLOAD, "kind": "nope"}))
    elif case == "missing_workload":
        files["workload"].unlink()
    elif case == "run_id":
        extra = ["--run-id", "Has.Dots"]
    assert pilot.main(_args(files, url, *extra)) == 2
    assert "error" in capsys.readouterr().err
    assert not files["out"].exists()


def test_argparse_errors_exit_2(files: dict[str, Path]) -> None:
    with pytest.raises(SystemExit) as exc:
        pilot.main(_args(files, "http://127.0.0.1:9", "--fallback", "remote"))
    assert exc.value.code == 2
