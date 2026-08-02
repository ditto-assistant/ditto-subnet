"""Unit tests for per-run keying + fail-open in the validator heartbeat upsert.

These exercise :func:`ditto.db.queries.heartbeats.upsert_validator_heartbeat`
directly against SQLite-in-memory, focusing on the run_token rebaseline and the
fail-open regression behaviour that the ``/validator/heartbeat`` endpoint relies
on (it no longer maps a regression to HTTP 409).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import get_args
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.benchmark_progress import (
    BenchmarkProgress,
    BenchmarkProgressStage,
)
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.db.models import Agent, ValidatorHeartbeat
from ditto.db.queries.heartbeats import (
    _STAGE_ORDER,
    live_validator_fleet_supports_protocol,
    upsert_validator_heartbeat,
)

_HOTKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
_DEADLINE = datetime(2030, 1, 1, tzinfo=UTC)


def _progress(
    stage: str,
    *,
    completed: int | None = None,
    total: int | None = None,
    run_token: str | None = None,
) -> dict:
    return BenchmarkProgress(
        stage=stage,  # type: ignore[arg-type]
        completed=completed,
        total=total,
        ticket_deadline=_DEADLINE,
        run_token=run_token,
    ).model_dump(mode="json")


async def _seed_agent(session: AsyncSession) -> UUID:
    aid = uuid4()
    async with session.begin():
        session.add(
            Agent(
                agent_id=aid,
                miner_hotkey="5Miner",
                name="a",
                sha256="ab" * 32,
                status=AgentStatus.EVALUATING,
                screening_policy_version=SCREENING_POLICY_VERSION,
                created_at=datetime.now(UTC),
            )
        )
    return aid


async def _upsert(
    session: AsyncSession,
    agent_id: UUID,
    progress: dict | None,
    *,
    reported_at: datetime,
    capacity: dict | None = None,
) -> tuple[ValidatorHeartbeat, bool]:
    async with session.begin():
        return await upsert_validator_heartbeat(
            session,
            validator_hotkey=_HOTKEY,
            software_version="0.1.0",
            protocol_version=4,
            code_digest="ab" * 32,
            state="running_benchmark",
            active_agent_id=agent_id,
            system_metrics=None,
            benchmark_progress=progress,
            reported_at=reported_at,
            seen_at=reported_at,
            signature="ab" * 64,
            benchmark_capacity=capacity,
        )


def _stored(row: ValidatorHeartbeat) -> BenchmarkProgress:
    assert row.benchmark_progress is not None
    return BenchmarkProgress.model_validate_json(json.dumps(row.benchmark_progress))


def _fleet_heartbeat(
    hotkey: str,
    *,
    now: datetime,
    protocol_version: int,
    supported_bench_versions: list[int] | None,
) -> ValidatorHeartbeat:
    capabilities = None
    if supported_bench_versions is not None:
        capabilities = {
            "screened_images": True,
            "require_screened_image": True,
            "source_build_fallback": False,
            "full_stack_managed": True,
            "stack_updater": True,
            "sandbox_egress_restricted": True,
            "ticket_inference": False,
            "signed_score_quorum": False,
            "executor_isolation": "ephemeral_vm",
            "scorer_benchmarks": {
                "status": "fresh_verified",
                "supported_bench_versions": supported_bench_versions,
                "observed_at": int(now.timestamp()),
                "software_version": "1.0.0",
                "source_revision": "a" * 40,
            },
        }
    return ValidatorHeartbeat(
        validator_hotkey=hotkey,
        software_version="1.0.0",
        protocol_version=protocol_version,
        code_digest="ab" * 32,
        state="polling",
        first_seen_at=now,
        reported_at=now,
        seen_at=now,
        signature="ab" * 64,
        capabilities=capabilities,
    )


async def test_fleet_protocol_gate_ignores_live_non_participating_validators(
    session: AsyncSession,
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    async with session.begin():
        session.add_all(
            [
                _fleet_heartbeat(
                    "bench-6-capable",
                    now=now,
                    protocol_version=14,
                    supported_bench_versions=[2, 6],
                ),
                _fleet_heartbeat(
                    "legacy-validator",
                    now=now,
                    protocol_version=6,
                    supported_bench_versions=None,
                ),
                _fleet_heartbeat(
                    "other-benchmark",
                    now=now,
                    protocol_version=8,
                    supported_bench_versions=[2, 5],
                ),
            ]
        )

    assert await live_validator_fleet_supports_protocol(
        session,
        minimum_protocol=14,
        bench_version=6,
        now=now,
    )


async def test_fleet_protocol_gate_requires_every_capable_validator(
    session: AsyncSession,
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    async with session.begin():
        session.add_all(
            [
                _fleet_heartbeat(
                    "current-capable",
                    now=now,
                    protocol_version=14,
                    supported_bench_versions=[2, 6],
                ),
                _fleet_heartbeat(
                    "old-capable",
                    now=now,
                    protocol_version=13,
                    supported_bench_versions=[2, 6],
                ),
            ]
        )

    assert not await live_validator_fleet_supports_protocol(
        session,
        minimum_protocol=14,
        bench_version=6,
        now=now,
    )


async def test_fleet_protocol_gate_fails_closed_without_capable_validators(
    session: AsyncSession,
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    async with session.begin():
        session.add(
            _fleet_heartbeat(
                "legacy-validator",
                now=now,
                protocol_version=6,
                supported_bench_versions=None,
            )
        )

    assert not await live_validator_fleet_supports_protocol(
        session,
        minimum_protocol=14,
        bench_version=6,
        now=now,
    )


async def test_same_run_regression_is_fail_open_and_keeps_previous(
    session: AsyncSession,
) -> None:
    agent_id = await _seed_agent(session)
    base = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    _, accepted = await _upsert(
        session,
        agent_id,
        _progress("running_benchmark", completed=51, total=114, run_token="a" * 16),
        reported_at=base,
    )
    assert accepted

    row, accepted = await _upsert(
        session,
        agent_id,
        _progress("running_benchmark", completed=40, total=114, run_token="a" * 16),
        reported_at=base + timedelta(seconds=1),
    )
    # The heartbeat is accepted (never rejected) but the stored progress floor is
    # kept — the public display must not move backward.
    assert accepted
    assert row.benchmark_progress_reported is True
    assert _stored(row).completed == 51
    # Liveness still advances so the validator does not read as stale.
    assert row.seen_at == base + timedelta(seconds=1)


async def test_new_run_token_rebaselines_instead_of_regressing(
    session: AsyncSession,
) -> None:
    agent_id = await _seed_agent(session)
    base = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    await _upsert(
        session,
        agent_id,
        _progress("running_benchmark", completed=51, total=114, run_token="a" * 16),
        reported_at=base,
    )

    # A fresh run_token means a new run (retry / next seed); its lower count is a
    # legitimate restart, not a regression.
    row, accepted = await _upsert(
        session,
        agent_id,
        _progress("running_benchmark", completed=1, total=114, run_token="b" * 16),
        reported_at=base + timedelta(seconds=1),
    )
    assert accepted
    assert _stored(row).completed == 1
    assert _stored(row).run_token == "b" * 16


async def test_same_run_monotonic_progress_is_accepted(
    session: AsyncSession,
) -> None:
    agent_id = await _seed_agent(session)
    base = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    await _upsert(
        session,
        agent_id,
        _progress("running_benchmark", completed=51, total=114, run_token="a" * 16),
        reported_at=base,
    )
    row, accepted = await _upsert(
        session,
        agent_id,
        _progress("running_benchmark", completed=90, total=114, run_token="a" * 16),
        reported_at=base + timedelta(seconds=1),
    )
    assert accepted
    assert _stored(row).completed == 90


async def test_newer_capacity_cannot_regress_secondary_slot_progress(
    session: AsyncSession,
) -> None:
    agent_id = await _seed_agent(session)
    second_agent = await _seed_agent(session)
    base = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)

    def capacity(completed: int) -> dict:
        return {
            "configured_slots": 2,
            "healthy_slots": ["slot-0", "slot-1"],
            "admission": "accepting",
            "active": [
                {
                    "slot_id": "slot-1",
                    "agent_id": str(second_agent),
                    "bench_version": 5,
                    "progress": _progress(
                        "running_benchmark",
                        completed=completed,
                        total=114,
                        run_token="c" * 16,
                    ),
                }
            ],
        }

    await _upsert(
        session,
        agent_id,
        None,
        reported_at=base,
        capacity=capacity(9),
    )
    row, accepted = await _upsert(
        session,
        agent_id,
        None,
        reported_at=base + timedelta(seconds=1),
        capacity=capacity(5),
    )
    assert accepted
    assert row.benchmark_capacity is not None
    assert row.benchmark_capacity["active"][0]["progress"]["completed"] == 9
    assert row.seen_at == base + timedelta(seconds=1)


async def test_monotonicity_failure_still_advances_liveness(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The liveness write survives an unexpected failure in payload reasoning.

    The endpoint guards its own capacity loop with a savepoint, but the
    monotonicity check runs *inside* this function, on the far side of that
    guard. An exception here would abort the same transaction that carries
    ``seen_at`` — the failure mode ``test_stage_order_covers_every_wire_stage``
    can only prevent for one known cause.
    """
    agent_id = await _seed_agent(session)
    base = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
    _, accepted = await _upsert(
        session,
        agent_id,
        _progress("running_benchmark", completed=3, total=10, run_token="a" * 16),
        reported_at=base,
    )
    assert accepted

    def _boom(*_args: object) -> None:
        raise KeyError("some_unordered_stage")

    monkeypatch.setattr(
        "ditto.db.queries.heartbeats._validate_same_lease_progress", _boom
    )
    row, accepted = await _upsert(
        session,
        agent_id,
        _progress("running_benchmark", completed=6, total=10, run_token="a" * 16),
        reported_at=base + timedelta(seconds=10),
    )
    assert accepted
    assert row.seen_at == base + timedelta(seconds=10)
    assert row.reported_at == base + timedelta(seconds=10)
    # Fail open on the payload: the unchecked report is stored rather than the
    # write being lost. The check is a display floor, not an authorization gate.
    assert _stored(row).completed == 6


def test_stage_order_covers_every_wire_stage() -> None:
    """``_STAGE_ORDER`` must be exhaustive over ``BenchmarkProgressStage``.

    ``_validate_same_lease_progress`` subscripts ``_STAGE_ORDER`` directly, so a
    stage missing from it raises ``KeyError`` rather than the
    ``HeartbeatProgressRegressionError`` every call site catches — surfacing as a
    500 from ``/validator/heartbeat``. mypy cannot catch the gap because a
    ``dict[Literal, int]`` literal need not be exhaustive, so this test is the
    only guard. It is written against ``get_args`` rather than a hardcoded list
    so that adding a stage to the wire enum fails here until it is ordered.
    """
    assert set(get_args(BenchmarkProgressStage)) == set(_STAGE_ORDER)


async def test_generating_dataset_does_not_regress_or_error(
    session: AsyncSession,
) -> None:
    """A ``generating_dataset`` heartbeat is accepted after ``preparing``.

    Regression test for the stage being absent from ``_STAGE_ORDER``: the second
    upsert below raised ``KeyError`` (a 500 at the endpoint), which froze
    ``seen_at`` and made an actively scoring validator read as heartbeat_stale.
    """
    agent_id = await _seed_agent(session)
    base = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
    _, accepted = await _upsert(
        session, agent_id, _progress("preparing", run_token="a" * 16), reported_at=base
    )
    assert accepted

    row, accepted = await _upsert(
        session,
        agent_id,
        _progress("generating_dataset", run_token="a" * 16),
        reported_at=base + timedelta(seconds=10),
    )
    assert accepted
    assert _stored(row).stage == "generating_dataset"
    assert row.seen_at == base + timedelta(seconds=10)


async def test_generating_dataset_is_ordered_before_running(
    session: AsyncSession,
) -> None:
    """The new stage takes its place in the lifecycle, not merely a slot.

    ``generating_dataset`` sits after the image is in hand and before the harness
    starts, so a late poll reporting it must not drag a run that already reached
    ``running_benchmark`` backwards.
    """
    agent_id = await _seed_agent(session)
    base = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
    await _upsert(
        session,
        agent_id,
        _progress("running_benchmark", completed=7, total=281, run_token="a" * 16),
        reported_at=base,
    )
    row, accepted = await _upsert(
        session,
        agent_id,
        _progress("generating_dataset", run_token="a" * 16),
        reported_at=base + timedelta(seconds=10),
    )
    # Fail-open: accepted for liveness, but the stage floor holds.
    assert accepted
    assert _stored(row).stage == "running_benchmark"
    assert _stored(row).completed == 7
    assert row.seen_at == base + timedelta(seconds=10)


async def test_relay_wait_can_resume_running_without_regression(
    session: AsyncSession,
) -> None:
    agent_id = await _seed_agent(session)
    base = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
    await _upsert(
        session,
        agent_id,
        _progress("running_benchmark", completed=7, total=281, run_token="a" * 16),
        reported_at=base,
    )
    waiting, accepted = await _upsert(
        session,
        agent_id,
        _progress("waiting_for_relay", completed=7, total=281, run_token="a" * 16),
        reported_at=base + timedelta(seconds=10),
    )
    assert accepted
    assert _stored(waiting).stage == "waiting_for_relay"

    resumed, accepted = await _upsert(
        session,
        agent_id,
        _progress("running_benchmark", completed=8, total=281, run_token="a" * 16),
        reported_at=base + timedelta(seconds=20),
    )
    assert accepted
    assert _stored(resumed).stage == "running_benchmark"
    assert _stored(resumed).completed == 8
