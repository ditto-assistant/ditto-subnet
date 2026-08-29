"""Contract tests for the scheduled screening-policy activation board.

The schedule decides WHEN the screening queue's required policy version rises;
the version text itself ships with the build. These tests cover the operator
surface (auth, optimistic concurrency, confirmation phrase, timezone and bound
validation) and the effective-version read that the queue, claim, heartbeat,
and verdict paths depend on.
"""

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)

from ditto.api_server.dependencies import get_session
from ditto.db.queries.screener_policy_activation import (
    insert_screener_policy_activation,
)
from ditto_screening_protocol import (
    SCREENING_FLOOR_POLICY_VERSION,
    SCREENING_POLICY_VERSION,
)

pytestmark = pytest.mark.asyncio

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}
_URL = "/api/v1/admin/screener-policy-activation"
_CONFIRMATION = "SCHEDULE SCREENER POLICY ACTIVATION"


@pytest.fixture
def activation_maker(
    session_maker: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    """Local alias for the root Postgres ``session_maker``."""
    return session_maker


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_ADMIN_TOKEN)
    app.state.session_maker = maker
    if getattr(app.state, "screener_policy_activation", None) is not None:
        app.state.screener_policy_activation.invalidate()

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


def _future(hours: float = 24.0) -> str:
    return (datetime.now(UTC) + timedelta(hours=hours)).isoformat()


def _payload(
    *,
    expected_revision: int = 0,
    confirmation: str = _CONFIRMATION,
    activate_at: str | None = None,
    target_policy_version: int | None = None,
    rescreen_scored: bool = True,
) -> dict[str, object]:
    body: dict[str, object] = {
        "expected_revision": expected_revision,
        "target_policy_version": (
            target_policy_version
            if target_policy_version is not None
            else SCREENING_POLICY_VERSION
        ),
        "activate_at": activate_at if activate_at is not None else _future(),
        "rescreen_scored": rescreen_scored,
        "reason": "scheduled v11 activation for the planner-forced I7 amendment",
        "actor": "backroom:test",
        "confirmation": confirmation,
    }
    return body


class TestAuth:
    async def test_read_requires_the_admin_token(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        activation_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, activation_maker)
        assert (await client.get(_URL)).status_code == 401

    async def test_write_requires_the_admin_token(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        activation_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, activation_maker)
        assert (await client.post(_URL, json=_payload())).status_code == 401


class TestDefaultAndRoundTrip:
    async def test_no_schedule_requires_the_floor_version(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        activation_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, activation_maker)
        body = (await client.get(_URL, headers=_HEADERS)).json()
        assert body["effective_policy_version"] == SCREENING_FLOOR_POLICY_VERSION
        assert body["floor_policy_version"] == SCREENING_FLOOR_POLICY_VERSION
        assert body["builtin_policy_version"] == SCREENING_POLICY_VERSION
        assert body["latest"] is None
        assert body["revisions"] == []

    async def test_pending_schedule_keeps_the_floor_version(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        activation_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, activation_maker)
        response = await client.post(_URL, json=_payload(), headers=_HEADERS)
        assert response.status_code == 200, response.text
        body = response.json()
        # A future activation is notice, not activation: the queue still
        # requires the floor version until activate_at passes.
        assert body["effective_policy_version"] == SCREENING_FLOOR_POLICY_VERSION
        assert body["latest"]["state"] == "pending"
        assert body["latest"]["revision"] == 1
        assert body["latest"]["target_policy_version"] == SCREENING_POLICY_VERSION

    async def test_past_activate_at_via_the_api_is_rejected_even_for_due_semantics(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        activation_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A due state is produced by TIME passing, never by a backdated write.

        The API refuses past timestamps outright (retroactive rule changes are
        exactly what the fairness timeline exists to prevent); the resolver's
        due-activation behavior is covered against directly-seeded rows in
        ``TestResolverDueActivation``.
        """
        _install(app, activation_maker)
        past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        response = await client.post(
            _URL,
            json=_payload(activate_at=past),
            headers=_HEADERS,
        )
        assert response.status_code == 422

    async def test_history_is_newest_first(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        activation_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, activation_maker)
        first = await client.post(_URL, json=_payload(), headers=_HEADERS)
        assert first.status_code == 200
        second = await client.post(
            _URL,
            json=_payload(
                expected_revision=1,
                rescreen_scored=False,
                activate_at=_future(hours=48.0),
            ),
            headers=_HEADERS,
        )
        assert second.status_code == 200, second.text
        body = second.json()
        assert [r["revision"] for r in body["revisions"]] == [2, 1]
        assert body["revisions"][0]["rescreen_scored"] is False


class TestWriteGuards:
    async def test_confirmation_phrase_is_enforced(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        activation_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, activation_maker)
        response = await client.post(
            _URL,
            json=_payload(confirmation="APPLY QUEUE POLICY SETTINGS"),
            headers=_HEADERS,
        )
        assert response.status_code == 409
        assert _CONFIRMATION in response.json()["message"]

    async def test_timezone_naive_activate_at_is_rejected(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        activation_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, activation_maker)
        naive = (datetime.now(UTC) + timedelta(hours=24)).replace(tzinfo=None)
        response = await client.post(
            _URL,
            json=_payload(activate_at=naive.isoformat()),
            headers=_HEADERS,
        )
        assert response.status_code == 422
        assert "timezone offset" in response.json()["message"]

    async def test_past_activate_at_is_rejected(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        activation_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, activation_maker)
        past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        response = await client.post(
            _URL, json=_payload(activate_at=past), headers=_HEADERS
        )
        assert response.status_code == 422
        assert "future" in response.json()["message"]

    async def test_target_at_the_floor_is_accepted_for_incident_rollback(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        activation_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, activation_maker)
        response = await client.post(
            _URL,
            json=_payload(
                target_policy_version=SCREENING_FLOOR_POLICY_VERSION,
                rescreen_scored=False,
            ),
            headers=_HEADERS,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["latest"]["target_policy_version"] == SCREENING_FLOOR_POLICY_VERSION
        assert body["latest"]["rescreen_scored"] is False

    async def test_target_below_the_floor_is_rejected(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        activation_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, activation_maker)
        response = await client.post(
            _URL,
            json=_payload(target_policy_version=SCREENING_FLOOR_POLICY_VERSION - 1),
            headers=_HEADERS,
        )
        assert response.status_code == 422

    async def test_target_above_the_build_is_rejected(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        activation_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, activation_maker)
        response = await client.post(
            _URL,
            json=_payload(target_policy_version=SCREENING_POLICY_VERSION + 1),
            headers=_HEADERS,
        )
        assert response.status_code == 422
        assert "implements" in response.json()["message"]

    async def test_stale_expected_revision_conflicts(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        activation_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, activation_maker)
        assert (
            await client.post(_URL, json=_payload(), headers=_HEADERS)
        ).status_code == 200
        response = await client.post(
            _URL, json=_payload(expected_revision=0), headers=_HEADERS
        )
        assert response.status_code == 409
        assert "refresh" in response.json()["message"]


class TestResolverDueActivation:
    async def test_due_activation_governs_and_clamps_to_the_build(
        self,
        activation_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        from ditto.api_server.screener_policy_activation import (
            resolve_screener_policy_activation,
        )

        async with activation_maker() as session:
            await insert_screener_policy_activation(
                session,
                parent_revision=0,
                target_policy_version=SCREENING_POLICY_VERSION,
                activate_at=datetime.now(UTC) - timedelta(minutes=1),
                rescreen_scored=True,
                reason="test: due activation governs the required version",
                actor="test",
            )
            await session.commit()
            policy = await resolve_screener_policy_activation(session)
            assert policy.required_policy_version == SCREENING_POLICY_VERSION
            assert policy.rescreen_stale_agents is True
            assert policy.rescreen_scored is True

    async def test_target_above_the_build_clamps_to_it(
        self,
        activation_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        from ditto.api_server.screener_policy_activation import (
            resolve_screener_policy_activation,
        )

        async with activation_maker() as session:
            # Simulates a rollback: a newer schedule row surviving on an older
            # build must never demand a version no deployed worker implements.
            await insert_screener_policy_activation(
                session,
                parent_revision=0,
                target_policy_version=SCREENING_POLICY_VERSION + 3,
                activate_at=datetime.now(UTC) - timedelta(minutes=1),
                rescreen_scored=False,
                reason="test: clamped to the deployed build's version",
                actor="test",
            )
            await session.commit()
            policy = await resolve_screener_policy_activation(session)
            assert policy.required_policy_version == SCREENING_POLICY_VERSION
