"""Contract tests for the operator-controlled validator-queue policy board."""

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)

from ditto.api_server.dependencies import get_session
from ditto.db.models import BenchmarkRollout

pytestmark = pytest.mark.asyncio

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}
_URL = "/api/v1/admin/queue-policy-settings"
_CONFIRMATION = "APPLY QUEUE POLICY SETTINGS"


@pytest.fixture
def settings_maker(
    session_maker: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    """Local alias for the root Postgres ``session_maker``."""
    return session_maker


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_ADMIN_TOKEN)
    app.state.session_maker = maker

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


def _settings(**overrides: object) -> dict[str, object]:
    """A COMPLETE policy body.

    The board stores the whole policy, so the endpoint requires every field to be
    present; sending a partial body is its own (tested) 422. Helpers therefore
    always start from the full shipped default.
    """
    settings: dict[str, object] = {
        "rescore_cohort_size": 25,
        "priority_cohort_size": 5,
        "lane_cycle_size": 4,
        "fresh_submission_slots": [0, 1, 3],
        "owner_concurrent_submission_limit": 2,
        "similarity_budget": {
            "enabled": True,
            "concurrent_submission_limit": 1,
            "jaccard_threshold": 0.9,
            "containment_threshold": 0.95,
        },
        "prev_gen_carryover": {
            "enabled": False,
            "max_agents": 10,
            "min_score_count": 2,
            "include_exhausted": False,
            "dedupe_scope": "coldkey",
            "require_cohort_complete": True,
            "require_desired_era_drained": True,
        },
        "deferred_source_review": {
            "mode": "off",
            "min_cohort_size": 8,
            "composite_mad_multiplier": 6.0,
            "axis_mad_multiplier": 6.0,
            "min_composite_delta": 0.10,
            "min_axis_delta": 0.15,
        },
    }
    settings.update(overrides)
    return settings


def _payload(
    *,
    expected_revision: int = 0,
    confirmation: str = _CONFIRMATION,
    settings: dict[str, object] | None = None,
    **setting_overrides: object,
) -> dict[str, object]:
    return {
        "scope": "*",
        "expected_revision": expected_revision,
        "settings": settings
        if settings is not None
        else _settings(**setting_overrides),
        "reason": "the subnet is scaling; widen the rescore cohort to 25",
        "actor": "backroom:test",
        "confirmation": confirmation,
    }


class TestAuth:
    async def test_read_requires_the_admin_token(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        assert (await client.get(_URL)).status_code == 401

    async def test_write_requires_the_admin_token(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        assert (await client.post(_URL, json=_payload())).status_code == 401


class TestDefaultAndRoundTrip:
    async def test_default_is_the_historical_ten_at_revision_zero(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        body = (await client.get(_URL, headers=_HEADERS)).json()
        assert body["current"] == []
        assert body["default"]["rescore_cohort_size"] == 10
        assert body["effective"]["revision"] == 0
        assert body["effective"]["source"] == "default"
        assert body["effective"]["settings"]["rescore_cohort_size"] == 10
        assert body["effective"]["min_cohort_size"] == 5
        assert body["effective"]["max_cohort_size"] == 25
        assert body["effective"]["open_rollout_desired_version"] is None
        assert body["effective"]["open_rollout_rescore_cohort_target"] is None
        assert body["effective"]["open_rollout_priority_cohort_target"] is None
        assert body["effective"]["open_rollout_overrides_setting"] is False
        assert body["effective"]["rollout_is_open"] is False
        # Every shipped default must equal the constant it replaced, or merging
        # this board silently retunes the live queue.
        assert body["default"]["priority_cohort_size"] == 5
        assert body["default"]["lane_cycle_size"] == 4
        assert body["default"]["fresh_submission_slots"] == [0, 1, 3]
        # ``max_agents`` is the one carryover default that is deliberately NOT
        # the historical value: the top 25 of the closing generation qualify to
        # enter the new era, matching max_cohort_size above. It is inert while
        # ``enabled`` is False, which is why widening it is not a queue retune.
        assert body["default"]["prev_gen_carryover"] == {
            "enabled": False,
            "max_agents": 25,
            "min_score_count": 2,
            "include_exhausted": False,
            "dedupe_scope": "coldkey",
            "require_cohort_complete": True,
            "require_desired_era_drained": True,
        }

    async def test_apply_then_get_reflects_the_revision(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        created = await client.post(_URL, headers=_HEADERS, json=_payload())
        assert created.status_code == 200, created.text
        assert created.json()["revision"] == 1
        assert created.json()["settings"]["rescore_cohort_size"] == 25
        assert created.json()["actor"] == "backroom:test"

        body = (await client.get(_URL, headers=_HEADERS)).json()
        assert len(body["current"]) == 1
        assert body["current"][0]["revision"] == 1
        assert body["effective"]["source"] == "revision"
        assert body["effective"]["settings"]["rescore_cohort_size"] == 25

    async def test_history_accumulates_append_only(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        await client.post(_URL, headers=_HEADERS, json=_payload(rescore_cohort_size=15))
        await client.post(
            _URL,
            headers=_HEADERS,
            json=_payload(expected_revision=1, rescore_cohort_size=25),
        )
        body = (await client.get(_URL, headers=_HEADERS)).json()
        assert [row["revision"] for row in body["history"]] == [2, 1]
        assert [row["settings"]["rescore_cohort_size"] for row in body["history"]] == [
            25,
            15,
        ]


class TestOpenRolloutIsReportedAsFrozen:
    async def test_open_rollout_target_is_surfaced_beside_the_setting(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """The operator must be able to see that raising it changes nothing yet."""
        _install(app, settings_maker)
        async with settings_maker() as session, session.begin():
            session.add(
                BenchmarkRollout(
                    rollout_id=uuid4(),
                    from_version=6,
                    desired_version=7,
                    status="collecting",
                    cohort_size=10,
                    rescore_cohort_target=10,
                    priority_cohort_target=5,
                    created_at=datetime.now(UTC),
                )
            )

        await client.post(_URL, headers=_HEADERS, json=_payload())
        effective = (await client.get(_URL, headers=_HEADERS)).json()["effective"]
        assert effective["settings"]["rescore_cohort_size"] == 25
        assert effective["open_rollout_desired_version"] == 7
        assert effective["open_rollout_rescore_cohort_target"] == 10
        assert effective["open_rollout_priority_cohort_target"] == 5
        assert effective["open_rollout_overrides_setting"] is True
        assert effective["rollout_is_open"] is True


class TestConfirmationAndConcurrency:
    async def test_wrong_confirmation_is_refused(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        response = await client.post(
            _URL, headers=_HEADERS, json=_payload(confirmation="APPLY")
        )
        assert response.status_code == 409
        assert _CONFIRMATION in response.json()["message"]
        assert (await client.get(_URL, headers=_HEADERS)).json()["current"] == []

    async def test_stale_expected_revision_is_refused(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        await client.post(_URL, headers=_HEADERS, json=_payload())
        response = await client.post(_URL, headers=_HEADERS, json=_payload())
        assert response.status_code == 409
        assert "expected 0, current 1" in response.json()["message"]

    async def test_non_global_scope_is_refused(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        response = await client.post(
            _URL, headers=_HEADERS, json={**_payload(), "scope": "board-1"}
        )
        assert response.status_code == 422


class TestValidation:
    @pytest.mark.parametrize("size", [4, 0, -1, 26, 100])
    async def test_out_of_range_sizes_fail_closed(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
        size: int,
    ) -> None:
        _install(app, settings_maker)
        response = await client.post(
            _URL, headers=_HEADERS, json=_payload(rescore_cohort_size=size)
        )
        assert response.status_code == 422
        assert (await client.get(_URL, headers=_HEADERS)).json()["current"] == []

    @pytest.mark.parametrize("size", [5, 10, 25])
    async def test_in_range_sizes_are_accepted(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
        size: int,
    ) -> None:
        _install(app, settings_maker)
        response = await client.post(
            _URL, headers=_HEADERS, json=_payload(rescore_cohort_size=size)
        )
        assert response.status_code == 200, response.text

    async def test_short_reason_is_refused(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        response = await client.post(
            _URL, headers=_HEADERS, json={**_payload(), "reason": "big"}
        )
        assert response.status_code == 422

    async def test_unknown_knob_is_refused(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # extra="forbid": a typo'd knob must not be silently stored as no-op
        # policy that an operator then believes is in force.
        _install(app, settings_maker)
        response = await client.post(
            _URL,
            headers=_HEADERS,
            json={
                **_payload(),
                "settings": {"rescore_cohort_size": 25, "cohort_size": 25},
            },
        )
        assert response.status_code == 422


class TestWholePolicyWrites:
    """A revision stores the whole policy, so a partial body must not silently
    reset the fields it omits."""

    async def test_partial_settings_are_refused_and_name_the_missing_fields(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        response = await client.post(
            _URL,
            headers=_HEADERS,
            json=_payload(settings={"rescore_cohort_size": 25}),
        )
        assert response.status_code == 422
        assert (await client.get(_URL, headers=_HEADERS)).json()["current"] == []

    async def test_partial_carryover_is_refused(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        response = await client.post(
            _URL,
            headers=_HEADERS,
            json=_payload(prev_gen_carryover={"enabled": True}),
        )
        assert response.status_code == 422

    async def test_enabling_carryover_round_trips(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        carryover = {
            "enabled": True,
            "max_agents": 16,
            "min_score_count": 2,
            "include_exhausted": False,
            "dedupe_scope": "coldkey",
            "require_cohort_complete": True,
            "require_desired_era_drained": True,
        }
        created = await client.post(
            _URL, headers=_HEADERS, json=_payload(prev_gen_carryover=carryover)
        )
        assert created.status_code == 200, created.text
        effective = (await client.get(_URL, headers=_HEADERS)).json()["effective"]
        assert effective["settings"]["prev_gen_carryover"] == carryover


class TestLaneFloors:
    """The lane split may be retuned but never collapsed onto one lane."""

    async def test_empty_fresh_lane_is_refused(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Starving new miners is the exact failure the lane split prevents."""
        _install(app, settings_maker)
        response = await client.post(
            _URL, headers=_HEADERS, json=_payload(fresh_submission_slots=[])
        )
        assert response.status_code == 422
        assert (await client.get(_URL, headers=_HEADERS)).json()["current"] == []

    async def test_fresh_lane_filling_the_whole_cycle_is_refused(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """With no cohort slot an open rollout could never reach quorum."""
        _install(app, settings_maker)
        response = await client.post(
            _URL,
            headers=_HEADERS,
            json=_payload(lane_cycle_size=4, fresh_submission_slots=[0, 1, 2, 3]),
        )
        assert response.status_code == 422

    async def test_slots_outside_the_cycle_are_refused(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        response = await client.post(
            _URL,
            headers=_HEADERS,
            json=_payload(lane_cycle_size=4, fresh_submission_slots=[0, 1, 7]),
        )
        assert response.status_code == 422

    async def test_duplicate_slots_are_refused(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        response = await client.post(
            _URL,
            headers=_HEADERS,
            json=_payload(lane_cycle_size=4, fresh_submission_slots=[0, 1, 1]),
        )
        assert response.status_code == 422

    async def test_priority_cohort_above_rescore_cohort_is_refused(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """The gate would wait on positions the cohort never fills."""
        _install(app, settings_maker)
        response = await client.post(
            _URL,
            headers=_HEADERS,
            json=_payload(rescore_cohort_size=10, priority_cohort_size=25),
        )
        assert response.status_code == 422

    async def test_a_wider_but_coherent_split_is_accepted(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        response = await client.post(
            _URL,
            headers=_HEADERS,
            json=_payload(lane_cycle_size=6, fresh_submission_slots=[0, 1, 2, 4]),
        )
        assert response.status_code == 200, response.text
        effective = (await client.get(_URL, headers=_HEADERS)).json()["effective"]
        assert effective["settings"]["lane_cycle_size"] == 6
        assert effective["settings"]["fresh_submission_slots"] == [0, 1, 2, 4]


class TestLaneModulusIsLockedDuringARollout:
    """Changing the lane cycle mid-rollout reassigns every validator's position
    in it, because the counter is measured from rollout start."""

    @staticmethod
    async def _open_rollout(maker: async_sessionmaker[AsyncSession]) -> None:
        async with maker() as session, session.begin():
            session.add(
                BenchmarkRollout(
                    rollout_id=uuid4(),
                    from_version=6,
                    desired_version=7,
                    status="collecting",
                    cohort_size=10,
                    rescore_cohort_target=10,
                    priority_cohort_target=5,
                    created_at=datetime.now(UTC),
                )
            )

    async def test_changing_the_cycle_size_is_refused_while_open(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        await self._open_rollout(settings_maker)
        response = await client.post(
            _URL,
            headers=_HEADERS,
            json=_payload(lane_cycle_size=6, fresh_submission_slots=[0, 1, 2, 4]),
        )
        assert response.status_code == 409
        assert "lane_cycle_size" in response.json()["message"]
        assert (await client.get(_URL, headers=_HEADERS)).json()["current"] == []

    async def test_changing_only_the_slot_set_is_refused_while_open(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        await self._open_rollout(settings_maker)
        response = await client.post(
            _URL, headers=_HEADERS, json=_payload(fresh_submission_slots=[0, 1, 2])
        )
        assert response.status_code == 409
        assert "fresh_submission_slots" in response.json()["message"]

    async def test_other_fields_stay_writable_while_open(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """The lock is per-field: an operator must still be able to retune the
        cohort sizes and the carryover gate during an incident."""
        _install(app, settings_maker)
        await self._open_rollout(settings_maker)
        response = await client.post(
            _URL,
            headers=_HEADERS,
            json=_payload(
                rescore_cohort_size=20,
                prev_gen_carryover={
                    "enabled": True,
                    "max_agents": 27,
                    "min_score_count": 2,
                    "include_exhausted": False,
                    "dedupe_scope": "coldkey",
                    "require_cohort_complete": True,
                    "require_desired_era_drained": True,
                },
            ),
        )
        assert response.status_code == 200, response.text

    async def test_resending_the_same_lane_values_is_allowed_while_open(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A whole-policy write that leaves the lane alone is not a lane change,
        which is what makes the per-field lock usable at all."""
        _install(app, settings_maker)
        await self._open_rollout(settings_maker)
        response = await client.post(
            _URL,
            headers=_HEADERS,
            json=_payload(lane_cycle_size=4, fresh_submission_slots=[0, 1, 3]),
        )
        assert response.status_code == 200, response.text

    async def test_the_same_lane_change_is_accepted_once_no_rollout_is_open(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        response = await client.post(
            _URL,
            headers=_HEADERS,
            json=_payload(lane_cycle_size=6, fresh_submission_slots=[0, 1, 2, 4]),
        )
        assert response.status_code == 200, response.text
