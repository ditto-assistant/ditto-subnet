"""Batched ATH rulings: presigned upload, dry-run preview, guarded execute.

The Hippius wire path is stubbed at ``app.state.traces_hippius``; the rulings
document contract, per-item guards, crown re-read on both legs, token binding,
and the audit annotations under test are real and run against Postgres.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.admin_ath_rulings import (
    ATH_RULINGS_CONFIRMATION,
    ATH_RULINGS_MAX_BYTES,
    AdminAthRuling,
    AdminAthRulingsDocument,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_ath_rulings import actor_slug, rulings_digest
from ditto.api_server.storage.errors import ObjectNotFoundError
from ditto.db.models import (
    Agent,
    AgentStatus,
    AthReview,
    AthReviewAction,
    BenchmarkRollout,
    Score,
)
from ditto.db.queries.benchmark_rollout import MIN_SCOREABLE_BENCH_VERSION

pytestmark = pytest.mark.asyncio

_TOKEN = "test-admin-token-at-least-32-characters"
_ACTOR = "operator@example.com"
_HEADERS = {"Authorization": f"Bearer {_TOKEN}", "X-Admin-Actor": _ACTOR}
_T0 = datetime(2026, 9, 13, 12, tzinfo=UTC)
_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "ath_rulings_replay_2026-09-13.json"
)
_PREVIEW = "/api/v1/admin/ath-rulings/batch-preview"
_EXECUTE = "/api/v1/admin/ath-rulings/batch-execute"
_UPLOAD = "/api/v1/admin/ath-rulings/upload-url"


@pytest.fixture
def maker(
    session_maker: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    return session_maker


@dataclass
class _StubRulingsStore:
    bucket: str = "ditto-subnet-traces"
    objects: dict[str, bytes] = field(default_factory=dict)
    presigned: list[tuple[str, str, int]] = field(default_factory=list)

    async def presigned_put_url(
        self, *, key: str, content_type: str, expires_in: int = 300
    ) -> str:
        self.presigned.append((key, content_type, expires_in))
        return f"https://s3.hippius.com/{self.bucket}/{key}?X-Amz-Signature=stub"

    async def get_object(self, *, key: str) -> bytes:
        if key not in self.objects:
            raise ObjectNotFoundError(key)
        return self.objects[key]


def _install(
    app: FastAPI,
    maker: async_sessionmaker[AsyncSession],
    store: _StubRulingsStore | None = None,
) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_TOKEN)
    app.state.traces_hippius = store

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


async def _activate(maker: async_sessionmaker[AsyncSession]) -> None:
    """Plant the activated era so the eligible ledger resolves to bench 7."""
    async with maker() as session, session.begin():
        session.add(
            BenchmarkRollout(
                rollout_id=uuid4(),
                from_version=MIN_SCOREABLE_BENCH_VERSION - 1,
                desired_version=MIN_SCOREABLE_BENCH_VERSION,
                status="activated",
                cohort_size=5,
                created_at=_T0 - timedelta(days=2),
                activated_at=_T0 - timedelta(days=1),
            )
        )


def _scores(agent_id: UUID, composite: float, count: int) -> list[Score]:
    return [
        Score(
            agent_id=agent_id,
            validator_hotkey=f"validator-{index}",
            run_id=f"run-{agent_id.hex[:8]}-{index}",
            signature=None,
            seed=7,
            composite=composite,
            tool_mean=composite,
            memory_mean=composite,
            median_ms=100,
            n=114,
            details={"bench_version": MIN_SCOREABLE_BENCH_VERSION},
            generated_at=_T0 + timedelta(minutes=index),
        )
        for index in range(count)
    ]


async def _seed_scored(
    maker: async_sessionmaker[AsyncSession],
    *,
    hotkey: str,
    composite: float,
    created_at: datetime,
    name: str = "agent",
    score_count: int = 3,
    agent_id: UUID | None = None,
    sha256: str | None = None,
) -> tuple[UUID, str]:
    agent_id = agent_id or uuid4()
    sha256 = sha256 or agent_id.hex * 2
    async with maker() as session, session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=hotkey,
                name=name,
                sha256=sha256,
                status=AgentStatus.SCORED,
                screening_policy_version=12,
                created_at=created_at,
            )
        )
        session.add_all(_scores(agent_id, composite, score_count))
    return agent_id, sha256


async def _seed_held(
    maker: async_sessionmaker[AsyncSession],
    *,
    hotkey: str,
    composite: float,
    created_at: datetime,
) -> tuple[UUID, str]:
    agent_id = uuid4()
    sha256 = agent_id.hex * 2
    reason = "Manual benchmark-overfit review"
    async with maker() as session, session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=hotkey,
                name="held",
                sha256=sha256,
                status=AgentStatus.ATH_PENDING_REVIEW,
                review_reason=reason,
                screening_policy_version=12,
                created_at=created_at,
            )
        )
        session.add_all(_scores(agent_id, composite, 3))
        session.add(
            AthReview(
                review_id=uuid4(),
                agent_id=agent_id,
                status="pending",
                opened_at=_T0,
                original_duplicate_of=None,
                original_reason=reason,
                original_policy_version=12,
                original_evidence={
                    "sha256": sha256,
                    "score_count": 3,
                    "previous_status": AgentStatus.SCORED.value,
                },
                algorithm_provenance={
                    "snapshot": "manual-admin-hold",
                    "review_kind": "benchmark_overfit",
                    "opened_by": "someone@example.com",
                    "backfilled": False,
                },
            )
        )
    return agent_id, sha256


def _ruling(
    action: str,
    agent_id: UUID,
    sha256: str,
    *,
    score_count: int = 3,
    reason: str = "Reject under policy v12 for I5: served prompt compiler",
    refs: tuple[str, ...] = ("src/baseline.rs:1195-1207",),
) -> dict[str, object]:
    return {
        "action": action,
        "agent_id": str(agent_id),
        "expected_sha256": sha256,
        "expected_score_count": score_count,
        "reason": reason,
        "evidence_references": list(refs),
    }


async def _status(maker: async_sessionmaker[AsyncSession], agent_id: UUID) -> str:
    async with maker() as session:
        agent = await session.get(Agent, agent_id)
        assert agent is not None
        return agent.status.value


async def _review(
    maker: async_sessionmaker[AsyncSession], agent_id: UUID
) -> tuple[AthReview | None, list[AthReviewAction]]:
    async with maker() as session:
        review = await session.scalar(
            select(AthReview).where(AthReview.agent_id == agent_id)
        )
        if review is None:
            return None, []
        actions = list(
            (
                await session.scalars(
                    select(AthReviewAction)
                    .where(AthReviewAction.review_id == review.review_id)
                    .order_by(AthReviewAction.created_at)
                )
            ).all()
        )
        return review, actions


async def _seed_board(
    maker: async_sessionmaker[AsyncSession],
) -> dict[str, tuple[UUID, str]]:
    await _activate(maker)
    return {
        "champion": await _seed_scored(
            maker,
            hotkey="5Champion",
            name="champion",
            composite=0.9,
            created_at=_T0 - timedelta(hours=3),
        ),
        "runner": await _seed_scored(
            maker,
            hotkey="5Runner",
            name="runner",
            composite=0.8,
            created_at=_T0 - timedelta(hours=2),
        ),
        "held": await _seed_held(
            maker, hotkey="5Held", composite=0.7, created_at=_T0 - timedelta(hours=1)
        ),
        "stale": await _seed_scored(
            maker,
            hotkey="5Stale",
            name="stale",
            composite=0.6,
            created_at=_T0 - timedelta(minutes=30),
        ),
        "uncited": await _seed_scored(
            maker,
            hotkey="5Uncited",
            name="uncited",
            composite=0.5,
            created_at=_T0 - timedelta(minutes=20),
        ),
    }


def _board_rulings(board: dict[str, tuple[UUID, str]]) -> list[dict[str, object]]:
    champion, runner, held, stale, uncited = (
        board["champion"],
        board["runner"],
        board["held"],
        board["stale"],
        board["uncited"],
    )
    return [
        _ruling("open", champion[0], champion[1], refs=()),
        _ruling("reject", runner[0], runner[1]),
        _ruling("clear", held[0], held[1], refs=()),
        _ruling("open", stale[0], "0" * 64, refs=()),
        _ruling("reject", uuid4(), "1" * 64),
        _ruling("reject", uncited[0], uncited[1], refs=()),
    ]


async def _snapshot(
    maker: async_sessionmaker[AsyncSession], board: dict[str, tuple[UUID, str]]
) -> dict[str, str]:
    return {
        name: await _status(maker, agent_id) for name, (agent_id, _) in board.items()
    }


async def test_preview_never_mutates_and_classifies_each_ruling(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    board = await _seed_board(maker)
    _install(app, maker)
    before = await _snapshot(maker, board)

    rulings = _board_rulings(board)
    response = await client.post(_PREVIEW, json={"rulings": rulings}, headers=_HEADERS)

    assert response.status_code == 200, response.text
    body = response.json()
    items = body["items"]
    assert [item["disposition"] for item in items] == [
        "ready",
        "ready",
        "ready",
        "stale_guard",
        "not_found",
        "invalid",
    ]
    assert [item["steps"] for item in items[:3]] == [
        ["open"],
        ["open", "reject"],
        ["clear"],
    ]
    # The champion is also the raw leader: holding it moves the crown. The
    # runner is not, and the held 0.7 agent would re-enter below the leader.
    assert [item["would_change_crown"] for item in items] == [
        True,
        False,
        False,
        False,
        False,
        False,
    ]
    assert items[3]["stale_guard"] is True
    assert items[3]["conflict_reason"] == "artifact sha256 changed"
    assert items[5]["conflict_reason"] == "reject requires evidence_references"
    assert body["ready_count"] == 3
    assert body["already_applied_count"] == 0
    assert body["blocked_count"] == 3
    assert body["crown_moving_count"] == 1
    assert body["board"]["champion_agent_id"] == str(board["champion"][0])
    assert body["board"]["raw_leader_agent_id"] == str(board["champion"][0])
    assert body["board"]["ranked_count"] == 4
    assert body["upload_key"] is None
    assert body["rulings_sha256"] == rulings_digest(
        [AdminAthRuling.model_validate(r) for r in rulings]
    )
    assert len(body["preview_token"].split(".")) == 3
    # A dry run leaves every row exactly as it found it.
    assert await _snapshot(maker, board) == before
    assert (await _review(maker, board["champion"][0]))[0] is None
    assert (await _review(maker, board["runner"][0]))[0] is None
    held_review, _ = await _review(maker, board["held"][0])
    assert held_review is not None and held_review.status == "pending"


async def test_execute_applies_rulings_independently_and_annotates_audit(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    board = await _seed_board(maker)
    _install(app, maker)
    rulings = _board_rulings(board)
    preview = await client.post(_PREVIEW, json={"rulings": rulings}, headers=_HEADERS)
    assert preview.status_code == 200, preview.text
    token = preview.json()["preview_token"]

    response = await client.post(
        _EXECUTE,
        json={
            "preview_token": token,
            "confirmation": ATH_RULINGS_CONFIRMATION,
            "rulings": rulings,
        },
        headers=_HEADERS,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    statuses = [(item["status"], item["agent_status"]) for item in body["items"]]
    assert statuses == [
        ("applied", AgentStatus.ATH_PENDING_REVIEW.value),
        ("applied", AgentStatus.BANNED.value),
        ("applied", AgentStatus.SCORED.value),
        ("failed", AgentStatus.SCORED.value),
        ("failed", None),
        ("failed", AgentStatus.SCORED.value),
    ]
    assert body["items"][1]["steps_applied"] == ["open", "reject"]
    assert body["items"][3]["message"] == "artifact sha256 changed"
    assert body["items"][4]["message"] == "agent not found"
    assert body["applied_count"] == 3
    assert body["failed_count"] == 3
    assert body["board_before"]["champion_agent_id"] == str(board["champion"][0])
    # Champion held, runner banned: the cleared 0.7 agent is what is left.
    assert body["board_after"]["champion_agent_id"] == str(board["held"][0])

    champion_review, champion_actions = await _review(maker, board["champion"][0])
    assert champion_review is not None and champion_review.status == "pending"
    assert champion_review.algorithm_provenance["review_kind"] == "benchmark_overfit"
    annotation = champion_review.algorithm_provenance["last_batch_ruling"]
    assert annotation["batch_id"] == body["batch_id"]
    assert annotation["action"] == "open"
    assert annotation["would_change_crown"] is True
    assert champion_actions == []

    runner_review, runner_actions = await _review(maker, board["runner"][0])
    assert runner_review is not None
    assert runner_review.status == "resolved"
    assert runner_review.resolution == "reject"
    assert runner_review.resolved_by == _ACTOR
    assert runner_review.resolution_reason == rulings[1]["reason"]
    assert [action.action for action in runner_actions] == ["reject"]
    evidence = runner_actions[0].evidence
    assert evidence["previous_status"] == AgentStatus.SCORED.value
    assert evidence["batch_ruling"]["batch_id"] == body["batch_id"]
    assert evidence["batch_ruling"]["evidence_references"] == [
        "src/baseline.rs:1195-1207"
    ]
    assert evidence["batch_ruling"]["rulings_sha256"] == body["rulings_sha256"]

    held_review, held_actions = await _review(maker, board["held"][0])
    assert held_review is not None and held_review.resolution == "clear"
    assert [action.action for action in held_actions] == ["clear"]
    assert held_actions[0].evidence["batch_ruling"]["index"] == 2

    # Replaying the same document is idempotent for the rows that landed.
    again = await client.post(_PREVIEW, json={"rulings": rulings}, headers=_HEADERS)
    assert again.status_code == 200
    assert [item["disposition"] for item in again.json()["items"]] == [
        "already_applied",
        "already_applied",
        "already_applied",
        "stale_guard",
        "not_found",
        "invalid",
    ]
    replay = await client.post(
        _EXECUTE,
        json={
            "preview_token": again.json()["preview_token"],
            "confirmation": ATH_RULINGS_CONFIRMATION,
            "rulings": rulings,
        },
        headers=_HEADERS,
    )
    assert replay.status_code == 200
    assert replay.json()["already_applied_count"] == 3
    assert replay.json()["applied_count"] == 0


async def test_execute_refuses_items_whose_crown_outcome_moved(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    await _activate(maker)
    champion = await _seed_scored(
        maker, hotkey="5A", name="a", composite=0.9, created_at=_T0 - timedelta(hours=2)
    )
    runner = await _seed_scored(
        maker, hotkey="5B", name="b", composite=0.8, created_at=_T0 - timedelta(hours=1)
    )
    _install(app, maker)
    rulings = [
        _ruling("reject", champion[0], champion[1]),
        _ruling("reject", runner[0], runner[1]),
    ]
    preview = await client.post(_PREVIEW, json={"rulings": rulings}, headers=_HEADERS)
    assert preview.status_code == 200
    assert [i["would_change_crown"] for i in preview.json()["items"]] == [True, False]

    # A new leader lands between preview and execute: the champion ruling's
    # crown effect is no longer what the operator confirmed.
    newcomer = await _seed_scored(
        maker, hotkey="5C", name="c", composite=0.95, created_at=_T0
    )
    response = await client.post(
        _EXECUTE,
        json={
            "preview_token": preview.json()["preview_token"],
            "confirmation": ATH_RULINGS_CONFIRMATION,
            "rulings": rulings,
        },
        headers=_HEADERS,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["board_before"]["champion_agent_id"] == str(newcomer[0])
    assert body["items"][0]["status"] == "failed"
    assert "crown arithmetic moved" in body["items"][0]["message"]
    assert body["items"][1]["status"] == "applied"
    assert await _status(maker, champion[0]) == AgentStatus.SCORED.value
    assert await _status(maker, runner[0]) == AgentStatus.BANNED.value
    assert (await _review(maker, champion[0]))[0] is None


async def test_execute_fails_closed_on_foreign_actor_tampering_and_drift(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    await _activate(maker)
    agent = await _seed_scored(
        maker, hotkey="5A", name="a", composite=0.9, created_at=_T0
    )
    _install(app, maker)
    rulings = [_ruling("reject", agent[0], agent[1])]
    preview = await client.post(_PREVIEW, json={"rulings": rulings}, headers=_HEADERS)
    assert preview.status_code == 200
    token = preview.json()["preview_token"]
    execute = {
        "preview_token": token,
        "confirmation": ATH_RULINGS_CONFIRMATION,
        "rulings": rulings,
    }

    foreign = await client.post(
        _EXECUTE,
        json=execute,
        headers={**_HEADERS, "X-Admin-Actor": "someone-else@example.com"},
    )
    assert foreign.status_code == 409
    assert "another operator" in foreign.text

    tampered = token[:-1] + ("0" if token[-1] != "0" else "1")
    bad_signature = await client.post(
        _EXECUTE, json={**execute, "preview_token": tampered}, headers=_HEADERS
    )
    assert bad_signature.status_code == 409
    assert "signature" in bad_signature.text

    edited = [_ruling("reject", agent[0], agent[1], reason="A different reason")]
    drifted = await client.post(
        _EXECUTE, json={**execute, "rulings": edited}, headers=_HEADERS
    )
    assert drifted.status_code == 409
    assert "rulings changed after preview" in drifted.text

    wrong_phrase = await client.post(
        _EXECUTE, json={**execute, "confirmation": "APPLY"}, headers=_HEADERS
    )
    assert wrong_phrase.status_code == 422

    missing_rulings = await client.post(
        _EXECUTE,
        json={"preview_token": token, "confirmation": ATH_RULINGS_CONFIRMATION},
        headers=_HEADERS,
    )
    assert missing_rulings.status_code == 422
    assert "resend the same rulings" in missing_rulings.text

    assert await _status(maker, agent[0]) == AgentStatus.SCORED.value


async def test_upload_round_trip_is_operator_scoped_and_bounded(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    await _activate(maker)
    agent = await _seed_scored(
        maker, hotkey="5A", name="a", composite=0.9, created_at=_T0
    )
    store = _StubRulingsStore()
    _install(app, maker, store)

    issued = await client.post(_UPLOAD, json={}, headers=_HEADERS)
    assert issued.status_code == 200, issued.text
    upload = issued.json()
    assert upload["key"].startswith(f"ath-rulings/v1/{actor_slug(_ACTOR)}/")
    assert actor_slug(_ACTOR) == "operator-example.com"
    assert upload["key"].endswith(".json")
    assert upload["method"] == "PUT"
    assert upload["content_type"] == "application/json"
    assert upload["expires_in"] == 300
    assert upload["max_bytes"] == ATH_RULINGS_MAX_BYTES
    assert store.presigned == [(upload["key"], "application/json", 300)]

    document = {
        "source": "docs/board-review.json",
        "rulings": [_ruling("reject", agent[0], agent[1])],
    }
    store.objects[upload["key"]] = json.dumps(document).encode()
    preview = await client.post(
        _PREVIEW, json={"upload_key": upload["key"]}, headers=_HEADERS
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["upload_key"] == upload["key"]
    assert body["source"] == "docs/board-review.json"
    assert body["items"][0]["disposition"] == "ready"
    # Inline and uploaded forms of the same rulings bind the same digest.
    assert body["rulings_sha256"] == rulings_digest(
        [AdminAthRuling.model_validate(r) for r in document["rulings"]]
    )

    foreign = await client.post(
        _PREVIEW,
        json={"upload_key": upload["key"]},
        headers={**_HEADERS, "X-Admin-Actor": "someone-else@example.com"},
    )
    assert foreign.status_code == 403
    missing = await client.post(
        _PREVIEW,
        json={"upload_key": f"ath-rulings/v1/{actor_slug(_ACTOR)}/nope.json"},
        headers=_HEADERS,
    )
    assert missing.status_code == 404
    store.objects[f"ath-rulings/v1/{actor_slug(_ACTOR)}/big.json"] = b"x" * (
        ATH_RULINGS_MAX_BYTES + 1
    )
    oversized = await client.post(
        _PREVIEW,
        json={"upload_key": f"ath-rulings/v1/{actor_slug(_ACTOR)}/big.json"},
        headers=_HEADERS,
    )
    assert oversized.status_code == 413
    store.objects[f"ath-rulings/v1/{actor_slug(_ACTOR)}/bad.json"] = b"{not json"
    invalid = await client.post(
        _PREVIEW,
        json={"upload_key": f"ath-rulings/v1/{actor_slug(_ACTOR)}/bad.json"},
        headers=_HEADERS,
    )
    assert invalid.status_code == 422

    # Execute re-downloads the exact document by the key bound in the token;
    # no rulings need to be resent.
    executed = await client.post(
        _EXECUTE,
        json={
            "preview_token": body["preview_token"],
            "confirmation": ATH_RULINGS_CONFIRMATION,
        },
        headers=_HEADERS,
    )
    assert executed.status_code == 200, executed.text
    assert executed.json()["upload_key"] == upload["key"]
    assert executed.json()["applied_count"] == 1
    assert await _status(maker, agent[0]) == AgentStatus.BANNED.value
    review, actions = await _review(maker, agent[0])
    assert review is not None and review.resolution == "reject"
    assert actions[-1].evidence["batch_ruling"]["upload_key"] == upload["key"]
    assert actions[-1].evidence["batch_ruling"]["source"] == "docs/board-review.json"

    # Changing the uploaded bytes after preview fails closed.
    store.objects[upload["key"]] = json.dumps(
        {"rulings": [_ruling("clear", agent[0], agent[1], refs=())]}
    ).encode()
    stale = await client.post(
        _EXECUTE,
        json={
            "preview_token": body["preview_token"],
            "confirmation": ATH_RULINGS_CONFIRMATION,
        },
        headers=_HEADERS,
    )
    assert stale.status_code == 409
    assert "rulings changed after preview" in stale.text


async def test_upload_paths_answer_503_when_storage_is_not_configured(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    _install(app, maker, store=None)
    issued = await client.post(_UPLOAD, json={}, headers=_HEADERS)
    assert issued.status_code == 503
    preview = await client.post(
        _PREVIEW,
        json={"upload_key": f"ath-rulings/v1/{actor_slug(_ACTOR)}/x.json"},
        headers=_HEADERS,
    )
    assert preview.status_code == 503
    # Inline previews do not depend on the bucket at all.
    inline = await client.post(
        _PREVIEW,
        json={"rulings": [_ruling("reject", uuid4(), "a" * 64)]},
        headers=_HEADERS,
    )
    assert inline.status_code == 200
    assert inline.json()["items"][0]["disposition"] == "not_found"


@pytest.mark.parametrize(
    ("payload", "fragment"),
    [
        ({}, "exactly one of upload_key or rulings"),
        (
            {"upload_key": "ath-rulings/v1/x.json", "rulings": []},
            "exactly one of upload_key or rulings",
        ),
        (
            {
                "rulings": [
                    _ruling("reject", UUID(int=1), "a" * 64),
                    _ruling("clear", UUID(int=1), "a" * 64, refs=()),
                ]
            },
            "each agent_id may appear only once",
        ),
        (
            {"rulings": [_ruling("reject", UUID(int=1), "a" * 64, refs=("no line",))]},
            "evidence_references",
        ),
        (
            {"rulings": [_ruling("hold", UUID(int=1), "a" * 64)]},
            "action",
        ),
        (
            {"rulings": [_ruling("reject", UUID(int=1), "a" * 64, reason="no")]},
            "reason",
        ),
    ],
)
async def test_document_validation_fails_closed(
    app: FastAPI,
    client: httpx.AsyncClient,
    maker: async_sessionmaker[AsyncSession],
    payload: dict[str, object],
    fragment: str,
) -> None:
    _install(app, maker)
    response = await client.post(_PREVIEW, json=payload, headers=_HEADERS)
    # The request-validation envelope hides field detail; 422 is the contract.
    assert response.status_code == 422, (fragment, response.text)


async def test_preview_and_execute_require_admin_actor(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    _install(app, maker)
    headers = {"Authorization": f"Bearer {_TOKEN}"}
    payload = {"rulings": [_ruling("reject", uuid4(), "a" * 64)]}
    assert (
        await client.post(_PREVIEW, json=payload, headers=headers)
    ).status_code == 422
    assert (await client.post(_UPLOAD, json={}, headers=headers)).status_code == 422
    assert (
        await client.post(_PREVIEW, json=payload, headers={"X-Admin-Actor": _ACTOR})
    ).status_code == 401


async def test_replay_of_the_2026_09_13_board_review_rejects(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    """The five top-5 rejects replay from the review JSON with citations."""
    raw = json.loads(_FIXTURE.read_text())
    document = AdminAthRulingsDocument.model_validate(raw)
    assert document.source == "docs/sn118-top5-board-review-2026-09-13.json"
    assert len(document.rulings) == 5
    assert {ruling.action for ruling in document.rulings} == {"reject"}
    assert all(ruling.evidence_references for ruling in document.rulings)
    assert all(ruling.expected_score_count == 3 for ruling in document.rulings)
    assert {str(r.agent_id)[:8] for r in document.rulings} == {
        "c25489aa",
        "db0d4d25",
        "5064eb97",
        "db9b919d",
        "dd0783d1",
    }
    assert len(_FIXTURE.read_bytes()) <= ATH_RULINGS_MAX_BYTES

    store = _StubRulingsStore()
    _install(app, maker, store)
    key = f"ath-rulings/v1/{actor_slug(_ACTOR)}/2026-09-13/replay.json"
    store.objects[key] = _FIXTURE.read_bytes()

    # Against a board that does not carry these rows the replay is inert.
    empty = await client.post(_PREVIEW, json={"upload_key": key}, headers=_HEADERS)
    assert empty.status_code == 200, empty.text
    assert {item["disposition"] for item in empty.json()["items"]} == {"not_found"}
    assert empty.json()["ready_count"] == 0

    await _activate(maker)
    for index, ruling in enumerate(document.rulings):
        await _seed_scored(
            maker,
            hotkey=raw["rulings"][index]["miner_hotkey"],
            name=raw["rulings"][index]["agent_name"],
            composite=0.82 - index * 0.01,
            created_at=_T0 - timedelta(hours=5 - index),
            agent_id=ruling.agent_id,
            sha256=ruling.expected_sha256,
        )
    preview = await client.post(_PREVIEW, json={"upload_key": key}, headers=_HEADERS)
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["ready_count"] == 5
    assert all(item["steps"] == ["open", "reject"] for item in body["items"])
    # lets_623 is seeded as the board leader, so its reject moves the crown.
    assert [item["would_change_crown"] for item in body["items"]] == [
        True,
        False,
        False,
        False,
        False,
    ]

    executed = await client.post(
        _EXECUTE,
        json={
            "preview_token": body["preview_token"],
            "confirmation": ATH_RULINGS_CONFIRMATION,
        },
        headers=_HEADERS,
    )
    assert executed.status_code == 200, executed.text
    assert executed.json()["applied_count"] == 5
    assert executed.json()["board_after"]["champion_agent_id"] is None
    for ruling in document.rulings:
        assert await _status(maker, ruling.agent_id) == AgentStatus.BANNED.value
        review, actions = await _review(maker, ruling.agent_id)
        assert review is not None
        assert review.resolution == "reject"
        assert review.resolution_reason == ruling.reason
        assert actions[-1].evidence["batch_ruling"]["evidence_references"] == list(
            ruling.evidence_references
        )
        assert actions[-1].evidence["batch_ruling"]["source"] == document.source
