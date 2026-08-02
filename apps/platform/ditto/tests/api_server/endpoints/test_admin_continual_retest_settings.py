"""Contract tests for hot-swappable continual-retest settings."""

from collections.abc import AsyncIterator
from dataclasses import replace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)

from ditto.api_server.continual_retest_settings import aggregate_is_active
from ditto.api_server.dependencies import get_session

pytestmark = pytest.mark.asyncio

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}
_URL = "/api/v1/admin/continual-retest-settings"


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
    app.state.continual_retest_settings.invalidate()


def _payload(
    *,
    expected_revision: int = 0,
    aggregate_mode: str = "fleet_ready",
    idle_retests_enabled: bool = False,
    rollout_standdown: str = "capable_validators",
    retest_cohort_size: int = 5,
    **band: object,
) -> dict[str, object]:
    settings: dict[str, object] = {
        "aggregate_mode": aggregate_mode,
        "idle_retests_enabled": idle_retests_enabled,
        "rollout_standdown": rollout_standdown,
        "retest_cohort_size": retest_cohort_size,
    }
    settings.update(band)
    return {
        "expected_revision": expected_revision,
        "scope": "*",
        "settings": settings,
        "reason": "operator-approved continual retest policy change",
        "actor": "operator@example.com",
        "confirmation": "APPLY CONTINUAL RETEST SETTINGS",
    }


async def test_defaults_are_safe_and_revision_is_audited(
    app: FastAPI,
    client: httpx.AsyncClient,
    settings_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, settings_maker)

    initial = await client.get(_URL, headers=_HEADERS)
    assert initial.status_code == 200, initial.text
    assert initial.json()["effective"]["settings"] == {
        "aggregate_mode": "fleet_ready",
        "idle_retests_enabled": False,
        "rollout_standdown": "capable_validators",
        "retest_cohort_size": 5,
        # The tie band ships INERT: "fixed" is the historical rank cutoff, so
        # merging the band changes no validator's cohort until an operator
        # switches the mode. z and the ceiling are pre-set for that moment.
        "retest_eligibility_mode": "fixed",
        "retest_eligibility_z": 1.64,
        "retest_cohort_max_size": 25,
        # NOT a no-op on merge: this one feeds the weight fold, and it ships
        # deliberately widened. "strict" is what emptied the intersection at
        # 03:56Z and reverted every agent to its quorum median; it stays
        # reachable as the one-revision rollback.
        "wave_membership": "participants",
    }
    assert initial.json()["effective"]["open_rollout_desired_version"] is None
    assert initial.json()["effective"]["rollout_standdown_active"] is False
    # The page renders the dial's bounds from the platform, never its own copy.
    assert initial.json()["effective"]["emission_set_size"] == 5
    assert initial.json()["effective"]["max_retest_cohort_size"] == 25
    assert initial.json()["effective"]["max_retest_eligibility_z"] == 3.0
    assert initial.json()["effective"]["eligible_agent_count"] == 0
    # Nothing on the board, so the cohort the rule resolves to is empty.
    assert initial.json()["effective"]["resolved_cohort_size"] == 0

    updated = await client.post(
        _URL,
        headers=_HEADERS,
        json=_payload(aggregate_mode="enabled", idle_retests_enabled=True),
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["revision"] == 1
    assert body["parent_revision"] == 0
    assert body["settings"]["aggregate_mode"] == "enabled"
    assert body["settings"]["idle_retests_enabled"] is True
    assert body["actor"] == "operator@example.com"

    refreshed = await client.get(_URL, headers=_HEADERS)
    assert refreshed.status_code == 200
    assert refreshed.json()["effective"]["revision"] == 1
    assert refreshed.json()["effective"]["aggregate_active"] is True


async def test_rejects_stale_revision_and_wrong_confirmation(
    app: FastAPI,
    client: httpx.AsyncClient,
    settings_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, settings_maker)
    first = await client.post(_URL, headers=_HEADERS, json=_payload())
    assert first.status_code == 200

    stale = await client.post(_URL, headers=_HEADERS, json=_payload())
    assert stale.status_code == 409

    wrong = _payload(expected_revision=1)
    wrong["confirmation"] = "ENABLE RETESTS"
    confirmation = await client.post(_URL, headers=_HEADERS, json=wrong)
    assert confirmation.status_code == 409


async def test_aggregate_modes_are_explicit() -> None:
    from ditto.api_models.continual_retest_settings import ContinualRetestSettings

    assert aggregate_is_active(
        ContinualRetestSettings(aggregate_mode="fleet_ready"),
        fleet_protocol_ready=True,
    )
    assert not aggregate_is_active(
        ContinualRetestSettings(aggregate_mode="fleet_ready"),
        fleet_protocol_ready=False,
    )
    assert aggregate_is_active(
        ContinualRetestSettings(aggregate_mode="enabled"),
        fleet_protocol_ready=False,
    )
    assert not aggregate_is_active(
        ContinualRetestSettings(aggregate_mode="disabled"),
        fleet_protocol_ready=True,
    )


async def test_open_rollout_surfaces_the_standdown_state(
    app: FastAPI,
    client: httpx.AsyncClient,
    settings_maker: async_sessionmaker[AsyncSession],
) -> None:
    """An operator must be able to see why retests went quiet."""
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from ditto.db.models import BenchmarkRollout

    _install(app, settings_maker)
    async with settings_maker() as session, session.begin():
        session.add(
            BenchmarkRollout(
                rollout_id=uuid4(),
                from_version=6,
                desired_version=7,
                status="collecting",
                cohort_size=5,
                created_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )

    collecting = await client.get(_URL, headers=_HEADERS)
    assert collecting.status_code == 200, collecting.text
    effective = collecting.json()["effective"]
    assert effective["open_rollout_desired_version"] == 7
    assert effective["rollout_standdown_active"] is True

    override = _payload(rollout_standdown="off")
    applied = await client.post(_URL, headers=_HEADERS, json=override)
    assert applied.status_code == 200, applied.text
    assert applied.json()["settings"]["rollout_standdown"] == "off"

    forced = await client.get(_URL, headers=_HEADERS)
    assert forced.json()["effective"]["open_rollout_desired_version"] == 7
    assert forced.json()["effective"]["rollout_standdown_active"] is False


async def test_retest_cohort_size_is_operator_controlled_and_bounded(
    app: FastAPI,
    client: httpx.AsyncClient,
    settings_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Top-10 and top-25 are one audited revision; outside the band is refused."""
    _install(app, settings_maker)

    widened = await client.post(
        _URL, headers=_HEADERS, json=_payload(retest_cohort_size=10)
    )
    assert widened.status_code == 200, widened.text
    assert widened.json()["settings"]["retest_cohort_size"] == 10

    effective = (await client.get(_URL, headers=_HEADERS)).json()["effective"]
    assert effective["settings"]["retest_cohort_size"] == 10

    top25 = await client.post(
        _URL,
        headers=_HEADERS,
        json=_payload(expected_revision=1, retest_cohort_size=25),
    )
    assert top25.status_code == 200, top25.text
    assert top25.json()["settings"]["retest_cohort_size"] == 25

    # Below the emission set the lane could not do its consensus job at all;
    # above the cap it would spend the whole fleet on rescores.
    for rejected in (4, 26):
        response = await client.post(
            _URL,
            headers=_HEADERS,
            json=_payload(expected_revision=2, retest_cohort_size=rejected),
        )
        assert response.status_code == 422, response.text


async def test_the_tie_band_is_operator_controlled_and_bounded(
    app: FastAPI,
    client: httpx.AsyncClient,
    settings_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The band is one audited revision away, with both rails enforced."""
    _install(app, settings_maker)

    applied = await client.post(
        _URL,
        headers=_HEADERS,
        json=_payload(
            retest_cohort_size=10,
            retest_eligibility_mode="statistical",
            retest_eligibility_z=1.64,
            retest_cohort_max_size=25,
        ),
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["settings"]["retest_eligibility_mode"] == "statistical"
    assert applied.json()["settings"]["retest_eligibility_z"] == 1.64

    # z is bounded: past three the band stops meaning "tied".
    over_z = await client.post(
        _URL,
        headers=_HEADERS,
        json=_payload(expected_revision=1, retest_eligibility_z=3.5),
    )
    assert over_z.status_code == 422, over_z.text

    # The ceiling is bounded by the same cap as the cohort itself.
    over_ceiling = await client.post(
        _URL,
        headers=_HEADERS,
        json=_payload(expected_revision=1, retest_cohort_max_size=26),
    )
    assert over_ceiling.status_code == 422, over_ceiling.text

    # A ceiling below the requested cohort would cut into the fixed rank.
    incoherent = await client.post(
        _URL,
        headers=_HEADERS,
        json=_payload(
            expected_revision=1,
            retest_cohort_size=20,
            retest_cohort_max_size=10,
        ),
    )
    assert incoherent.status_code == 422, incoherent.text


async def test_stored_revisions_predating_the_tie_band_still_load() -> None:
    """A revision written before the band exists resolves to the fixed cutoff.

    The board is append-only, so every revision Peyton has already written must
    keep meaning exactly what it meant when he wrote it.
    """
    from ditto.api_server.continual_retest_settings import settings_from_row

    class _Row:
        revision = 4
        settings = {
            "aggregate_mode": "enabled",
            "idle_retests_enabled": True,
            "rollout_standdown": "all",
            "retest_cohort_size": 25,
        }

    resolved = settings_from_row(_Row())  # type: ignore[arg-type]
    assert resolved.retest_cohort_size == 25
    assert resolved.retest_eligibility_mode == "fixed"


async def test_wave_membership_is_operator_controlled(
    app: FastAPI,
    client: httpx.AsyncClient,
    settings_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The mode that moves the weight fold is one audited revision away."""
    _install(app, settings_maker)

    for mode in ("strict", "per_agent", "participants"):
        payload = _payload(expected_revision=0)
        payload["settings"]["wave_membership"] = mode  # type: ignore[index]
        applied = await client.post(_URL, headers=_HEADERS, json=payload)
        assert applied.status_code in (200, 409), applied.text
        if applied.status_code == 200:
            assert applied.json()["settings"]["wave_membership"] == mode
            break

    rejected = _payload(expected_revision=1)
    rejected["settings"]["wave_membership"] = "everyone"  # type: ignore[index]
    assert (await client.post(_URL, headers=_HEADERS, json=rejected)).status_code == 422


async def test_stored_revisions_predating_wave_membership_still_load() -> None:
    """An existing revision acquires the shipped default, not its old fold.

    The board is append-only and v7 is live. ``participants`` is a deliberate,
    authorized change to the fold, so a revision written before the field
    existed resolves to it exactly as a fresh board does -- the field is absent
    from the stored JSON, so there is no stored intent to preserve.
    """
    from ditto.api_server.continual_retest_settings import settings_from_row

    class _Row:
        revision = 5
        settings = {
            "aggregate_mode": "enabled",
            "idle_retests_enabled": True,
            "rollout_standdown": "all",
            "retest_cohort_size": 25,
        }

    resolved = settings_from_row(_Row())  # type: ignore[arg-type]
    assert resolved.retest_cohort_size == 25
    assert resolved.wave_membership == "participants"


async def test_stored_revisions_predating_the_cohort_dial_still_load() -> None:
    """A revision written before this field exists resolves to the top five."""
    from ditto.api_server.continual_retest_settings import settings_from_row

    class _Row:
        revision = 3
        settings = {
            "aggregate_mode": "enabled",
            "idle_retests_enabled": True,
            "rollout_standdown": "all",
        }

    resolved = settings_from_row(_Row())  # type: ignore[arg-type]
    assert resolved.aggregate_mode == "enabled"
    assert resolved.retest_cohort_size == 5


async def test_rollout_standdown_modes_are_explicit() -> None:
    from ditto.api_models.continual_retest_settings import ContinualRetestSettings
    from ditto.api_server.continual_retest_settings import rollout_standdown_reason

    default = ContinualRetestSettings()
    assert default.rollout_standdown == "capable_validators"
    # No open rollout is the only state in which the lane is unconditionally on.
    assert (
        rollout_standdown_reason(
            default,
            open_rollout_desired_version=None,
            validator_supports_desired_version=True,
        )
        is None
    )
    reason = rollout_standdown_reason(
        default,
        open_rollout_desired_version=7,
        validator_supports_desired_version=True,
    )
    assert reason is not None
    assert "resume automatically" in reason
    # Capacity the rollout cannot consume keeps confirming the active era.
    assert (
        rollout_standdown_reason(
            default,
            open_rollout_desired_version=7,
            validator_supports_desired_version=False,
        )
        is None
    )
    assert (
        rollout_standdown_reason(
            ContinualRetestSettings(rollout_standdown="all"),
            open_rollout_desired_version=7,
            validator_supports_desired_version=False,
        )
        is not None
    )
    assert (
        rollout_standdown_reason(
            ContinualRetestSettings(rollout_standdown="off"),
            open_rollout_desired_version=7,
            validator_supports_desired_version=True,
        )
        is None
    )
