"""Smoke test for the ditto.bench.runner harness driver and report writer.

Uses :class:`HarnessConfig.command` to bypass Docker and run a small inline
Python "echo-harness" subprocess that implements the JSON line protocol.
This validates the stdio framing, timeout enforcement, and report
aggregation paths without requiring a real Docker daemon in CI.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from ditto.bench import SCHEMA_VERSION
from ditto.bench.loader.cases import ExpectedToolCall, ToolCallCase
from ditto.bench.loader.taxonomy import Mechanism
from ditto.bench.runner.docker import (
    HarnessConfig,
    HarnessDriver,
    HarnessTimeoutError,
)
from ditto.bench.runner.report import aggregate, write_report
from ditto.bench.runner.scoring import (
    CoreScoreInputs,
    Score,
    ToolCallScore,
    score_core,
)

ECHO_HARNESS = r"""
import json
import sys

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    resp = {
        "schema_version": req["schema_version"],
        "challenge_id": req["challenge_id"],
        "validator_seed": req["validator_seed"],
        "tool_calls": [{"hop": 1, "name": "search_memories", "args": "{}"}],
        "total_latency_ms": 42,
    }
    sys.stdout.write(json.dumps(resp) + "\n")
    sys.stdout.flush()
"""


SLEEP_HARNESS = r"""
import sys
import time

for line in sys.stdin:
    # Never respond — used to exercise the timeout path.
    time.sleep(60)
"""


def _echo_harness_config() -> HarnessConfig:
    """Build a HarnessConfig that runs an inline Python echo harness."""
    return HarnessConfig(command=[sys.executable, "-c", ECHO_HARNESS])


def test_harness_driver_round_trips_a_challenge() -> None:
    """Send one challenge through the driver and validate the response."""
    config = _echo_harness_config()
    with HarnessDriver(config) as harness:
        response = harness.send(
            {
                "schema_version": SCHEMA_VERSION,
                "challenge_id": "abc",
                "mechanism": "ditto_core",
                "case_id": "smoke-1",
                "prompt": "hello",
                "validator_seed": "0123456789abcdef",
                "issued_at": "2026-05-15T17:00:00Z",
                "deadline_ms": 5000,
            },
            deadline_ms=5000,
        )
    assert response["challenge_id"] == "abc"
    assert response["validator_seed"] == "0123456789abcdef"
    assert response["tool_calls"][0]["name"] == "search_memories"


def test_harness_driver_handles_multiple_sequential_challenges() -> None:
    """The driver must keep the container alive across N challenges."""
    config = _echo_harness_config()
    with HarnessDriver(config) as harness:
        for i in range(5):
            response = harness.send(
                {
                    "schema_version": SCHEMA_VERSION,
                    "challenge_id": f"c-{i}",
                    "mechanism": "ditto_core",
                    "case_id": f"case-{i}",
                    "prompt": "x",
                    "validator_seed": "deadbeefdeadbeef",
                    "issued_at": "2026-05-15T17:00:00Z",
                    "deadline_ms": 5000,
                },
                deadline_ms=5000,
            )
            assert response["challenge_id"] == f"c-{i}"


def test_harness_driver_raises_on_timeout() -> None:
    """A harness that never responds within deadline_ms raises HarnessTimeoutError."""
    config = HarnessConfig(command=[sys.executable, "-c", SLEEP_HARNESS])
    with HarnessDriver(config) as harness, pytest.raises(HarnessTimeoutError):
        harness.send(
            {
                "schema_version": SCHEMA_VERSION,
                "challenge_id": "stuck",
                "mechanism": "ditto_core",
                "case_id": "stuck",
                "prompt": "x",
                "validator_seed": "feedfacefeedface",
                "issued_at": "2026-05-15T17:00:00Z",
                "deadline_ms": 200,
            },
            deadline_ms=200,
        )


def test_aggregate_produces_per_mechanism_breakdown() -> None:
    """Aggregating two cases yields one MechanismAggregate per mechanism."""
    case = ToolCallCase(
        id="a",
        category="memory_lookup",
        prompt="",
        expected_tools=[ExpectedToolCall(name="search_memories")],
    )
    s1 = score_core(
        CoreScoreInputs(
            case=case,
            tool=ToolCallScore(name_f1=1.0, arg_f1=1.0, trajectory_penalty=0.0),
            latency_ms=10,
            budget_latency_ms=1000,
        )
    )
    s2 = score_core(
        CoreScoreInputs(
            case=ToolCallCase(
                id="b",
                category="memory_lookup",
                prompt="",
                expected_tools=[ExpectedToolCall(name="search_memories")],
            ),
            tool=ToolCallScore(name_f1=0.5, arg_f1=0.5, trajectory_penalty=0.0),
            latency_ms=10,
            budget_latency_ms=1000,
        )
    )
    aggs = aggregate([s1, s2])
    assert len(aggs) == 1
    agg = aggs[0]
    assert agg.mechanism is Mechanism.CORE
    assert agg.count == 2
    assert agg.per_category.get("memory_lookup") is not None


def test_write_report_emits_valid_json(tmp_path: Path) -> None:
    """write_report should produce a parseable JSON file with the score schema."""
    case = ToolCallCase(
        id="x",
        category="memory_lookup",
        prompt="",
        expected_tools=[ExpectedToolCall(name="search_memories")],
    )
    s = score_core(
        CoreScoreInputs(
            case=case,
            tool=ToolCallScore(name_f1=1.0, arg_f1=1.0, trajectory_penalty=0.0),
            latency_ms=10,
            budget_latency_ms=1000,
        )
    )
    out = tmp_path / "report.json"
    report = write_report([s], image="my-harness:dev", out_path=out)
    assert report.image == "my-harness:dev"
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert loaded["schema_version"] == SCHEMA_VERSION
    assert loaded["image"] == "my-harness:dev"
    assert loaded["scores"][0]["case_id"] == "x"
    assert loaded["scores"][0]["mechanism"] == "ditto_core"


def test_score_to_dict_omits_empty_domain_and_notes() -> None:
    """Score.to_dict omits domain and notes when empty (avoids report noise)."""
    s = Score(
        schema_version=SCHEMA_VERSION,
        case_id="x",
        mechanism=Mechanism.CORE,
        score=0.5,
    )
    d = s.to_dict()
    assert "domain" not in d
    assert "notes" not in d
