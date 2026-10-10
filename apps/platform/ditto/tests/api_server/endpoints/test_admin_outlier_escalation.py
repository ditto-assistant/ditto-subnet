"""Operator read of the anomalous-score outlier escalation (issue #476).

Covers the env loader's per-field source reporting, that the settings scoring
consumes are unchanged by it, the admin endpoint's posture and bounded
audit-chain activity, and the read-only dry-run replay over the scored ledger.
"""

from __future__ import annotations

import math
import os
import statistics
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
    AxisObservation,
    OutlierEscalationSettings,
    load_outlier_escalation_settings,
)
from ditto.db.models import Agent, AthReview, Score, ScoreAuditEntry
from ditto.db.queries.audit import EVENT_AUDIT, append_audit_entry
from ditto.db.queries.scores import list_eligible_ledger, list_scores_for_agent

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
    "per_axis_enforce",
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
            "DITTO_OUTLIER_ESCALATION_PER_AXIS_ENFORCE": " True ",
        }
    )
    assert loaded.settings == OutlierEscalationSettings(
        mode="enforce",
        min_bench_version=13,
        min_cohort_size=12,
        modified_z_threshold=4.5,
        min_composite_floor=0.85,
        per_axis_enforce=True,
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
            "DITTO_OUTLIER_ESCALATION_PER_AXIS_ENFORCE": "enforce",
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
        "per_axis_enforce": False,
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
            "DITTO_OUTLIER_ESCALATION_PER_AXIS_ENFORCE": "on",
        },
    )

    body = (await client.get(_URL, headers=_HEADERS)).json()

    assert body["settings"] == {
        "mode": "observe",
        "min_bench_version": 13,
        "min_cohort_size": 10,
        "modified_z_threshold": 5.5,
        "min_composite_floor": 0.8,
        "per_axis_enforce": True,
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
        "per_axis_enforce": "default",
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


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

_DRY_RUN_URL = f"{_URL}/dry-run"


async def _seed_scored(
    session_maker: async_sessionmaker[AsyncSession],
    *composites: float,
    status: AgentStatus = AgentStatus.SCORED,
    bench_version: int = 12,
    memory_mean: float | None = None,
) -> Agent:
    """One agent with one score row per composite (its quorum).

    Both axes equal the composite unless ``memory_mean`` pins that axis."""
    agent = _agent()
    agent.status = status
    async with session_maker() as session, session.begin():
        session.add(agent)
        await session.flush()
        session.add_all(
            Score(
                agent_id=agent.agent_id,
                validator_hotkey=f"validator-{index}",
                bench_version=bench_version,
                run_id=f"run-{agent.agent_id.hex[:8]}-{index}",
                signature=None,
                seed=7,
                composite=composite,
                tool_mean=composite,
                memory_mean=composite if memory_mean is None else memory_mean,
                median_ms=100,
                n=114,
                details={},
                generated_at=datetime.now(UTC),
            )
            for index, composite in enumerate(composites)
        )
    return agent


async def _seed_spread_ledger(
    session_maker: async_sessionmaker[AsyncSession],
) -> tuple[Agent, Agent]:
    """The spread cohort plus a 0.99 spike and a 0.70 row below the floor."""
    for composite in _SPREAD_COHORT:
        await _seed_scored(session_maker, composite, composite, composite)
    spike = await _seed_scored(session_maker, 0.98, 0.99, 0.995)
    high = await _seed_scored(session_maker, 0.70, 0.70, 0.70)
    return spike, high


@pytest.mark.asyncio
async def test_dry_run_requires_the_admin_token(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    assert (await client.get(_DRY_RUN_URL)).status_code == 401


@pytest.mark.asyncio
async def test_dry_run_counts_would_trigger_rows_even_when_off(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(app, session_maker)
    _load_env(monkeypatch, {})
    spike, _high = await _seed_spread_ledger(session_maker)
    # Outside the scoring-time ledger: a held agent, and another bench version.
    await _seed_scored(session_maker, 0.99, status=AgentStatus.ATH_PENDING_REVIEW)
    await _seed_scored(session_maker, 0.99, bench_version=11)

    response = await client.get(
        _DRY_RUN_URL, headers=_HEADERS, params={"bench_version": 12}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["bench_version"] == 12
    assert body["bench_version_in_scope"] is True
    assert body["settings"]["mode"] == "off"
    assert body["overridden_fields"] == []
    assert body["ledger_size"] == len(_SPREAD_COHORT) + 2
    assert body["cohort_size"] == len(_SPREAD_COHORT) + 1
    assert body["cohort_too_small"] is False
    assert body["ledger_median"] == pytest.approx(0.605, abs=1e-9)
    assert body["ledger_mad"] == pytest.approx(0.01, abs=1e-9)
    # The 0.70 row is out-of-band by distance but below the 0.90 floor.
    assert body["would_trigger_count"] == 1
    assert body["truncated"] is False
    [entry] = body["would_trigger"]
    assert entry["agent_id"] == str(spike.agent_id)
    assert entry["miner_hotkey"] == spike.miner_hotkey
    evidence = entry["evidence"]
    assert evidence["composite"] == pytest.approx(0.99)
    assert evidence["cohort_size"] == len(_SPREAD_COHORT) + 1
    assert evidence["modified_z"] > 6.0
    assert evidence["upward"] is True
    assert evidence["above_floor"] is True


@pytest.mark.asyncio
async def test_dry_run_matches_the_live_decision_on_the_same_cohort(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finalize a candidate the way scoring does, then replay the ledger.

    The candidate's cohort at finalization is the ledger it is not yet in; in
    the replay it is the ledger without it. Both must yield the same evidence.
    """
    _install(app, session_maker)
    _load_env(monkeypatch, {})
    for composite in [*_SPREAD_COHORT, 0.70]:
        await _seed_scored(session_maker, composite, composite, composite)
    candidate = await _seed_scored(
        session_maker, 0.98, 0.99, 0.995, status=AgentStatus.EVALUATING
    )

    async with session_maker() as session, session.begin():
        agent = await session.get(Agent, candidate.agent_id)
        assert agent is not None
        agent_scores = await list_scores_for_agent(
            session, agent_id=agent.agent_id, bench_version=12
        )
        median_composite = statistics.median(s.composite for s in agent_scores)
        eligible = await list_eligible_ledger(session, bench_version=12)
        agent.status = AgentStatus.SCORED
        await _evaluate_and_record_outlier_escalation(
            session,
            agent=agent,
            bench_version=12,
            composite=median_composite,
            cohort=[row.composite for row in eligible],
            settings=OutlierEscalationSettings(mode="observe"),
            now=datetime.now(UTC),
            axes={
                "tool_mean": AxisObservation(
                    value=statistics.median(s.tool_mean for s in agent_scores),
                    cohort=[row.tool_mean for row in eligible],
                ),
                "memory_mean": AxisObservation(
                    value=statistics.median(s.memory_mean for s in agent_scores),
                    cohort=[row.memory_mean for row in eligible],
                ),
            },
        )
    async with session_maker() as session:
        live = await session.scalar(
            select(ScoreAuditEntry.payload).where(
                ScoreAuditEntry.agent_id == candidate.agent_id,
                ScoreAuditEntry.event == EVENT_AUDIT,
            )
        )
    assert live is not None

    body = (
        await client.get(_DRY_RUN_URL, headers=_HEADERS, params={"bench_version": 12})
    ).json()

    [entry] = body["would_trigger"]
    assert entry["agent_id"] == str(candidate.agent_id)
    live_evidence = live["evidence"]
    # The composite evidence matches the public entry exactly. The per-axis
    # statistics are only in the (admin) replay; the public entry recorded
    # the neutral projection, which the replay's flags reproduce.
    per_axis_keys = {"per_axis", "per_axis_outlier_axes", "per_axis_enforce"}
    assert {
        key: value
        for key, value in entry["evidence"].items()
        if key not in per_axis_keys
    } == {
        key: live_evidence.get(key)
        for key in entry["evidence"]
        if key not in per_axis_keys
    }
    assert [
        {"axis": axis["axis"], "outlier": axis["outlier"]}
        for axis in entry["evidence"]["per_axis"]
    ] == live_evidence["per_axis"]
    assert all(
        axis["cohort_median"] is not None for axis in entry["evidence"]["per_axis"]
    )


@pytest.mark.asyncio
async def test_dry_run_applies_overrides_over_effective_settings(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(app, session_maker)
    _load_env(monkeypatch, {"DITTO_OUTLIER_ESCALATION_MODE": "observe"})
    spike, high = await _seed_spread_ledger(session_maker)

    body = (
        await client.get(
            _DRY_RUN_URL,
            headers=_HEADERS,
            params={"bench_version": 12, "min_composite_floor": 0.65, "limit": 1},
        )
    ).json()

    assert body["overridden_fields"] == ["min_composite_floor"]
    assert body["settings"] == {
        "mode": "observe",
        "min_bench_version": 12,
        "min_cohort_size": 8,
        "modified_z_threshold": 6.0,
        "min_composite_floor": 0.65,
        "per_axis_enforce": False,
    }
    assert body["would_trigger_count"] == 2
    assert body["limit"] == 1
    assert body["truncated"] is True
    assert [row["agent_id"] for row in body["would_trigger"]] == [str(spike.agent_id)]

    both = (
        await client.get(
            _DRY_RUN_URL,
            headers=_HEADERS,
            params={"bench_version": 12, "min_composite_floor": 0.65},
        )
    ).json()
    assert [row["agent_id"] for row in both["would_trigger"]] == [
        str(spike.agent_id),
        str(high.agent_id),
    ]

    thin = (
        await client.get(
            _DRY_RUN_URL,
            headers=_HEADERS,
            params={
                "bench_version": 12,
                "min_cohort_size": 20,
                "modified_z_threshold": 2.5,
            },
        )
    ).json()
    assert thin["overridden_fields"] == ["min_cohort_size", "modified_z_threshold"]
    assert thin["cohort_too_small"] is True
    assert thin["would_trigger_count"] == 0
    # The effective settings scoring uses are untouched by an override.
    assert v.OUTLIER_ESCALATION_SETTINGS.min_composite_floor == 0.9


@pytest.mark.asyncio
async def test_dry_run_defaults_to_the_active_bench_version(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(app, session_maker)
    _load_env(monkeypatch, {})

    body = (await client.get(_DRY_RUN_URL, headers=_HEADERS)).json()

    # No rollout on record resolves to the scoreable floor, below v12 scope.
    assert body["bench_version"] == 7
    assert body["bench_version_in_scope"] is False
    assert body["ledger_size"] == 0
    assert body["cohort_size"] == 0
    assert body["cohort_too_small"] is True
    assert body["ledger_median"] is None
    assert body["ledger_mad"] is None
    assert body["would_trigger"] == []


@pytest.mark.asyncio
async def test_dry_run_query_bounds_are_enforced(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    for params in (
        {"limit": 0},
        {"limit": 101},
        {"bench_version": 0},
        {"min_cohort_size": 0},
        {"min_cohort_size": 1001},
        {"modified_z_threshold": 0},
        {"modified_z_threshold": "nan"},
        {"modified_z_threshold": "inf"},
        {"min_composite_floor": -0.1},
        {"min_composite_floor": 1.5},
        {"min_composite_floor": "nan"},
    ):
        response = await client.get(_DRY_RUN_URL, headers=_HEADERS, params=params)
        assert response.status_code == 422, params


@pytest.mark.asyncio
async def test_dry_run_mutates_nothing_even_in_enforce(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(app, session_maker)
    _load_env(monkeypatch, {"DITTO_OUTLIER_ESCALATION_MODE": "enforce"})
    spike, _high = await _seed_spread_ledger(session_maker)

    async def _snapshot() -> tuple[int, int, int]:
        async with session_maker() as session:
            audit = await session.scalar(
                select(func.count()).select_from(ScoreAuditEntry)
            )
            reviews = await session.scalar(select(func.count()).select_from(AthReview))
            scored = await session.scalar(
                select(func.count())
                .select_from(Agent)
                .where(Agent.status == AgentStatus.SCORED)
            )
            return int(audit or 0), int(reviews or 0), int(scored or 0)

    before = await _snapshot()
    settings_before = v.OUTLIER_ESCALATION_SETTINGS
    response = await client.get(
        _DRY_RUN_URL,
        headers=_HEADERS,
        params={"bench_version": 12, "modified_z_threshold": 1.0},
    )
    assert response.status_code == 200, response.text
    assert response.json()["would_trigger_count"] >= 1
    assert await _snapshot() == before
    assert v.OUTLIER_ESCALATION_SETTINGS is settings_before
    async with session_maker() as session:
        held = await session.get(Agent, spike.agent_id)
        assert held is not None
        assert held.status == AgentStatus.SCORED
        assert held.review_reason is None


# ---------------------------------------------------------------------------
# Per-axis evidence (outlier-escalation-v2)
# ---------------------------------------------------------------------------


async def _record_axis_evidence(
    session_maker: async_sessionmaker[AsyncSession], *, now: datetime
) -> Agent:
    """A composite in band with memory far out of band, through the real helper."""
    agent = _agent()
    async with session_maker() as session:
        async with session.begin():
            session.add(agent)
        async with session.begin():
            await _evaluate_and_record_outlier_escalation(
                session,
                agent=agent,
                bench_version=12,
                composite=0.615,
                cohort=_SPREAD_COHORT,
                settings=OutlierEscalationSettings(mode="observe"),
                now=now,
                axes={
                    "tool_mean": AxisObservation(value=0.61, cohort=_SPREAD_COHORT),
                    "memory_mean": AxisObservation(value=0.95, cohort=_SPREAD_COHORT),
                },
            )
    return agent


@pytest.mark.asyncio
async def test_axis_evidence_is_counted_and_listed_apart_from_holds(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(app, session_maker)
    _load_env(monkeypatch, {})
    now = datetime.now(UTC)
    observed = await _escalate(session_maker, mode="observe", now=now)
    old_axis = await _record_axis_evidence(session_maker, now=now - timedelta(days=10))
    axis = await _record_axis_evidence(session_maker, now=now - timedelta(hours=1))

    body = (
        await client.get(_URL, headers=_HEADERS, params={"window_hours": 24})
    ).json()

    activity = body["activity"]
    # Evidence-only entries never inflate the would-be-hold / hold counts.
    assert activity["observed_total"] == 1
    assert activity["enforced_total"] == 0
    assert [row["agent_id"] for row in activity["recent"]] == [str(observed.agent_id)]
    assert activity["axis_evidence_total"] == 2
    assert activity["axis_evidence_in_window"] == 1
    assert activity["recent_axis_evidence_truncated"] is False
    recent_axis = activity["recent_axis_evidence"]
    assert [row["agent_id"] for row in recent_axis] == [
        str(axis.agent_id),
        str(old_axis.agent_id),
    ]
    first = recent_axis[0]
    assert first["enforced"] is False
    assert first["algorithm_version"] == OUTLIER_ALGORITHM_VERSION
    evidence = first["evidence"]
    assert evidence["trigger"] is None
    # Read back from the public chain: only axis + flag were recorded, so the
    # policy flag and every per-axis statistic are null; the outlier axes are
    # derived from the flags.
    assert evidence["per_axis_enforce"] is None
    assert evidence["per_axis_outlier_axes"] == ["memory_mean"]
    by_axis = {entry["axis"]: entry for entry in evidence["per_axis"]}
    assert set(by_axis) == {"tool_mean", "memory_mean"}
    assert by_axis["memory_mean"]["outlier"] is True
    assert by_axis["tool_mean"]["outlier"] is False
    for entry in by_axis.values():
        for key in ("value", "cohort_size", "cohort_median", "cohort_mad"):
            assert entry[key] is None
        assert entry["modified_z"] is None
        assert entry["upward"] is None
    # The composite hold entry also carries a (null) per-axis projection
    # because the helper was called without axes.
    assert activity["recent"][0]["evidence"]["per_axis"] is None

    bounded = (await client.get(_URL, headers=_HEADERS, params={"limit": 1})).json()
    assert bounded["activity"]["recent_axis_evidence_truncated"] is True
    assert len(bounded["activity"]["recent_axis_evidence"]) == 1


@pytest.mark.asyncio
async def test_posture_reports_the_per_axis_enforce_setting(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(app, session_maker)
    _load_env(
        monkeypatch,
        {
            "DITTO_OUTLIER_ESCALATION_MODE": "enforce",
            "DITTO_OUTLIER_ESCALATION_PER_AXIS_ENFORCE": "maybe",
        },
    )

    response = await client.get(_URL, headers=_HEADERS)

    body = response.json()
    assert body["settings"]["per_axis_enforce"] is False
    assert body["defaults"]["per_axis_enforce"] is False
    assert body["sources"]["per_axis_enforce"] == "default_invalid_env"
    assert body["invalid_env_fields"] == ["per_axis_enforce"]
    assert (
        body["env_vars"]["per_axis_enforce"]
        == "DITTO_OUTLIER_ESCALATION_PER_AXIS_ENFORCE"
    )
    assert "maybe" not in response.text


@pytest.mark.asyncio
async def test_dry_run_reports_per_axis_evidence(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(app, session_maker)
    _load_env(monkeypatch, {})
    spike, _high = await _seed_spread_ledger(session_maker)
    # Composite in band (0.61) with the memory axis far out of band.
    axis_row = await _seed_scored(session_maker, 0.61, 0.61, 0.61, memory_mean=0.95)

    body = (
        await client.get(_DRY_RUN_URL, headers=_HEADERS, params={"bench_version": 12})
    ).json()

    assert body["settings"]["per_axis_enforce"] is False
    assert body["would_trigger_count"] == 1
    [held] = body["would_trigger"]
    assert held["agent_id"] == str(spike.agent_id)
    assert held["evidence"]["trigger"] == "composite"
    assert {entry["axis"] for entry in held["evidence"]["per_axis"]} == {
        "tool_mean",
        "memory_mean",
    }
    # The 0.70 row's axes are out of band too (it is below the composite floor),
    # alongside the single-axis row. Highest composite first.
    assert body["axis_evidence_count"] == 2
    assert body["axis_evidence_truncated"] is False
    assert [row["agent_id"] for row in body["axis_evidence"]] == [
        str(_high.agent_id),
        str(axis_row.agent_id),
    ]
    axis_evidence = body["axis_evidence"][1]["evidence"]
    assert axis_evidence["trigger"] is None
    assert axis_evidence["per_axis_outlier_axes"] == ["memory_mean"]

    # Opting in (with a floor the in-band row clears) moves the single-axis row
    # into would_trigger without touching the live settings.
    opted = (
        await client.get(
            _DRY_RUN_URL,
            headers=_HEADERS,
            params={
                "bench_version": 12,
                "per_axis_enforce": "true",
                "min_composite_floor": 0.6,
            },
        )
    ).json()
    assert opted["overridden_fields"] == ["min_composite_floor", "per_axis_enforce"]
    assert opted["settings"]["per_axis_enforce"] is True
    triggers = {
        row["agent_id"]: row["evidence"]["trigger"] for row in opted["would_trigger"]
    }
    assert triggers[str(axis_row.agent_id)] == "per_axis"
    assert triggers[str(spike.agent_id)] == "composite"
    assert str(axis_row.agent_id) not in {
        row["agent_id"] for row in opted["axis_evidence"]
    }
    assert v.OUTLIER_ESCALATION_SETTINGS.per_axis_enforce is False


@pytest.mark.asyncio
async def test_public_audit_feed_carries_no_per_axis_statistics(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """issue #476: no per-axis threshold, cohort statistic or z-score reaches
    the public, hash-chained /audit feed, and the chain still verifies."""
    from ditto.db.queries.audit import list_audit_entries, verify_audit_chain

    _install(app, session_maker)
    _load_env(monkeypatch, {})
    await _record_axis_evidence(session_maker, now=datetime.now(UTC))
    held = _agent()
    async with session_maker() as session:
        async with session.begin():
            session.add(held)
        async with session.begin():
            await _evaluate_and_record_outlier_escalation(
                session,
                agent=held,
                bench_version=12,
                composite=0.99,
                cohort=_SPREAD_COHORT,
                settings=OutlierEscalationSettings(mode="enforce"),
                now=datetime.now(UTC),
                axes={
                    "tool_mean": AxisObservation(value=0.99, cohort=_SPREAD_COHORT),
                    "memory_mean": AxisObservation(value=0.95, cohort=_SPREAD_COHORT),
                },
            )

    body = (await client.get("/api/v1/public/audit")).json()

    outlier_entries = [
        entry
        for entry in body["entries"]
        if entry["payload"].get("audit_kind")
        in {"anomalous_score", "anomalous_score_axis"}
    ]
    assert len(outlier_entries) == 2
    for entry in outlier_entries:
        evidence = entry["payload"]["evidence"]
        assert "per_axis_enforce" not in evidence
        assert "per_axis_outlier_axes" not in evidence
        for axis in evidence["per_axis"]:
            assert set(axis) == {"axis", "outlier"}
    async with session_maker() as session:
        assert verify_audit_chain(await list_audit_entries(session, limit=1000))
        review = await session.scalar(
            select(AthReview).where(AthReview.agent_id == held.agent_id)
        )
    # The operator-only snapshot keeps the full per-axis statistics.
    assert review is not None
    private_axes = review.original_evidence["per_axis"]
    assert all(axis["cohort_median"] is not None for axis in private_axes)
