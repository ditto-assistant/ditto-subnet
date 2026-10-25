"""Retained job evidence is reusable context, never admission by assertion."""

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_server.dependencies import get_session
from ditto.db.models import Agent, AgentStatus, ScreeningAttempt, SubmissionSourceReview
from ditto_screening_protocol import (
    SourceReviewFinding,
    SourceReviewInvariant,
    SourceReviewInvariantAssessment,
    SourceReviewInvariantDecision,
)

TOKEN = "test-admin-token-at-least-32-characters"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "X-Admin-Actor": "test-operator"}


async def test_readiness_reuses_exact_job_binding_without_accepting_a_certificate(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    app.state.config = replace(app.state.config, admin_api_token=TOKEN)

    async def exact_session() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_session] = exact_session
    agent_id, attempt_id, review_id = uuid4(), uuid4(), uuid4()
    sha = "a" * 64
    now = datetime.now(UTC)
    finding = SourceReviewFinding(
        artifact_sha256=sha,
        prompt_revision="test-source-policy-v13",
        risk_level="low",
        confidence=0.99,
        categories=["none"],
        summary="Synthetic source-only reviewer assertion.",
        invariant_assessment=SourceReviewInvariantAssessment(
            schema_version=2,
            decisions=[
                SourceReviewInvariantDecision(
                    invariant=invariant,
                    disposition="pass",
                    pass_clause="unreachable_nonruntime_code",
                    summary="Synthetic reviewer assertion, not runtime evidence.",
                )
                for invariant in SourceReviewInvariant
            ],
        ),
    )
    observation = {
        "ok": True,
        "risk_level": "low",
        "categories": ["none"],
        "finding": finding.model_dump(mode="json"),
        "finding_digest": finding.canonical_digest(),
        "clearance_certified": True,
        "private_unknown_field": "must-not-leak",
    }
    async with session_maker() as session, session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey="test-miner",
                name="test",
                sha256=sha,
                status=AgentStatus.SCORED,
                created_at=now,
            )
        )
        await session.flush()
        session.add(
            ScreeningAttempt(
                attempt_id=attempt_id,
                agent_id=agent_id,
                screener_hotkey="test-worker",
                artifact_sha256=sha,
                policy_version=13,
                status="passed",
                started_at=now,
                deadline=now + timedelta(minutes=10),
                finished_at=now,
            )
        )
        await session.flush()
        session.add(
            SubmissionSourceReview(
                review_id=review_id,
                agent_id=agent_id,
                attempt_id=attempt_id,
                environment="production",
                artifact_sha256=sha,
                status="consumed",
                observation=observation,
                completed_at=now,
                consumed_at=now,
                job_token_hash="b" * 64,
            )
        )
    path = (
        f"/api/v1/admin/screening-submissions/{agent_id}/attempts/"
        f"{attempt_id}/verification-readiness"
    )
    assert (await client.get(path)).status_code == 401
    response = await client.get(path, headers=HEADERS)
    assert response.status_code == 200, response.text
    record = response.json()["source_review"]
    assert record["review_id"] == str(review_id)
    assert record["binding_matches"] is True
    assert record["observation_status"] == "recorded_unverified"
    assert record["observation_ok"] is True
    assert record["clearance_certified"] is True
    assert record["source_admission_accepted"] is False
    assert record["finding_digest"] == finding.canonical_digest()
    assert record["finding"]["invariant_assessment"]["schema_version"] == 2
    assert "must-not-leak" not in response.text
    assert "job_token_hash" not in response.text
    assert all(c["record_status"] == "not_recorded" for c in response.json()["checks"])
    async with session_maker() as session, session.begin():
        row = await session.get(SubmissionSourceReview, review_id)
        assert row is not None
        row.observation = {**observation, "finding_digest": "c" * 64}
    invalid = await client.get(path, headers=HEADERS)
    assert invalid.status_code == 200
    assert invalid.json()["source_review"]["observation_status"] == "invalid"
    assert invalid.json()["source_review"]["finding"] is None
    # The legacy completion parser allows this digest-valid but inconsistent
    # finding. Do not forward it into Backroom's stricter finding schema.
    inconsistent = finding.model_copy(
        update={
            "invariant_assessment": SourceReviewInvariantAssessment(
                schema_version=2,
                decisions=[
                    SourceReviewInvariantDecision(
                        invariant=invariant,
                        disposition="inconclusive",
                        summary="Unresolved, despite the stored low-risk claim.",
                    )
                    for invariant in SourceReviewInvariant
                ],
            ),
        }
    )
    async with session_maker() as session, session.begin():
        row = await session.get(SubmissionSourceReview, review_id)
        assert row is not None
        row.observation = {
            **observation,
            "finding": inconsistent.model_dump(mode="json"),
            "finding_digest": inconsistent.canonical_digest(),
        }
    inconsistent_response = await client.get(path, headers=HEADERS)
    assert inconsistent_response.status_code == 200
    assert (
        inconsistent_response.json()["source_review"]["observation_status"] == "invalid"
    )
    assert inconsistent_response.json()["source_review"]["finding"] is None
    async with session_maker() as session, session.begin():
        row = await session.get(SubmissionSourceReview, review_id)
        assert row is not None
        row.observation = observation
        row.artifact_sha256 = "d" * 64
    mismatch = (await client.get(path, headers=HEADERS)).json()["source_review"]
    assert mismatch["binding_matches"] is False
    assert mismatch["observation_status"] == "invalid"
    assert mismatch["finding"] is None
    async with session_maker() as session, session.begin():
        row = await session.get(SubmissionSourceReview, review_id)
        assert row is not None
        row.artifact_sha256 = sha
        row.observation = {"ok": False, "error_code": "source-review-provider-failed"}
    no_finding = (await client.get(path, headers=HEADERS)).json()["source_review"]
    assert no_finding["observation_status"] == "finding_not_recorded"
    assert no_finding["observation_ok"] is False
    assert no_finding["source_admission_accepted"] is False
    assert no_finding["finding"] is None
