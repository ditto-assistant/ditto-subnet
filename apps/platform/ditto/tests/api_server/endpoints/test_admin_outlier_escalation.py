"""Operator read of the anomalous-score outlier escalation (issue #476).

Covers the env loader's per-field source reporting, that the settings scoring
consumes are unchanged by it, the admin endpoint's posture and bounded
audit-chain activity, and the read-only dry-run replay over the scored ledger.
"""

from __future__ import annotations

import math
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints import validator as v
from ditto.api_server.outlier_escalation import (
    OUTLIER_ALGORITHM_VERSION,
    OUTLIER_ESCALATION_ENV_VARS,
    OUTLIER_REVIEW_KIND,
    OutlierEscalationSettings,
    load_outlier_escalation_settings,
)
from ditto.db.models import Agent, Score

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


# ---------------------------------------------------------------------------
# Loader: sources, and scoring settings unchanged
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Per-axis evidence (outlier-escalation-v2)
# ---------------------------------------------------------------------------


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
