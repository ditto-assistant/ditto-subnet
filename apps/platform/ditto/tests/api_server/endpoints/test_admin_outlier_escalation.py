"""Operator read of the anomalous-score outlier escalation (issue #476).

Covers the env loader's per-field source reporting, that the settings scoring
consumes are unchanged by it, and the admin endpoint's posture and bounded
audit-chain activity.
"""

from __future__ import annotations

import math
import os
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import product
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints import validator as v
from ditto.api_server.endpoints.validator import (
    _evaluate_and_record_outlier_escalation,
    _outlier_escalation_settings_from_env,
)
from ditto.api_server.outlier_escalation import (
    OUTLIER_ALGORITHM_VERSION,
    OUTLIER_ESCALATION_ENV_VARS,
    OUTLIER_REVIEW_KIND,
    OutlierEscalationSettings,
    load_outlier_escalation_settings,
)
from ditto.db.models import Agent, AthReview, ScoreAuditEntry
from ditto.db.queries.audit import EVENT_AUDIT, append_audit_entry

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}
_URL = "/api/v1/admin/outlier-escalation"
_SPREAD_COHORT = [0.58, 0.59, 0.60, 0.61, 0.62, 0.60, 0.60, 0.61]
_FIELDS = (
    "mode",
    "min_bench_version",
    "min_cohort_size",
    "modified_z_threshold",
    "min_composite_floor",
)


def _legacy_settings_from_env() -> OutlierEscalationSettings:
    """Verbatim copy of the env builder before source reporting existed.

    The equivalence test below pins the new loader to this, so a change to the
    fallback rules cannot slip in behind the visibility work.
    """
    defaults = OutlierEscalationSettings()

    mode = (
        os.environ.get("DITTO_OUTLIER_ESCALATION_MODE", defaults.mode).strip().lower()
    )
    if mode not in {"off", "observe", "enforce"}:
        mode = defaults.mode

    def _int(name: str, fallback: int) -> int:
        try:
            return int(os.environ[name])
        except (KeyError, ValueError):
            return fallback

    def _float(name: str, fallback: float) -> float:
        try:
            return float(os.environ[name])
        except (KeyError, ValueError):
            return fallback

    return OutlierEscalationSettings(
        mode=mode,
        min_bench_version=_int(
            "DITTO_OUTLIER_ESCALATION_MIN_BENCH_VERSION", defaults.min_bench_version
        ),
        min_cohort_size=_int(
            "DITTO_OUTLIER_ESCALATION_MIN_COHORT_SIZE", defaults.min_cohort_size
        ),
        modified_z_threshold=_float(
            "DITTO_OUTLIER_ESCALATION_MODIFIED_Z_THRESHOLD",
            defaults.modified_z_threshold,
        ),
        min_composite_floor=_float(
            "DITTO_OUTLIER_ESCALATION_MIN_COMPOSITE_FLOOR",
            defaults.min_composite_floor,
        ),
    )


def _same(left: OutlierEscalationSettings, right: OutlierEscalationSettings) -> bool:
    def eq(a: object, b: object) -> bool:
        if isinstance(a, float) and isinstance(b, float):
            return (math.isnan(a) and math.isnan(b)) or a == b
        return a == b and type(a) is type(b)

    return all(eq(getattr(left, name), getattr(right, name)) for name in _FIELDS)


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in OUTLIER_ESCALATION_ENV_VARS.values():
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# Loader: sources, and scoring settings unchanged
# ---------------------------------------------------------------------------


def test_loader_reports_default_for_every_unset_field() -> None:
    loaded = load_outlier_escalation_settings({})
    assert loaded.settings == OutlierEscalationSettings()
    assert all(getattr(loaded.sources, name) == "default" for name in _FIELDS)


def test_loader_reports_each_field_from_env() -> None:
    loaded = load_outlier_escalation_settings(
        {
            "DITTO_OUTLIER_ESCALATION_MODE": " Enforce ",
            "DITTO_OUTLIER_ESCALATION_MIN_BENCH_VERSION": "13",
            "DITTO_OUTLIER_ESCALATION_MIN_COHORT_SIZE": "12",
            "DITTO_OUTLIER_ESCALATION_MODIFIED_Z_THRESHOLD": "4.5",
            "DITTO_OUTLIER_ESCALATION_MIN_COMPOSITE_FLOOR": "0.85",
        }
    )
    assert loaded.settings == OutlierEscalationSettings(
        mode="enforce",
        min_bench_version=13,
        min_cohort_size=12,
        modified_z_threshold=4.5,
        min_composite_floor=0.85,
    )
    assert all(getattr(loaded.sources, name) == "env" for name in _FIELDS)


def test_loader_reports_rejected_env_and_falls_back() -> None:
    loaded = load_outlier_escalation_settings(
        {
            "DITTO_OUTLIER_ESCALATION_MODE": "enforcing",
            "DITTO_OUTLIER_ESCALATION_MIN_BENCH_VERSION": "12.0",
            "DITTO_OUTLIER_ESCALATION_MIN_COHORT_SIZE": "",
            "DITTO_OUTLIER_ESCALATION_MODIFIED_Z_THRESHOLD": "six",
            "DITTO_OUTLIER_ESCALATION_MIN_COMPOSITE_FLOOR": "0,9",
        }
    )
    assert loaded.settings == OutlierEscalationSettings()
    assert all(
        getattr(loaded.sources, name) == "default_invalid_env" for name in _FIELDS
    )


_MODE_VALUES = (None, "", "off", "OBSERVE", " enforce\n", "nonsense")
_INT_VALUES = (None, "", "12", " 13 ", "1_000", "-1", "12.5", "x")
_FLOAT_VALUES = (None, "", "6.0", "1e3", " 0.9 ", "inf", "nan", "0,9", "x")


def test_loader_matches_the_legacy_builder_exactly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scoring behaviour is identical: every accepted/rejected input resolves
    to the same settings as the pre-visibility builder."""
    cases = 0
    for mode, int_value, float_value in product(
        _MODE_VALUES, _INT_VALUES, _FLOAT_VALUES
    ):
        _clear_env(monkeypatch)
        assignments = {
            "DITTO_OUTLIER_ESCALATION_MODE": mode,
            "DITTO_OUTLIER_ESCALATION_MIN_BENCH_VERSION": int_value,
            "DITTO_OUTLIER_ESCALATION_MIN_COHORT_SIZE": int_value,
            "DITTO_OUTLIER_ESCALATION_MODIFIED_Z_THRESHOLD": float_value,
            "DITTO_OUTLIER_ESCALATION_MIN_COMPOSITE_FLOOR": float_value,
        }
        for name, value in assignments.items():
            if value is not None:
                monkeypatch.setenv(name, value)
        legacy = _legacy_settings_from_env()
        assert _same(_outlier_escalation_settings_from_env(), legacy), assignments
        assert _same(load_outlier_escalation_settings(os.environ).settings, legacy)
        cases += 1
    assert cases == len(_MODE_VALUES) * len(_INT_VALUES) * len(_FLOAT_VALUES)


def test_scoring_consumes_the_loaded_settings_object() -> None:
    assert v.OUTLIER_ESCALATION_SETTINGS is v.OUTLIER_ESCALATION_SETTINGS_LOAD.settings
    assert _same(v.OUTLIER_ESCALATION_SETTINGS, _outlier_escalation_settings_from_env())


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


def _install(
    app: FastAPI,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_ADMIN_TOKEN)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


def _load_env(monkeypatch: pytest.MonkeyPatch, environ: dict[str, str]) -> None:
    loaded = load_outlier_escalation_settings(environ)
    monkeypatch.setattr(v, "OUTLIER_ESCALATION_SETTINGS_LOAD", loaded)
    monkeypatch.setattr(v, "OUTLIER_ESCALATION_SETTINGS", loaded.settings)


def _agent() -> Agent:
    return Agent(
        agent_id=uuid4(),
        miner_hotkey=f"miner-{uuid4().hex[:8]}",
        name="outlier-agent",
        sha256="ab" * 32,
        status=AgentStatus.SCORED,
        screening_policy_version=9,
    )


async def _escalate(
    session_maker: async_sessionmaker[AsyncSession],
    *,
    mode: str,
    now: datetime,
) -> Agent:
    """Drive the real scoring-path helper so the trail is the production shape."""
    agent = _agent()
    async with session_maker() as session:
        async with session.begin():
            session.add(agent)
        async with session.begin():
            await _evaluate_and_record_outlier_escalation(
                session,
                agent=agent,
                bench_version=12,
                composite=0.99,
                cohort=_SPREAD_COHORT,
                settings=OutlierEscalationSettings(mode=mode),
                now=now,
            )
    return agent


@pytest.mark.asyncio
async def test_read_requires_the_admin_token(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    assert (await client.get(_URL)).status_code == 401


@pytest.mark.asyncio
async def test_default_posture_is_off_with_no_env(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(app, session_maker)
    _load_env(monkeypatch, {})

    response = await client.get(_URL, headers=_HEADERS)

    assert response.status_code == 200, response.text
    body = response.json()
    defaults = {
        "mode": "off",
        "min_bench_version": 12,
        "min_cohort_size": 8,
        "modified_z_threshold": 6.0,
        "min_composite_floor": 0.9,
    }
    assert body["settings"] == defaults
    assert body["defaults"] == defaults
    assert body["sources"] == dict.fromkeys(_FIELDS, "default")
    assert body["invalid_env_fields"] == []
    assert body["env_vars"] == dict(OUTLIER_ESCALATION_ENV_VARS)
    assert body["review_kind"] == OUTLIER_REVIEW_KIND
    assert body["algorithm_version"] == OUTLIER_ALGORITHM_VERSION
    assert body["pending_review_count"] == 0
    activity = body["activity"]
    assert activity["observed_total"] == 0
    assert activity["enforced_total"] == 0
    assert activity["observed_in_window"] == 0
    assert activity["enforced_in_window"] == 0
    assert activity["latest_recorded_at"] is None
    assert activity["recent"] == []
    assert activity["recent_truncated"] is False
    assert activity["recent_limit"] == 20
    assert activity["window_hours"] == 168


@pytest.mark.asyncio
async def test_reports_every_field_from_env(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(app, session_maker)
    _load_env(
        monkeypatch,
        {
            "DITTO_OUTLIER_ESCALATION_MODE": "observe",
            "DITTO_OUTLIER_ESCALATION_MIN_BENCH_VERSION": "13",
            "DITTO_OUTLIER_ESCALATION_MIN_COHORT_SIZE": "10",
            "DITTO_OUTLIER_ESCALATION_MODIFIED_Z_THRESHOLD": "5.5",
            "DITTO_OUTLIER_ESCALATION_MIN_COMPOSITE_FLOOR": "0.8",
        },
    )

    body = (await client.get(_URL, headers=_HEADERS)).json()

    assert body["settings"] == {
        "mode": "observe",
        "min_bench_version": 13,
        "min_cohort_size": 10,
        "modified_z_threshold": 5.5,
        "min_composite_floor": 0.8,
    }
    assert body["sources"] == dict.fromkeys(_FIELDS, "env")
    assert body["invalid_env_fields"] == []


@pytest.mark.asyncio
async def test_rejected_env_is_reported_without_echoing_the_value(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(app, session_maker)
    sentinel = "sk-live-must-not-appear"
    _load_env(
        monkeypatch,
        {
            "DITTO_OUTLIER_ESCALATION_MODE": "enforcing",
            "DITTO_OUTLIER_ESCALATION_MIN_COHORT_SIZE": "12",
            "DITTO_OUTLIER_ESCALATION_MODIFIED_Z_THRESHOLD": sentinel,
        },
    )

    response = await client.get(_URL, headers=_HEADERS)
    body = response.json()

    # The mistyped mode silently fell back to off; now it is visible.
    assert body["settings"]["mode"] == "off"
    assert body["settings"]["modified_z_threshold"] == 6.0
    assert body["settings"]["min_cohort_size"] == 12
    assert body["sources"] == {
        "mode": "default_invalid_env",
        "min_bench_version": "default",
        "min_cohort_size": "env",
        "modified_z_threshold": "default_invalid_env",
        "min_composite_floor": "default",
    }
    assert body["invalid_env_fields"] == ["mode", "modified_z_threshold"]
    assert sentinel not in response.text
    assert "enforcing" not in response.text


@pytest.mark.asyncio
async def test_non_finite_env_threshold_is_an_explicit_null(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The legacy parser accepts "inf"/"nan"; the read must not hide or crash
    on them. The value is reported null with source env, so it stands out."""
    _install(app, session_maker)
    _load_env(
        monkeypatch,
        {
            "DITTO_OUTLIER_ESCALATION_MODIFIED_Z_THRESHOLD": "inf",
            "DITTO_OUTLIER_ESCALATION_MIN_COMPOSITE_FLOOR": "nan",
        },
    )

    response = await client.get(_URL, headers=_HEADERS)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["settings"]["modified_z_threshold"] is None
    assert body["settings"]["min_composite_floor"] is None
    assert body["sources"]["modified_z_threshold"] == "env"
    assert body["sources"]["min_composite_floor"] == "env"
    assert math.isinf(v.OUTLIER_ESCALATION_SETTINGS.modified_z_threshold)


@pytest.mark.asyncio
async def test_counts_and_lists_observe_and_enforce_activity(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(app, session_maker)
    _load_env(monkeypatch, {"DITTO_OUTLIER_ESCALATION_MODE": "enforce"})
    now = datetime.now(UTC)

    old_observed = await _escalate(
        session_maker, mode="observe", now=now - timedelta(days=10)
    )
    recent_observed = await _escalate(
        session_maker, mode="observe", now=now - timedelta(hours=2)
    )
    enforced = await _escalate(
        session_maker, mode="enforce", now=now - timedelta(hours=1)
    )

    copy_held, resolved = _agent(), _agent()
    async with session_maker() as session, session.begin():
        session.add_all([copy_held, resolved])
    async with session_maker() as session, session.begin():
        # A different EVENT_AUDIT kind on the same chain must not be counted.
        await append_audit_entry(
            session,
            agent_id=uuid4(),
            validator_hotkey=None,
            event=EVENT_AUDIT,
            payload={"audit_kind": "integrity_double_check", "enforced": True},
            recorded_at=now,
        )
        # Pending reviews of another kind, and a resolved outlier review, are
        # not pending outlier holds.
        session.add_all(
            [
                AthReview(
                    review_id=uuid4(),
                    agent_id=copy_held.agent_id,
                    status="pending",
                    opened_at=now,
                    original_policy_version=9,
                    original_evidence={},
                    algorithm_provenance={"review_kind": "copy"},
                ),
                AthReview(
                    review_id=uuid4(),
                    agent_id=resolved.agent_id,
                    status="resolved",
                    opened_at=now - timedelta(days=3),
                    resolved_at=now - timedelta(days=2),
                    resolved_by="operator@example.com",
                    resolution="clear",
                    resolution_reason="Genuine improvement verified",
                    original_policy_version=9,
                    original_evidence={},
                    algorithm_provenance={"review_kind": OUTLIER_REVIEW_KIND},
                ),
            ]
        )

    response = await client.get(_URL, headers=_HEADERS, params={"window_hours": 24})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["pending_review_count"] == 1
    activity = body["activity"]
    assert activity["window_hours"] == 24
    assert activity["observed_total"] == 2
    assert activity["enforced_total"] == 1
    assert activity["observed_in_window"] == 1
    assert activity["enforced_in_window"] == 1
    assert activity["recent_truncated"] is False
    recent = activity["recent"]
    assert [row["agent_id"] for row in recent] == [
        str(enforced.agent_id),
        str(recent_observed.agent_id),
        str(old_observed.agent_id),
    ]
    assert [row["enforced"] for row in recent] == [True, False, False]
    assert datetime.fromisoformat(activity["latest_recorded_at"]) == (
        datetime.fromisoformat(recent[0]["recorded_at"])
    )
    first = recent[0]
    assert first["bench_version"] == 12
    assert first["algorithm_version"] == OUTLIER_ALGORITHM_VERSION
    evidence = first["evidence"]
    assert evidence["composite"] == pytest.approx(0.99)
    assert evidence["cohort_size"] == len(_SPREAD_COHORT)
    assert evidence["cohort_median"] == pytest.approx(0.60, abs=1e-9)
    assert evidence["cohort_mad"] == pytest.approx(0.01, abs=1e-9)
    assert evidence["modified_z"] > 6.0
    assert evidence["upward"] is True
    assert evidence["above_floor"] is True
    assert evidence["min_cohort_size"] == 8
    assert evidence["modified_z_threshold"] == 6.0
    assert evidence["min_composite_floor"] == 0.9


@pytest.mark.asyncio
async def test_recent_list_is_bounded_and_reports_truncation(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(app, session_maker)
    _load_env(monkeypatch, {})
    now = datetime.now(UTC)
    agents = [
        await _escalate(session_maker, mode="observe", now=now - timedelta(hours=h))
        for h in (3, 2, 1)
    ]

    body = (await client.get(_URL, headers=_HEADERS, params={"limit": 2})).json()

    activity = body["activity"]
    assert activity["recent_limit"] == 2
    assert activity["recent_truncated"] is True
    assert [row["agent_id"] for row in activity["recent"]] == [
        str(agents[2].agent_id),
        str(agents[1].agent_id),
    ]
    # Counts are exact aggregates, independent of the page bound.
    assert activity["observed_total"] == 3

    exact = (await client.get(_URL, headers=_HEADERS, params={"limit": 3})).json()
    assert exact["activity"]["recent_truncated"] is False
    assert len(exact["activity"]["recent"]) == 3


@pytest.mark.asyncio
async def test_query_bounds_are_enforced(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    for params in (
        {"limit": 0},
        {"limit": 101},
        {"window_hours": 0},
        {"window_hours": 721},
    ):
        response = await client.get(_URL, headers=_HEADERS, params=params)
        assert response.status_code == 422, params


@pytest.mark.asyncio
async def test_read_mutates_nothing(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(app, session_maker)
    _load_env(monkeypatch, {})
    await _escalate(session_maker, mode="enforce", now=datetime.now(UTC))

    async def _snapshot() -> tuple[int, int]:
        async with session_maker() as session:
            audit = await session.scalar(
                select(func.count()).select_from(ScoreAuditEntry)
            )
            reviews = await session.scalar(select(func.count()).select_from(AthReview))
            return int(audit or 0), int(reviews or 0)

    before = await _snapshot()
    settings_before = v.OUTLIER_ESCALATION_SETTINGS
    assert (await client.get(_URL, headers=_HEADERS)).status_code == 200
    assert await _snapshot() == before
    assert v.OUTLIER_ESCALATION_SETTINGS is settings_before
