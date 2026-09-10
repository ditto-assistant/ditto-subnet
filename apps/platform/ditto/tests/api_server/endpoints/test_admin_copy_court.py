"""Copy-hold triage court API regression coverage."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_server.dependencies import get_session
from ditto.db.models import (
    Agent,
    AgentStatus,
    AthCopyCourtRecommendation,
    AthReview,
    CopyCourtSettingsRevision,
)

_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_TOKEN}", "X-Admin-Actor": "operator"}
_T0 = datetime(2026, 9, 9, 12, tzinfo=UTC)


@pytest.fixture
def maker(
    session_maker: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    return session_maker


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_TOKEN)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


async def _seed_copy_hold(
    maker: async_sessionmaker[AsyncSession],
    *,
    reason: str = "legacy near-copy signal",
) -> tuple:
    """A rejected ancestor plus a held candidate, with a resolved ATH review."""
    original_id, agent_id, review_id, ancestor_review_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    sha = original_id.hex * 2
    async with maker() as session, session.begin():
        session.add_all(
            [
                Agent(
                    agent_id=original_id,
                    miner_hotkey="5Ancestor",
                    name="unione",
                    version=21,
                    sha256=sha,
                    normalized_source_hash="aa" * 32,
                    status=AgentStatus.BANNED,
                    created_at=_T0 - timedelta(hours=48),
                ),
                Agent(
                    agent_id=agent_id,
                    miner_hotkey="5Ancestor",
                    name="Uniking",
                    version=1,
                    sha256=sha,
                    normalized_source_hash="aa" * 32,
                    status=AgentStatus.ATH_PENDING_REVIEW,
                    duplicate_of=original_id,
                    review_reason=reason,
                    screening_policy_version=12,
                    created_at=_T0,
                ),
                AthReview(
                    review_id=ancestor_review_id,
                    agent_id=original_id,
                    status="resolved",
                    opened_at=_T0 - timedelta(hours=40),
                    resolved_at=_T0 - timedelta(hours=39),
                    resolved_by="operator",
                    resolution="reject",
                    resolution_reason="Reject unione v21: host overwrite of the "
                    "graded slot via calculated_money_answer (limb b).",
                    original_reason="prior pattern",
                    original_policy_version=12,
                    original_evidence={"sha256": sha},
                    algorithm_provenance={},
                ),
                AthReview(
                    review_id=review_id,
                    agent_id=agent_id,
                    status="pending",
                    opened_at=_T0,
                    original_duplicate_of=original_id,
                    original_reason=reason,
                    original_policy_version=12,
                    original_evidence={"sha256": sha},
                    algorithm_provenance={},
                ),
                CopyCourtSettingsRevision(
                    parent_revision=0,
                    settings={
                        "mode": "shadow",
                        "byte_identical_resubmission_mode": "shadow",
                    },
                    checksum="cc" * 32,
                    reason="enable shadow triage for the byte-identical class",
                    actor="operator",
                ),
            ]
        )
    return agent_id, original_id, review_id, sha


async def _recommend(
    maker: async_sessionmaker[AsyncSession],
) -> AthCopyCourtRecommendation | None:
    from ditto.api_server.copy_hold_court import CopyHoldCourt

    court = CopyHoldCourt(session_maker=maker)
    await court.tick()
    async with maker() as session:
        return await session.scalar(select(AthCopyCourtRecommendation))


async def test_settings_requires_confirmation_and_revision(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    _install(app, maker)
    payload = {
        "expected_revision": 0,
        "settings": {"mode": "shadow", "byte_identical_resubmission_mode": "shadow"},
        "reason": "enable shadow triage for byte-identical resubmissions",
        "confirmation": "WRONG CONFIRMATION",
    }
    response = await client.post(
        "/api/v1/admin/copy-court/settings", json=payload, headers=_HEADERS
    )
    assert response.status_code == 409

    payload["confirmation"] = "APPLY COPY COURT SHADOW"
    response = await client.post(
        "/api/v1/admin/copy-court/settings", json=payload, headers=_HEADERS
    )
    assert response.status_code == 200
    body = response.json()
    assert body["revision"] == 1 and body["parent_revision"] == 0

    stale = {**payload, "expected_revision": 0}
    response = await client.post(
        "/api/v1/admin/copy-court/settings", json=stale, headers=_HEADERS
    )
    assert response.status_code == 409


async def test_settings_get_returns_history(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    _install(app, maker)
    response = await client.get("/api/v1/admin/copy-court/settings", headers=_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["current"] is None and body["history"] == []

    await client.post(
        "/api/v1/admin/copy-court/settings",
        json={
            "expected_revision": 0,
            "settings": {"mode": "shadow"},
            "reason": "first shadow revision",
            "actor": "operator",
            "confirmation": "APPLY COPY COURT SHADOW",
        },
        headers=_HEADERS,
    )
    response = await client.get("/api/v1/admin/copy-court/settings", headers=_HEADERS)
    body = response.json()
    assert body["current"]["settings"]["mode"] == "shadow"
    assert len(body["history"]) == 1


async def test_byte_identical_resubmission_gets_reject_recommendation(
    app: FastAPI,
    client: httpx.AsyncClient,
    maker: async_sessionmaker[AsyncSession],
) -> None:
    ancestor_hint = uuid4()
    agent_id, original_id, review_id, sha = await _seed_copy_hold(
        maker,
        reason=(
            "Same miner, previously rejected as unione v21 "
            f"(agent {ancestor_hint}, uploaded 2026-09-01T12:38:29+00:00). This "
            "upload is byte-identical to that rejected artifact. Held for "
            "operator review to check whether the cited behavior was removed."
        ),
    )
    _install(app, maker)
    recommendation = await _recommend(maker)
    assert recommendation is not None
    assert recommendation.verdict == "reject"
    assert recommendation.hold_class == "rejected_resubmission_byte_identical"
    assert "unione v21" in recommendation.reason
    assert recommendation.citations[0]["agent_id"] == str(original_id)
    assert recommendation.evidence["identity_basis"] == "exact_sha256"
    # Shadow mode never touched the hold.
    async with maker() as session:
        agent = await session.get(Agent, agent_id)
        review = await session.get(AthReview, review_id)
        assert agent is not None and review is not None
        assert agent.status == AgentStatus.ATH_PENDING_REVIEW
        assert review.status == "pending"

    # The recommendation endpoint surfaces it while the hold is pending.
    response = await client.get(
        "/api/v1/admin/copy-court/recommendations", headers=_HEADERS
    )
    assert response.status_code == 200
    items = response.json()["items"]
    assert any(item["agent_id"] == str(agent_id) for item in items)


async def test_reason_hint_without_sha_proof_escalates(
    app: FastAPI,
    maker: async_sessionmaker[AsyncSession],
) -> None:
    ancestor_hint = uuid4()
    _original_id, agent_id, review_id, _sha = await _seed_copy_hold(
        maker,
        reason=(
            "Same miner, previously rejected as unione v21 "
            f"(agent {ancestor_hint}, uploaded 2026-09-01T12:38:29+00:00). This "
            "upload is byte-identical to that rejected artifact. Held for "
            "operator review to check whether the cited behavior was removed."
        ),
    )
    # The stored bytes differ from the ancestor's: the hint lies.
    async with maker() as session, session.begin():
        agent = await session.get(Agent, agent_id)
        assert agent is not None
        agent.sha256 = "bb" * 32
        agent.normalized_source_hash = "cc" * 32
    _install(app, maker)
    recommendation = await _recommend(maker)
    assert recommendation is not None
    assert recommendation.verdict == "escalate"
    assert recommendation.hold_class == "unknown"


async def test_near_duplicate_escalates_with_evidence(
    app: FastAPI,
    maker: async_sessionmaker[AsyncSession],
) -> None:
    ancestor_hint = uuid4()
    original_id, agent_id, _review_id, _sha = await _seed_copy_hold(
        maker,
        reason=(
            f"content near-duplicate of agent {ancestor_hint}: composite delta "
            "0.0255, jaccard 0.969, containment 0.977"
        ),
    )
    # Different source hashes: not a proven identity.
    async with maker() as session, session.begin():
        candidate = await session.get(Agent, agent_id)
        ancestor = await session.get(Agent, original_id)
        assert candidate is not None and ancestor is not None
        candidate.normalized_source_hash = "dd" * 32
        ancestor.normalized_source_hash = "ee" * 32
    _install(app, maker)
    recommendation = await _recommend(maker)
    assert recommendation is not None
    assert recommendation.verdict == "escalate"
    assert recommendation.hold_class == "near_duplicate"


async def test_off_mode_records_nothing(
    app: FastAPI, maker: async_sessionmaker[AsyncSession]
) -> None:
    await _seed_copy_hold(maker)
    async with maker() as session, session.begin():
        latest = await session.scalar(
            select(CopyCourtSettingsRevision)
            .order_by(CopyCourtSettingsRevision.revision.desc())
            .limit(1)
        )
        assert latest is not None
        session.add(
            CopyCourtSettingsRevision(
                parent_revision=latest.revision,
                settings={"mode": "off"},
                checksum="ab" * 32,
                reason="court disabled, keep collecting nothing",
                actor="operator",
            )
        )
    _install(app, maker)
    recommendation = await _recommend(maker)
    assert recommendation is None
