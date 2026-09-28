"""Audited calibration and appeal controls over actual paid Postgres history."""

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.submission_attempts import AttemptControlSettings
from ditto.api_server.dependencies import get_session
from ditto.db.models import Agent, SubmissionAttemptCalibration
from ditto.db.queries.submission_attempts import compare_attempt
from ditto.tests.submission_attempt_fixtures import NOW, paid_attempt, profile

pytestmark = pytest.mark.asyncio
TOKEN = "test-admin-token-at-least-32-characters"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
PATH = "/api/v1/admin/submission-attempts"


def install(app: FastAPI, maker: async_sessionmaker[AsyncSession]):
    app.state.config = replace(app.state.config, admin_api_token=TOKEN)

    async def dependency() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = dependency


def policy(mode="shadow", revision=0, **kwargs):
    return {
        "expected_revision": revision,
        "settings": AttemptControlSettings(mode=mode).model_dump(),
        "actor": "operator@example.com",
        "reason": "reviewed admission settings",
        "confirmation": f"SET SUBMISSION ATTEMPT MODE {mode.upper()}",
        **kwargs,
    }


async def test_shadow_default_auth_revision_and_enforcement_gate(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker,
):
    install(app, session_maker)
    assert (await client.get(PATH)).status_code == 401
    initial = await client.get(PATH, headers=HEADERS)
    assert initial.json()["current"]["settings"]["mode"] == "shadow"
    rejected = await client.post(PATH, json=policy("enforce"), headers=HEADERS)
    assert rejected.status_code == 409
    invalid = policy()
    invalid["settings"]["fast_repair_limit"] = 0
    assert (await client.post(PATH, json=invalid, headers=HEADERS)).status_code == 422
    updated = await client.post(PATH, json=policy("off"), headers=HEADERS)
    assert updated.status_code == 200, updated.text
    assert updated.json()["actor"] == "operator@example.com"
    assert (await client.post(PATH, json=policy(), headers=HEADERS)).status_code == 409
    wrong = policy(revision=updated.json()["revision"], confirmation="wrong")
    assert (await client.post(PATH, json=wrong, headers=HEADERS)).status_code == 409


async def test_historical_replay_reports_false_throttles_and_is_not_eligible(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker,
    session: AsyncSession,
):
    install(app, session_maker)
    async with session.begin():
        anchor = await paid_attempt(session, classification="first_submission")
        for i in range(3):
            await paid_attempt(
                session, lineage=anchor, submitted_at=NOW - timedelta(minutes=9 - i)
            )
        candidate = await paid_attempt(
            session, lineage=anchor, submitted_at=NOW - timedelta(minutes=5)
        )
    payload = {
        "settings": AttemptControlSettings().model_dump(),
        "cases": [
            {
                "agent_id": str(candidate),
                "expected_classification": "small_source_delta",
                "expected_throttled": False,
            }
        ],
        "actor": "operator@example.com",
        "reason": "independently reviewed repair replay",
    }
    response = await client.post(PATH + "/replay", headers=HEADERS, json=payload)
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["false_throttles"] == 1
    assert report["immediate_admissions_deferred"] == 1
    assert report["eligible_for_enforcement"] is False
    audit = await client.get(
        PATH + "/replay/" + report["calibration_id"], headers=HEADERS
    )
    assert audit.json()["actor"] == "operator@example.com"
    assert audit.json()["report"] == report
    enforce = await client.post(
        PATH,
        headers=HEADERS,
        json=policy("enforce", calibration_id=report["calibration_id"]),
    )
    assert enforce.status_code == 409
    assert "profile" not in audit.text and "fingerprint" not in audit.text


async def test_complete_replay_allows_only_matching_fresh_policy(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker,
    session: AsyncSession,
):
    install(app, session_maker)
    cases = []

    async def add(expected, throttled=False, **kwargs):
        agent = await paid_attempt(session, classification=expected, **kwargs)
        cases.append(
            {
                "agent_id": str(agent),
                "expected_classification": expected,
                "expected_throttled": throttled,
            }
        )
        return agent

    async with session.begin():
        anchor = await add("first_submission", submitted_at=NOW - timedelta(minutes=20))
        for i in range(3):
            await add(
                "small_source_delta",
                lineage=anchor,
                submitted_at=NOW - timedelta(minutes=19 - i),
            )
        await add(
            "small_source_delta",
            True,
            lineage=anchor,
            submitted_at=NOW - timedelta(minutes=15),
        )
        material = profile("f")
        material["fingerprint"]["m"] = list(range(200, 300))
        await add(
            "material_new_work",
            source=material,
            submitted_at=NOW - timedelta(minutes=14),
        )
        infra = await add(
            "first_submission",
            hotkey="infra-key",
            coldkey="infra-owner",
            outcome="failed",
            submitted_at=NOW - timedelta(minutes=12),
        )
        await add(
            "infrastructure_retry",
            hotkey="infra-key",
            coldkey="infra-owner",
            lineage=infra,
            submitted_at=NOW - timedelta(minutes=11),
        )
        repair = await add(
            "first_submission",
            hotkey="repair-key",
            coldkey="repair-owner",
            reason="docker-build",
            submitted_at=NOW - timedelta(minutes=10),
        )
        await add(
            "packaging_only_repair",
            hotkey="repair-key",
            coldkey="repair-owner",
            lineage=repair,
            fast_repair=True,
            source=profile(packaging="f"),
            submitted_at=NOW - timedelta(minutes=9),
        )
    response = await client.post(
        PATH + "/replay",
        headers=HEADERS,
        json={
            "settings": AttemptControlSettings().model_dump(),
            "cases": cases,
            "actor": "reviewer@example.com",
            "reason": "independently labeled actual paid submissions",
        },
    )
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["eligible_for_enforcement"], report
    calibration_id = report["calibration_id"]
    changed = policy("enforce", calibration_id=calibration_id)
    changed["settings"]["low_information_limit"] = 4
    assert (await client.post(PATH, headers=HEADERS, json=changed)).status_code == 409
    async with session.begin():
        calibration = await session.get(SubmissionAttemptCalibration, calibration_id)
        assert calibration is not None
        calibration.created_at -= timedelta(days=8)
    assert (
        await client.post(
            PATH, headers=HEADERS, json=policy("enforce", calibration_id=calibration_id)
        )
    ).status_code == 409
    async with session.begin():
        assert calibration is not None
        calibration.created_at += timedelta(days=8)
    accepted = await client.post(
        PATH, headers=HEADERS, json=policy("enforce", calibration_id=calibration_id)
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["settings"]["mode"] == "enforce"


async def test_appeal_is_audited_idempotent_and_does_not_change_agent_verdict(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker,
    session: AsyncSession,
):
    install(app, session_maker)
    now = datetime.now(UTC)
    async with session.begin():
        anchor = await paid_attempt(session, submitted_at=now - timedelta(minutes=10))
        await paid_attempt(
            session, lineage=anchor, submitted_at=now - timedelta(minutes=9)
        )
        previous = await paid_attempt(
            session, lineage=anchor, submitted_at=now - timedelta(minutes=8)
        )
    before = await compare_attempt(
        session,
        profile=profile(),
        hotkey="hotkey-a",
        coldkey="owner-a",
        netuid=118,
        settings=AttemptControlSettings(),
        revision=0,
        now=now,
    )
    assert before.retry_at is not None
    original_status = await session.scalar(
        select(Agent.status).where(Agent.agent_id == previous)
    )
    await session.rollback()
    payload = {
        "agent_id": str(previous),
        "expected_policy_revision": 0,
        "actor": "operator@example.com",
        "reason": "confirmed legitimate packaging repair",
        "confirmation": f"ALLOW SUBMISSION RETRY {previous}",
    }
    first = await client.post(PATH + "/appeal", headers=HEADERS, json=payload)
    second = await client.post(PATH + "/appeal", headers=HEADERS, json=payload)
    assert first.status_code == second.status_code == 200, first.text
    assert first.json() == second.json()
    now = datetime.now(UTC)
    after = await compare_attempt(
        session,
        profile=profile(),
        hotkey="hotkey-a",
        coldkey="owner-a",
        netuid=118,
        settings=AttemptControlSettings(),
        revision=0,
        now=now,
    )
    assert after.retry_at is None
    record = await client.get(PATH + "/" + str(previous), headers=HEADERS)
    assert record.json()["appeals"][0]["actor"] == "operator@example.com"
    assert (
        await session.scalar(select(Agent.status).where(Agent.agent_id == previous))
        == original_status
    )
    await session.rollback()
    # A repair whose lexical sketch differs can leave an older artifact as the
    # strongest match. Its consumed appeal must not authorize further retries.
    repair = profile("f")
    repair["fingerprint"]["m"] = list(range(1, 101))
    quoted = await compare_attempt(
        session,
        profile=repair,
        hotkey="hotkey-a",
        coldkey="owner-a",
        netuid=118,
        settings=AttemptControlSettings(),
        revision=0,
        now=now,
    )
    assert quoted.appeal_id is not None
    await session.rollback()
    async with session.begin():
        await paid_attempt(
            session,
            source=repair,
            decision=quoted,
            submitted_at=now + timedelta(seconds=1),
        )
    reused = await compare_attempt(
        session,
        profile=profile(),
        hotkey="hotkey-a",
        coldkey="owner-a",
        netuid=118,
        settings=AttemptControlSettings(),
        revision=0,
        now=now + timedelta(seconds=2),
    )
    assert reused.reference_agent_id == previous
    assert reused.appeal_id is None
    assert reused.retry_at is not None
    missing = await client.get(PATH + "/" + str(uuid4()), headers=HEADERS)
    assert missing.status_code == 404
