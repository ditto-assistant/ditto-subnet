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
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)

from ditto.api_models.agent_status import AgentStatus
from ditto.api_server.dependencies import get_session
from ditto.db.models import (
    Agent,
    BenchmarkRollout,
    Score,
    ScoredPolicyRescreenRelease,
    ScoredScreeningSnapshotRestoration,
    ScreenerHeartbeat,
    ScreeningAttempt,
)
from ditto.db.queries.screener_policy_activation import (
    insert_screener_policy_activation,
)
from ditto_screening_protocol import (
    SCREENING_ACTIVATION_CEILING_POLICY_VERSION,
    SCREENING_FLOOR_POLICY_VERSION,
    SCREENING_POLICY_VERSION,
)

pytestmark = pytest.mark.asyncio

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}
_URL = "/api/v1/admin/screener-policy-activation"
_CONFIRMATION = "SCHEDULE SCREENER POLICY ACTIVATION"
_RESTORE_CONFIRMATION = "RESTORE SCORED SCREENING SNAPSHOT"
_ADVANCE_CONFIRMATION = "ADVANCE SCORED POLICY RESCREEN"


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
    canary_only: bool = False,
) -> dict[str, object]:
    body: dict[str, object] = {
        "expected_revision": expected_revision,
        "target_policy_version": (
            target_policy_version
            if target_policy_version is not None
            else SCREENING_ACTIVATION_CEILING_POLICY_VERSION
        ),
        "activate_at": activate_at if activate_at is not None else _future(),
        "rescreen_scored": rescreen_scored,
        "canary_only": canary_only,
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
        assert (
            body["latest"]["target_policy_version"]
            == SCREENING_ACTIVATION_CEILING_POLICY_VERSION
        )
        assert body["latest"]["canary_only"] is False

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

    async def test_distributed_v13_is_not_activation_ready(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        activation_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, activation_maker)
        response = await client.post(
            _URL,
            json=_payload(target_policy_version=SCREENING_POLICY_VERSION),
            headers=_HEADERS,
        )

        assert response.status_code == 422
        assert "not activation-ready" in response.json()["message"]
        assert str(SCREENING_ACTIVATION_CEILING_POLICY_VERSION) in response.text

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
    async def test_due_activation_governs_at_the_activation_ceiling(
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
                target_policy_version=SCREENING_ACTIVATION_CEILING_POLICY_VERSION,
                activate_at=datetime.now(UTC) - timedelta(minutes=1),
                rescreen_scored=True,
                reason="test: due activation governs the required version",
                actor="test",
            )
            await session.commit()
            policy = await resolve_screener_policy_activation(session)
            assert (
                policy.required_policy_version
                == SCREENING_ACTIVATION_CEILING_POLICY_VERSION
            )
            assert policy.rescreen_stale_agents is True
            assert policy.rescreen_scored is True
            # A full activation still releases scored rows one at a time. The
            # global V11 requirement applies to fresh work, while the target
            # here fences each retained V10 score behind an explicit release.
            assert (
                policy.scored_rescreen_policy_version
                == SCREENING_ACTIVATION_CEILING_POLICY_VERSION
            )

    async def test_due_v13_row_does_not_override_valid_v12_activation(
        self,
        activation_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        from ditto.api_server.screener_policy_activation import (
            ScreenerPolicyActivationResolver,
            resolve_screener_policy_activation,
        )

        async with activation_maker() as session:
            v12 = await insert_screener_policy_activation(
                session,
                parent_revision=0,
                target_policy_version=SCREENING_ACTIVATION_CEILING_POLICY_VERSION,
                activate_at=datetime.now(UTC) - timedelta(minutes=2),
                rescreen_scored=True,
                reason="test: valid v12 activation remains authoritative",
                actor="test",
            )
            v13 = await insert_screener_policy_activation(
                session,
                parent_revision=v12.revision,
                target_policy_version=SCREENING_POLICY_VERSION,
                activate_at=datetime.now(UTC) - timedelta(minutes=1),
                rescreen_scored=True,
                reason="test: incomplete v13 activation must remain ineffective",
                actor="test",
            )
            await session.commit()

            policy = await resolve_screener_policy_activation(session)

            assert (
                policy.required_policy_version
                == SCREENING_ACTIVATION_CEILING_POLICY_VERSION
            )
            assert policy.governing_revision == v12.revision
            assert policy.latest_revision == v13.revision

            cached = await ScreenerPolicyActivationResolver(ttl_seconds=0).resolve(
                activation_maker
            )
            assert (
                cached.required_policy_version
                == SCREENING_ACTIVATION_CEILING_POLICY_VERSION
            )
            assert cached.governing_revision == v12.revision
            assert cached.latest_revision == v13.revision

    async def test_due_version_above_activation_ceiling_cannot_govern(
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
            assert policy.required_policy_version == SCREENING_FLOOR_POLICY_VERSION
            assert policy.scored_rescreen_policy_version is None

    async def test_due_canary_keeps_ordinary_queue_at_the_floor(
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
                target_policy_version=SCREENING_ACTIVATION_CEILING_POLICY_VERSION,
                activate_at=datetime.now(UTC) - timedelta(minutes=1),
                rescreen_scored=True,
                canary_only=True,
                reason="test: isolate one scored v11 policy canary",
                actor="test",
            )
            await session.commit()
            policy = await resolve_screener_policy_activation(session)
            assert policy.required_policy_version == SCREENING_FLOOR_POLICY_VERSION
            assert (
                policy.scored_rescreen_policy_version
                == SCREENING_ACTIVATION_CEILING_POLICY_VERSION
            )
            assert policy.rescreen_stale_agents is True


class TestRestoreScoredSnapshot:
    async def test_restores_exact_historical_pass_without_requeueing(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        activation_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, activation_maker)
        now = datetime.now(UTC)
        agent_id = uuid4()
        prior_attempt_id = uuid4()
        displaced_attempt_id = uuid4()
        async with activation_maker() as session:
            source = await insert_screener_policy_activation(
                session,
                parent_revision=0,
                target_policy_version=SCREENING_POLICY_VERSION,
                activate_at=now - timedelta(hours=2),
                rescreen_scored=True,
                reason="test source activation rescreened the scored cohort",
                actor="test",
            )
            current = await insert_screener_policy_activation(
                session,
                parent_revision=source.revision,
                target_policy_version=SCREENING_FLOOR_POLICY_VERSION,
                activate_at=now - timedelta(hours=1),
                rescreen_scored=False,
                reason="test rollback disabled automatic scored rescreening",
                actor="test",
            )
            session.add(
                Agent(
                    agent_id=agent_id,
                    miner_hotkey="5HK-restore-test",
                    name="restore-test",
                    sha256="12" * 32,
                    status=AgentStatus.SCREENING_FAILED,
                    screening_policy_version=SCREENING_POLICY_VERSION,
                    screening_reason="Screening was interrupted",
                    screening_reason_code="source-review-http-502",
                )
            )
            session.add_all(
                [
                    ScreeningAttempt(
                        attempt_id=prior_attempt_id,
                        agent_id=agent_id,
                        screener_hotkey="5Screener",
                        policy_version=SCREENING_FLOOR_POLICY_VERSION - 1,
                        status="passed",
                        started_at=now - timedelta(hours=4),
                        deadline=now - timedelta(hours=3, minutes=30),
                        finished_at=now - timedelta(hours=3, minutes=45),
                    ),
                    ScreeningAttempt(
                        attempt_id=displaced_attempt_id,
                        agent_id=agent_id,
                        screener_hotkey="5Screener",
                        policy_version=SCREENING_POLICY_VERSION,
                        status="failed",
                        started_at=now - timedelta(minutes=110),
                        deadline=now - timedelta(minutes=40),
                        finished_at=now - timedelta(minutes=100),
                        reason_code="source-review-http-502",
                    ),
                ]
            )
            for index in range(3):
                session.add(
                    Score(
                        agent_id=agent_id,
                        validator_hotkey=f"5Validator-{index}",
                        bench_version=12,
                        run_id=f"run-{index}",
                        signature=None,
                        seed=42,
                        composite=0.7,
                        tool_mean=0.7,
                        memory_mean=0.7,
                        median_ms=100,
                        n=10,
                        details=None,
                        generated_at=now - timedelta(hours=3),
                    )
                )
            await session.commit()

        response = await client.post(
            f"{_URL}/restore-scored-snapshot",
            headers=_HEADERS,
            json={
                "expected_current_activation_revision": current.revision,
                "source_activation_revision": source.revision,
                "source_policy_version": SCREENING_POLICY_VERSION,
                "target_policy_version": SCREENING_FLOOR_POLICY_VERSION,
                "bench_version": 12,
                "expected_count": 1,
                "reason": "restore the pre-v11 scored screening snapshot",
                "actor": "backroom:test",
                "confirmation": _RESTORE_CONFIRMATION,
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["restored_count"] == 1
        assert body["submissions"][0]["restored_policy_version"] == 9

        async with activation_maker() as session:
            agent = await session.get(Agent, agent_id)
            assert agent is not None
            assert agent.status == AgentStatus.SCORED
            assert agent.screening_policy_version == 9
            assert agent.screening_reason is None
            assert agent.screening_reason_code is None
            displaced = await session.get(ScreeningAttempt, displaced_attempt_id)
            assert displaced is not None and displaced.status == "failed"
            audits = list(
                await session.scalars(
                    select(ScoredScreeningSnapshotRestoration).where(
                        ScoredScreeningSnapshotRestoration.agent_id == agent_id
                    )
                )
            )
            assert len(audits) == 1
            assert audits[0].restored_attempt_id == prior_attempt_id
            assert audits[0].score_count == 3

    async def test_expected_count_guard_leaves_cohort_untouched(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        activation_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, activation_maker)
        now = datetime.now(UTC)
        async with activation_maker() as session:
            source = await insert_screener_policy_activation(
                session,
                parent_revision=0,
                target_policy_version=SCREENING_POLICY_VERSION,
                activate_at=now - timedelta(hours=2),
                rescreen_scored=True,
                reason="test source activation rescreened the scored cohort",
                actor="test",
            )
            current = await insert_screener_policy_activation(
                session,
                parent_revision=source.revision,
                target_policy_version=SCREENING_FLOOR_POLICY_VERSION,
                activate_at=now - timedelta(hours=1),
                rescreen_scored=False,
                reason="test rollback disabled automatic scored rescreening",
                actor="test",
            )
            await session.commit()

        response = await client.post(
            f"{_URL}/restore-scored-snapshot",
            headers=_HEADERS,
            json={
                "expected_current_activation_revision": current.revision,
                "source_activation_revision": source.revision,
                "source_policy_version": SCREENING_POLICY_VERSION,
                "target_policy_version": SCREENING_FLOOR_POLICY_VERSION,
                "bench_version": 12,
                "expected_count": 1,
                "reason": "restore the pre-v11 scored screening snapshot",
                "confirmation": _RESTORE_CONFIRMATION,
            },
        )
        assert response.status_code == 409
        assert "expected 1, current 0" in response.json()["message"]


class TestScoredPolicyRescreenCheckpoint:
    async def test_isolated_canary_requires_an_immutable_l4_enforce_revision(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        activation_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """No V11 canary may escape the global review-off posture unaudited."""
        _install(app, activation_maker)
        async with activation_maker() as session:
            activation = await insert_screener_policy_activation(
                session,
                parent_revision=0,
                target_policy_version=SCREENING_ACTIVATION_CEILING_POLICY_VERSION,
                activate_at=datetime.now(UTC) - timedelta(minutes=5),
                rescreen_scored=True,
                canary_only=True,
                reason="test canary must bind immutable L4-enforce settings",
                actor="test",
            )
            await session.commit()

        response = await client.post(
            f"{_URL}/advance-scored-rescreen",
            headers=_HEADERS,
            json={
                "expected_activation_revision": activation.revision,
                "expected_agent_id": str(uuid4()),
                "reason": "do not release a canary without a fixed L4 posture",
                "confirmation": _ADVANCE_CONFIRMATION,
            },
        )

        assert response.status_code == 422
        assert "immutable L4-enforce review settings" in response.json()["message"]

    async def test_fills_a_bounded_top_down_window_without_changing_the_board(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        activation_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A V11 policy activation cannot de-list the V10 board as a batch."""
        _install(app, activation_maker)
        now = datetime.now(UTC)
        first_id, second_id, third_id = uuid4(), uuid4(), uuid4()
        async with activation_maker() as session:
            activation = await insert_screener_policy_activation(
                session,
                parent_revision=0,
                target_policy_version=SCREENING_ACTIVATION_CEILING_POLICY_VERSION,
                activate_at=now - timedelta(minutes=5),
                rescreen_scored=True,
                reason="canary v11 scored policy rollout retains the v10 board",
                actor="test",
            )
            session.add(
                BenchmarkRollout(
                    rollout_id=uuid4(),
                    from_version=11,
                    desired_version=12,
                    status="activated",
                    cohort_size=5,
                    created_at=now - timedelta(days=1),
                    activated_at=now - timedelta(hours=1),
                )
            )
            for agent_id, name, composite in (
                (first_id, "first", 0.9),
                (second_id, "second", 0.8),
                (third_id, "third", 0.7),
            ):
                session.add(
                    Agent(
                        agent_id=agent_id,
                        miner_hotkey=f"5HK-rescreen-{name}",
                        name=name,
                        sha256=("ab" if name == "first" else "cd") * 32,
                        status=AgentStatus.SCORED,
                        screening_policy_version=SCREENING_FLOOR_POLICY_VERSION,
                    )
                )
                for index in range(3):
                    session.add(
                        Score(
                            agent_id=agent_id,
                            validator_hotkey=f"5Validator-{name}-{index}",
                            bench_version=12,
                            run_id=f"run-{name}-{index}",
                            signature=None,
                            seed=42,
                            composite=composite,
                            tool_mean=composite,
                            memory_mean=composite,
                            median_ms=100,
                            n=10,
                            details=None,
                            generated_at=now - timedelta(hours=2),
                        )
                    )
            await session.commit()

        checkpoint = await client.get(f"{_URL}/scored-rescreen", headers=_HEADERS)
        assert checkpoint.status_code == 200, checkpoint.text
        assert checkpoint.json() == {
            "activation_revision": activation.revision,
            "target_policy_version": SCREENING_ACTIVATION_CEILING_POLICY_VERSION,
            "current": None,
            "active": [],
            "next_agent_id": str(first_id),
            "next_position": 1,
        }

        first_release = {
            "expected_activation_revision": activation.revision,
            "expected_agent_id": str(first_id),
            "max_active_releases": 2,
            "reason": "release the first v11 result for operator inspection",
            "confirmation": _ADVANCE_CONFIRMATION,
        }
        response = await client.post(
            f"{_URL}/advance-scored-rescreen",
            headers=_HEADERS,
            json=first_release,
        )
        assert response.status_code == 200, response.text
        assert response.json()["current"] == {
            "activation_revision": activation.revision,
            "target_policy_version": SCREENING_ACTIVATION_CEILING_POLICY_VERSION,
            "agent_id": str(first_id),
            "position": 1,
            "state": "pending",
            "attempt_id": None,
            "review_settings_revision": None,
        }
        assert [row["agent_id"] for row in response.json()["active"]] == [
            str(first_id),
            str(second_id),
        ]
        # The rollout window is full. The original V10 scores remain eligible
        # while the two V11 attestations wait, and no whole-board requeue can
        # be smuggled through a second checkpoint call.
        blocked = await client.post(
            f"{_URL}/advance-scored-rescreen",
            headers=_HEADERS,
            json=first_release,
        )
        assert blocked.status_code == 409
        async with activation_maker() as session:
            first = await session.get(Agent, first_id)
            second = await session.get(Agent, second_id)
            third = await session.get(Agent, third_id)
            assert first is not None and first.status == AgentStatus.SCORED
            assert second is not None and second.status == AgentStatus.SCORED
            assert third is not None and third.status == AgentStatus.SCORED
            release = await session.scalar(
                select(ScoredPolicyRescreenRelease).where(
                    ScoredPolicyRescreenRelease.agent_id == first_id
                )
            )
            assert release is not None
            release.state = "terminal"
            first.screening_policy_version = SCREENING_POLICY_VERSION
            await session.commit()

        # A terminal verdict frees exactly one slot. The next explicit
        # checkpoint backfills it with the next ranked agent, not every stale
        # score on the board.
        next_checkpoint = await client.get(f"{_URL}/scored-rescreen", headers=_HEADERS)
        assert next_checkpoint.status_code == 200, next_checkpoint.text
        assert next_checkpoint.json()["current"]["agent_id"] == str(second_id)
        assert [row["agent_id"] for row in next_checkpoint.json()["active"]] == [
            str(second_id)
        ]
        assert next_checkpoint.json()["next_agent_id"] == str(third_id)
        assert next_checkpoint.json()["next_position"] == 3

        refill = await client.post(
            f"{_URL}/advance-scored-rescreen",
            headers=_HEADERS,
            json={
                **first_release,
                "expected_agent_id": str(third_id),
            },
        )
        assert refill.status_code == 200, refill.text
        assert [row["agent_id"] for row in refill.json()["active"]] == [
            str(second_id),
            str(third_id),
        ]

        # A non-verdict keeps the retained score on the board but stops any
        # further window growth until an operator explicitly retries that row.
        async with activation_maker() as session:
            release = await session.scalar(
                select(ScoredPolicyRescreenRelease).where(
                    ScoredPolicyRescreenRelease.agent_id == second_id
                )
            )
            assert release is not None
            release.state = "paused"
            await session.commit()

        paused = await client.post(
            f"{_URL}/advance-scored-rescreen",
            headers=_HEADERS,
            json={**first_release, "expected_agent_id": str(third_id)},
        )
        assert paused.status_code == 409
        retry = await client.post(
            f"{_URL}/advance-scored-rescreen",
            headers=_HEADERS,
            json={
                **first_release,
                "expected_agent_id": str(second_id),
                "retry_paused": True,
            },
        )
        assert retry.status_code == 200, retry.text
        assert retry.json()["current"]["agent_id"] == str(second_id)
        assert retry.json()["current"]["state"] == "pending"


def _fleet_heartbeat(
    instance_id: str,
    *,
    protocol_version: int,
    policy_version: int,
    release: dict[str, object] | None,
    seen_at: datetime,
) -> ScreenerHeartbeat:
    envelope: dict[str, object] = {
        "system_metrics": None,
        "screening_progress": None,
        "review_settings": None,
        "host_specs": None,
    }
    if release is not None:
        envelope["release"] = release
    return ScreenerHeartbeat(
        screener_hotkey="5EKvqERH4xCV2MuQwb8cenyCVayfvrfjHaoeDPb9RFXxbsND",
        instance_id=instance_id,
        software_version="0.21.2",
        protocol_version=protocol_version,
        policy_version=policy_version,
        state="polling",
        system_metrics=envelope,
        first_seen_at=seen_at,
        reported_at=seen_at,
        seen_at=seen_at,
        signature="ab" * 64,
    )


class TestFleetReadiness:
    async def test_read_summarizes_fresh_v7_heartbeats(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        activation_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, activation_maker)
        now = datetime.now(UTC)
        current = SCREENING_POLICY_VERSION
        async with activation_maker() as session, session.begin():
            session.add_all(
                [
                    _fleet_heartbeat(
                        "subnet-screener-1-worker-1",
                        protocol_version=7,
                        policy_version=current,
                        release={
                            "builtin_policy_version": current,
                            "revision": "c393bc10488ee0b203d08e6d29df877d508f25d9",
                            "version": "0.230.0",
                            "activated_at": 1788717939,
                        },
                        seen_at=now,
                    ),
                    _fleet_heartbeat(
                        "subnet-screener-1-worker-2",
                        protocol_version=7,
                        policy_version=current - 1,
                        release={"builtin_policy_version": current - 1},
                        seen_at=now,
                    ),
                    _fleet_heartbeat(
                        "legacy-v6-worker",
                        protocol_version=6,
                        policy_version=current - 1,
                        release=None,
                        seen_at=now,
                    ),
                    _fleet_heartbeat(
                        "stale-worker",
                        protocol_version=7,
                        policy_version=current,
                        release={"builtin_policy_version": current + 9},
                        seen_at=now - timedelta(hours=3),
                    ),
                ]
            )
        response = await client.get(_URL, headers=_HEADERS)
        assert response.status_code == 200, response.text
        fleet = response.json()["fleet"]
        # The stale worker is ignored; the v6 worker cannot announce a build.
        assert fleet["instances_reporting"] == 3
        assert fleet["instances_with_release"] == 2
        assert fleet["instances_without_release"] == ["legacy-v6-worker"]
        assert fleet["min_builtin_policy_version"] == current - 1
        assert fleet["max_builtin_policy_version"] == current
        assert fleet["lagging_instances"] == ["subnet-screener-1-worker-2"]
        # A worker that cannot announce its build makes the ceiling unknown.
        assert fleet["safe_to_schedule_up_to"] is None
        assert fleet["release_revisions"] == [
            "c393bc10488ee0b203d08e6d29df877d508f25d9"
        ]
        assert fleet["release_versions"] == ["0.230.0"]

    async def test_read_reports_a_safe_ceiling_when_every_worker_announces(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        activation_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, activation_maker)
        now = datetime.now(UTC)
        async with activation_maker() as session, session.begin():
            session.add(
                _fleet_heartbeat(
                    "subnet-screener-1-worker-1",
                    protocol_version=7,
                    policy_version=SCREENING_POLICY_VERSION,
                    release={"builtin_policy_version": SCREENING_POLICY_VERSION},
                    seen_at=now,
                )
            )
        response = await client.get(_URL, headers=_HEADERS)
        assert response.status_code == 200, response.text
        fleet = response.json()["fleet"]
        assert (
            fleet["safe_to_schedule_up_to"]
            == SCREENING_ACTIVATION_CEILING_POLICY_VERSION
        )
        assert fleet["lagging_instances"] == []
        assert fleet["instances_without_release"] == []
