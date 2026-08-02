"""Coverage for the admin lease-revocation read path.

The table this reads has existed, and been written to, since the lease liveness
gate shipped. Nothing could read it, which is most of why a lease incident cost a
day. These tests pin the part that made it expensive: the ``evidence`` blob has
to come back whole, because ``reason`` alone is a bare code and the evidence is
what actually explains the verdict.
"""

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ditto.api_server.dependencies import get_session
from ditto.db.models import ValidatorLeaseAudit

pytestmark = pytest.mark.asyncio

_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_TOKEN}"}
_URL = "/api/v1/admin/lease-revocations"
_T0 = datetime(2026, 7, 26, 5, tzinfo=UTC)
_AGENT = UUID("11111111-1111-4111-8111-111111111111")
_OTHER_AGENT = UUID("22222222-2222-4222-8222-222222222222")

_EVIDENCE = {
    "idle": True,
    "reason": "idle_capacity_reports_slot_free",
    "heartbeat_age_seconds": 30.0,
    "lease_age_seconds": 360.0,
    "attempt_count": 3,
    "active_slot_ids": [],
    "nested": {"admission": "accepting"},
}


@pytest.fixture
def lr_maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_TOKEN)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


async def _seed(maker: async_sessionmaker[AsyncSession]) -> None:
    async with maker() as session, session.begin():
        session.add_all(
            [
                ValidatorLeaseAudit(
                    audit_id=uuid4(),
                    agent_id=_AGENT,
                    validator_hotkey="5Alpha",
                    slot_id="slot-0",
                    bench_version=7,
                    action="force_expired",
                    reason="idle_capacity_reports_slot_free",
                    context="issue_ticket",
                    evidence=_EVIDENCE,
                    recorded_at=_T0,
                ),
                ValidatorLeaseAudit(
                    audit_id=uuid4(),
                    agent_id=_OTHER_AGENT,
                    validator_hotkey="5Beta",
                    slot_id="slot-1",
                    bench_version=7,
                    action="closed_unserviceable",
                    reason="lease_deadline_passed",
                    context="score_retest",
                    evidence={"idle": True, "reason": "lease_deadline_passed"},
                    recorded_at=_T0 + timedelta(minutes=5),
                ),
            ]
        )


async def test_read_requires_the_admin_token(
    app: FastAPI, client: httpx.AsyncClient, lr_maker: async_sessionmaker[AsyncSession]
) -> None:
    _install(app, lr_maker)
    assert (await client.get(_URL)).status_code == 401


async def test_returns_newest_first_with_evidence_intact(
    app: FastAPI, client: httpx.AsyncClient, lr_maker: async_sessionmaker[AsyncSession]
) -> None:
    await _seed(lr_maker)
    _install(app, lr_maker)

    body = (await client.get(_URL, headers=_HEADERS)).json()

    assert body["total"] == 2
    assert [item["action"] for item in body["revocations"]] == [
        "closed_unserviceable",
        "force_expired",
    ]
    # Whole, nested structure included. `reason` alone is a bare code; this blob
    # is the part that says what the verdict was actually taken on.
    assert body["revocations"][1]["evidence"] == _EVIDENCE


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ({"agent_id": str(_AGENT)}, ["force_expired"]),
        ({"validator_hotkey": "5Beta"}, ["closed_unserviceable"]),
        ({"action": "force_expired"}, ["force_expired"]),
        ({"context": "score_retest"}, ["closed_unserviceable"]),
    ],
)
async def test_filters(
    app: FastAPI,
    client: httpx.AsyncClient,
    lr_maker: async_sessionmaker[AsyncSession],
    query: dict[str, str],
    expected: list[str],
) -> None:
    await _seed(lr_maker)
    _install(app, lr_maker)

    body = (await client.get(_URL, headers=_HEADERS, params=query)).json()

    assert [item["action"] for item in body["revocations"]] == expected
    assert body["total"] == len(expected)


async def test_paging_keeps_the_total_unfiltered_by_page(
    app: FastAPI, client: httpx.AsyncClient, lr_maker: async_sessionmaker[AsyncSession]
) -> None:
    await _seed(lr_maker)
    _install(app, lr_maker)

    body = (
        await client.get(_URL, headers=_HEADERS, params={"limit": 1, "offset": 1})
    ).json()

    assert body["total"] == 2
    assert [item["action"] for item in body["revocations"]] == ["force_expired"]


async def test_an_empty_ledger_is_a_finding_not_an_error(
    app: FastAPI, client: httpx.AsyncClient, lr_maker: async_sessionmaker[AsyncSession]
) -> None:
    """Production's table was empty, which is itself the answer.

    It means no lease was revoked by the platform in the window, so a run that
    died did so by another path. The endpoint has to say that plainly rather
    than 404 or error, or the emptiness gets misread as "not wired up yet".
    """
    _install(app, lr_maker)

    response = await client.get(_URL, headers=_HEADERS)

    assert response.status_code == 200
    assert response.json()["total"] == 0
    assert response.json()["revocations"] == []
