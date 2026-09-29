"""Guarded withdrawal of an unsupported precautionary ATH hold."""

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.emission_eligibility import (
    WITHDRAWN_REVIEW_REASON,
    EmissionEligibilitySettings,
)
from ditto.api_server.ath_hold_withdrawal import (
    WITHDRAW_CONFIRMATION,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.emission_eligibility import ResolvedEligibilityPolicy, classify
from ditto.api_server.endpoints.admin_ath_rulings import (
    _b64,
    _sign_preview,
    _unb64,
)
from ditto.api_server.endpoints.admin_quarantine import BATCH_PREVIEW_TTL
from ditto.api_server.endpoints.public import _ath_review_public_snapshot
from ditto.db.models import Agent, AgentStatus, AthReview, AthReviewAction, Score
from ditto.db.queries.benchmark_rollout import MIN_SCOREABLE_BENCH_VERSION
from ditto.db.queries.emission_eligibility import load_review_postures
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
    assert preview_body["emission_reward_eligible"] is True
    assert preview_body["emission_gate"] == "off"
    assert (
        preview_body["would_change_emission_crown"]
        == preview_body["would_change_crown"]
    )
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
    assert result["emission_reward_eligible"] is True
    assert result["emission_gate"] == "off"

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
        assert review is not None
        assert review.status == "resolved"
        assert review.resolution == "withdraw"
        assert review.original_reason.startswith("Manual precautionary")
        assert review.resolved_by == "operator"
        assert [action.action for action in actions] == ["withdraw"]
        assert score_count == 3
        assert any(row.agent_id == agent_id for row in ledger)

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
    """Under enforce, a same-hotkey sibling's clear never pays the withdrawn row."""
    from ditto.tests.api_server.endpoints.test_emission_eligibility_ledger import (
        _fleet,
        _set_posture,
    )

    agent_id, sha256 = await _seed_scored(maker, hotkey=_SS58)
    sibling_id, sibling_sha = await _seed_scored(maker, hotkey=_SS58, composite=0.4)
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
                original_evidence={
                    "sha256": sibling_sha,
                    "previous_status": "scored",
                },
                algorithm_provenance={"snapshot": "manual-admin-hold"},
            )
        )
    async with maker() as session:
        await _set_posture(session, enforcement="enforce")
        await _fleet(session, seen_at=datetime.now(UTC))
    _install(app, maker)
    app.state.emission_eligibility.invalidate()
    opened = await _open_manual_hold(client, agent_id, sha256)
    body = _preview_body(opened, sha256)
    preview = await _preview(client, agent_id, body)
    assert preview.status_code == 200, preview.text
    assert preview.json()["emission_gate"] == "enforce"
    assert preview.json()["emission_reward_eligible"] is False
    assert preview.json()["would_change_emission_crown"] is False
    executed = await _execute(client, agent_id, body, preview.json()["preview_token"])
    assert executed.status_code == 200, executed.text
    assert executed.json()["emission_reward_eligible"] is False

    async with maker() as session:
        sibling = await session.scalar(
            select(AthReview).where(AthReview.agent_id == sibling_id)
        )
        assert sibling is not None and sibling.resolution == "clear"

    board = await client.get("/api/v1/public/leaderboard")
    assert board.status_code == 200, board.text
    entries = {entry["agent_id"]: entry for entry in board.json()["entries"]}
    withdrawn = entries[str(agent_id)]
    assert withdrawn["rank"] is not None
    record = withdrawn["reward_eligibility"]
    assert record["enforcement"] == "enforce"
    assert record["state"] == "unresolved_review"
    assert record["reward_eligible"] is False
    # Withheld like an open review, but never described as "still open".
    assert record["reason"] == WITHDRAWN_REVIEW_REASON
    # The sibling's own clear still certifies the sibling, and only the sibling.
    async with maker() as session:
        postures = await load_review_postures(session, [agent_id, sibling_id])
    policy = ResolvedEligibilityPolicy(
        settings=EmissionEligibilitySettings(enforcement="enforce")
    )
    now = datetime.now(UTC)
    for candidate, digest, state in (
        (sibling_id, sibling_sha, "eligible"),
        (agent_id, sha256, "unresolved_review"),
    ):
        decision = classify(
            agent_id=candidate,
            artifact_sha256=digest,
            bench_version=MIN_SCOREABLE_BENCH_VERSION,
            posture=postures[candidate],
            policy=policy,
            now=now,
        )
        assert decision.state == state


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


async def _resolve(
    client: httpx.AsyncClient, agent_id: UUID, resolution: str, reason: str
) -> dict:
    resolved = await client.post(
        f"/api/v1/admin/copy-reviews/{agent_id}/resolve",
        json={"resolution": resolution, "reason": reason},
        headers=_HEADERS,
    )
    assert resolved.status_code == 200, resolved.text
    return resolved.json()


async def _reopen(
    client: httpx.AsyncClient,
    agent_id: UUID,
    sha256: str,
    reason: str,
    *,
    score_count: int = 3,
) -> dict:
    reopened = await client.post(
        f"/api/v1/admin/copy-reviews/{agent_id}/open",
        json={
            "expected_sha256": sha256,
            "expected_score_count": score_count,
            "reason": reason,
        },
        headers=_HEADERS,
    )
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["reopened"] is True
    return reopened.json()


async def _review_state(
    maker: async_sessionmaker[AsyncSession], agent_id: UUID
) -> tuple[AthReview, Agent, list[str]]:
    async with maker() as session:
        review = await session.scalar(
            select(AthReview).where(AthReview.agent_id == agent_id)
        )
        agent = await session.get(Agent, agent_id)
        assert review is not None and agent is not None
        actions = list(
            await session.scalars(
                select(AthReviewAction.action)
                .where(AthReviewAction.review_id == review.review_id)
                .order_by(AthReviewAction.created_at, AthReviewAction.action_id)
            )
        )
    return review, agent, actions


_PRIOR_RULING = "review has a prior clear/reject ruling"


async def test_reopened_rejection_cannot_be_withdrawn(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    """A reject reopened for reconsideration is settled by clear or reject.

    Withdrawing it would restore the banned artifact to ``scored`` and turn the
    reject into a ``withdraw`` that the precedent search no longer returns.
    """
    agent_id, sha256 = await _seed_scored(maker)
    _install(app, maker)
    opened = await _open_manual_hold(client, agent_id, sha256)
    body = _preview_body(opened, sha256)
    early = await _preview(client, agent_id, body)
    assert early.status_code == 200, early.text
    rejected = await _resolve(
        client, agent_id, "reject", "Violation: I5 production engine is emulated"
    )
    assert rejected["agent_status"] == AgentStatus.BANNED
    reopened = await _reopen(
        client, agent_id, sha256, "Reconsidering the rejection on appeal"
    )

    preview = await _preview(client, agent_id, _preview_body(reopened, sha256))
    assert preview.status_code == 409, preview.text
    assert _PRIOR_RULING in preview.text
    # A token issued before the ruling is refused under the execute lock too.
    stale = await _execute(client, agent_id, body, early.json()["preview_token"])
    assert stale.status_code == 409, stale.text
    assert _PRIOR_RULING in stale.text

    review, agent, actions = await _review_state(maker, agent_id)
    assert review.status == "pending" and review.resolution is None
    assert agent.status == AgentStatus.ATH_PENDING_REVIEW
    assert actions == ["reject", "reopen"]
    # The reconsideration still ends in a ruling that stays citable.
    await _resolve(client, agent_id, "reject", "Violation upheld after appeal")
    precedents = await client.get(
        "/api/v1/admin/copy-reviews/precedents",
        params={"q": "Violation upheld", "resolution": "reject"},
        headers=_HEADERS,
    )
    assert precedents.status_code == 200, precedents.text
    assert [item["agent_id"] for item in precedents.json()["items"]] == [str(agent_id)]


async def test_precautionary_rehold_of_a_cleared_artifact_cannot_be_withdrawn(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    """A withdrawal would strip the prior clear's certification under enforce."""
    agent_id, sha256 = await _seed_scored(maker)
    _install(app, maker)
    await _open_manual_hold(client, agent_id, sha256)
    await _resolve(client, agent_id, "clear", "Certified clear of this artifact")
    reopened = await _reopen(client, agent_id, sha256, "Precautionary re-hold")
    body = _preview_body(reopened, sha256)

    preview = await _preview(client, agent_id, body)
    assert preview.status_code == 409, preview.text
    assert _PRIOR_RULING in preview.text
    assert "resolve it with clear or reject" in preview.text

    review, agent, actions = await _review_state(maker, agent_id)
    assert review.status == "pending"
    assert agent.status == AgentStatus.ATH_PENDING_REVIEW
    assert actions == ["clear", "reopen"]
    cleared = await _resolve(client, agent_id, "clear", "Certified clear again")
    assert cleared["review"]["resolution"] == "clear"


async def test_spent_preview_token_cannot_withdraw_a_reopened_hold(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id, sha256 = await _seed_scored(maker)
    _install(app, maker)
    opened = await _open_manual_hold(client, agent_id, sha256)
    body = _preview_body(opened, sha256)
    preview = await _preview(client, agent_id, body)
    assert preview.status_code == 200, preview.text
    token = preview.json()["preview_token"]
    first = await _execute(client, agent_id, body, token)
    assert first.status_code == 200, first.text
    await _reopen(client, agent_id, sha256, "Re-held: new evidence from an operator")

    replay = await _execute(client, agent_id, body, token)
    assert replay.status_code == 409, replay.text
    assert "review lifecycle changed" in replay.text
    review, agent, actions = await _review_state(maker, agent_id)
    assert review.status == "pending"
    assert agent.status == AgentStatus.ATH_PENDING_REVIEW
    assert actions == ["withdraw", "reopen"]

    # A withdrawn-then-reopened hold is still withdrawable with a fresh preview.
    fresh = await _preview(client, agent_id, body)
    assert fresh.status_code == 200, fresh.text
    second = await _execute(client, agent_id, body, fresh.json()["preview_token"])
    assert second.status_code == 200, second.text
    _review, _agent, actions = await _review_state(maker, agent_id)
    assert actions == ["withdraw", "reopen", "withdraw"]


async def test_unused_preview_from_before_a_withdraw_and_reopen_is_refused(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id, sha256 = await _seed_scored(maker)
    _install(app, maker)
    opened = await _open_manual_hold(client, agent_id, sha256)
    body = _preview_body(opened, sha256)
    stale = await _preview(client, agent_id, body)
    assert stale.status_code == 200, stale.text
    other = await _preview(client, agent_id, body)
    landed = await _execute(client, agent_id, body, other.json()["preview_token"])
    assert landed.status_code == 200, landed.text
    await _reopen(client, agent_id, sha256, "Re-held after the first withdrawal")

    refused = await _execute(client, agent_id, body, stale.json()["preview_token"])
    assert refused.status_code == 409, refused.text
    assert "review lifecycle changed" in refused.text
    review, _agent, actions = await _review_state(maker, agent_id)
    assert review.status == "pending"
    assert actions == ["withdraw", "reopen"]


async def test_stale_preview_after_clear_and_reopen_is_refused(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id, sha256 = await _seed_scored(maker)
    _install(app, maker)
    opened = await _open_manual_hold(client, agent_id, sha256)
    body = _preview_body(opened, sha256)
    preview = await _preview(client, agent_id, body)
    assert preview.status_code == 200, preview.text
    await _resolve(client, agent_id, "clear", "Cleared by another operator")
    await _reopen(client, agent_id, sha256, "Reopened by another operator")

    refused = await _execute(client, agent_id, body, preview.json()["preview_token"])
    assert refused.status_code == 409, refused.text
    review, agent, actions = await _review_state(maker, agent_id)
    assert review.status == "pending"
    assert agent.status == AgentStatus.ATH_PENDING_REVIEW
    assert actions == ["clear", "reopen"]


def _token_parts(token: str) -> tuple[int, dict]:
    issued_text, body, _digest = token.split(".", 2)
    return int(issued_text), json.loads(_unb64(body))


def _resigned(token: str, *, issued_at: int | None = None, **changes: object) -> str:
    """Re-sign a real preview token with the real secret after an edit."""
    issued, payload = _token_parts(token)
    return _sign_preview(
        _TOKEN,
        {**payload, **changes},
        issued if issued_at is None else issued_at,
    )


async def _preview_token(client: httpx.AsyncClient, agent_id: UUID, body: dict) -> str:
    preview = await _preview(client, agent_id, body)
    assert preview.status_code == 200, preview.text
    return preview.json()["preview_token"]


async def _assert_still_held(
    maker: async_sessionmaker[AsyncSession], agent_id: UUID
) -> None:
    review, agent, actions = await _review_state(maker, agent_id)
    assert review.status == "pending"
    assert agent.status == AgentStatus.ATH_PENDING_REVIEW
    assert "withdraw" not in actions


async def test_execute_refuses_a_tampered_token(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id, sha256 = await _seed_scored(maker)
    _install(app, maker)
    body = _preview_body(await _open_manual_hold(client, agent_id, sha256), sha256)
    token = await _preview_token(client, agent_id, body)
    issued, signed_body, digest = token.split(".", 2)
    _issued, payload = _token_parts(token)
    # Claim the withdrawal restores ``live`` while keeping the old signature.
    assert payload["restored_status"] == AgentStatus.SCORED
    forged_body = _b64(
        json.dumps(
            {**payload, "restored_status": AgentStatus.LIVE.value},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )
    assert forged_body != signed_body
    for forged in (
        f"{issued}.{forged_body}.{digest}",
        f"{issued}.{signed_body}.{'0' * len(digest)}",
    ):
        refused = await _execute(client, agent_id, body, forged)
        assert refused.status_code == 409, refused.text
        assert "signature mismatch" in refused.text
    garbage = await _execute(client, agent_id, body, "x" * 32)
    assert garbage.status_code == 422, garbage.text
    await _assert_still_held(maker, agent_id)


async def test_execute_refuses_a_token_issued_to_another_operator(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id, sha256 = await _seed_scored(maker)
    _install(app, maker)
    body = _preview_body(await _open_manual_hold(client, agent_id, sha256), sha256)
    token = await _preview_token(client, agent_id, body)
    refused = await client.post(
        f"/api/v1/admin/copy-reviews/{agent_id}/withdraw",
        json={**body, "preview_token": token, "confirmation": WITHDRAW_CONFIRMATION},
        headers={**_HEADERS, "X-Admin-Actor": "someone-else"},
    )
    assert refused.status_code == 409, refused.text
    assert "issued to another operator" in refused.text
    await _assert_still_held(maker, agent_id)


async def test_execute_refuses_an_expired_token(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id, sha256 = await _seed_scored(maker)
    _install(app, maker)
    body = _preview_body(await _open_manual_hold(client, agent_id, sha256), sha256)
    token = await _preview_token(client, agent_id, body)
    issued, _payload = _token_parts(token)
    expired = _resigned(
        token,
        issued_at=issued - int(BATCH_PREVIEW_TTL.total_seconds()) - 60,
    )
    refused = await _execute(client, agent_id, body, expired)
    assert refused.status_code == 409, refused.text
    assert "preview expired" in refused.text
    await _assert_still_held(maker, agent_id)


async def test_execute_refuses_a_rulings_kind_token(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id, sha256 = await _seed_scored(maker)
    _install(app, maker)
    body = _preview_body(await _open_manual_hold(client, agent_id, sha256), sha256)
    token = await _preview_token(client, agent_id, body)
    refused = await _execute(
        client, agent_id, body, _resigned(token, kind="ath_rulings_batch")
    )
    assert refused.status_code == 422, refused.text
    assert "unsupported preview token" in refused.text
    await _assert_still_held(maker, agent_id)


async def test_execute_refuses_a_confirmation_mismatch(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id, sha256 = await _seed_scored(maker)
    _install(app, maker)
    body = _preview_body(await _open_manual_hold(client, agent_id, sha256), sha256)
    token = await _preview_token(client, agent_id, body)
    refused = await client.post(
        f"/api/v1/admin/copy-reviews/{agent_id}/withdraw",
        json={**body, "preview_token": token, "confirmation": "withdraw ath hold"},
        headers=_HEADERS,
    )
    assert refused.status_code == 422, refused.text
    assert WITHDRAW_CONFIRMATION in refused.text
    await _assert_still_held(maker, agent_id)


async def test_execute_refuses_a_reason_that_differs_from_the_preview(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id, sha256 = await _seed_scored(maker)
    _install(app, maker)
    body = _preview_body(await _open_manual_hold(client, agent_id, sha256), sha256)
    token = await _preview_token(client, agent_id, body)
    refused = await _execute(
        client, agent_id, {**body, "reason": "A different public reason"}, token
    )
    assert refused.status_code == 409, refused.text
    assert "preview token does not match this request" in refused.text
    await _assert_still_held(maker, agent_id)


async def test_execute_refuses_a_token_for_another_agent(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id, sha256 = await _seed_scored(maker)
    other_id, other_sha = await _seed_scored(maker, hotkey="5Other")
    _install(app, maker)
    body = _preview_body(await _open_manual_hold(client, agent_id, sha256), sha256)
    other_body = _preview_body(
        await _open_manual_hold(client, other_id, other_sha), other_sha
    )
    token = await _preview_token(client, agent_id, body)
    for request_body in (body, other_body):
        refused = await _execute(client, other_id, request_body, token)
        assert refused.status_code == 409, refused.text
        assert "preview token does not match this request" in refused.text
    await _assert_still_held(maker, agent_id)
    await _assert_still_held(maker, other_id)


async def test_withdrawal_restores_a_live_agent_to_live(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id, sha256 = await _seed_scored(maker, status=AgentStatus.LIVE)
    _install(app, maker)
    body = _preview_body(await _open_manual_hold(client, agent_id, sha256), sha256)
    preview = await _preview(client, agent_id, body)
    assert preview.status_code == 200, preview.text
    assert preview.json()["restored_status"] == AgentStatus.LIVE
    executed = await _execute(client, agent_id, body, preview.json()["preview_token"])
    assert executed.status_code == 200, executed.text
    assert executed.json()["agent_status"] == AgentStatus.LIVE
    _review, agent, _actions = await _review_state(maker, agent_id)
    assert agent.status == AgentStatus.LIVE


async def test_reopened_withdrawal_restores_the_status_the_reopen_recorded(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    """The newest reopen, not the original hold, says what the agent was."""
    agent_id, sha256 = await _seed_scored(maker)
    _install(app, maker)
    opened = await _open_manual_hold(client, agent_id, sha256)
    body = _preview_body(opened, sha256)
    first = await _execute(
        client, agent_id, body, await _preview_token(client, agent_id, body)
    )
    assert first.status_code == 200, first.text
    assert first.json()["agent_status"] == AgentStatus.SCORED
    # Promoted between the withdrawal and the next precautionary hold.
    async with maker() as session, session.begin():
        agent = await session.get(Agent, agent_id)
        assert agent is not None
        agent.status = AgentStatus.LIVE
    await _reopen(client, agent_id, sha256, "Re-held while live")
    preview = await _preview(client, agent_id, body)
    assert preview.status_code == 200, preview.text
    assert preview.json()["restored_status"] == AgentStatus.LIVE
    second = await _execute(client, agent_id, body, preview.json()["preview_token"])
    assert second.status_code == 200, second.text
    assert second.json()["agent_status"] == AgentStatus.LIVE
    async with maker() as session:
        review = await session.scalar(
            select(AthReview).where(AthReview.agent_id == agent_id)
        )
        assert review is not None
        assert review.original_evidence["previous_status"] == AgentStatus.SCORED
        withdrawals = list(
            await session.scalars(
                select(AthReviewAction)
                .where(
                    AthReviewAction.review_id == review.review_id,
                    AthReviewAction.action == "withdraw",
                )
                .order_by(AthReviewAction.created_at)
            )
        )
    assert [row.evidence["previous_status"] for row in withdrawals] == [
        AgentStatus.SCORED,
        AgentStatus.LIVE,
    ]


async def test_off_mode_emission_reason_never_claims_a_terminal_review(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id, sha256 = await _seed_scored(maker)
    _install(app, maker)
    body = _preview_body(await _open_manual_hold(client, agent_id, sha256), sha256)
    preview = await _preview(client, agent_id, body)
    assert preview.status_code == 200, preview.text
    assert preview.json()["emission_gate"] == "off"
    reason = preview.json()["emission_reason"]
    assert "Review is terminal" not in reason
    assert "Reward eligibility is off" in reason
    executed = await _execute(client, agent_id, body, preview.json()["preview_token"])
    assert executed.status_code == 200, executed.text
    assert executed.json()["emission_reason"] == reason


async def _add_score(maker: async_sessionmaker[AsyncSession], agent_id: UUID) -> None:
    """A late validator report; the score endpoint accepts one during a hold."""
    async with maker() as session, session.begin():
        session.add(
            Score(
                agent_id=agent_id,
                validator_hotkey="validator-late",
                run_id=f"late-{agent_id.hex[:8]}",
                signature=None,
                seed=7,
                bench_version=MIN_SCOREABLE_BENCH_VERSION,
                composite=0.97,
                tool_mean=0.97,
                memory_mean=0.90,
                median_ms=100,
                n=114,
                details={},
                generated_at=_T0 + timedelta(hours=1),
            )
        )


async def _audit(client: httpx.AsyncClient, agent_id: UUID) -> dict:
    audit = await client.get(
        f"/api/v1/admin/copy-reviews/{agent_id}/audit", headers=_HEADERS
    )
    assert audit.status_code == 200, audit.text
    return audit.json()


def _audit_guards(audit: dict, reason: str = _REASON) -> dict:
    """Exactly what Backroom sends: the audit's current guards, not the held ones."""
    return {
        "review_id": audit["review"]["review_id"],
        "expected_sha256": audit["current_artifact_sha256"],
        "expected_score_count": audit["current_score_count"],
        "expected_agent_status": audit["agent_status"],
        "reason": reason,
    }


async def test_audit_reports_the_current_guards_after_a_score_arrives(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id, sha256 = await _seed_scored(maker)
    _install(app, maker)
    await _open_manual_hold(client, agent_id, sha256)
    await _add_score(maker, agent_id)

    audit = await _audit(client, agent_id)
    assert audit["held_score_count"] == 3
    assert audit["current_score_count"] == 4
    assert audit["held_artifact_sha256"] == audit["current_artifact_sha256"] == sha256
    assert audit["withdrawable"] is True
    assert audit["withdrawal_refusal"] is None

    held = {**_audit_guards(audit), "expected_score_count": audit["held_score_count"]}
    stale = await _preview(client, agent_id, held)
    assert stale.status_code == 409
    assert "score count changed" in stale.text
    body = _audit_guards(audit)
    preview = await _preview(client, agent_id, body)
    assert preview.status_code == 200, preview.text
    executed = await _execute(client, agent_id, body, preview.json()["preview_token"])
    assert executed.status_code == 200, executed.text


async def test_audit_guards_follow_a_reopen_of_a_withdrawn_hold(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id, sha256 = await _seed_scored(maker)
    _install(app, maker)
    opened = await _open_manual_hold(client, agent_id, sha256)
    body = _preview_body(opened, sha256)
    first = await _execute(
        client, agent_id, body, await _preview_token(client, agent_id, body)
    )
    assert first.status_code == 200, first.text
    await _add_score(maker, agent_id)
    await _reopen(client, agent_id, sha256, "Re-held after a retest", score_count=4)

    audit = await _audit(client, agent_id)
    assert audit["held_score_count"] == 3
    assert audit["current_score_count"] == 4
    assert audit["withdrawable"] is True
    preview = await _preview(client, agent_id, _audit_guards(audit))
    assert preview.status_code == 200, preview.text


async def test_audit_says_why_a_hold_cannot_be_withdrawn(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    ruled_id, ruled_sha = await _seed_scored(maker)
    automated_id, automated_sha = await _seed_scored(
        maker, status=AgentStatus.ATH_PENDING_REVIEW, hotkey="5Automated"
    )
    async with maker() as session, session.begin():
        automated = await session.get(Agent, automated_id)
        assert automated is not None
        automated.review_reason = "Score-finalization copy hold"
        session.add(
            AthReview(
                review_id=uuid4(),
                agent_id=automated_id,
                status="pending",
                opened_at=_T0,
                original_reason=automated.review_reason,
                original_policy_version=8,
                original_evidence={"sha256": automated_sha, "score_count": 3},
                algorithm_provenance={"snapshot": "score-finalization"},
            )
        )
    _install(app, maker)
    await _open_manual_hold(client, ruled_id, ruled_sha)
    await _resolve(client, ruled_id, "clear", "Certified clear of this artifact")
    await _reopen(client, ruled_id, ruled_sha, "Precautionary re-hold")

    for agent_id, refusal in (
        (ruled_id, _PRIOR_RULING),
        (automated_id, "only a manual precautionary hold can be withdrawn"),
    ):
        audit = await _audit(client, agent_id)
        assert audit["withdrawable"] is False
        assert refusal in audit["withdrawal_refusal"]
        preview = await _preview(client, agent_id, _audit_guards(audit))
        assert preview.status_code == 409
        assert refusal in preview.text


async def test_withdrawn_holds_are_listed_by_resolution(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    """A withdrawn hold leaves the pending queue but stays findable."""
    withdrawn_id, withdrawn_sha = await _seed_scored(maker)
    cleared_id, cleared_sha = await _seed_scored(maker, hotkey="5Cleared")
    pending_id, pending_sha = await _seed_scored(maker, hotkey="5Pending")
    _install(app, maker)
    body = _preview_body(
        await _open_manual_hold(client, withdrawn_id, withdrawn_sha), withdrawn_sha
    )
    executed = await _execute(
        client, withdrawn_id, body, await _preview_token(client, withdrawn_id, body)
    )
    assert executed.status_code == 200, executed.text
    await _open_manual_hold(client, cleared_id, cleared_sha)
    await _resolve(client, cleared_id, "clear", "Certified clear of this artifact")
    await _open_manual_hold(client, pending_id, pending_sha)

    async def _listed(**params: str) -> dict:
        listing = await client.get(
            "/api/v1/admin/copy-reviews",
            params={"generation": "all", **params},
            headers=_HEADERS,
        )
        assert listing.status_code == 200, listing.text
        return listing.json()

    queue = await _listed(status="pending")
    assert [item["agent_id"] for item in queue["items"]] == [str(pending_id)]
    withdrawn = await _listed(status="resolved", resolution="withdraw")
    assert withdrawn["resolution"] == "withdraw"
    assert withdrawn["count"] == 1
    [row] = withdrawn["items"]
    assert row["agent_id"] == str(withdrawn_id)
    assert row["resolution"] == "withdraw"
    assert row["resolution_reason"] == _REASON
    cleared = await _listed(status="resolved", resolution="clear")
    assert [item["agent_id"] for item in cleared["items"]] == [str(cleared_id)]
    everything = await _listed(status="all")
    assert everything["resolution"] is None
    assert everything["count"] == 3
    invalid = await client.get(
        "/api/v1/admin/copy-reviews",
        params={"resolution": "withdrawn"},
        headers=_HEADERS,
    )
    assert invalid.status_code == 422


async def test_withdraw_audit_projects_the_recorded_reward_posture(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id, sha256 = await _seed_scored(maker)
    _install(app, maker)
    body = _preview_body(await _open_manual_hold(client, agent_id, sha256), sha256)
    preview = await _preview(client, agent_id, body)
    assert preview.status_code == 200, preview.text
    executed = await _execute(client, agent_id, body, preview.json()["preview_token"])
    assert executed.status_code == 200, executed.text

    audit = await _audit(client, agent_id)
    assert audit["withdrawable"] is False
    assert audit["withdrawal_refusal"] == "review already withdrawn"
    [withdrawal] = audit["action_history"]
    assert withdrawal["action"] == "withdraw"
    assert withdrawal["previous_status"] == AgentStatus.SCORED
    assert withdrawal["artifact_sha256"] == sha256
    assert withdrawal["score_count"] == 3
    assert withdrawal["emission_gate"] == preview.json()["emission_gate"] == "off"
    assert withdrawal["emission_reward_eligible"] is True
    assert withdrawal["eligibility_state"] == "eligible"
    assert isinstance(withdrawal["eligibility_revision"], int)
    assert isinstance(withdrawal["eligibility_checksum"], str)
    assert withdrawal["eligibility_checksum"]


async def test_reopen_after_a_withdrawal_reports_the_superseded_withdrawal(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id, sha256 = await _seed_scored(maker)
    _install(app, maker)
    body = _preview_body(await _open_manual_hold(client, agent_id, sha256), sha256)
    executed = await _execute(
        client, agent_id, body, await _preview_token(client, agent_id, body)
    )
    assert executed.status_code == 200, executed.text
    await _reopen(client, agent_id, sha256, "Re-held on new evidence")

    item = await client.get(f"/api/v1/admin/copy-reviews/{agent_id}", headers=_HEADERS)
    assert item.status_code == 200, item.text
    original = item.json()["original"]
    assert original["reason_source"] == "reconsideration"
    assert original["reason"] == "Re-held on new evidence"
    assert original["superseded_resolution"] == "withdraw"
    assert original["superseded_resolution_reason"] == _REASON
    audit = await _audit(client, agent_id)
    assert audit["review"]["original"]["superseded_resolution"] == "withdraw"
