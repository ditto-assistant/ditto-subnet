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
    for encoding in (None, "", "Bad!"):
        summary = summarize({"format": encoding}, probe_count=3, elapsed_s=1)
        assert summary["status"] == "calibration_failed", encoding
        assert summary["encoding"] is None


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
            "SSL_CERT_FILE",
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "NO_PROXY",
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


async def test_worker_stamps_the_revision_the_gate_ran_under() -> None:
    from ditto_screener.worker import ScreenerWorker

    worker = ScreenerWorker.__new__(ScreenerWorker)
    worker._platform = Mock(submit_probe_battery=AsyncMock())
    agent, attempt = uuid4(), uuid4()
    summary = {
        "status": "complete",
        "encoding": "answer_line",
        "leads": [],
        "battery_ms": 10,
        "settings_revision": 7,
        "settings_scope": "ditto-screener-on",
        "settings_checksum": "b" * 64,
    }
    await worker._submit_probe_battery(
        agent_id=agent, attempt_id=attempt, artifact_sha256="a" * 64, summary=summary
    )
    request = worker._platform.submit_probe_battery.await_args.args[1]
    assert (request.settings_revision, request.settings_scope) == (
        7,
        "ditto-screener-on",
    )
    worker._platform.submit_probe_battery.reset_mock()
    await worker._submit_probe_battery(
        agent_id=agent,
        attempt_id=attempt,
        artifact_sha256="a" * 64,
        summary={**summary, "settings_revision": 0},
    )
    worker._platform.submit_probe_battery.assert_not_awaited()


# --- report-only reviewer agent -------------------------------------------


def test_review_summary_keeps_only_bounded_identifiers() -> None:
    from ditto_screener.probe_review.gate_shadow import summarize_review

    result = {
        "replay": {
            "verified": True,
            "findings": [
                {
                    "invariant": "I7",
                    "verified": True,
                    "claim": "private text never leaves the node",
                    "source_citations": ["src/tma/tools/executor.py:172", "x y:1"],
                },
                {"invariant": "I9", "verified": True},
            ]
            + [{"invariant": "I2", "verified": False}] * 6,
        },
        "requests": 9,
        "prompt_tokens": 50_000,
        "completion_tokens": 3_000,
        "cost_usd": 0.31,
        "wall_s": 41.5,
    }
    summary = summarize_review(result, status="complete")
    assert summary["verified"] and summary["invariants"] == ["I7"]
    assert summary["findings"][0]["citations"] == ["src/tma/tools/executor.py:172"]
    assert len(summary["findings"]) == 4
    assert "private" not in json.dumps(summary)
    from ditto_screener.review_settings import ProbeReviewSummary

    ProbeReviewSummary.model_validate(summary, strict=False)


class _FakeSession:
    def __init__(self, **kwargs: Any) -> None:
        self.root = kwargs["root"]
        self.image = kwargs["image"]
        self.probe_count = 3
        self.events: list[str] = []

    def start(self) -> None:
        self.events.append("start")

    def stop(self) -> None:
        self.events.append("stop")

    def restart_harness(self) -> None:
        self.events.append("restart")


def _fake_child(
    monkeypatch: pytest.MonkeyPatch, *, encoding: str | None = "answer_line"
):
    from ditto_screener.probe_review import gate_shadow

    sessions: list[_FakeSession] = []

    def session(**kwargs: Any) -> _FakeSession:
        sessions.append(_FakeSession(**kwargs))
        return sessions[-1]

    monkeypatch.setattr(gate_shadow, "Session", session)
    monkeypatch.setattr(
        gate_shadow.battery,
        "run_battery",
        lambda _s: {"format": encoding, "items": [], "leads": []},
    )
    monkeypatch.setattr(gate_shadow.tools, "extract_source", lambda *_a: 1)
    monkeypatch.setattr(gate_shadow.tools, "triage", lambda _a: {"leads": []})
    return gate_shadow, sessions


def test_gate_child_emits_battery_before_review(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    gate_shadow, sessions = _fake_child(monkeypatch)
    seen: dict[str, Any] = {}

    class Completer:
        def __enter__(self) -> str:
            return "completer"

        def __exit__(self, *_: object) -> None:
            seen["closed"] = True

    monkeypatch.setattr(
        gate_shadow.agent_loop, "http_completer", lambda *_a, **_k: Completer()
    )

    def review(_session: Any, **kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"complete": True, "replay": {"verified": False, "findings": []}}

    monkeypatch.setattr(gate_shadow.agent_loop, "review", review)
    lines: list[dict[str, Any]] = []
    spec = gate_shadow.ReviewSpec(
        key_file="/k", base_url="u", model="m", max_cost_usd=0.5, timeout_seconds=300
    )
    gate_shadow.run_gate_battery(
        image="img",
        root=str(tmp_path),
        artifact_sha256="a" * 64,
        bench_version=13,
        token="t",
        emit=lines.append,
        archive=str(tmp_path / "a.tar"),
        review=spec,
        deadline_seconds=400,
    )
    assert lines[0]["status"] == "complete" and "review" not in lines[0]
    assert lines[1]["review"]["status"] == "complete"
    assert seen["ledger"].max_cost_usd == 0.5 and seen["closed"]
    assert seen["deadline_seconds"] <= 300 - 45
    assert sessions[0].events == ["start", "restart", "stop"]
    assert (tmp_path / "prepare.json").exists()


def test_gate_child_skips_review_without_time_or_calibration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    spec_args = {"key_file": "/k", "base_url": "u", "model": "m", "max_cost_usd": 1.0}
    for encoding, deadline in (("answer_line", 90.0), (None, 900.0)):
        gate_shadow, sessions = _fake_child(monkeypatch, encoding=encoding)
        monkeypatch.setattr(
            gate_shadow.agent_loop,
            "review",
            lambda *_a, **_k: pytest.fail("review must not run"),
        )
        lines: list[dict[str, Any]] = []
        gate_shadow.run_gate_battery(
            image="img",
            root=str(tmp_path),
            artifact_sha256="a" * 64,
            bench_version=13,
            token="t",
            emit=lines.append,
            archive=str(tmp_path / "a.tar"),
            review=gate_shadow.ReviewSpec(timeout_seconds=420, **spec_args),
            deadline_seconds=deadline,
        )
        assert lines[1]["review"]["status"] == "skipped"
        assert sessions[-1].events[-1] == "stop"


def test_gate_child_review_failure_reports_error_without_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    gate_shadow, _ = _fake_child(monkeypatch)

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("secret detail")

    monkeypatch.setattr(gate_shadow.agent_loop, "http_completer", boom)
    lines: list[dict[str, Any]] = []
    gate_shadow.run_gate_battery(
        image="img",
        root=str(tmp_path),
        artifact_sha256="a" * 64,
        bench_version=13,
        token="t",
        emit=lines.append,
        archive=str(tmp_path / "a.tar"),
        review=gate_shadow.ReviewSpec(
            key_file="/k", base_url="u", model="m", max_cost_usd=1, timeout_seconds=420
        ),
        deadline_seconds=900,
    )
    assert lines[1]["review"]["status"] == "error"
    # Stage and exception class only; the message never leaves the child.
    assert lines[1]["review"]["error"] == "review:RuntimeError"
    assert "secret" not in json.dumps(lines)


async def test_gate_keeps_the_battery_when_the_review_is_killed(
    make_config: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    key = tmp_path / "key"
    key.write_text("k")
    line = json.dumps({"status": "complete", "encoding": "answer_line", "leads": []})
    seen = _child(
        monkeypatch,
        f"import sys,time; print({json.dumps(line)}, flush=True); time.sleep(30)",
    )
    gate = BuildGate(
        make_config(probe_review_key_file=str(key)),
        Mock(),
        policy=Mock(),
        journal=Mock(),
    )
    gate._run = AsyncMock(return_value=(0, ""))  # type: ignore[method-assign]
    summary = await gate._run_probe_battery(
        image_id="sha256:" + "c" * 64,
        artifact_sha256="a" * 64,
        bench_version=13,
        budget=1.5,
        archive_path=str(tmp_path / "a.tar"),
    )
    assert summary["status"] == "complete"
    assert summary["review"]["status"] == "timeout"
    args = seen[0]
    assert args[args.index("--review-key-file") + 1] == str(key)
    assert "--archive" in args


async def test_gate_without_archive_passes_no_key(
    make_config: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    key = tmp_path / "key"
    key.write_text("k")
    payload = {"status": "complete", "encoding": "answer_line", "leads": []}
    seen = _child(monkeypatch, f"print({json.dumps(json.dumps(payload))})")
    gate = BuildGate(
        make_config(probe_review_key_file=str(key)),
        Mock(),
        policy=Mock(),
        journal=Mock(),
    )
    gate._run = AsyncMock(return_value=(0, ""))  # type: ignore[method-assign]
    summary = await gate._run_probe_battery(
        image_id="sha256:" + "c" * 64,
        artifact_sha256="a" * 64,
        bench_version=13,
        budget=30,
    )
    assert "review" not in summary
    assert "--review-key-file" not in seen[0]


async def test_battery_keeps_its_own_cap_when_the_reviewer_is_on(
    make_config: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    key = tmp_path / "key"
    key.write_text("k")
    _child(monkeypatch, "import time; time.sleep(30)")
    gate = BuildGate(
        make_config(probe_review_key_file=str(key)),
        Mock(),
        policy=Mock(),
        journal=Mock(),
    )
    gate._run = AsyncMock(return_value=(0, ""))  # type: ignore[method-assign]
    loop = asyncio.get_running_loop()
    started = loop.time()
    summary = await gate._run_probe_battery(
        image_id="sha256:" + "c" * 64,
        artifact_sha256="a" * 64,
        bench_version=13,
        budget=20,
        battery_budget=0.5,
        archive_path=str(tmp_path / "a.tar"),
    )
    assert loop.time() - started < 10, (
        "the review allowance must not extend the battery"
    )
    assert summary["status"] == "timeout"
    # The child never reached the review phase.
    assert summary["review"]["status"] == "skipped"


def test_review_timeout_floor_leaves_a_usable_review(make_config: Any) -> None:
    from pydantic import ValidationError

    from ditto_screener.probe_review import gate_shadow
    from ditto_screener.review_settings import ReviewSettings, bootstrap_review_settings

    valid = bootstrap_review_settings(make_config()).settings.model_dump(mode="json")
    ok = ReviewSettings.model_validate({**valid, "probe_review_timeout_seconds": 120})
    assert ok.probe_review_timeout_seconds - gate_shadow._REVIEW_TAIL_S >= (
        gate_shadow._MIN_REVIEW_S
    )
    with pytest.raises(ValidationError, match="probe_review_timeout_seconds"):
        ReviewSettings.model_validate({**valid, "probe_review_timeout_seconds": 90})


async def test_cancelled_battery_kills_its_child(
    make_config: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    pid_file = tmp_path / "pid"
    _child(
        monkeypatch,
        f"import os,time; open({str(pid_file)!r},'w').write(str(os.getpid()));"
        " time.sleep(60)",
    )
    gate = _gate(make_config)
    task = asyncio.create_task(
        gate._run_probe_battery(
            image_id="sha256:" + "c" * 64,
            artifact_sha256="a" * 64,
            bench_version=13,
            budget=60,
        )
    )
    for _ in range(100):
        if pid_file.exists() and pid_file.read_text():
            break
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    import os

    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_review_summary_rejects_nan_and_reviewer_without_battery(
    make_config: Any,
) -> None:
    from pydantic import ValidationError

    from ditto_screener.review_settings import (
        ProbeReviewSummary,
        ReviewSettings,
        bootstrap_review_settings,
    )

    with pytest.raises(ValidationError):
        ProbeReviewSummary.model_validate(
            {"status": "complete", "cost_usd": float("nan")}, strict=False
        )
    valid = bootstrap_review_settings(make_config()).settings.model_dump(mode="json")
    with pytest.raises(ValidationError, match="requires the probe battery"):
        ReviewSettings.model_validate({**valid, "probe_review_mode": "shadow"})
    both = {**valid, "probe_review_mode": "shadow", "probe_battery_mode": "shadow"}
    assert ReviewSettings.model_validate(both).probe_review_mode == "shadow"


def test_extract_source_writes_only_contained_regular_files(tmp_path: Any) -> None:
    import io
    import tarfile

    from ditto_screener.probe_review.tools import extract_source

    archive = tmp_path / "a.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for name, kind in (
            ("ok/agent.py", tarfile.REGTYPE),
            ("../escape.py", tarfile.REGTYPE),
            ("/abs.py", tarfile.REGTYPE),
            ("link", tarfile.SYMTYPE),
            ("link/through.py", tarfile.REGTYPE),
            ("clash", tarfile.REGTYPE),
            ("clash/inner.py", tarfile.REGTYPE),
            ("dir/file.py", tarfile.REGTYPE),
            ("dir", tarfile.REGTYPE),
        ):
            info = tarfile.TarInfo(name)
            info.type = kind
            data = b"x = 1\n"
            if kind == tarfile.SYMTYPE:
                info.linkname = str(tmp_path / "outside")
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    dest = tmp_path / "src"
    extract_source(archive, dest)
    assert (dest / "ok" / "agent.py").exists()
    assert not (tmp_path / "escape.py").exists()
    assert not (dest / "link").is_symlink()
    assert not (tmp_path / "outside").exists()
    # Conflicting file/directory names keep the first and do not abort.
    assert (dest / "clash").is_file() and (dest / "dir" / "file.py").is_file()


def test_error_label_is_bounded_and_carries_http_status() -> None:
    import httpx

    from ditto_screener.probe_review.gate_shadow import _error_label

    request = httpx.Request("POST", "https://router.example/v1/chat/completions")
    response = httpx.Response(402, request=request, text="secret detail")
    error = httpx.HTTPStatusError("secret detail", request=request, response=response)
    assert _error_label("review", error) == "review:HTTPStatusError:402"
    assert _error_label("prepare", KeyError("x")) == "prepare:KeyError"


def test_review_error_label_survives_the_wire_model() -> None:
    from pydantic import ValidationError

    from ditto_screener.review_settings import ProbeReviewSummary

    kept = ProbeReviewSummary.model_validate(
        {"status": "error", "error": "review:HTTPStatusError:402"}, strict=False
    )
    assert kept.error == "review:HTTPStatusError:402"
    with pytest.raises(ValidationError):
        ProbeReviewSummary.model_validate(
            {"status": "error", "error": "review: leaked text"}, strict=False
        )
