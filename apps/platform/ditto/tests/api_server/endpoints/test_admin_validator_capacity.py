"""Backroom read of fleet validator capacity (ditto-subnet#2036)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.api_models.ticket_status import TicketPurpose, TicketStatus
from ditto.api_models.validator_slot_settings import ValidatorSlotSettings
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints import admin_validator_capacity
from ditto.api_server.endpoints import public as public_endpoint
from ditto.db.models import Agent, BenchmarkDataset, ValidatorHeartbeat, ValidatorTicket

pytestmark = pytest.mark.asyncio
_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}
_URL = "/api/v1/admin/validator-capacity"
# No rollout row is seeded, so the active era is the scoreable floor.
_BENCH = 7
_SERVING = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
_OBSOLETE = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
_DRAINING = "5FLSigC9HGRKVhB9FiEo4Y3koPsNmBmLJbpXg2mp1hXcS59Y"
_STALE = "5DAAnrj7VHTznn2AWBemMuyBwZWs6FNFjdyVXUeYum3PTXFy"


class _SlotSettings:
    """A resolver stub: the app under test runs no lifespan to install one."""

    def __init__(self, settings: ValidatorSlotSettings) -> None:
        self._settings = settings

    async def resolve(self, _session_maker: Any) -> ValidatorSlotSettings:
        return self._settings


def _install(
    app: FastAPI,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_ADMIN_TOKEN)
    app.state.validator_slot_settings = _SlotSettings(
        ValidatorSlotSettings(max_concurrent_slots=4, disk_percent_ceiling=90)
    )

    async def _session() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    # Serviceability is the fleet view's own verdict, pinned by test_public;
    # here only its effect on the roll-up matters, so choose it per hotkey.
    monkeypatch.setattr(
        public_endpoint,
        "_bench_serviceability",
        lambda row, **_: (
            "software_obsolete" if row.validator_hotkey == _OBSOLETE else "serving"
        ),
    )


async def _seed_agent(
    session: AsyncSession, *, name: str, created_at: datetime
) -> UUID:
    """One evaluating submission the fleet-wide queue filter admits."""
    agent_id = uuid4()
    session.add(
        Agent(
            agent_id=agent_id,
            miner_hotkey="5" + "F" * 47,
            name=name,
            sha256="ab" * 32,
            status=AgentStatus.EVALUATING,
            screening_policy_version=SCREENING_POLICY_VERSION,
            created_at=created_at,
            screened_image_sha256="12" * 32,
            screened_image_size_bytes=123,
            screened_image_id="sha256:" + "34" * 32,
            screened_image_ref=f"ditto-screen/{agent_id}:latest",
            screened_image_upload_id=uuid4(),
            screened_image_verified_at=created_at,
        )
    )
    await session.flush()
    session.add(
        BenchmarkDataset(
            agent_id=agent_id,
            bench_version=_BENCH,
            seed=1,
            sha256="cd" * 32,
            run_size="full",
        )
    )
    return agent_id


def _lease(
    agent_id: UUID, *, hotkey: str, slot_id: str, issued_at: datetime
) -> ValidatorTicket:
    return ValidatorTicket(
        agent_id=agent_id,
        validator_hotkey=hotkey,
        slot_id=slot_id,
        status=TicketStatus.ISSUED,
        purpose=TicketPurpose.CANONICAL_QUORUM,
        purpose_revision=1,
        issued_at=issued_at,
        deadline=issued_at + timedelta(hours=3),
        bench_version=_BENCH,
        attempt_count=1,
    )


def _heartbeat(
    hotkey: str,
    *,
    seen_at: datetime,
    configured_slots: int = 8,
    admission: str = "accepting",
    active: list[dict[str, Any]] | None = None,
) -> ValidatorHeartbeat:
    active = active or []
    return ValidatorHeartbeat(
        validator_hotkey=hotkey,
        software_version="0.36.0",
        protocol_version=16,
        code_digest="ab" * 32,
        state="running_benchmark" if active else "idle",
        active_agent_id=None,
        first_seen_at=seen_at - timedelta(days=1),
        benchmark_capacity={
            "configured_slots": configured_slots,
            "healthy_slots": (
                [f"slot-{index}" for index in range(configured_slots)]
                if admission == "accepting"
                else []
            ),
            "admission": admission,
            "active": active,
        },
        claimed_slots=[
            {"slot_id": slot["slot_id"], "agent_id": slot["agent_id"]}
            for slot in active
        ],
        reported_at=seen_at,
        seen_at=seen_at,
        signature="ab" * 64,
    )


class TestAuth:
    async def test_read_requires_the_admin_token(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install(app, session_maker, monkeypatch)
        assert (await client.get(_URL)).status_code == 401


class TestCapacitySummary:
    async def test_an_empty_fleet_reports_zeros_not_an_error(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install(app, session_maker, monkeypatch)

        response = await client.get(_URL, headers=_HEADERS)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["active_bench_version"] == _BENCH
        assert body["live_validator_count"] == 0
        assert body["serviceable_validator_count"] == 0
        assert body["serviceable_slots"] == 0
        assert body["claimed_slots"] == 0
        assert body["active_assignment_count"] == 0
        assert body["estimated_remaining_slot_minutes"] == 0
        assert body["unestimated_assignment_count"] == 0
        assert body["eligible_unleased_count"] == 0
        assert body["oldest_eligible_unleased_age_seconds"] is None
        assert body["validators"] == []
        assert body["validators_truncated"] is False
        assert [lane["request_kind"] for lane in body["relay"]] == [
            "chat",
            "embedding",
        ]
        assert all(lane["active_requests"] == 0 for lane in body["relay"])
        assert all(lane["saturation"] == 0 for lane in body["relay"])
        assert all(lane["global_limit"] >= 1 for lane in body["relay"])

    async def test_mixed_fleet_separates_serviceable_from_claimed_slots(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install(app, session_maker, monkeypatch)
        now = datetime.now(UTC)
        seen_at = now - timedelta(seconds=30)
        issued_at = seen_at - timedelta(minutes=60)
        async with session_maker() as session, session.begin():
            reporting = await _seed_agent(
                session, name="reporting", created_at=now - timedelta(hours=5)
            )
            silent = await _seed_agent(
                session, name="silent", created_at=now - timedelta(hours=4)
            )
            await _seed_agent(
                session, name="oldest-waiting", created_at=now - timedelta(hours=2)
            )
            await _seed_agent(
                session, name="newer-waiting", created_at=now - timedelta(minutes=30)
            )
            session.add(
                _lease(
                    reporting, hotkey=_SERVING, slot_id="slot-0", issued_at=issued_at
                )
            )
            session.add(
                _lease(silent, hotkey=_SERVING, slot_id="slot-1", issued_at=issued_at)
            )
            session.add(
                _heartbeat(
                    _SERVING,
                    seen_at=seen_at,
                    active=[
                        {
                            "slot_id": "slot-0",
                            "agent_id": str(reporting),
                            "bench_version": _BENCH,
                            "progress": {
                                "stage": "running_benchmark",
                                "completed": 30,
                                "total": 90,
                                "ticket_deadline": (
                                    issued_at + timedelta(hours=3)
                                ).isoformat(),
                            },
                        },
                        {
                            "slot_id": "slot-1",
                            "agent_id": str(silent),
                            "bench_version": _BENCH,
                            "progress": None,
                        },
                    ],
                )
            )
            session.add(_heartbeat(_OBSOLETE, seen_at=seen_at, configured_slots=2))
            session.add(_heartbeat(_DRAINING, seen_at=seen_at, admission="draining"))

        response = await client.get(_URL, headers=_HEADERS)

        assert response.status_code == 200, response.text
        body = response.json()
        rows = {row["validator_hotkey"]: row for row in body["validators"]}
        # Eight advertised under a cap of four; the other two fund nothing.
        assert rows[_SERVING]["serviceable_slots"] == 4
        assert rows[_SERVING]["claimed_slots"] == 2
        assert rows[_OBSOLETE]["bench_serviceability"] == "software_obsolete"
        assert rows[_OBSOLETE]["serviceable_slots"] == 0
        assert rows[_DRAINING]["admission"] == "draining"
        assert rows[_DRAINING]["serviceable_slots"] == 0
        assert body["live_validator_count"] == 3
        assert body["serviceable_validator_count"] == 1
        assert body["serviceable_slots"] == 4
        assert body["claimed_slots"] == 2
        assert body["active_assignment_count"] == 2

        by_slot = {item["slot_id"]: item for item in rows[_SERVING]["assignments"]}
        # 30 checks in the 60 minutes from issue to heartbeat: 0.5/min, so the
        # remaining 60 checks project to 120 slot-minutes.
        assert by_slot["slot-0"]["completed_checks"] == 30
        assert by_slot["slot-0"]["total_checks"] == 90
        assert by_slot["slot-0"]["checks_per_minute"] == 0.5
        assert by_slot["slot-0"]["estimated_remaining_slot_minutes"] == 120.0
        assert by_slot["slot-0"]["age_seconds"] >= 60 * 60
        # A leased slot with no progress yet is unknown, never zero.
        assert by_slot["slot-1"]["stage"] is None
        assert by_slot["slot-1"]["checks_per_minute"] is None
        assert by_slot["slot-1"]["estimated_remaining_slot_minutes"] is None
        assert body["estimated_remaining_slot_minutes"] == 120.0
        assert body["unestimated_assignment_count"] == 1

        # The two leased submissions are not waiting; the older of the other
        # two sets the queue age.
        assert body["eligible_unleased_count"] == 2
        oldest = body["oldest_eligible_unleased_age_seconds"]
        assert 2 * 60 * 60 <= oldest < 2 * 60 * 60 + 60

    async def test_a_stale_heartbeat_contributes_nothing(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install(app, session_maker, monkeypatch)
        now = datetime.now(UTC)
        async with session_maker() as session, session.begin():
            session.add(_heartbeat(_STALE, seen_at=now - timedelta(minutes=20)))
            session.add(_heartbeat(_SERVING, seen_at=now - timedelta(seconds=30)))

        body = (await client.get(_URL, headers=_HEADERS)).json()

        assert [row["validator_hotkey"] for row in body["validators"]] == [_SERVING]
        assert body["live_validator_count"] == 1
        assert body["serviceable_slots"] == 4

    async def test_rows_are_capped_but_totals_cover_the_whole_fleet(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install(app, session_maker, monkeypatch)
        monkeypatch.setattr(admin_validator_capacity, "VALIDATOR_ROW_LIMIT", 2)
        now = datetime.now(UTC)
        async with session_maker() as session, session.begin():
            for hotkey in (_SERVING, _DRAINING, _STALE):
                session.add(_heartbeat(hotkey, seen_at=now - timedelta(seconds=30)))

        body = (await client.get(_URL, headers=_HEADERS)).json()

        assert len(body["validators"]) == 2
        assert body["validators_truncated"] is True
        assert body["live_validator_count"] == 3
        assert body["serviceable_slots"] == 12
