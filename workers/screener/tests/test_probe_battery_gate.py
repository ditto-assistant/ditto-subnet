"""Report-only probe battery in the gate: bounded, killable, never authoritative."""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from ditto_screener import gate as gate_module
from ditto_screener.gate import BuildGate
from ditto_screener.probe_review.gate_shadow import container_names, summarize
from ditto_screener.review_settings import ProbeBatteryObservationRequest


def test_summary_keeps_only_well_formed_lead_names() -> None:
    report = {
        "format": "answer_line",
        "seed_first": True,
        "items": [{}] * 200,
        "leads": [
            {"item_id": "i7_identical_twice", "invariant": "I7/I6", "prompt": "x"},
            {"item_id": "Bad Id", "invariant": "I2"},
            {"item_id": "i2_pointer", "invariant": "I9"},
        ]
        + [{"item_id": f"i4_{n}", "invariant": "I4"} for n in range(40)],
    }
    summary = summarize(report, probe_count=9_999, elapsed_s=7_200)
    assert summary["status"] == "complete"
    assert summary["leads"][0] == {"item_id": "i7_identical_twice", "invariant": "I7"}
    assert all(set(lead) == {"item_id", "invariant"} for lead in summary["leads"])
    assert len(summary["leads"]) == 32
    assert summary["items_run"] == 128 and summary["probe_count"] == 512
    assert summary["battery_ms"] == 3_600_000
    ProbeBatteryObservationRequest.model_validate(
        {
            **summary,
            "attempt_id": uuid4(),
            "artifact_sha256": "a" * 64,
            "settings_revision": 1,
            "settings_scope": "*",
            "settings_checksum": "b" * 64,
        },
        strict=False,
    )


def test_uncalibrated_battery_reports_no_encoding() -> None:
    summary = summarize({"format": None}, probe_count=3, elapsed_s=1)
    assert summary["status"] == "calibration_failed" and summary["encoding"] is None


def _gate(make_config: Any) -> BuildGate:
    gate = BuildGate(make_config(), Mock(), policy=Mock(), journal=Mock())
    gate._run = AsyncMock(return_value=(0, ""))  # type: ignore[method-assign]
    return gate


def _child(monkeypatch: pytest.MonkeyPatch, script: str) -> list[list[str]]:
    seen: list[list[str]] = []
    real = asyncio.create_subprocess_exec

    async def fake(*args: str, **kwargs: Any) -> Any:
        seen.append([*args])
        assert set(kwargs["env"]) <= {
            "PATH",
            "HOME",
            "LANG",
            "TMPDIR",
            "XDG_RUNTIME_DIR",
            "PROBE_REVIEW_DOCKER_BIN",
            "DOCKER_HOST",
        }
        return await real(sys.executable, "-c", script, **kwargs)

    monkeypatch.setattr(gate_module.asyncio, "create_subprocess_exec", fake)
    return seen


async def test_battery_child_summary_is_returned_and_containers_removed(
    make_config: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {"status": "complete", "encoding": "answer_line", "leads": []}
    seen = _child(monkeypatch, f"print({json.dumps(json.dumps(payload))})")
    monkeypatch.setenv("PLATFORM_TOKEN_SHOULD_NOT_LEAK", "secret")
    gate = _gate(make_config)
    summary = await gate._run_probe_battery(
        image_id="sha256:" + "c" * 64,
        artifact_sha256="a" * 64,
        bench_version=13,
        budget=30,
    )
    assert summary["status"] == "complete" and "battery_ms" in summary
    assert seen[0][2:4] == ["ditto_screener.probe_review", "gate-battery"]
    token = seen[0][seen[0].index("--token") + 1]
    network, gateway, harness = container_names(token)
    calls = [call.args[0] for call in gate._run.await_args_list]  # type: ignore[attr-defined]
    assert ["rm", "-f", harness, gateway] in calls
    assert ["network", "rm", network] in calls


async def test_battery_child_is_killed_at_its_budget(
    make_config: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _child(monkeypatch, "import time; time.sleep(30)")
    gate = _gate(make_config)
    summary = await gate._run_probe_battery(
        image_id="sha256:" + "c" * 64,
        artifact_sha256="a" * 64,
        bench_version=13,
        budget=0.5,
    )
    assert summary["status"] == "timeout" and summary["leads"] == []


async def test_failed_child_reports_error_without_its_output(
    make_config: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _child(monkeypatch, "print('private prompt text'); raise SystemExit(3)")
    gate = _gate(make_config)
    summary = await gate._run_probe_battery(
        image_id="sha256:" + "c" * 64,
        artifact_sha256="a" * 64,
        bench_version=13,
        budget=30,
    )
    assert summary["status"] == "error"
    assert "private" not in json.dumps(summary)


def test_battery_report_pops_once(make_config: Any) -> None:
    gate = _gate(make_config)
    attempt = uuid4()
    gate._probe_battery_reports[attempt] = {"status": "complete"}
    assert gate.pop_probe_battery(attempt) == {"status": "complete"}
    assert gate.pop_probe_battery(attempt) is None
