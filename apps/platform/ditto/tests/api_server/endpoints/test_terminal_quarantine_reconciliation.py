"""Terminal ATH rulings never leave an active screening quarantine behind.

Regression coverage for ditto-subnet#2038: a scored policy rescreen can hold an
active quarantine while the agent keeps its board position, and a terminal ATH
reject of that exact agent used to leave the row active with no guarded way to
close it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_server.dependencies import get_chain_client, get_session
from ditto.db.models import (
    Agent,
    AgentStatus,
    AthReview,
    AthReviewAction,
    Score,
    ScreeningAttempt,
    ScreeningQuarantine,
    ScreeningQuarantineResolution,
    ScreeningReviewEvent,
)
from ditto.db.queries import screening_review_events
from ditto.db.queries.agents import resolve_review
from ditto.db.queries.benchmark_rollout import MIN_SCOREABLE_BENCH_VERSION
from ditto.db.queries.source_review_queue_slo import (
    load_source_review_queue_slo_snapshot,
)

_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_TOKEN}", "X-Admin-Actor": "operator"}
_T0 = datetime(2026, 9, 20, 12, tzinfo=UTC)
_HOTKEY = "5" + "T" * 47
_ATH_REASON = "Family compiler on served /run (I5); precedent family-compiler"
_MINER_REASON = "Rejected by ATH review"


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_TOKEN)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    # The batch resolver resolves a chain client even though a reject never
    # prepares a release dataset.
    app.dependency_overrides[get_chain_client] = lambda: MagicMock()


async def _seed(
    maker: async_sessionmaker[AsyncSession],
    *,
    status: AgentStatus,
    ath_resolution: str | None,
) -> tuple[UUID, str, UUID]:
    """One scored agent carrying an active quarantine from a retained rescreen.

    ``ath_resolution=None`` holds the agent in a pending ATH review;
    ``"reject"`` reproduces the pre-fix ghost: a resolved terminal ruling with
    the quarantine still active.
    """
    agent_id, attempt_id, quarantine_id = uuid4(), uuid4(), uuid4()
    review_id = uuid4()
    sha256 = agent_id.hex * 2
    async with maker() as session, session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=_HOTKEY,
                name="teacup",
                sha256=sha256,
                status=status,
                review_reason=_ATH_REASON,
                screening_reason=_MINER_REASON,
                screening_reason_code="source-review-high-risk",
                screening_policy_version=13,
                created_at=_T0,
            )
        )
        for index in range(3):
            session.add(
                Score(
                    agent_id=agent_id,
                    validator_hotkey=f"validator-{index}",
                    run_id=f"run-{index}",
                    signature=None,
                    seed=7,
                    composite=0.91,
                    tool_mean=0.91,
                    memory_mean=0.9,
                    median_ms=100,
                    n=114,
                    details={"bench_version": MIN_SCOREABLE_BENCH_VERSION},
                    generated_at=_T0 + timedelta(minutes=index),
                )
            )
        await session.flush()
        session.add(
            ScreeningAttempt(
                attempt_id=attempt_id,
                agent_id=agent_id,
                screener_hotkey=_HOTKEY,
                policy_version=13,
                status="quarantined",
                started_at=_T0 + timedelta(hours=1),
                deadline=_T0 + timedelta(hours=2),
                finished_at=_T0 + timedelta(hours=1, minutes=5),
                artifact_sha256=sha256,
            )
        )
        await session.flush()
        session.add(
            ScreeningQuarantine(
                quarantine_id=quarantine_id,
                agent_id=agent_id,
                attempt_id=attempt_id,
                screener_hotkey=_HOTKEY,
                policy_version=13,
                manifest_digest="b" * 64,
                reason_code="source-review-high-risk",
                status="active",
                created_at=_T0 + timedelta(hours=1, minutes=5),
            )
        )
        resolved = ath_resolution is not None
        session.add(
            AthReview(
                review_id=review_id,
                agent_id=agent_id,
                status="resolved" if resolved else "pending",
                opened_at=_T0 + timedelta(hours=2),
                resolved_at=_T0 + timedelta(hours=3) if resolved else None,
                resolved_by="operator" if resolved else None,
                resolution=ath_resolution,
                resolution_reason=_ATH_REASON if resolved else None,
                original_duplicate_of=None,
                original_reason=_ATH_REASON,
                original_policy_version=13,
                original_evidence={
                    "sha256": sha256,
                    "score_count": 3,
                    "previous_status": AgentStatus.SCORED.value,
                },
                algorithm_provenance={
                    "snapshot": "manual-admin-hold",
                    "review_kind": "benchmark_overfit",
                    "algorithm_version": "manual-ath-review-v1",
                    "opened_by": "operator",
                    "backfilled": False,
                    "opened_at_source": "admin-request",
                },
            )
        )
        if ath_resolution == "reject":
            # A resolved review without its specific reject ledger action is
            # not a proven current ruling. Model the real writer's atomic pair.
            await session.flush()
            session.add(
                AthReviewAction(
                    action_id=uuid4(),
                    review_id=review_id,
                    action="reject",
                    reason=_ATH_REASON,
                    actor="operator",
                    evidence={"sha256": sha256},
                    created_at=_T0 + timedelta(hours=3),
                )
            )
    return agent_id, sha256, quarantine_id


async def _active_quarantine_count(
    maker: async_sessionmaker[AsyncSession], agent_id: UUID
) -> int:
    async with maker() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(ScreeningQuarantine)
                .where(
                    ScreeningQuarantine.agent_id == agent_id,
                    ScreeningQuarantine.status == "active",
                )
            )
            or 0
        )


async def test_terminal_ath_reject_closes_matching_active_quarantine(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_id, sha256, quarantine_id = await _seed(
        session_maker, status=AgentStatus.ATH_PENDING_REVIEW, ath_resolution=None
    )
    moderation = AsyncMock()
    monkeypatch.setattr(
        screening_review_events, "record_moderation_audit_if_enabled", moderation
    )
    _install(app, session_maker)

    response = await client.post(
        f"/api/v1/admin/copy-reviews/{agent_id}/resolve",
        json={"resolution": "reject", "reason": _ATH_REASON},
        headers=_HEADERS,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["agent_status"] == AgentStatus.BANNED
    assert body["reconciled_quarantine_ids"] == [str(quarantine_id)]
    assert await _active_quarantine_count(session_maker, agent_id) == 0
    async with session_maker() as session:
        quarantine = await session.get(ScreeningQuarantine, quarantine_id)
        review = await session.scalar(
            select(AthReview).where(AthReview.agent_id == agent_id)
        )
        agent = await session.get(Agent, agent_id)
        resolutions = (
            await session.scalars(
                select(ScreeningQuarantineResolution).where(
                    ScreeningQuarantineResolution.quarantine_id == quarantine_id
                )
            )
        ).all()
        event = await session.scalar(
            select(ScreeningReviewEvent).where(
                ScreeningReviewEvent.quarantine_id == quarantine_id
            )
        )
        assert review is not None
        action = await session.scalar(
            select(AthReviewAction).where(
                AthReviewAction.review_id == review.review_id,
                AthReviewAction.action == "reject",
            )
        )
    assert agent is not None and quarantine is not None
    assert agent.status == AgentStatus.BANNED
    assert agent.screening_reason == _MINER_REASON
    assert (quarantine.status, quarantine.resolution, quarantine.resolved_by) == (
        "resolved",
        "reject",
        "operator",
    )
    assert (
        quarantine.resolution_reason == f"Closed by terminal ATH ruling: {_ATH_REASON}"
    )
    assert [(row.resolution, row.actor) for row in resolutions] == [
        ("reject", "operator")
    ]
    assert event is not None
    assert (event.prior_agent_status, event.next_agent_status) == ("banned", "banned")
    assert action is not None
    # The evidence names this exact ruling: the review and its reject action.
    assert event.evidence["terminal_reconciliation"] == {
        "source": "ath_ruling",
        "agent_status": "banned",
        "artifact_sha256": sha256,
        "ath_review_id": str(review.review_id),
        "ath_action_id": str(action.action_id),
        "ath_resolved_at": review.resolved_at.isoformat()
        if review.resolved_at
        else None,
    }
    assert action.evidence["reconciled_quarantine_ids"] == [str(quarantine_id)]
    # The ATH ruling is the authoritative public outcome; closing the orphan
    # publishes no second moderation record.
    moderation.assert_not_awaited()

    audit = await client.get(
        f"/api/v1/admin/copy-reviews/{agent_id}/audit", headers=_HEADERS
    )
    assert audit.status_code == 200
    assert audit.json()["action_history"][-1]["reconciled_quarantine_ids"] == [
        str(quarantine_id)
    ]
    listing = await client.get("/api/v1/admin/screening-quarantines", headers=_HEADERS)
    assert listing.json()["count"] == 0


async def test_clear_leaves_a_non_terminal_quarantine_untouched(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id, _sha256, _quarantine_id = await _seed(
        session_maker, status=AgentStatus.ATH_PENDING_REVIEW, ath_resolution=None
    )
    _install(app, session_maker)

    response = await client.post(
        f"/api/v1/admin/copy-reviews/{agent_id}/resolve",
        json={"resolution": "clear", "reason": "Served path uses the real model"},
        headers=_HEADERS,
    )

    assert response.status_code == 200, response.text
    assert response.json()["reconciled_quarantine_ids"] == []
    assert await _active_quarantine_count(session_maker, agent_id) == 1


async def test_operator_reconciles_preexisting_terminal_ghost(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id, sha256, quarantine_id = await _seed(
        session_maker, status=AgentStatus.BANNED, ath_resolution="reject"
    )
    _install(app, session_maker)

    listing = await client.get("/api/v1/admin/screening-quarantines", headers=_HEADERS)
    assert listing.status_code == 200
    body = listing.json()
    assert (body["count"], body["terminal_ghost_count"], body["actionable_count"]) == (
        1,
        1,
        0,
    )
    assert body["oldest_actionable_created_at"] is None
    assert body["items"][0]["terminal_ghost"] is True
    assert body["items"][0]["agent_status"] == AgentStatus.BANNED
    async with session_maker() as session:
        snapshot = await load_source_review_queue_slo_snapshot(session)
    assert snapshot.terminal_quarantine_ghost_count == 1
    assert (snapshot.backlog_count, snapshot.oldest_age_seconds) == (0, None)

    # The unfenced single resolver still refuses, and names the fenced path.
    single = await client.post(
        f"/api/v1/admin/screening-quarantines/{quarantine_id}/resolve",
        json={"resolution": "reject", "reason": "Close the orphan"},
        headers=_HEADERS,
    )
    assert single.status_code == 409
    assert "fenced batch reject" in single.json()["message"]

    decision = {
        "quarantine_id": str(quarantine_id),
        "expected_agent_id": str(agent_id),
        "expected_artifact_sha256": sha256,
        "resolution": "reject",
        "reason": "Agent is banned under its ATH ruling; closing the orphaned hold",
    }
    stale = await client.post(
        "/api/v1/admin/screening-quarantines/batch-preview",
        json={"decisions": [{**decision, "expected_artifact_sha256": "0" * 64}]},
        headers=_HEADERS,
    )
    assert stale.json()["items"][0]["disposition"] == "conflict"
    assert stale.json()["items"][0]["message"] == "submission identity changed"
    release = await client.post(
        "/api/v1/admin/screening-quarantines/batch-preview",
        json={"decisions": [{**decision, "resolution": "release"}]},
        headers=_HEADERS,
    )
    released = release.json()["items"][0]
    assert (released["disposition"], released["terminal_reconciliation"]) == (
        "conflict",
        True,
    )

    preview = await client.post(
        "/api/v1/admin/screening-quarantines/batch-preview",
        json={"decisions": [decision]},
        headers=_HEADERS,
    )
    assert preview.status_code == 200
    item = preview.json()["items"][0]
    assert item["disposition"] == "ready"
    assert item["resulting_agent_status"] == AgentStatus.BANNED
    assert item["terminal_reconciliation"] is True
    assert item["public_record_hash"] is None
    request = {
        "decisions": [decision],
        "preview_token": preview.json()["preview_token"],
        "confirmed": True,
    }
    executed = await client.post(
        "/api/v1/admin/screening-quarantines/batch-resolve",
        json=request,
        headers=_HEADERS,
    )
    assert executed.status_code == 200
    applied = executed.json()["items"][0]
    assert (applied["status"], applied["agent_status"]) == ("applied", "banned")
    assert applied["terminal_reconciliation"] is True

    replay = await client.post(
        "/api/v1/admin/screening-quarantines/batch-resolve",
        json=request,
        headers=_HEADERS,
    )
    assert replay.json()["already_applied_count"] == 1

    async with session_maker() as session:
        agent = await session.get(Agent, agent_id)
        review = await session.scalar(
            select(AthReview).where(AthReview.agent_id == agent_id)
        )
        event = await session.scalar(
            select(ScreeningReviewEvent).where(
                ScreeningReviewEvent.quarantine_id == quarantine_id
            )
        )
        resolutions = int(
            await session.scalar(
                select(func.count())
                .select_from(ScreeningQuarantineResolution)
                .where(ScreeningQuarantineResolution.quarantine_id == quarantine_id)
            )
            or 0
        )
        snapshot = await load_source_review_queue_slo_snapshot(session)
    # The terminal ruling is untouched: same status, same miner-visible reason.
    assert agent is not None and review is not None and event is not None
    assert agent.status == AgentStatus.BANNED
    assert agent.screening_reason == _MINER_REASON
    assert agent.screening_reason_code == "source-review-high-risk"
    assert (review.status, review.resolution) == ("resolved", "reject")
    assert resolutions == 1
    assert event.actor == "operator"
    assert event.evidence["terminal_reconciliation"]["source"] == (
        "operator_reconciliation"
    )
    assert event.evidence["terminal_reconciliation"]["ath_review_id"] == str(
        review.review_id
    )
    assert snapshot.terminal_quarantine_ghost_count == 0
    assert await _active_quarantine_count(session_maker, agent_id) == 0


async def test_concurrent_ath_reject_and_screening_resolution_serialize(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The ATH ruling takes the screening resolvers' lock order.

    A screening resolution locks the quarantine and then the agent. While it
    holds the quarantine, a terminal ATH reject must wait for it instead of
    taking the agent lock first (a lock-order inversion Postgres would break
    with a deadlock), then close the quarantine once the resolution finds the
    agent is not quarantined and backs off.
    """
    agent_id, _sha256, quarantine_id = await _seed(
        session_maker, status=AgentStatus.ATH_PENDING_REVIEW, ath_resolution=None
    )
    _install(app, session_maker)

    async with session_maker() as screening, session_maker() as observer:
        await screening.begin()
        await screening.execute(
            select(ScreeningQuarantine)
            .where(ScreeningQuarantine.quarantine_id == quarantine_id)
            .with_for_update()
        )
        ruling = asyncio.create_task(
            client.post(
                f"/api/v1/admin/copy-reviews/{agent_id}/resolve",
                json={"resolution": "reject", "reason": _ATH_REASON},
                headers=_HEADERS,
            )
        )
        waiting = 0
        for _ in range(100):
            waiting = int(
                await observer.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = current_database() "
                        "AND wait_event_type = 'Lock'"
                    )
                )
                or 0
            )
            await observer.rollback()
            if waiting or ruling.done():
                break
            await asyncio.sleep(0.05)
        assert waiting == 1, "the ATH ruling must queue behind the quarantine lock"
        assert not ruling.done()

        agent = await asyncio.wait_for(
            screening.scalar(
                select(Agent).where(Agent.agent_id == agent_id).with_for_update()
            ),
            timeout=5,
        )
        assert agent is not None
        assert agent.status == AgentStatus.ATH_PENDING_REVIEW
        await screening.rollback()

    response = await asyncio.wait_for(ruling, timeout=10)
    assert response.status_code == 200, response.text
    assert response.json()["reconciled_quarantine_ids"] == [str(quarantine_id)]
    assert await _active_quarantine_count(session_maker, agent_id) == 0


async def test_cli_ban_closes_matching_active_quarantine(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The owner-only ``scripts/resolve_review.py`` ban obeys the same rule."""
    agent_id, _sha256, quarantine_id = await _seed(
        session_maker, status=AgentStatus.ATH_PENDING_REVIEW, ath_resolution=None
    )

    async with session_maker() as session, session.begin():
        agent = await resolve_review(
            session, agent_id=agent_id, decision=AgentStatus.BANNED
        )
        assert agent is not None and agent.status == AgentStatus.BANNED

    assert await _active_quarantine_count(session_maker, agent_id) == 0
    async with session_maker() as session:
        event = await session.scalar(
            select(ScreeningReviewEvent).where(
                ScreeningReviewEvent.quarantine_id == quarantine_id
            )
        )
    assert event is not None
    assert event.actor == "cli:scripts/resolve_review.py"
    assert event.evidence["terminal_reconciliation"]["source"] == "cli_ban"
    assert event.evidence["terminal_reconciliation"]["ath_review_id"] is None


# --- Fenced operator reconciliation (review of #2554) -----------------------

_RECONCILE_REASON = "Agent is banned under its ATH ruling; closing the orphaned hold"


def _decision(
    agent_id: UUID,
    sha256: str,
    quarantine_id: UUID,
    *,
    resolution: str = "reject",
    reason: str = _RECONCILE_REASON,
) -> dict[str, str]:
    return {
        "quarantine_id": str(quarantine_id),
        "expected_agent_id": str(agent_id),
        "expected_artifact_sha256": sha256,
        "resolution": resolution,
        "reason": reason,
    }


async def _preview(
    client: httpx.AsyncClient,
    decisions: list[dict[str, str]],
    *,
    actor: str = "operator",
) -> httpx.Response:
    return await client.post(
        "/api/v1/admin/screening-quarantines/batch-preview",
        json={"decisions": decisions},
        headers={**_HEADERS, "X-Admin-Actor": actor},
    )


async def _execute(
    client: httpx.AsyncClient,
    decisions: list[dict[str, str]],
    token: str,
    *,
    actor: str = "operator",
) -> httpx.Response:
    return await client.post(
        "/api/v1/admin/screening-quarantines/batch-resolve",
        json={"decisions": decisions, "preview_token": token, "confirmed": True},
        headers={**_HEADERS, "X-Admin-Actor": actor},
    )


async def _review_id(maker: async_sessionmaker[AsyncSession], agent_id: UUID) -> UUID:
    async with maker() as session:
        review_id = await session.scalar(
            select(AthReview.review_id).where(AthReview.agent_id == agent_id)
        )
    assert review_id is not None
    return review_id


async def _record_action(
    maker: async_sessionmaker[AsyncSession],
    *,
    review_id: UUID,
    action: str,
    at: datetime,
    evidence: dict[str, object] | None = None,
) -> UUID:
    """Append one ledger action; a reject also becomes the review's ruling."""
    action_id = uuid4()
    async with maker() as session, session.begin():
        session.add(
            AthReviewAction(
                action_id=action_id,
                review_id=review_id,
                action=action,
                reason=f"{action} recorded by the test ledger",
                actor="operator",
                evidence=evidence or {"previous_status": "scored"},
                created_at=at,
            )
        )
        if action == "reject":
            review = await session.get(AthReview, review_id)
            assert review is not None
            review.resolved_at = at
    return action_id


async def _resolution_count(
    maker: async_sessionmaker[AsyncSession], quarantine_id: UUID
) -> int:
    async with maker() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(ScreeningQuarantineResolution)
                .where(ScreeningQuarantineResolution.quarantine_id == quarantine_id)
            )
            or 0
        )


async def _lock_waiters(observer: AsyncSession) -> int:
    waiting = int(
        await observer.scalar(
            text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            )
        )
        or 0
    )
    await observer.rollback()
    return waiting


async def test_replay_after_ath_ruling_closed_the_quarantine_is_already_applied(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """An operator reject of a quarantine the ATH ruling already closed replays."""
    agent_id, sha256, quarantine_id = await _seed(
        session_maker, status=AgentStatus.ATH_PENDING_REVIEW, ath_resolution=None
    )
    _install(app, session_maker)
    ruled = await client.post(
        f"/api/v1/admin/copy-reviews/{agent_id}/resolve",
        json={"resolution": "reject", "reason": _ATH_REASON},
        headers=_HEADERS,
    )
    assert ruled.status_code == 200
    review_id = await _review_id(session_maker, agent_id)

    decision = _decision(agent_id, sha256, quarantine_id)
    preview = await _preview(client, [decision], actor="another-operator")
    item = preview.json()["items"][0]
    assert item["disposition"] == "already_applied", item
    assert item["terminal_reconciliation"] is True
    assert item["terminal_ruling"]["ath_review_id"] == str(review_id)
    assert item["terminal_ruling"]["ath_action_id"] is not None
    # A different screening decision on the closed row is still refused.
    release = await _preview(
        client, [_decision(agent_id, sha256, quarantine_id, resolution="release")]
    )
    assert release.json()["items"][0]["disposition"] == "conflict"

    executed = await _execute(
        client,
        [decision],
        preview.json()["preview_token"],
        actor="another-operator",
    )
    assert executed.status_code == 200, executed.text
    assert executed.json()["items"][0]["status"] == "already_applied"
    assert await _resolution_count(session_maker, quarantine_id) == 1


async def test_replay_by_another_operator_or_reason_is_already_applied(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id, sha256, quarantine_id = await _seed(
        session_maker, status=AgentStatus.BANNED, ath_resolution="reject"
    )
    _install(app, session_maker)
    decision = _decision(agent_id, sha256, quarantine_id)
    first = await _preview(client, [decision])
    applied = await _execute(client, [decision], first.json()["preview_token"])
    assert applied.json()["items"][0]["status"] == "applied"

    replay = _decision(
        agent_id, sha256, quarantine_id, reason="Second look: already closed"
    )
    second = await _preview(client, [replay], actor="backroom:other-user")
    assert second.json()["items"][0]["disposition"] == "already_applied"
    executed = await _execute(
        client,
        [replay],
        second.json()["preview_token"],
        actor="backroom:other-user",
    )
    assert executed.json()["already_applied_count"] == 1
    assert await _resolution_count(session_maker, quarantine_id) == 1


async def test_evidence_binds_the_current_reject_not_a_historical_one(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id, sha256, quarantine_id = await _seed(
        session_maker, status=AgentStatus.BANNED, ath_resolution="reject"
    )
    review_id = await _review_id(session_maker, agent_id)
    # reject -> reopen (reconsideration) -> reject again: only the last one
    # is the ruling that holds the agent banned today.
    historical = await _record_action(
        session_maker, review_id=review_id, action="reject", at=_T0 + timedelta(hours=3)
    )
    await _record_action(
        session_maker,
        review_id=review_id,
        action="reopen",
        at=_T0 + timedelta(hours=4),
        evidence={"sha256": sha256, "score_count": 3, "previous_status": "scored"},
    )
    current = await _record_action(
        session_maker, review_id=review_id, action="reject", at=_T0 + timedelta(hours=5)
    )
    _install(app, session_maker)

    decision = _decision(agent_id, sha256, quarantine_id)
    preview = await _preview(client, [decision])
    ruling = preview.json()["items"][0]["terminal_ruling"]
    assert ruling["ath_review_id"] == str(review_id)
    assert ruling["ath_action_id"] == str(current)
    assert ruling["ath_action_id"] != str(historical)
    executed = await _execute(client, [decision], preview.json()["preview_token"])
    assert executed.json()["items"][0]["status"] == "applied"

    async with session_maker() as session:
        event = await session.scalar(
            select(ScreeningReviewEvent).where(
                ScreeningReviewEvent.quarantine_id == quarantine_id
            )
        )
    assert event is not None
    evidence = event.evidence["terminal_reconciliation"]
    assert evidence["ath_action_id"] == str(current)
    assert evidence["ath_review_id"] == str(review_id)
    assert evidence["artifact_sha256"] == sha256


@pytest.mark.parametrize(
    "unidentified", ["no_review", "no_action", "later_clear", "other_artifact"]
)
async def test_unidentified_terminal_ruling_is_never_reconciled(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    unidentified: str,
) -> None:
    agent_id, sha256, quarantine_id = await _seed(
        session_maker, status=AgentStatus.BANNED, ath_resolution="reject"
    )
    async with session_maker() as session, session.begin():
        review = await session.scalar(
            select(AthReview).where(AthReview.agent_id == agent_id)
        )
        assert review is not None
        if unidentified == "no_review":
            await session.delete(review)
        elif unidentified == "no_action":
            await session.execute(
                delete(AthReviewAction).where(
                    AthReviewAction.review_id == review.review_id
                )
            )
        elif unidentified == "later_clear":
            session.add(
                AthReviewAction(
                    action_id=uuid4(),
                    review_id=review.review_id,
                    action="clear",
                    reason="Supersedes the historical reject",
                    actor="operator",
                    evidence={"sha256": sha256},
                    created_at=_T0 + timedelta(hours=4),
                )
            )
        else:
            # The ruling was recorded against a different artifact digest.
            review.original_evidence = {**review.original_evidence, "sha256": "e" * 64}
    _install(app, session_maker)

    decision = _decision(agent_id, sha256, quarantine_id)
    preview = await _preview(client, [decision])
    item = preview.json()["items"][0]
    assert item["disposition"] == "conflict"
    assert item["terminal_reconciliation"] is True
    assert item["terminal_ruling"] is None
    assert "not reconcilable" in item["message"]
    executed = await _execute(client, [decision], preview.json()["preview_token"])
    assert executed.json()["items"][0]["status"] == "failed"
    assert await _active_quarantine_count(session_maker, agent_id) == 1


async def test_ruling_change_between_preview_and_execute_refuses_the_batch(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id, sha256, quarantine_id = await _seed(
        session_maker, status=AgentStatus.BANNED, ath_resolution="reject"
    )
    _install(app, session_maker)
    decision = _decision(agent_id, sha256, quarantine_id)
    preview = await _preview(client, [decision])
    assert preview.json()["items"][0]["disposition"] == "ready"

    await _record_action(
        session_maker,
        review_id=await _review_id(session_maker, agent_id),
        action="reject",
        at=_T0 + timedelta(hours=6),
    )
    executed = await _execute(client, [decision], preview.json()["preview_token"])

    assert executed.status_code == 409
    assert executed.json()["message"] == (
        "terminal ruling changed after preview; preview again"
    )
    assert await _active_quarantine_count(session_maker, agent_id) == 1


async def test_normal_preview_refused_when_agent_turns_terminal_before_execute(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A previewed screening reject never silently becomes a reconciliation."""
    agent_id, sha256, quarantine_id = await _seed(
        session_maker, status=AgentStatus.QUARANTINED, ath_resolution=None
    )
    _install(app, session_maker)
    decision = _decision(agent_id, sha256, quarantine_id)
    preview = await _preview(client, [decision])
    item = preview.json()["items"][0]
    assert (item["disposition"], item["terminal_reconciliation"]) == ("ready", False)
    assert item["resulting_agent_status"] == AgentStatus.REJECTED

    async with session_maker() as session, session.begin():
        agent = await session.get(Agent, agent_id)
        review = await session.scalar(
            select(AthReview).where(AthReview.agent_id == agent_id)
        )
        assert agent is not None and review is not None
        agent.status = AgentStatus.BANNED
        review.status = "resolved"
        review.resolution = "reject"
        review.resolved_at = _T0 + timedelta(hours=3)
        review.resolved_by = "operator"
        review.resolution_reason = _ATH_REASON
    executed = await _execute(client, [decision], preview.json()["preview_token"])

    assert executed.status_code == 409
    assert executed.json()["message"] == (
        "terminal ruling changed after preview; preview again"
    )
    assert await _active_quarantine_count(session_maker, agent_id) == 1
    assert await _resolution_count(session_maker, quarantine_id) == 0


@pytest.mark.parametrize("change", ["new_ruling", "turned_terminal"])
async def test_terminal_state_change_under_the_lock_refuses_the_item(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    change: str,
) -> None:
    """The execute transaction re-derives the terminal fence under its locks.

    Another writer holds the quarantine row while the execution is already
    past its token check and per-item preview, changes the terminal state and
    commits. The execution must refuse the item once it gets the lock.
    """
    status = AgentStatus.BANNED if change == "new_ruling" else AgentStatus.QUARANTINED
    agent_id, sha256, quarantine_id = await _seed(
        session_maker,
        status=status,
        ath_resolution="reject" if change == "new_ruling" else None,
    )
    review_id = await _review_id(session_maker, agent_id)
    _install(app, session_maker)
    decision = _decision(agent_id, sha256, quarantine_id)
    preview = await _preview(client, [decision])
    assert preview.json()["items"][0]["disposition"] == "ready"

    async with session_maker() as writer, session_maker() as observer:
        await writer.begin()
        await writer.execute(
            select(ScreeningQuarantine)
            .where(ScreeningQuarantine.quarantine_id == quarantine_id)
            .with_for_update()
        )
        execution = asyncio.create_task(
            _execute(client, [decision], preview.json()["preview_token"])
        )
        for _ in range(200):
            if await _lock_waiters(observer) or execution.done():
                break
            await asyncio.sleep(0.05)
        assert not execution.done(), execution.result().text
        assert await _lock_waiters(observer) == 1

        if change == "new_ruling":
            writer.add(
                AthReviewAction(
                    action_id=uuid4(),
                    review_id=review_id,
                    action="reject",
                    reason="re-ruled while the batch waited",
                    actor="operator",
                    evidence={"previous_status": "scored"},
                    created_at=_T0 + timedelta(hours=7),
                )
            )
        else:
            agent = await writer.get(Agent, agent_id)
            review = await writer.get(AthReview, review_id)
            assert agent is not None and review is not None
            agent.status = AgentStatus.BANNED
            review.status = "resolved"
            review.resolution = "reject"
            review.resolved_at = _T0 + timedelta(hours=7)
            review.resolved_by = "operator"
            review.resolution_reason = _ATH_REASON
        await writer.commit()

    executed = await asyncio.wait_for(execution, timeout=10)
    assert executed.status_code == 200, executed.text
    item = executed.json()["items"][0]
    assert item["status"] == "failed"
    assert item["message"] == "terminal ruling changed after preview"
    assert await _active_quarantine_count(session_maker, agent_id) == 1
    assert await _resolution_count(session_maker, quarantine_id) == 0


async def test_concurrent_executions_close_the_quarantine_once(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id, sha256, quarantine_id = await _seed(
        session_maker, status=AgentStatus.BANNED, ath_resolution="reject"
    )
    _install(app, session_maker)
    decision = _decision(agent_id, sha256, quarantine_id)
    preview = await _preview(client, [decision])
    token = preview.json()["preview_token"]

    async with session_maker() as holder, session_maker() as observer:
        await holder.begin()
        await holder.execute(
            select(ScreeningQuarantine)
            .where(ScreeningQuarantine.quarantine_id == quarantine_id)
            .with_for_update()
        )
        executions = [
            asyncio.create_task(_execute(client, [decision], token)) for _ in range(2)
        ]
        for _ in range(200):
            if await _lock_waiters(observer) == 2:
                break
            await asyncio.sleep(0.05)
        assert await _lock_waiters(observer) == 2
        await holder.rollback()

    responses = await asyncio.wait_for(asyncio.gather(*executions), timeout=10)
    statuses = sorted(response.json()["items"][0]["status"] for response in responses)
    assert statuses == ["already_applied", "applied"]
    assert await _resolution_count(session_maker, quarantine_id) == 1
    assert await _active_quarantine_count(session_maker, agent_id) == 0
