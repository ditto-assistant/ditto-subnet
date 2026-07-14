"""Tests for the validator's dittobench-api request contract."""

from __future__ import annotations

import json
from dataclasses import asdict
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from ditto.validator.dittobench import (
    DittobenchClient,
    DittobenchProgressSnapshot,
    safe_progress_snapshot,
)
from ditto.validator.errors import DittobenchError


@pytest.mark.parametrize(
    ("status", "stage", "expected_counts"),
    [
        ("queued", "preparing", (None, None)),
        ("building", "building_harness", (None, None)),
        ("generating", "starting_harness", (None, None)),
        ("seeding", "running_benchmark", (None, None)),
        ("running", "running_benchmark", (51, 114)),
        ("scoring", "finalizing", (114, 114)),
        ("done", "finalizing", (114, 114)),
        ("failed", "failed_retrying", (None, None)),
    ],
)
def test_safe_progress_status_mapping(
    status: str, stage: str, expected_counts: tuple[int | None, int | None]
) -> None:
    done = 114 if status in {"scoring", "done"} else 51
    snapshot = safe_progress_snapshot(
        {
            "status": status,
            "progress": {"stage": "private", "done": done, "total": 114},
            "partial": [{"case_id": "private-case"}],
            "seed": 8675309,
            "run_id": "private-run",
            "error": "private error body",
        }
    )
    assert snapshot is not None
    assert snapshot.stage == stage
    assert (snapshot.completed, snapshot.total) == expected_counts


@pytest.mark.parametrize(
    ("completed", "total"),
    [
        (float("nan"), 114),
        (1, float("inf")),
        (True, 114),
        (-1, 114),
        (115, 114),
        (1, 10_001),
        ("51", 114),
    ],
)
def test_malformed_source_counts_degrade_to_unknown(
    completed: object, total: object
) -> None:
    snapshot = safe_progress_snapshot(
        {"status": "running", "progress": {"done": completed, "total": total}}
    )
    assert snapshot is not None
    assert snapshot.stage == "running_benchmark"
    assert snapshot.completed is None
    assert snapshot.total is None


@pytest.mark.asyncio
async def test_submit_does_not_forward_model_provider_credentials() -> None:
    request_body: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        request_body.update(json.loads(request.content))
        return httpx.Response(202, json={"run_id": "run-1"})

    config = SimpleNamespace(
        dittobench_api_url="http://dittobench.test",
        run_size="full",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = DittobenchClient(config, http)  # type: ignore[arg-type]
        run_id = await client._submit(tarball_url="https://example.test/agent.tgz")

    assert run_id == "run-1"
    assert request_body == {
        "tarball_url": "https://example.test/agent.tgz",
        "run_size": "full",
    }


@pytest.mark.asyncio
async def test_timeout_cancels_background_run() -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "DELETE":
            return httpx.Response(202, json={"status": "failed"})
        return httpx.Response(200, json={"run_id": "run-1", "status": "running"})

    config = SimpleNamespace(
        dittobench_api_url="http://dittobench.test",
        dittobench_timeout_seconds=0.01,
        dittobench_poll_seconds=0.02,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = DittobenchClient(config, http)  # type: ignore[arg-type]
        with pytest.raises(DittobenchError, match="did not finish"):
            await client._poll("run-1")

    assert methods == ["GET", "DELETE"]


@pytest.mark.asyncio
async def test_timeout_tolerates_older_scorer_without_cancel_route() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(405)
        return httpx.Response(200, json={"run_id": "run-1", "status": "running"})

    config = SimpleNamespace(
        dittobench_api_url="http://dittobench.test",
        dittobench_timeout_seconds=0.01,
        dittobench_poll_seconds=0.02,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = DittobenchClient(config, http)  # type: ignore[arg-type]
        with pytest.raises(DittobenchError, match="did not finish"):
            await client._poll("run-1")


def _done_job() -> dict[str, object]:
    return {
        "status": "done",
        "run_id": "private-run-id",
        "seed": 42,
        "error": "private error body",
        "partial": [{"case_id": "private-case", "expected": ["private-tool"]}],
        "progress": {"stage": "scoring", "done": 114, "total": 114},
        "report": {
            "run_id": "private-run-id",
            "composite": 0.9,
            "tool_mean": 0.9,
            "memory_mean": 0.9,
            "median_ms": 100,
            "n": 114,
            "generated_at": "2026-07-14T12:00:00Z",
            "per_case": [],
        },
    }


def _poll_config() -> SimpleNamespace:
    return SimpleNamespace(
        dittobench_api_url="http://dittobench.test",
        dittobench_timeout_seconds=1.0,
        dittobench_poll_seconds=0.0,
    )


@pytest.mark.asyncio
async def test_poll_callback_receives_only_allowlisted_snapshot() -> None:
    seen: list[dict[str, object]] = []

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_done_job())

    async def progress(snapshot: DittobenchProgressSnapshot) -> None:
        seen.append(asdict(snapshot))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        report = await DittobenchClient(cast(Any, _poll_config()), http)._poll(
            "private-run-id", progress_callback=progress
        )

    assert report.n == 114
    assert seen == [{"stage": "finalizing", "completed": 114, "total": 114}]
    serialized = json.dumps(seen)
    for forbidden in ("case_id", "expected", "seed", "run_id", "error", "partial"):
        assert forbidden not in serialized


@pytest.mark.asyncio
async def test_progress_callback_failure_cannot_abort_completed_report() -> None:
    responses = [
        {"status": "queued"},
        {"status": "building"},
        {"status": "generating"},
        {"status": "running", "progress": {"done": 51, "total": 114}},
        {"status": "scoring", "progress": {"done": 114, "total": 114}},
        _done_job(),
    ]

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=responses.pop(0))

    attempts = 0

    async def broken_callback(_: DittobenchProgressSnapshot) -> None:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("telemetry sink unavailable")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        report = await DittobenchClient(cast(Any, _poll_config()), http)._poll(
            "private-run-id", progress_callback=broken_callback
        )

    assert report.run_id == "private-run-id"
    assert report.composite == 0.9
    assert attempts == 6
