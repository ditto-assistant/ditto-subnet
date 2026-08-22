"""Admin contract tests for shadow core qualification."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_server.dependencies import get_session
from ditto.db.models import Agent
from ditto.db.queries.core_qualification import observe_core_qualification
from ditto.db.queries.scores import MIN_ELIGIBLE_CASES, upsert_score

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}
_BENCH_VERSION = 12
_POLICY_URL = f"/api/v1/admin/core-qualification/policy?bench_version={_BENCH_VERSION}"


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_ADMIN_TOKEN)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


def _policy() -> dict[str, object]:
    return {
        "schema": "ditto-core-qualification-policy-v1",
        "weight_eligible": False,
        "bench_version": _BENCH_VERSION,
        "enter_composite": 0.8,
        "enter_tool_mean": 0.8,
        "enter_memory_mean": 0.8,
        "exit_composite": 0.7,
        "exit_tool_mean": 0.7,
        "exit_memory_mean": 0.7,
        "enter_observations": 1,
        "exit_observations": 2,
    }


def _payload(*, expected_revision: int = 0) -> dict[str, object]:
    return {
        "expected_revision": expected_revision,
        "policy": _policy(),
        "reason": "calibrate shadow core qualification",
        "actor": "operator@example.com",
        "confirmation": "APPLY SHADOW CORE QUALIFICATION V12",
    }


async def test_policy_is_unconfigured_until_a_confirmed_revision(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    assert (await client.get(_POLICY_URL)).status_code == 401

    initial = await client.get(_POLICY_URL, headers=_HEADERS)
    assert initial.status_code == 200, initial.text
    assert initial.json()["configured"] is False
    assert initial.json()["current"] is None

    created = await client.post(
        "/api/v1/admin/core-qualification/policy",
        headers=_HEADERS,
        json=_payload(),
    )
    assert created.status_code == 200, created.text
    assert created.headers["cache-control"] == "no-store"
    assert created.json()["current"]["revision"] == 1
    assert created.json()["current"]["policy"]["weight_eligible"] is False

    stale = await client.post(
        "/api/v1/admin/core-qualification/policy",
        headers=_HEADERS,
        json=_payload(),
    )
    assert stale.status_code == 409

    wrong = _payload(expected_revision=1)
    wrong["confirmation"] = "APPLY CORE QUALIFICATION"
    rejected = await client.post(
        "/api/v1/admin/core-qualification/policy",
        headers=_HEADERS,
        json=wrong,
    )
    assert rejected.status_code == 422


async def test_agent_view_invalidates_observation_on_artifact_change(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    assert (
        await client.post(
            "/api/v1/admin/core-qualification/policy",
            headers=_HEADERS,
            json=_payload(),
        )
    ).status_code == 200
    now = datetime.now(UTC)
    agent_id = uuid4()
    async with session_maker() as session, session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey="5CoreViewMiner111111111111111111111111111111111",
                name="core-view-agent",
                sha256="ab" * 32,
                status=AgentStatus.SCORED,
                screening_policy_version=9,
                screened_image_sha256="cd" * 32,
                screened_image_size_bytes=1234,
                screened_image_id="sha256:" + "ef" * 32,
                screened_image_ref="ditto-screen/core-view:latest",
                screened_image_upload_id=uuid4(),
                screened_image_verified_at=now,
                created_at=now,
            )
        )
        for index in range(3):
            await upsert_score(
                session,
                agent_id=agent_id,
                validator_hotkey=f"view-validator-{index}",
                bench_version=_BENCH_VERSION,
                run_id=f"view-run-{index}",
                seed=index,
                composite=0.9,
                tool_mean=0.9,
                memory_mean=0.9,
                median_ms=100,
                n=MIN_ELIGIBLE_CASES,
                generated_at=now,
                signature=(f"{index + 1:02x}" * 64),
            )
    refresh_payload = {
        "bench_version": 12,
        "reason": "backfill current qualified score evidence",
        "actor": "operator@example.com",
        "confirmation": "REFRESH SHADOW CORE QUALIFICATION V12",
    }
    refreshed = await client.post(
        f"/api/v1/admin/agents/{agent_id}/core-qualification/refresh",
        headers=_HEADERS,
        json=refresh_payload,
    )
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["qualified"] is True
    assert refreshed.json()["current_observation"]["source"] == "admin_refresh"
    assert refreshed.json()["current_observation"]["actor"] == "operator@example.com"

    replay = await client.post(
        f"/api/v1/admin/agents/{agent_id}/core-qualification/refresh",
        headers=_HEADERS,
        json=refresh_payload,
    )
    assert replay.status_code == 200
    assert replay.json()["total"] == 1

    url = f"/api/v1/admin/agents/{agent_id}/core-qualification?bench_version=12"
    current = await client.get(url, headers=_HEADERS)
    assert current.status_code == 200, current.text
    assert current.json()["qualified"] is True
    assert current.json()["current_observation"]["current"] is True

    revised = await client.post(
        "/api/v1/admin/core-qualification/policy",
        headers=_HEADERS,
        json=_payload(expected_revision=1),
    )
    assert revised.status_code == 200, revised.text
    policy_stale = await client.get(url, headers=_HEADERS)
    assert policy_stale.status_code == 200
    assert policy_stale.json()["qualified"] is False
    assert policy_stale.json()["current_observation"] is None
    assert policy_stale.json()["observations"][0]["stale_reason"] == "policy_changed"

    async with session_maker() as session, session.begin():
        observed = await observe_core_qualification(
            session,
            agent_id=agent_id,
            bench_version=_BENCH_VERSION,
            now=now,
        )
        assert observed is not None and observed.row.qualified

    async with session_maker() as session, session.begin():
        agent = await session.get(Agent, agent_id, with_for_update=True)
        assert agent is not None
        agent.sha256 = "bc" * 32

    stale = await client.get(url, headers=_HEADERS)
    assert stale.status_code == 200, stale.text
    assert stale.json()["qualified"] is False
    assert stale.json()["current_observation"] is None
    assert any(
        row["policy_revision"] == 2 and row["stale_reason"] == "artifact_changed"
        for row in stale.json()["observations"]
    )
