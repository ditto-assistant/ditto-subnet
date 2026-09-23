"""Guarded withdrawal of an unsupported precautionary ATH hold."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_server.ath_hold_withdrawal import (
    WITHDRAW_CONFIRMATION,
    emission_withheld_agent_ids,
    withdrawal_reward_decision,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.public import _ath_review_public_snapshot
from ditto.db.models import Agent, AgentStatus, AthReview, AthReviewAction, Score
from ditto.db.queries.benchmark_rollout import MIN_SCOREABLE_BENCH_VERSION
from ditto.db.queries.scores import list_eligible_ledger

_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_TOKEN}", "X-Admin-Actor": "operator"}
_T0 = datetime(2026, 7, 16, 12, tzinfo=UTC)
_REASON = (
    "Precautionary hold withdrawn. No misconduct finding and no completed "
    "certification."
)
_SS58 = "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"


@pytest.fixture
def maker(
    session_maker: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    return session_maker


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_TOKEN)
    app.state.session_maker = maker

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


async def _seed_scored(
    maker: async_sessionmaker[AsyncSession],
    *,
    status: AgentStatus = AgentStatus.SCORED,
    hotkey: str = "5Benchmax",
    composite: float = 0.97,
) -> tuple[UUID, str]:
    agent_id = uuid4()
    sha256 = agent_id.hex * 2
    async with maker() as session, session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=hotkey,
                name="benchmax",
                sha256=sha256,
                status=status,
                screening_policy_version=8,
                created_at=_T0,
            )
        )
        for index in range(3):
            session.add(
                Score(
                    agent_id=agent_id,
                    validator_hotkey=f"validator-{index}",
                    run_id=f"manual-hold-run-{agent_id.hex[:8]}-{index}",
                    signature=None,
                    seed=7,
                    bench_version=MIN_SCOREABLE_BENCH_VERSION,
                    composite=composite,
                    tool_mean=composite,
                    memory_mean=0.90,
                    median_ms=100,
                    n=114,
                    details={"bench_version": MIN_SCOREABLE_BENCH_VERSION},
                    generated_at=_T0 + timedelta(minutes=index),
                )
            )
    return agent_id, sha256


async def _open_manual_hold(
    client: httpx.AsyncClient, agent_id: UUID, sha256: str
) -> dict:
    opened = await client.post(
        f"/api/v1/admin/copy-reviews/{agent_id}/open",
        json={
            "expected_sha256": sha256,
            "expected_score_count": 3,
            "reason": "Manual precautionary hold pending a transferable ruling",
        },
        headers=_HEADERS,
    )
    assert opened.status_code == 200, opened.text
    return opened.json()


def _preview_body(opened: dict, sha256: str, **overrides: object) -> dict:
    body = {
        "review_id": opened["review"]["review_id"],
        "expected_sha256": sha256,
        "expected_score_count": 3,
        "expected_agent_status": AgentStatus.ATH_PENDING_REVIEW,
        "reason": _REASON,
    }
    body.update(overrides)
    return body


async def _preview(
    client: httpx.AsyncClient, agent_id: UUID, body: dict
) -> httpx.Response:
    return await client.post(
        f"/api/v1/admin/copy-reviews/{agent_id}/withdraw/preview",
        json=body,
        headers=_HEADERS,
    )


async def _execute(
    client: httpx.AsyncClient, agent_id: UUID, body: dict, token: str
) -> httpx.Response:
    return await client.post(
        f"/api/v1/admin/copy-reviews/{agent_id}/withdraw",
        json={
            **body,
            "preview_token": token,
            "confirmation": WITHDRAW_CONFIRMATION,
        },
        headers=_HEADERS,
    )


async def test_pending_manual_hold_withdraws_without_clear_or_reject(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id, sha256 = await _seed_scored(maker)
    _install(app, maker)
    opened = await _open_manual_hold(client, agent_id, sha256)
    body = _preview_body(opened, sha256)
    preview = await _preview(client, agent_id, body)
    assert preview.status_code == 200, preview.text
    preview_body = preview.json()
    assert preview_body["emission_reward_eligible"] is False
    assert preview_body["emission_gate"] == "unavailable"
    assert preview_body["would_change_emission_crown"] is False
    assert preview_body["restored_status"] == AgentStatus.SCORED
    assert (
        preview_body["board_after"]["ranked_count"]
        == preview_body["board_before"]["ranked_count"] + 1
    )
    assert "misconduct finding" in preview_body["emission_reason"]
    assert "certification" in preview_body["emission_reason"]

    executed = await _execute(client, agent_id, body, preview_body["preview_token"])
    assert executed.status_code == 200, executed.text
    result = executed.json()
    assert result["review"]["resolution"] == "withdraw"
    assert result["review"]["resolution_reason"] == _REASON
    assert result["review"]["original"]["reason"].startswith("Manual precautionary")
    assert result["agent_status"] == AgentStatus.SCORED
    assert result["emission_reward_eligible"] is False
    assert result["emission_gate"] == "unavailable"

    async with maker() as session:
        review = await session.scalar(
            select(AthReview).where(AthReview.agent_id == agent_id)
        )
        assert review is not None and review.original_reason is not None
        actions = list(
            await session.scalars(
                select(AthReviewAction).where(
                    AthReviewAction.review_id == review.review_id
                )
            )
        )
        score_count = await session.scalar(
            select(func.count()).select_from(Score).where(Score.agent_id == agent_id)
        )
        ledger = await list_eligible_ledger(session)
        withheld = await emission_withheld_agent_ids(session, [agent_id])
        assert review is not None
        assert review.status == "resolved"
        assert review.resolution == "withdraw"
        assert review.original_reason.startswith("Manual precautionary")
        assert review.resolved_by == "operator"
        assert [action.action for action in actions] == ["withdraw"]
        assert score_count == 3
        assert any(row.agent_id == agent_id for row in ledger)
        assert withheld == {agent_id}

        @dataclass
        class _Row:
            agent: Agent

        agent = await session.get(Agent, agent_id)
        assert agent is not None
        snapshots, _medians = await _ath_review_public_snapshot(session, [_Row(agent)])
    snapshot = snapshots[agent_id]
    assert snapshot.event == "withdrawn"
    assert snapshot.reason == _REASON
    assert snapshot.original_reason.startswith("Manual precautionary")


async def test_withdrawal_does_not_transfer_a_sibling_clearance(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id, sha256 = await _seed_scored(maker, hotkey=_SS58)
    sibling_id, _sibling_sha = await _seed_scored(maker, hotkey=_SS58, composite=0.4)
    async with maker() as session, session.begin():
        session.add(
            AthReview(
                review_id=uuid4(),
                agent_id=sibling_id,
                status="resolved",
                opened_at=_T0,
                resolved_at=_T0 + timedelta(minutes=5),
                resolved_by="operator",
                resolution="clear",
                resolution_reason="Sibling artifact cleared on its own record",
                original_reason="Sibling hold",
                original_policy_version=8,
                original_evidence={"sha256": "ab" * 32, "previous_status": "scored"},
                algorithm_provenance={"snapshot": "manual-admin-hold"},
            )
        )
    _install(app, maker)
    opened = await _open_manual_hold(client, agent_id, sha256)
    body = _preview_body(opened, sha256)
    preview = await _preview(client, agent_id, body)
    assert preview.status_code == 200, preview.text
    executed = await _execute(client, agent_id, body, preview.json()["preview_token"])
    assert executed.status_code == 200, executed.text

    async with maker() as session:
        sibling = await session.scalar(
            select(AthReview).where(AthReview.agent_id == sibling_id)
        )
        held = await session.get(Agent, agent_id)
        assert sibling is not None and sibling.resolution == "clear"
        assert held is not None
        decision = withdrawal_reward_decision(
            agent_id=agent_id, artifact_sha256=held.sha256
        )
        withheld = await emission_withheld_agent_ids(session, [agent_id, sibling_id])
    assert decision.reward_eligible is False
    assert decision.gate == "unavailable"
    assert withheld == {agent_id}

    board = await client.get("/api/v1/public/leaderboard")
    assert board.status_code == 200, board.text
    payload = board.json()
    withdrawn = next(
        entry for entry in payload["entries"] if entry["agent_id"] == str(agent_id)
    )
    assert withdrawn["emission_eligible"] is False
    assert withdrawn["rank"] is not None
    emissions = payload["emissions"]
    assert emissions is None or emissions["champion_agent_id"] != str(agent_id)


@pytest.mark.parametrize(
    ("field", "value", "detail"),
    [
        ("expected_sha256", "0" * 64, "artifact sha256 changed"),
        ("expected_score_count", 2, "score count changed"),
        ("expected_agent_status", AgentStatus.SCORED, "agent status changed"),
        (
            "review_id",
            "00000000-0000-4000-8000-000000000001",
            "review id does not match",
        ),
    ],
)
async def test_preview_refuses_stale_guards(
    app: FastAPI,
    client: httpx.AsyncClient,
    maker: async_sessionmaker[AsyncSession],
    field: str,
    value: object,
    detail: str,
) -> None:
    agent_id, sha256 = await _seed_scored(maker)
    _install(app, maker)
    opened = await _open_manual_hold(client, agent_id, sha256)
    preview = await _preview(
        client, agent_id, _preview_body(opened, sha256, **{field: value})
    )
    assert preview.status_code == 409
    assert detail in preview.text
    async with maker() as session:
        review = await session.scalar(
            select(AthReview).where(AthReview.agent_id == agent_id)
        )
        assert review is not None and review.status == "pending"


async def test_execute_refuses_stale_sha_score_and_resolved_review(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id, sha256 = await _seed_scored(maker)
    _install(app, maker)
    opened = await _open_manual_hold(client, agent_id, sha256)
    body = _preview_body(opened, sha256)
    preview = await _preview(client, agent_id, body)
    assert preview.status_code == 200, preview.text
    token = preview.json()["preview_token"]

    async with maker() as session, session.begin():
        agent = await session.get(Agent, agent_id)
        assert agent is not None
        agent.sha256 = "ab" * 32
    stale_sha = await _execute(client, agent_id, body, token)
    assert stale_sha.status_code == 409
    assert "artifact sha256 changed" in stale_sha.text

    async with maker() as session, session.begin():
        agent = await session.get(Agent, agent_id)
        assert agent is not None
        agent.sha256 = sha256
        session.add(
            Score(
                agent_id=agent_id,
                validator_hotkey="validator-extra",
                run_id=f"extra-{agent_id.hex[:8]}",
                signature=None,
                seed=7,
                bench_version=MIN_SCOREABLE_BENCH_VERSION,
                composite=0.97,
                tool_mean=0.97,
                memory_mean=0.90,
                median_ms=100,
                n=114,
                details={},
                generated_at=_T0,
            )
        )
    stale_score = await _execute(client, agent_id, body, token)
    assert stale_score.status_code == 409
    assert "score count changed" in stale_score.text

    async with maker() as session, session.begin():
        extra = await session.scalar(
            select(Score).where(Score.validator_hotkey == "validator-extra")
        )
        assert extra is not None
        await session.delete(extra)
    resolved = await client.post(
        f"/api/v1/admin/copy-reviews/{agent_id}/resolve",
        json={"resolution": "clear", "reason": "Resolved by another operator"},
        headers=_HEADERS,
    )
    assert resolved.status_code == 200, resolved.text
    replay = await _execute(client, agent_id, body, token)
    assert replay.status_code == 409
    assert "review already resolved" in replay.text


async def test_repeated_withdrawal_is_refused(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id, sha256 = await _seed_scored(maker)
    _install(app, maker)
    opened = await _open_manual_hold(client, agent_id, sha256)
    body = _preview_body(opened, sha256)
    preview = await _preview(client, agent_id, body)
    token = preview.json()["preview_token"]
    first = await _execute(client, agent_id, body, token)
    assert first.status_code == 200, first.text
    second = await _execute(client, agent_id, body, token)
    assert second.status_code == 409
    assert "review already withdrawn" in second.text
    async with maker() as session:
        actions = list(
            await session.scalars(
                select(AthReviewAction)
                .join(AthReview, AthReview.review_id == AthReviewAction.review_id)
                .where(AthReview.agent_id == agent_id)
            )
        )
    assert [action.action for action in actions] == ["withdraw"]


async def test_concurrent_withdrawal_lets_one_request_land(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id, sha256 = await _seed_scored(maker)
    _install(app, maker)
    opened = await _open_manual_hold(client, agent_id, sha256)
    body = _preview_body(opened, sha256)
    preview = await _preview(client, agent_id, body)
    assert preview.status_code == 200, preview.text
    token = preview.json()["preview_token"]
    first, second = await asyncio.gather(
        _execute(client, agent_id, body, token),
        _execute(client, agent_id, body, token),
    )
    codes = sorted(response.status_code for response in (first, second))
    assert codes == [200, 409]
    texts = first.text + second.text
    assert "review already withdrawn" in texts or "board changed" in texts
    async with maker() as session:
        review = await session.scalar(
            select(AthReview).where(AthReview.agent_id == agent_id)
        )
        assert review is not None
        action_count = await session.scalar(
            select(func.count())
            .select_from(AthReviewAction)
            .where(AthReviewAction.review_id == review.review_id)
        )
    assert review is not None and review.resolution == "withdraw"
    assert action_count == 1


async def test_automated_hold_cannot_be_withdrawn(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id, sha256 = await _seed_scored(maker, status=AgentStatus.ATH_PENDING_REVIEW)
    review_id = uuid4()
    async with maker() as session, session.begin():
        agent = await session.get(Agent, agent_id)
        assert agent is not None
        agent.review_reason = "Score-finalization copy hold"
        session.add(
            AthReview(
                review_id=review_id,
                agent_id=agent_id,
                status="pending",
                opened_at=_T0,
                original_reason=agent.review_reason,
                original_policy_version=8,
                original_evidence={
                    "sha256": sha256,
                    "score_count": 3,
                    "previous_status": "scored",
                },
                algorithm_provenance={"snapshot": "score-finalization"},
            )
        )
    _install(app, maker)
    preview = await _preview(
        client,
        agent_id,
        _preview_body({"review": {"review_id": str(review_id)}}, sha256),
    )
    assert preview.status_code == 409
    assert "only a manual precautionary hold can be withdrawn" in preview.text


async def test_withdrawn_hold_is_not_a_precedent(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id, sha256 = await _seed_scored(maker)
    _install(app, maker)
    opened = await _open_manual_hold(client, agent_id, sha256)
    body = _preview_body(opened, sha256)
    preview = await _preview(client, agent_id, body)
    executed = await _execute(client, agent_id, body, preview.json()["preview_token"])
    assert executed.status_code == 200, executed.text
    precedents = await client.get(
        "/api/v1/admin/copy-reviews/precedents",
        params={"q": "Precautionary hold withdrawn", "resolution": "all"},
        headers=_HEADERS,
    )
    assert precedents.status_code == 200, precedents.text
    assert precedents.json()["items"] == []
