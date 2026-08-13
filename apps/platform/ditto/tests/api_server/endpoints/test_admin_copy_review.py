"""Durable ATH copy-review API regression coverage."""

import gzip
import hashlib
import io
import tarfile
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)

from ditto.api_server.dependencies import get_session, get_storage_client
from ditto.api_server.fingerprint import reference_corpus_provenance
from ditto.api_server.storage import ObjectDownloadFailedError
from ditto.db.models import (
    Agent,
    AgentStatus,
    ArtifactFetchAudit,
    AthReview,
    AthReviewAction,
    BenchmarkRollout,
    Score,
)
from ditto.db.queries.benchmark_rollout import MIN_SCOREABLE_BENCH_VERSION
from ditto.db.queries.scores import list_eligible_ledger

_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_TOKEN}", "X-Admin-Actor": "operator"}
_T0 = datetime(2026, 7, 16, 12, tzinfo=UTC)
_CORPUS_ID = reference_corpus_provenance()["corpus_id"]


@pytest.fixture
def maker(
    session_maker: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    """Local alias for the root Postgres ``session_maker``."""
    return session_maker


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_TOKEN)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


async def _seed(
    maker: async_sessionmaker[AsyncSession],
    *,
    opened_at: datetime = _T0,
) -> tuple[UUID, UUID]:
    original_id, agent_id, review_id = uuid4(), uuid4(), uuid4()
    async with maker() as session, session.begin():
        session.add_all(
            [
                Agent(
                    agent_id=original_id,
                    miner_hotkey="5Original",
                    name="original",
                    sha256=original_id.hex * 2,
                    status=AgentStatus.SCORED,
                    created_at=_T0 - timedelta(hours=1),
                ),
                Agent(
                    agent_id=agent_id,
                    miner_hotkey="5Held",
                    name="held",
                    sha256=agent_id.hex * 2,
                    status=AgentStatus.ATH_PENDING_REVIEW,
                    duplicate_of=original_id,
                    review_reason="legacy near-copy signal",
                    screening_policy_version=8,
                    created_at=_T0,
                ),
                AthReview(
                    review_id=review_id,
                    agent_id=agent_id,
                    status="pending",
                    opened_at=opened_at,
                    original_duplicate_of=original_id,
                    original_reason="legacy near-copy signal",
                    original_policy_version=8,
                    original_evidence={
                        "content_fingerprint_version": 1,
                        "structural_fingerprint_version": 1,
                        "prompt_fingerprint_version": "p1",
                    },
                    algorithm_provenance={
                        "reference_provenance": "legacy",
                        "backfilled": True,
                    },
                ),
            ]
        )
    return agent_id, original_id


async def _score_review_for_generation(
    maker: async_sessionmaker[AsyncSession],
    *,
    agent_id: UUID,
    bench_version: int,
) -> None:
    async with maker() as session, session.begin():
        session.add(
            Score(
                agent_id=agent_id,
                validator_hotkey=f"validator-v{bench_version}",
                run_id=f"generation-{bench_version}",
                signature=None,
                seed=bench_version,
                bench_version=bench_version,
                composite=0.8,
                tool_mean=0.8,
                memory_mean=0.8,
                median_ms=100,
                n=114,
                details={"bench_version": bench_version},
                generated_at=_T0,
            )
        )


async def _activate_generation(
    maker: async_sessionmaker[AsyncSession], *, bench_version: int
) -> None:
    async with maker() as session, session.begin():
        session.add(
            BenchmarkRollout(
                rollout_id=uuid4(),
                from_version=bench_version - 1,
                desired_version=bench_version,
                status="activated",
                cohort_size=5,
                created_at=_T0 - timedelta(hours=2),
                activated_at=_T0 - timedelta(hours=1),
            )
        )


def _fingerprint(prefix: str, *, corpus: str = _CORPUS_ID) -> dict:
    values = [f"{prefix}{i:015x}" for i in range(12)]
    return {"v": 2, "corpus": corpus, "k": 256, "card": 12, "m": values}


async def _add_finalized_scores(
    maker: async_sessionmaker[AsyncSession], *, agent_ids: tuple[UUID, UUID]
) -> None:
    """Give both sides of the pair a quorum in the era the ledger is reading.

    The dedicated ``/current-comparison`` endpoint reads through
    ``list_scores_for_agent``, which filters to ``active_bench_version()``. On
    an empty database that answers ``DEFAULT_BENCH_VERSION`` (2), so scores
    written at the current era would simply not be seen and the endpoint would
    fail closed with a 409. This used to line up by accident -- the ``Score``
    model defaulted to 2 as well -- and stopped lining up when the retired-era
    floor moved that default to v7. Recording the v6 -> v7 activation puts the
    ledger's authority where production's is, so the pair is compared instead
    of being reported unavailable.
    """
    async with maker() as session, session.begin():
        session.add(
            BenchmarkRollout(
                rollout_id=uuid4(),
                from_version=MIN_SCOREABLE_BENCH_VERSION - 1,
                desired_version=MIN_SCOREABLE_BENCH_VERSION,
                status="activated",
                cohort_size=5,
                created_at=_T0 - timedelta(days=1),
                activated_at=_T0 - timedelta(hours=1),
            )
        )
        for agent_id in agent_ids:
            for index, composite in enumerate((0.79, 0.80, 0.81)):
                session.add(
                    Score(
                        agent_id=agent_id,
                        validator_hotkey=f"validator-{index}",
                        run_id=f"run-{index}",
                        signature=None,
                        seed=7,
                        bench_version=MIN_SCOREABLE_BENCH_VERSION,
                        composite=composite,
                        tool_mean=composite,
                        memory_mean=composite,
                        median_ms=100 + index,
                        n=114,
                        details={"bench_version": MIN_SCOREABLE_BENCH_VERSION},
                        generated_at=_T0 + timedelta(minutes=index),
                    )
                )


async def _seed_current_comparison(
    maker: async_sessionmaker[AsyncSession],
    *,
    reference_corpus: str = _CORPUS_ID,
) -> tuple[UUID, UUID]:
    agent_id, original_id = await _seed(maker)
    async with maker() as session, session.begin():
        candidate = await session.get(Agent, agent_id)
        reference = await session.get(Agent, original_id)
        assert candidate is not None and reference is not None
        candidate.content_fingerprint = _fingerprint("c")
        reference.content_fingerprint = _fingerprint("r", corpus=reference_corpus)
        candidate.size_bytes = 500_001
        reference.size_bytes = 500_000
        # The endpoint must use AthReview.original_duplicate_of, not this mutable
        # field, when it reconstructs current comparison evidence.
        candidate.duplicate_of = None
    await _add_finalized_scores(maker, agent_ids=(agent_id, original_id))
    return agent_id, original_id


async def _seed_scored_agent(
    maker: async_sessionmaker[AsyncSession],
    *,
    score_count: int = 3,
    status: AgentStatus = AgentStatus.SCORED,
) -> tuple[UUID, str]:
    agent_id = uuid4()
    sha256 = agent_id.hex * 2
    async with maker() as session, session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey="5Benchmax",
                name="benchmax",
                sha256=sha256,
                status=status,
                screening_policy_version=8,
                created_at=_T0,
            )
        )
        for index in range(score_count):
            session.add(
                Score(
                    agent_id=agent_id,
                    validator_hotkey=f"validator-{index}",
                    run_id=f"manual-hold-run-{index}",
                    signature=None,
                    seed=7,
                    composite=0.97,
                    tool_mean=0.97,
                    memory_mean=0.90,
                    median_ms=100,
                    n=114,
                    details={"bench_version": MIN_SCOREABLE_BENCH_VERSION},
                    generated_at=_T0 + timedelta(minutes=index),
                )
            )
    return agent_id, sha256


async def test_manual_open_holds_exact_scored_artifact_and_removes_it_from_ledger(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id, sha256 = await _seed_scored_agent(maker)
    _install(app, maker)
    payload = {
        "expected_sha256": sha256,
        "expected_score_count": 3,
        "reason": "Deterministic benchmark-family routing requires ATH review",
    }

    response = await client.post(
        f"/api/v1/admin/copy-reviews/{agent_id}/open",
        json=payload,
        headers=_HEADERS,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["agent_status"] == AgentStatus.ATH_PENDING_REVIEW
    assert body["idempotent"] is False
    assert body["review"]["original"]["review_kind"] == "benchmark_overfit"
    assert body["review"]["original"]["duplicate_of"] is None
    retry = await client.post(
        f"/api/v1/admin/copy-reviews/{agent_id}/open",
        json=payload,
        headers=_HEADERS,
    )
    assert retry.status_code == 200 and retry.json()["idempotent"] is True

    audit = await client.get(
        f"/api/v1/admin/copy-reviews/{agent_id}/audit", headers=_HEADERS
    )
    assert audit.status_code == 200
    audit_body = audit.json()
    assert audit_body["review"]["review_id"] == body["review"]["review_id"]
    assert audit_body["review"]["original"]["review_kind"] == "benchmark_overfit"
    assert audit_body["review"]["original"]["reason"] == payload["reason"]
    assert {key: value for key, value in audit_body.items() if key != "review"} == {
        "agent_status": AgentStatus.ATH_PENDING_REVIEW,
        "held_artifact_sha256": sha256,
        "held_score_count": 3,
        "previous_status": AgentStatus.SCORED,
        "opened_by": "operator",
        "action_history": [],
    }

    async with maker() as session:
        agent = await session.get(Agent, agent_id)
        review = await session.scalar(
            select(AthReview).where(AthReview.agent_id == agent_id)
        )
        ledger = await list_eligible_ledger(session)
        assert agent is not None and review is not None
        assert agent.status == AgentStatus.ATH_PENDING_REVIEW
        assert agent.review_reason == payload["reason"]
        assert review.original_evidence["sha256"] == sha256
        assert review.original_evidence["score_count"] == 3
        assert review.algorithm_provenance["opened_by"] == "operator"
        assert all(row.agent_id != agent_id for row in ledger)


@pytest.mark.parametrize(
    ("field", "value", "detail"),
    [
        ("expected_sha256", "0" * 64, "artifact sha256 changed"),
        ("expected_score_count", 2, "score count changed"),
    ],
)
async def test_manual_open_rejects_stale_identity_guards(
    app: FastAPI,
    client: httpx.AsyncClient,
    maker: async_sessionmaker[AsyncSession],
    field: str,
    value: str | int,
    detail: str,
) -> None:
    agent_id, sha256 = await _seed_scored_agent(maker)
    _install(app, maker)
    payload: dict[str, object] = {
        "expected_sha256": sha256,
        "expected_score_count": 3,
        "reason": "Manual benchmark-overfit review",
    }
    payload[field] = value

    response = await client.post(
        f"/api/v1/admin/copy-reviews/{agent_id}/open",
        json=payload,
        headers=_HEADERS,
    )

    assert response.status_code == 409
    assert detail in response.text
    async with maker() as session:
        agent = await session.get(Agent, agent_id)
        assert agent is not None and agent.status == AgentStatus.SCORED
        assert (
            await session.scalar(
                select(AthReview).where(AthReview.agent_id == agent_id)
            )
            is None
        )


async def test_clearing_manual_hold_restores_live_status(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id, sha256 = await _seed_scored_agent(maker, status=AgentStatus.LIVE)
    _install(app, maker)
    opened = await client.post(
        f"/api/v1/admin/copy-reviews/{agent_id}/open",
        json={
            "expected_sha256": sha256,
            "expected_score_count": 3,
            "reason": "Manual benchmark-overfit review",
        },
        headers=_HEADERS,
    )
    assert opened.status_code == 200

    resolved = await client.post(
        f"/api/v1/admin/copy-reviews/{agent_id}/resolve",
        json={"resolution": "clear", "reason": "General behavior confirmed"},
        headers=_HEADERS,
    )

    assert resolved.status_code == 200
    assert resolved.json()["agent_status"] == AgentStatus.LIVE


async def test_detailed_manual_hold_reasons_are_preserved_without_truncation(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id, sha256 = await _seed_scored_agent(maker)
    _install(app, maker)
    hold_reason = "Detailed source evidence for the hold. " + "h" * 1_000
    resolution_reason = (
        "Detailed adjudication evidence for the decision. " + "r" * 1_000
    )

    opened = await client.post(
        f"/api/v1/admin/copy-reviews/{agent_id}/open",
        json={
            "expected_sha256": sha256,
            "expected_score_count": 3,
            "reason": hold_reason,
        },
        headers=_HEADERS,
    )
    assert opened.status_code == 200
    assert opened.json()["review"]["original"]["reason"] == hold_reason

    resolved = await client.post(
        f"/api/v1/admin/copy-reviews/{agent_id}/resolve",
        json={"resolution": "clear", "reason": resolution_reason},
        headers=_HEADERS,
    )
    assert resolved.status_code == 200
    assert resolved.json()["review"]["resolution_reason"] == resolution_reason

    audit = await client.get(
        f"/api/v1/admin/copy-reviews/{agent_id}/audit", headers=_HEADERS
    )
    assert audit.status_code == 200
    assert audit.json()["action_history"][-1]["reason"] == resolution_reason

    async with maker() as session:
        agent = await session.get(Agent, agent_id)
        review = await session.scalar(
            select(AthReview).where(AthReview.agent_id == agent_id)
        )
        assert agent is not None and review is not None
        assert agent.review_reason == hold_reason
        assert review.resolution_reason == resolution_reason


async def test_resolved_review_reopens_without_rewriting_original_evidence(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id, sha256 = await _seed_scored_agent(maker, status=AgentStatus.LIVE)
    _install(app, maker)
    first_reason = "Deterministic benchmark-family routing"
    opened = await client.post(
        f"/api/v1/admin/copy-reviews/{agent_id}/open",
        json={
            "expected_sha256": sha256,
            "expected_score_count": 3,
            "reason": first_reason,
        },
        headers=_HEADERS,
    )
    assert opened.status_code == 200
    review_id = UUID(opened.json()["review"]["review_id"])
    initial_opened_at = datetime.fromisoformat(opened.json()["review"]["opened_at"])
    assert opened.json()["reopened"] is False
    cleared = await client.post(
        f"/api/v1/admin/copy-reviews/{agent_id}/resolve",
        json={"resolution": "clear", "reason": "Initial source review cleared it"},
        headers=_HEADERS,
    )
    assert cleared.status_code == 200

    reopen_payload = {
        "expected_sha256": sha256,
        "expected_score_count": 3,
        "reason": "New benchmark-overfit evidence requires another review",
    }
    reopened = await client.post(
        f"/api/v1/admin/copy-reviews/{agent_id}/open",
        json=reopen_payload,
        headers=_HEADERS,
    )
    assert reopened.status_code == 200
    assert reopened.json()["reopened"] is True
    assert reopened.json()["idempotent"] is False
    assert reopened.json()["review"]["review_id"] == str(review_id)
    assert reopened.json()["review"]["original"]["reason"] == first_reason
    retry = await client.post(
        f"/api/v1/admin/copy-reviews/{agent_id}/open",
        json=reopen_payload,
        headers=_HEADERS,
    )
    assert retry.status_code == 200
    assert retry.json()["idempotent"] is True
    assert retry.json()["reopened"] is True

    audit = await client.get(
        f"/api/v1/admin/copy-reviews/{agent_id}/audit", headers=_HEADERS
    )
    assert audit.status_code == 200
    assert [event["action"] for event in audit.json()["action_history"]] == [
        "clear",
        "reopen",
    ]
    assert audit.json()["action_history"][1] == {
        "action": "reopen",
        "reason": reopen_payload["reason"],
        "actor": "operator",
        "created_at": audit.json()["action_history"][1]["created_at"],
        "previous_status": "live",
        "artifact_sha256": sha256,
        "score_count": 3,
    }

    recleared = await client.post(
        f"/api/v1/admin/copy-reviews/{agent_id}/resolve",
        json={"resolution": "clear", "reason": "Second review also cleared it"},
        headers=_HEADERS,
    )
    assert recleared.status_code == 200
    assert recleared.json()["agent_status"] == AgentStatus.LIVE
    async with maker() as session:
        review = await session.scalar(
            select(AthReview).where(AthReview.agent_id == agent_id)
        )
        actions = list(
            await session.scalars(
                select(AthReviewAction).where(AthReviewAction.review_id == review_id)
            )
        )
        assert review is not None and review.original_reason == first_reason
        assert review.opened_at.replace(tzinfo=UTC) == initial_opened_at
        assert review.reopened_at is not None and review.reopened_at > review.opened_at
        assert len(actions) == 3


@pytest.mark.parametrize("previous_status", [AgentStatus.SCORED, AgentStatus.LIVE])
async def test_rejected_review_reopens_and_clear_restores_previous_status(
    app: FastAPI,
    client: httpx.AsyncClient,
    maker: async_sessionmaker[AsyncSession],
    previous_status: AgentStatus,
) -> None:
    agent_id, sha256 = await _seed_scored_agent(maker, status=previous_status)
    _install(app, maker)
    opened = await client.post(
        f"/api/v1/admin/copy-reviews/{agent_id}/open",
        json={
            "expected_sha256": sha256,
            "expected_score_count": 3,
            "reason": "Manual benchmark-overfit review",
        },
        headers=_HEADERS,
    )
    assert opened.status_code == 200
    rejected = await client.post(
        f"/api/v1/admin/copy-reviews/{agent_id}/resolve",
        json={"resolution": "reject", "reason": "Initial review rejected it"},
        headers=_HEADERS,
    )
    assert rejected.status_code == 200
    assert rejected.json()["agent_status"] == AgentStatus.BANNED

    reopen_payload = {
        "expected_sha256": sha256,
        "expected_score_count": 3,
        "reason": "Operator reconsideration requires a guarded reversal",
    }
    reopened = await client.post(
        f"/api/v1/admin/copy-reviews/{agent_id}/open",
        json=reopen_payload,
        headers=_HEADERS,
    )
    assert reopened.status_code == 200
    assert reopened.json()["reopened"] is True
    assert reopened.json()["idempotent"] is False
    assert reopened.json()["agent_status"] == AgentStatus.ATH_PENDING_REVIEW

    retry = await client.post(
        f"/api/v1/admin/copy-reviews/{agent_id}/open",
        json=reopen_payload,
        headers=_HEADERS,
    )
    assert retry.status_code == 200
    assert retry.json()["idempotent"] is True

    audit = await client.get(
        f"/api/v1/admin/copy-reviews/{agent_id}/audit", headers=_HEADERS
    )
    assert audit.status_code == 200
    assert [event["action"] for event in audit.json()["action_history"]] == [
        "reject",
        "reopen",
    ]
    assert audit.json()["action_history"][1]["previous_status"] == previous_status

    cleared = await client.post(
        f"/api/v1/admin/copy-reviews/{agent_id}/resolve",
        json={
            "resolution": "clear",
            "reason": "Reconsideration found no current-policy violation",
        },
        headers=_HEADERS,
    )
    assert cleared.status_code == 200
    assert cleared.json()["agent_status"] == previous_status
    async with maker() as session:
        agent = await session.get(Agent, agent_id)
        scores = list(
            await session.scalars(select(Score).where(Score.agent_id == agent_id))
        )
        assert agent is not None and agent.status == previous_status
        assert len(scores) == 3


async def test_resolved_clear_does_not_reopen_an_unrelated_ban(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id, sha256 = await _seed_scored_agent(maker)
    _install(app, maker)
    opened = await client.post(
        f"/api/v1/admin/copy-reviews/{agent_id}/open",
        json={
            "expected_sha256": sha256,
            "expected_score_count": 3,
            "reason": "Manual benchmark-overfit review",
        },
        headers=_HEADERS,
    )
    assert opened.status_code == 200
    cleared = await client.post(
        f"/api/v1/admin/copy-reviews/{agent_id}/resolve",
        json={"resolution": "clear", "reason": "Initial evidence was clear"},
        headers=_HEADERS,
    )
    assert cleared.status_code == 200
    async with maker() as session, session.begin():
        agent = await session.get(Agent, agent_id)
        assert agent is not None
        agent.status = AgentStatus.BANNED

    response = await client.post(
        f"/api/v1/admin/copy-reviews/{agent_id}/open",
        json={
            "expected_sha256": sha256,
            "expected_score_count": 3,
            "reason": "This unrelated ban must remain in force",
        },
        headers=_HEADERS,
    )
    assert response.status_code == 409
    assert "agent is banned" in response.text


async def test_reopen_still_fails_closed_on_changed_score_count(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id, sha256 = await _seed_scored_agent(maker)
    _install(app, maker)
    opened = await client.post(
        f"/api/v1/admin/copy-reviews/{agent_id}/open",
        json={
            "expected_sha256": sha256,
            "expected_score_count": 3,
            "reason": "Manual benchmark-overfit review",
        },
        headers=_HEADERS,
    )
    assert opened.status_code == 200
    cleared = await client.post(
        f"/api/v1/admin/copy-reviews/{agent_id}/resolve",
        json={"resolution": "clear", "reason": "Initial evidence was clear"},
        headers=_HEADERS,
    )
    assert cleared.status_code == 200
    response = await client.post(
        f"/api/v1/admin/copy-reviews/{agent_id}/open",
        json={
            "expected_sha256": sha256,
            "expected_score_count": 2,
            "reason": "New evidence requires another review",
        },
        headers=_HEADERS,
    )
    assert response.status_code == 409
    assert "score count changed" in response.text


async def test_list_is_bounded_oldest_first_and_private(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    await _seed(maker, opened_at=_T0 + timedelta(hours=1))
    oldest, _ = await _seed(maker, opened_at=_T0)
    _install(app, maker)
    response = await client.get(
        "/api/v1/admin/copy-reviews?generation=all&limit=1&offset=0",
        headers=_HEADERS,
    )
    assert response.status_code == 200
    body = response.json()
    assert (body["count"], body["limit"], body["offset"]) == (2, 1, 0)
    assert body["items"][0]["agent_id"] == str(oldest)
    serialized = response.text.lower()
    assert "sha256" not in serialized and '"m":' not in serialized


async def _set_review_kind(
    maker: async_sessionmaker[AsyncSession], *, agent_id: UUID, review_kind: str
) -> None:
    async with maker() as session, session.begin():
        review = (
            await session.scalars(
                select(AthReview).where(AthReview.agent_id == agent_id)
            )
        ).one()
        review.algorithm_provenance = dict(review.algorithm_provenance) | {
            "review_kind": review_kind
        }


async def test_list_filters_by_review_kind_without_hiding_legacy_copy_holds(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    """A kind filter must agree with the kind the rows report.

    ``review_kind`` lives in ``algorithm_provenance`` and postdates the holds it
    describes, so the oldest rows carry no key and the list renders them as
    ``copy``. A filter matching only the literal string would drop exactly
    those rows while every row it returned still said ``copy`` -- an omission
    with nothing on its face to reveal it.
    """
    legacy_copy, _ = await _seed(maker, opened_at=_T0)
    deferred, _ = await _seed(maker, opened_at=_T0 + timedelta(hours=1))
    overfit, _ = await _seed(maker, opened_at=_T0 + timedelta(hours=2))
    explicit_copy, _ = await _seed(maker, opened_at=_T0 + timedelta(hours=3))
    await _set_review_kind(
        maker, agent_id=deferred, review_kind="deferred_source_review"
    )
    await _set_review_kind(maker, agent_id=overfit, review_kind="benchmark_overfit")
    await _set_review_kind(maker, agent_id=explicit_copy, review_kind="copy")
    _install(app, maker)

    async def _agent_ids(query: str) -> list[str]:
        response = await client.get(
            f"/api/v1/admin/copy-reviews?generation=all&{query}", headers=_HEADERS
        )
        assert response.status_code == 200
        return [item["agent_id"] for item in response.json()["items"]]

    assert await _agent_ids("review_kind=deferred_source_review") == [str(deferred)]
    assert await _agent_ids("review_kind=benchmark_overfit") == [str(overfit)]
    assert await _agent_ids("review_kind=copy") == [
        str(legacy_copy),
        str(explicit_copy),
    ]
    assert len(await _agent_ids("limit=200")) == 4

    filtered = await client.get(
        "/api/v1/admin/copy-reviews?generation=all&review_kind=copy", headers=_HEADERS
    )
    body = filtered.json()
    # count must describe the filtered set, not the table.
    assert body["count"] == 2
    assert body["review_kind"] == "copy"
    assert {item["original"]["review_kind"] for item in body["items"]} == {"copy"}


async def test_list_rows_carry_the_live_agent_status(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    """A pending review whose agent moved on is a stranded hold, not a queue row.

    ``ath_reviews.status`` and ``agents.status`` are separate columns; several
    paths can move the agent and leave the review pending. Without the agent's
    status on the row, a listing cannot distinguish an agent actually waiting
    for adjudication from one that already left the hold -- and ``resolve``
    rejects the second with a 409.
    """
    held, _ = await _seed(maker, opened_at=_T0)
    stranded, _ = await _seed(maker, opened_at=_T0 + timedelta(hours=1))
    async with maker() as session, session.begin():
        agent = await session.get(Agent, stranded)
        assert agent is not None
        agent.status = AgentStatus.SCORED
    _install(app, maker)

    response = await client.get(
        "/api/v1/admin/copy-reviews?generation=all", headers=_HEADERS
    )
    assert response.status_code == 200
    statuses = {
        item["agent_id"]: item["agent_status"] for item in response.json()["items"]
    }
    assert statuses[str(held)] == "ath_pending_review"
    assert statuses[str(stranded)] == "scored"


async def test_list_defaults_to_active_generation_and_filters_count_and_pages(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    historical, _ = await _seed(maker, opened_at=_T0 - timedelta(hours=1))
    active_oldest, _ = await _seed(maker, opened_at=_T0)
    active_newest, _ = await _seed(maker, opened_at=_T0 + timedelta(hours=1))
    await _score_review_for_generation(
        maker, agent_id=historical, bench_version=MIN_SCOREABLE_BENCH_VERSION
    )
    await _score_review_for_generation(
        maker, agent_id=active_oldest, bench_version=MIN_SCOREABLE_BENCH_VERSION + 1
    )
    await _score_review_for_generation(
        maker, agent_id=active_newest, bench_version=MIN_SCOREABLE_BENCH_VERSION + 1
    )
    await _activate_generation(maker, bench_version=MIN_SCOREABLE_BENCH_VERSION + 1)
    _install(app, maker)

    first = await client.get(
        "/api/v1/admin/copy-reviews?limit=1&offset=0", headers=_HEADERS
    )
    second = await client.get(
        "/api/v1/admin/copy-reviews?limit=1&offset=1", headers=_HEADERS
    )
    history = await client.get(
        "/api/v1/admin/copy-reviews?generation=history", headers=_HEADERS
    )
    all_generations = await client.get(
        "/api/v1/admin/copy-reviews?generation=all", headers=_HEADERS
    )

    assert first.status_code == second.status_code == history.status_code == 200
    assert first.json()["generation"] == "active"
    assert first.json()["active_bench_version"] == MIN_SCOREABLE_BENCH_VERSION + 1
    assert first.json()["count"] == second.json()["count"] == 2
    page_ids = [
        first.json()["items"][0]["agent_id"],
        second.json()["items"][0]["agent_id"],
    ]
    assert page_ids == [
        str(active_oldest),
        str(active_newest),
    ]
    assert history.json()["generation"] == "history"
    assert history.json()["count"] == 1
    assert history.json()["items"][0]["agent_id"] == str(historical)
    assert all_generations.json()["count"] == 3


async def test_default_generation_tracks_a_benchmark_authority_bump(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    prior, _ = await _seed(maker)
    current, _ = await _seed(maker, opened_at=_T0 + timedelta(hours=1))
    await _score_review_for_generation(
        maker, agent_id=prior, bench_version=MIN_SCOREABLE_BENCH_VERSION
    )
    await _score_review_for_generation(
        maker, agent_id=current, bench_version=MIN_SCOREABLE_BENCH_VERSION + 1
    )
    await _activate_generation(maker, bench_version=MIN_SCOREABLE_BENCH_VERSION + 1)
    _install(app, maker)

    response = await client.get("/api/v1/admin/copy-reviews", headers=_HEADERS)

    assert response.status_code == 200
    assert response.json()["active_bench_version"] == MIN_SCOREABLE_BENCH_VERSION + 1
    assert response.json()["count"] == 1
    assert response.json()["items"][0]["agent_id"] == str(current)


async def test_rollout_generation_surfaces_target_reviews_without_mixing_history(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    active, _ = await _seed(maker, opened_at=_T0)
    rollout_target, _ = await _seed(maker, opened_at=_T0 + timedelta(hours=1))
    historical, _ = await _seed(maker, opened_at=_T0 + timedelta(hours=2))
    active_version = MIN_SCOREABLE_BENCH_VERSION + 1
    target_version = active_version + 1
    await _score_review_for_generation(
        maker, agent_id=active, bench_version=active_version
    )
    await _score_review_for_generation(
        maker, agent_id=rollout_target, bench_version=target_version
    )
    await _score_review_for_generation(
        maker, agent_id=historical, bench_version=MIN_SCOREABLE_BENCH_VERSION
    )
    await _activate_generation(maker, bench_version=active_version)
    async with maker() as session, session.begin():
        session.add(
            BenchmarkRollout(
                rollout_id=uuid4(),
                from_version=active_version,
                desired_version=target_version,
                status="collecting",
                cohort_size=5,
                created_at=_T0,
            )
        )
    _install(app, maker)

    active_response = await client.get(
        "/api/v1/admin/copy-reviews?generation=active", headers=_HEADERS
    )
    rollout_response = await client.get(
        "/api/v1/admin/copy-reviews?generation=rollout", headers=_HEADERS
    )
    history_response = await client.get(
        "/api/v1/admin/copy-reviews?generation=history", headers=_HEADERS
    )

    assert active_response.status_code == 200
    assert rollout_response.status_code == 200
    assert history_response.status_code == 200
    assert active_response.json()["rollout_bench_version"] == target_version
    assert [item["agent_id"] for item in rollout_response.json()["items"]] == [
        str(rollout_target)
    ]
    assert [item["agent_id"] for item in history_response.json()["items"]] == [
        str(historical)
    ]


async def test_rollout_generation_is_empty_without_open_transition(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    active, _ = await _seed(maker)
    await _score_review_for_generation(
        maker, agent_id=active, bench_version=MIN_SCOREABLE_BENCH_VERSION
    )
    await _activate_generation(maker, bench_version=MIN_SCOREABLE_BENCH_VERSION)
    _install(app, maker)

    response = await client.get(
        "/api/v1/admin/copy-reviews?generation=rollout", headers=_HEADERS
    )

    assert response.status_code == 200
    assert response.json()["rollout_bench_version"] is None
    assert response.json()["count"] == 0
    assert response.json()["items"] == []


async def test_original_evidence_names_the_matched_submission(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id, original_id = await _seed(maker)
    _install(app, maker)
    listing = await client.get(
        "/api/v1/admin/copy-reviews?generation=all", headers=_HEADERS
    )
    assert listing.status_code == 200
    original = listing.json()["items"][0]["original"]
    assert original["duplicate_of"] == str(original_id)
    assert original["duplicate_of_name"] == "original"
    assert original["duplicate_of_hotkey"] == "5Original"
    assert original["duplicate_of_submitted_at"] is not None
    detail = await client.get(
        f"/api/v1/admin/copy-reviews/{agent_id}", headers=_HEADERS
    )
    assert detail.status_code == 200
    assert detail.json()["original"]["duplicate_of_name"] == "original"


async def test_matched_identity_is_null_when_reference_row_is_gone(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id, _ = await _seed(maker)
    async with maker() as session, session.begin():
        review = (
            await session.execute(
                select(AthReview).where(AthReview.agent_id == agent_id)
            )
        ).scalar_one()
        review.original_duplicate_of = None
    _install(app, maker)
    detail = await client.get(
        f"/api/v1/admin/copy-reviews/{agent_id}", headers=_HEADERS
    )
    assert detail.status_code == 200
    original = detail.json()["original"]
    assert original["duplicate_of"] is None
    assert original["duplicate_of_name"] is None
    assert original["duplicate_of_hotkey"] is None


async def test_list_can_embed_current_comparisons_in_one_request(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    comparable, _ = await _seed_current_comparison(maker)
    scoreless, _ = await _seed(maker, opened_at=_T0 + timedelta(hours=1))
    _install(app, maker)
    response = await client.get(
        "/api/v1/admin/copy-reviews?generation=all&include=current_comparison",
        headers=_HEADERS,
    )
    assert response.status_code == 200
    by_agent = {item["agent_id"]: item for item in response.json()["items"]}
    embedded = by_agent[str(comparable)]["current_comparison"]
    assert embedded["availability"] == "available"
    assert embedded["algorithm_version"] == "reference-aware-v2"
    assert embedded["current_decision"] in {"clear", "hold", "inconclusive_review"}
    # A row the dedicated endpoint would 409 embeds the fail-closed state.
    failed_closed = by_agent[str(scoreless)]["current_comparison"]
    assert failed_closed == {
        "availability": "unavailable",
        "bulk_eligible": False,
        "reason": "current comparison unavailable",
    }
    # No fingerprint material leaks through the embedded form either.
    serialized = response.text.lower()
    assert "sha256" not in serialized and '"m":' not in serialized


async def test_list_without_include_omits_comparisons(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    await _seed_current_comparison(maker)
    _install(app, maker)
    response = await client.get("/api/v1/admin/copy-reviews", headers=_HEADERS)
    assert response.status_code == 200
    assert response.json()["items"][0]["current_comparison"] is None


async def test_embedded_comparison_matches_the_dedicated_endpoint(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id, _ = await _seed_current_comparison(maker)
    _install(app, maker)
    dedicated = await client.get(
        f"/api/v1/admin/copy-reviews/{agent_id}/current-comparison", headers=_HEADERS
    )
    listing = await client.get(
        "/api/v1/admin/copy-reviews?include=current_comparison", headers=_HEADERS
    )
    assert dedicated.status_code == 200 and listing.status_code == 200
    embedded = listing.json()["items"][0]["current_comparison"]
    assert embedded == dedicated.json()


async def test_current_comparison_is_unavailable_without_finalized_scores(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id, _ = await _seed(maker)
    _install(app, maker)
    detail = await client.get(
        f"/api/v1/admin/copy-reviews/{agent_id}", headers=_HEADERS
    )
    assert detail.status_code == 200
    comparison = await client.get(
        f"/api/v1/admin/copy-reviews/{agent_id}/current-comparison", headers=_HEADERS
    )
    assert comparison.status_code == 409
    assert "current comparison unavailable" in comparison.text


async def test_current_comparison_returns_only_corrected_aggregate_wire(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id, _ = await _seed_current_comparison(maker)
    _install(app, maker)
    response = await client.get(
        f"/api/v1/admin/copy-reviews/{agent_id}/current-comparison", headers=_HEADERS
    )

    assert response.status_code == 200
    body = response.json()
    assert body["availability"] == "available"
    assert body["bulk_eligible"] is True
    assert body["current_decision"] == "clear"
    assert body["chronology_direction"] == "reference_earlier"
    assert body["lexical"]["candidate_cardinality"] == 12
    serialized = response.text.lower()
    for forbidden in (
        "sha256",
        "normalized_source_hash",
        "artifact",
        "source_path",
        '"m"',
        "credential",
    ):
        assert forbidden not in serialized
    async with maker() as session:
        agent = await session.get(Agent, agent_id)
        review = await session.scalar(
            select(AthReview).where(AthReview.agent_id == agent_id)
        )
        assert agent is not None and review is not None
        assert agent.status == AgentStatus.ATH_PENDING_REVIEW
        assert agent.duplicate_of is None
        assert review.algorithm_provenance == {
            "reference_provenance": "legacy",
            "backfilled": True,
        }


async def test_incompatible_current_comparison_is_never_bulk_eligible(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id, _ = await _seed_current_comparison(maker, reference_corpus="older-corpus")
    _install(app, maker)
    response = await client.get(
        f"/api/v1/admin/copy-reviews/{agent_id}/current-comparison", headers=_HEADERS
    )

    assert response.status_code == 200
    assert response.json()["current_decision"] == "clear"
    assert response.json()["triggered"] is False
    assert response.json()["bulk_eligible"] is False


async def test_clear_is_durable_preserves_evidence_and_retries_idempotently(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id, original_id = await _seed(maker)
    _install(app, maker)
    payload = {"resolution": "release", "reason": "Corrected comparison clears it"}
    first = await client.post(
        f"/api/v1/admin/copy-reviews/{agent_id}/resolve", json=payload, headers=_HEADERS
    )
    assert first.status_code == 200 and first.json()["idempotent"] is False
    retry = await client.post(
        f"/api/v1/admin/copy-reviews/{agent_id}/resolve", json=payload, headers=_HEADERS
    )
    assert retry.status_code == 200 and retry.json()["idempotent"] is True
    async with maker() as session:
        agent = await session.get(Agent, agent_id)
        review = await session.scalar(
            select(AthReview).where(AthReview.agent_id == agent_id)
        )
        assert agent is not None and review is not None
        assert agent.status == AgentStatus.SCORED
        assert agent.duplicate_of == original_id and agent.review_reason is not None
        assert review.resolution == "clear" and review.resolved_by == "operator"
        assert review.resolution_reason == payload["reason"]


async def test_conflicting_retry_and_changed_snapshot_fail_closed(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id, _ = await _seed(maker)
    _install(app, maker)
    async with maker() as session, session.begin():
        agent = await session.get(Agent, agent_id)
        assert agent is not None
        agent.duplicate_of = None
    mismatch = await client.post(
        f"/api/v1/admin/copy-reviews/{agent_id}/resolve",
        json={"resolution": "ban", "reason": "Confirmed copied implementation"},
        headers=_HEADERS,
    )
    assert mismatch.status_code == 409


async def test_whitespace_actor_and_reason_are_rejected(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id, _ = await _seed(maker)
    _install(app, maker)
    actor = await client.post(
        f"/api/v1/admin/copy-reviews/{agent_id}/resolve",
        json={"resolution": "clear", "reason": "valid reason"},
        headers={**_HEADERS, "X-Admin-Actor": "   "},
    )
    reason = await client.post(
        f"/api/v1/admin/copy-reviews/{agent_id}/resolve",
        json={"resolution": "clear", "reason": "   "},
        headers=_HEADERS,
    )
    assert actor.status_code == 422 and reason.status_code == 422


async def test_changed_hold_reason_fails_closed(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id, _ = await _seed(maker)
    _install(app, maker)
    async with maker() as session, session.begin():
        agent = await session.get(Agent, agent_id)
        assert agent is not None
        agent.review_reason = "different evidence"
    response = await client.post(
        f"/api/v1/admin/copy-reviews/{agent_id}/resolve",
        json={"resolution": "clear", "reason": "Operator cleared evidence"},
        headers=_HEADERS,
    )
    assert response.status_code == 409


def _tarball(files: dict[str, str]) -> bytes:
    """Build a gzip tarball of ``path -> text`` files, like an agent artifact."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        for path, text in files.items():
            data = text.encode("utf-8")
            info = tarfile.TarInfo(name=path)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return gzip.compress(raw.getvalue())


async def _seed_diff_pair(
    maker: async_sessionmaker[AsyncSession],
    candidate_files: dict[str, str],
    reference_files: dict[str, str],
) -> tuple[UUID, UUID, dict[str, bytes]]:
    """Seed a held/reference pair whose sha256 match real tarball bytes.

    Returns the ids plus the ``key -> tar bytes`` map the storage stub serves.
    """
    candidate_tar = _tarball(candidate_files)
    reference_tar = _tarball(reference_files)
    reference_id, candidate_id, review_id = uuid4(), uuid4(), uuid4()
    objects = {
        f"{candidate_id}/agent.tar.gz": candidate_tar,
        f"{reference_id}/agent.tar.gz": reference_tar,
    }
    async with maker() as session, session.begin():
        session.add_all(
            [
                Agent(
                    agent_id=reference_id,
                    miner_hotkey="5Original",
                    name="original",
                    sha256=hashlib.sha256(reference_tar).hexdigest(),
                    status=AgentStatus.SCORED,
                    created_at=_T0 - timedelta(hours=1),
                ),
                Agent(
                    agent_id=candidate_id,
                    miner_hotkey="5Held",
                    name="held",
                    sha256=hashlib.sha256(candidate_tar).hexdigest(),
                    status=AgentStatus.ATH_PENDING_REVIEW,
                    duplicate_of=reference_id,
                    review_reason="near-copy signal",
                    screening_policy_version=8,
                    created_at=_T0,
                ),
                AthReview(
                    review_id=review_id,
                    agent_id=candidate_id,
                    status="pending",
                    opened_at=_T0,
                    original_duplicate_of=reference_id,
                    original_reason="near-copy signal",
                    original_policy_version=8,
                    original_evidence={},
                    algorithm_provenance={},
                ),
            ]
        )
    return candidate_id, reference_id, objects


def _install_storage(app: FastAPI, objects: dict[str, bytes]) -> None:
    storage = MagicMock()

    async def _get_object(*, key: str, max_bytes: int) -> bytes:
        del max_bytes
        if key not in objects:
            raise ObjectDownloadFailedError(key)
        return objects[key]

    storage.get_object = AsyncMock(side_effect=_get_object)

    async def _fake_storage() -> MagicMock:
        return storage

    app.dependency_overrides[get_storage_client] = _fake_storage


async def test_source_diff_manifest_classifies_files(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    candidate_id, reference_id, objects = await _seed_diff_pair(
        maker,
        candidate_files={
            "src/main.rs": "fn main() {}\n",
            "src/util.rs": "fn util() -> i32 { 1 }\n",
            "src/new.rs": "fn extra() {}\n",
        },
        reference_files={
            "src/main.rs": "fn main() {}\n",
            "src/util.rs": "fn util() -> i32 { 2 }\n",
            "src/gone.rs": "fn gone() {}\n",
        },
    )
    _install(app, maker)
    _install_storage(app, objects)
    response = await client.get(
        f"/api/v1/admin/copy-reviews/{candidate_id}/source-diff", headers=_HEADERS
    )
    assert response.status_code == 200
    body = response.json()
    assert body["reference_agent_id"] == str(reference_id)
    assert (body["identical_count"], body["modified_count"]) == (1, 1)
    assert (body["added_count"], body["removed_count"]) == (1, 1)
    by_path = {entry["path"]: entry for entry in body["files"]}
    assert by_path["src/main.rs"]["status"] == "identical"
    assert by_path["src/util.rs"]["status"] == "modified"
    assert by_path["src/new.rs"]["status"] == "added"
    assert by_path["src/gone.rs"]["status"] == "removed"


async def test_source_diff_audits_both_agents_whose_source_was_read(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    """A diff exposes two miners' source, so it must record two fetches.

    The reference agent never asked to be part of anyone else's review. If only
    the candidate were audited, "who read this submission's source" would come
    back empty for the agent that was actually copied *from*.
    """
    candidate_id, reference_id, objects = await _seed_diff_pair(
        maker,
        candidate_files={"src/main.rs": "fn main() {}\n"},
        reference_files={"src/main.rs": "fn main() {}\n"},
    )
    _install(app, maker)
    _install_storage(app, objects)

    response = await client.get(
        f"/api/v1/admin/copy-reviews/{candidate_id}/source-diff", headers=_HEADERS
    )

    assert response.status_code == 200
    async with maker() as s:
        rows = (await s.scalars(select(ArtifactFetchAudit))).all()
    assert {row.agent_id for row in rows} == {candidate_id, reference_id}
    assert all(row.endpoint == "admin.get_copy_review_source_diff" for row in rows)
    assert all(row.requester_kind == "admin" for row in rows)
    roles = {row.agent_id: (row.detail or {}).get("role") for row in rows}
    assert roles[candidate_id] == "candidate"
    assert roles[reference_id] == "reference"


async def test_source_diff_file_audits_the_path_that_was_read(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    candidate_id, reference_id, objects = await _seed_diff_pair(
        maker,
        candidate_files={"src/util.rs": "fn util() -> i32 { 1 }\n"},
        reference_files={"src/util.rs": "fn util() -> i32 { 2 }\n"},
    )
    _install(app, maker)
    _install_storage(app, objects)

    response = await client.get(
        f"/api/v1/admin/copy-reviews/{candidate_id}/source-diff/file",
        headers=_HEADERS,
        params={"path": "src/util.rs"},
    )

    assert response.status_code == 200
    async with maker() as s:
        rows = (await s.scalars(select(ArtifactFetchAudit))).all()
    assert len(rows) == 2
    assert {row.agent_id for row in rows} == {candidate_id, reference_id}
    assert all((row.detail or {}).get("path") == "src/util.rs" for row in rows)


async def test_source_diff_file_returns_unified_body(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    candidate_id, _, objects = await _seed_diff_pair(
        maker,
        candidate_files={"src/util.rs": "fn util() -> i32 { 1 }\n"},
        reference_files={"src/util.rs": "fn util() -> i32 { 2 }\n"},
    )
    _install(app, maker)
    _install_storage(app, objects)
    response = await client.get(
        f"/api/v1/admin/copy-reviews/{candidate_id}/source-diff/file",
        params={"path": "src/util.rs"},
        headers=_HEADERS,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["candidate_present"] and body["reference_present"]
    joined = "\n".join(body["diff_lines"])
    assert "{ 2 }" in joined and "{ 1 }" in joined


async def test_source_diff_requires_admin_actor(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    candidate_id, _, objects = await _seed_diff_pair(
        maker, {"a.rs": "x\n"}, {"a.rs": "y\n"}
    )
    _install(app, maker)
    _install_storage(app, objects)
    response = await client.get(
        f"/api/v1/admin/copy-reviews/{candidate_id}/source-diff",
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )
    assert response.status_code == 422


async def test_source_diff_missing_file_is_404(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    candidate_id, _, objects = await _seed_diff_pair(
        maker, {"a.rs": "x\n"}, {"a.rs": "y\n"}
    )
    _install(app, maker)
    _install_storage(app, objects)
    response = await client.get(
        f"/api/v1/admin/copy-reviews/{candidate_id}/source-diff/file",
        params={"path": "ghost.rs"},
        headers=_HEADERS,
    )
    assert response.status_code == 404


async def test_source_diff_digest_mismatch_is_502(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    candidate_id, _, objects = await _seed_diff_pair(
        maker, {"a.rs": "x\n"}, {"a.rs": "y\n"}
    )
    # Corrupt the stored candidate bytes so they no longer match agent.sha256.
    objects[f"{candidate_id}/agent.tar.gz"] = _tarball({"a.rs": "tampered\n"})
    _install(app, maker)
    _install_storage(app, objects)
    response = await client.get(
        f"/api/v1/admin/copy-reviews/{candidate_id}/source-diff", headers=_HEADERS
    )
    assert response.status_code == 502
