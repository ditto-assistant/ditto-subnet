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
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.admin_ath_rulings import (
    ATH_RULINGS_CONFIRMATION,
    ATH_RULINGS_MAX_BYTES,
    AdminAthRuling,
    AdminAthRulingsDocument,
)
from ditto.api_models.continual_retest_settings import ContinualRetestSettings
from ditto.api_models.emission_eligibility import EmissionEligibilitySettings
from ditto.api_server.dependencies import get_session
from ditto.api_server.emission_eligibility import ResolvedEligibilityPolicy
from ditto.api_server.endpoints import admin_ath_rulings as rulings_mod
from ditto.api_server.endpoints import scoring as scoring_mod
from ditto.api_server.endpoints.admin_ath_rulings import actor_slug, rulings_digest
from ditto.api_server.storage.errors import ObjectNotFoundError
from ditto.db.models import (
    Agent,
    AgentStatus,
    AthReview,
    AthReviewAction,
    BenchmarkRollout,
    ContinualRetestSettingsRevision,
    EvaluationPayment,
    LedgerEpochSnapshot,
    Score,
    TicketStatus,
    ValidatorHeartbeat,
    ValidatorTicket,
)
from ditto.db.queries.benchmark_rollout import MIN_SCOREABLE_BENCH_VERSION
from ditto.db.queries.scores import list_eligible_ledger

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
_LEADERBOARD = "/api/v1/public/leaderboard"
# The public emissions block validates hotkeys as SS58; the crown-parity test
# needs real-shaped ones.
_SS58_A = "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"
_SS58_B = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
_SS58_VALIDATOR = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
# The first validator protocol that caps the dethrone band at the challenger's
# remaining headroom (scoring._DETHRONE_BAND_CLAMP_PROTOCOL).
_BAND_CLAMP_PROTOCOL = 24


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
    # Judged cumulatively: holding the 0.9 champion (also the raw leader) moves
    # the crown; with it gone the 0.8 runner IS the champion, so rejecting it
    # moves the crown again; with both gone the cleared 0.7 agent re-enters
    # above the 0.6 leader that is left. Blocked items never touch the board.
    assert [item["would_change_crown"] for item in items] == [
        True,
        True,
        True,
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
    assert body["crown_moving_count"] == 3
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
    source = "docs/sn118-board-review-2026-09-13.md"
    preview = await client.post(
        _PREVIEW, json={"rulings": rulings, "source": source}, headers=_HEADERS
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["source"] == source
    token = preview.json()["preview_token"]

    # Execute is not asked for the inline source again; the token carries it.
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
    # Each landed item was judged against the real board after the previous
    # one: the cascade the preview simulated is what execute re-read.
    assert [item["would_change_crown"] for item in body["items"][:3]] == [
        True,
        True,
        True,
    ]
    assert [item["annotated"] for item in body["items"]] == [
        True,
        True,
        True,
        False,
        False,
        False,
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
    assert annotation["source"] == source
    assert annotation["board_fingerprint"] == body["board_before"]["fingerprint"]
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
    assert evidence["batch_ruling"]["source"] == source
    # Judged against the board after the champion was held, not the pre-batch
    # snapshot: the runner was the champion it removed.
    assert evidence["batch_ruling"]["would_change_crown"] is True
    assert (
        evidence["batch_ruling"]["board_fingerprint"]
        != body["board_before"]["fingerprint"]
    )
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
    # Rejecting the champion makes the runner the champion, so the runner's
    # reject moves the crown too.
    assert [i["would_change_crown"] for i in preview.json()["items"]] == [True, True]

    # A new leader lands between preview and execute: neither ruling's crown
    # effect is what the operator confirmed -- the champion is no longer the
    # champion, and the runner would no longer inherit the crown.
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
    assert [item["status"] for item in body["items"]] == ["failed", "failed"]
    assert all("crown arithmetic moved" in i["message"] for i in body["items"])
    assert await _status(maker, champion[0]) == AgentStatus.SCORED.value
    assert await _status(maker, runner[0]) == AgentStatus.SCORED.value
    assert (await _review(maker, champion[0]))[0] is None
    assert (await _review(maker, runner[0]))[0] is None

    # Re-previewing against the new board lets the batch through: the newcomer
    # holds the crown, so neither reject touches it any more.
    again = await client.post(_PREVIEW, json={"rulings": rulings}, headers=_HEADERS)
    assert [i["would_change_crown"] for i in again.json()["items"]] == [False, False]
    retried = await client.post(
        _EXECUTE,
        json={
            "preview_token": again.json()["preview_token"],
            "confirmation": ATH_RULINGS_CONFIRMATION,
            "rulings": rulings,
        },
        headers=_HEADERS,
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["applied_count"] == 2
    assert retried.json()["board_after"]["champion_agent_id"] == str(newcomer[0])


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

    # A non-ASCII digest segment must be a signature mismatch, not a 500 from
    # compare_digest refusing mixed str input.
    issued, body_segment, _digest = token.split(".", 2)
    non_ascii = await client.post(
        _EXECUTE,
        json={**execute, "preview_token": f"{issued}.{body_segment}.{'é' * 64}"},
        headers=_HEADERS,
    )
    assert non_ascii.status_code == 409
    assert "signature" in non_ascii.text

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
    # The five rulings empty the board: each reject removes the champion the
    # previous one left behind, so every one of them moves the crown.
    assert [item["would_change_crown"] for item in body["items"]] == [True] * 5
    assert body["crown_moving_count"] == 5

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


def _heartbeat(
    hotkey: str, *, protocol_version: int, now: datetime
) -> ValidatorHeartbeat:
    """A fresh, benchmark-capable heartbeat: what fleet-protocol gates read."""
    return ValidatorHeartbeat(
        validator_hotkey=hotkey,
        software_version="1.0.0",
        protocol_version=protocol_version,
        code_digest="ab" * 32,
        state="idle",
        reported_at=now,
        seen_at=now,
        signature="cd" * 64,
        capabilities={
            "screened_images": True,
            "require_screened_image": True,
            "source_build_fallback": False,
            "full_stack_managed": True,
            "stack_updater": True,
            "sandbox_egress_restricted": True,
            "ticket_inference": False,
            "signed_score_quorum": False,
            "executor_isolation": "ephemeral_vm",
            "scorer_benchmarks": {
                "status": "fresh_verified",
                "supported_bench_versions": [MIN_SCOREABLE_BENCH_VERSION],
                "observed_at": int(now.timestamp()),
                "software_version": "1.0.0",
                "source_revision": "a" * 40,
            },
        },
    )


async def test_board_matches_the_public_crown_under_the_ceiling_band_clamp(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    """The fleet-gated band clamp flips the live crown; the tool must follow.

    A saturated 0.997012 incumbent and a later perfect 1.0 challenger: the
    uncapped decayed band demands more score than exists, so the incumbent
    keeps the crown until every live capable validator advertises protocol 24,
    when the cap makes the perfect challenger the champion. The preview's
    ``board`` must say what the public leaderboard's ``emissions`` say in both
    states -- otherwise ``would_change_crown`` lies about the visible crown.
    """
    await _activate(maker)
    saturated = await _seed_scored(
        maker,
        hotkey=_SS58_A,
        name="saturated",
        composite=0.997012,
        created_at=_T0 - timedelta(hours=2),
    )
    perfect = await _seed_scored(
        maker,
        hotkey=_SS58_B,
        name="perfect",
        composite=1.0,
        created_at=_T0 - timedelta(hours=1),
    )
    _install(app, maker)
    rulings = [_ruling("reject", perfect[0], perfect[1])]

    public = (await client.get(_LEADERBOARD)).json()
    assert public["emissions"]["ceiling_band_clamp_active"] is False
    assert public["emissions"]["champion_agent_id"] == str(saturated[0])
    assert public["emissions"]["raw_leader_agent_id"] == str(perfect[0])
    preview = await client.post(_PREVIEW, json={"rulings": rulings}, headers=_HEADERS)
    assert preview.status_code == 200, preview.text
    board = preview.json()["board"]
    assert board["champion_agent_id"] == public["emissions"]["champion_agent_id"]
    assert board["raw_leader_agent_id"] == public["emissions"]["raw_leader_agent_id"]
    assert board["champion_hotkey"] == _SS58_A
    # Rejecting the raw leader moves the crown arithmetic even while the
    # incumbent keeps the crown.
    assert preview.json()["items"][0]["would_change_crown"] is True

    # Every live capable validator now speaks protocol 24: the public board
    # caps the band and crowns the perfect challenger. So must the tool.
    now = datetime.now(UTC)
    async with maker() as session, session.begin():
        session.add(
            _heartbeat(_SS58_VALIDATOR, protocol_version=_BAND_CLAMP_PROTOCOL, now=now)
        )
    public = (await client.get(_LEADERBOARD)).json()
    assert public["emissions"]["ceiling_band_clamp_active"] is True
    assert public["emissions"]["champion_agent_id"] == str(perfect[0])
    preview = await client.post(_PREVIEW, json={"rulings": rulings}, headers=_HEADERS)
    assert preview.status_code == 200, preview.text
    board = preview.json()["board"]
    assert board["champion_agent_id"] == str(perfect[0])
    assert board["champion_agent_id"] == public["emissions"]["champion_agent_id"]
    assert board["champion_hotkey"] == _SS58_B
    assert board["champion_score"] == pytest.approx(1.0)

    # A protocol-23 straggler joining the live fleet withdraws the clamp again
    # on both surfaces; a bare project_koth(entries) could not follow this.
    async with maker() as session, session.begin():
        session.add(_heartbeat(_SS58_A, protocol_version=23, now=now))
    public = (await client.get(_LEADERBOARD)).json()
    assert public["emissions"]["ceiling_band_clamp_active"] is False
    preview = await client.post(_PREVIEW, json={"rulings": rulings}, headers=_HEADERS)
    assert preview.json()["board"]["champion_agent_id"] == str(saturated[0])
    assert (
        preview.json()["board"]["champion_agent_id"]
        == public["emissions"]["champion_agent_id"]
    )


async def _pin_for_court(
    maker: async_sessionmaker[AsyncSession],
    champion_id: UUID,
    *,
    epoch: int = 1,
    legacy: bool = False,
) -> None:
    async with maker() as session, session.begin():
        rows = await list_eligible_ledger(session, dedupe_owners=False)
        root = next(r.emission_owner_root for r in rows if r.agent_id == champion_id)
        session.add(
            LedgerEpochSnapshot(
                snapshot_id=uuid4(),
                netuid=118,
                epoch_index=epoch,
                last_epoch_block=epoch * 360,
                pinned_block=epoch * 360 + 2,
                pinned_block_hash="0x" + "ab" * 32,
                pinned_at=datetime.now(UTC),
                bench_version=MIN_SCOREABLE_BENCH_VERSION,
                entries=[],
                context={},
                champion_agent_id=champion_id,
                champion_owner_root=None if legacy else root,
                # Deliberately different: the next fold must anchor on champion.
                incumbent_agent_id=None,
                ledger_digest=f"{epoch:064x}",
            )
        )


async def _execute_court(
    client: httpx.AsyncClient, preview: dict, rulings: list[dict]
) -> dict:
    response = await client.post(
        _EXECUTE,
        json={
            "preview_token": preview["preview_token"],
            "confirmation": ATH_RULINGS_CONFIRMATION,
            "rulings": rulings,
        },
        headers=_HEADERS,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _court_incumbency(
    app: FastAPI, maker: async_sessionmaker[AsyncSession], *, enabled: bool = True
) -> None:
    app.state.session_maker = maker
    async with maker() as session, session.begin():
        session.add(
            ContinualRetestSettingsRevision(
                parent_revision=0,
                scope="*",
                settings=ContinualRetestSettings(
                    crown_incumbent_mode="fleet_ready" if enabled else "disabled",
                    statistical_band_mode="fleet_ready",
                ).model_dump(mode="json"),
                checksum="ab" * 32,
                reason="court public parity regression",
                actor=_ACTOR,
            )
        )
        session.add(
            _heartbeat(_SS58_VALIDATOR, protocol_version=29, now=datetime.now(UTC))
        )
    app.state.continual_retest_settings.invalidate()


@pytest.mark.parametrize(
    "legacy,enabled", [(False, True), (True, True), (False, False)]
)
async def test_court_follows_pinned_champion_and_refuses_pin_rotation(
    app: FastAPI,
    client: httpx.AsyncClient,
    maker: async_sessionmaker[AsyncSession],
    legacy: bool,
    enabled: bool,
) -> None:
    await _activate(maker)
    early = await _seed_scored(maker, hotkey=_SS58_A, composite=0.80, created_at=_T0)
    pinned = await _seed_scored(
        maker, hotkey=_SS58_B, composite=0.802, created_at=_T0 + timedelta(hours=1)
    )
    _install(app, maker)
    await _court_incumbency(app, maker, enabled=enabled)
    await _pin_for_court(maker, pinned[0], legacy=legacy)
    public = (await client.get(_LEADERBOARD)).json()["emissions"]
    rulings = [_ruling("reject", early[0], early[1])]
    preview = await client.post(_PREVIEW, json={"rulings": rulings}, headers=_HEADERS)
    assert preview.status_code == 200, preview.text
    assert preview.json()["board"]["champion_agent_id"] == public["champion_agent_id"]
    assert public["champion_agent_id"] == str(pinned[0] if enabled else early[0])
    await _pin_for_court(maker, pinned[0], epoch=2, legacy=legacy)
    result = await _execute_court(client, preview.json(), rulings)
    assert result["items"][0]["status"] == "failed"
    assert (await _status(maker, early[0])) == AgentStatus.SCORED.value


async def test_court_removal_promotes_a_coldkey_sibling_before_projecting(
    app: FastAPI,
    client: httpx.AsyncClient,
    maker: async_sessionmaker[AsyncSession],
) -> None:
    await _activate(maker)
    predecessor = await _seed_scored(
        maker, hotkey=_SS58_A, composite=0.80, created_at=_T0
    )
    successor = await _seed_scored(
        maker, hotkey=_SS58_B, composite=0.802, created_at=_T0 + timedelta(hours=1)
    )
    rival = await _seed_scored(
        maker,
        hotkey=_SS58_VALIDATOR,
        composite=0.799,
        created_at=_T0 + timedelta(hours=2),
    )
    async with maker() as session, session.begin():
        for index, (identity, hotkey) in enumerate(
            [(predecessor, _SS58_A), (successor, _SS58_B)]
        ):
            session.add(
                EvaluationPayment(
                    block_hash=f"0x{index + 1:064x}",
                    extrinsic_index=0,
                    agent_id=identity[0],
                    miner_hotkey=hotkey,
                    miner_coldkey="shared-court-owner",
                    amount_rao=1,
                    dest_address="5Treasury",
                    timestamp=_T0,
                )
            )
    _install(app, maker)
    await _court_incumbency(app, maker)
    await _pin_for_court(maker, successor[0])
    rulings = [
        _ruling("reject", successor[0], successor[1]),
        _ruling("reject", predecessor[0], predecessor[1]),
    ]
    preview = await client.post(_PREVIEW, json={"rulings": rulings}, headers=_HEADERS)
    assert preview.status_code == 200, preview.text
    assert [i["would_change_crown"] for i in preview.json()["items"]] == [True, True]
    result = await _execute_court(client, preview.json(), rulings)
    assert [i["status"] for i in result["items"]] == ["applied", "applied"]
    public = (await client.get(_LEADERBOARD)).json()["emissions"]
    assert (
        result["board_after"]["champion_agent_id"]
        == public["champion_agent_id"]
        == str(rival[0])
    )


@pytest.mark.parametrize(
    "previous", [AgentStatus.SCORED, AgentStatus.LIVE, AgentStatus.EVALUATING]
)
async def test_court_restore_matches_real_status_and_original_pin_anchor(
    app: FastAPI,
    client: httpx.AsyncClient,
    maker: async_sessionmaker[AsyncSession],
    previous: AgentStatus,
) -> None:
    await _activate(maker)
    held = await _seed_held(maker, hotkey=_SS58_B, composite=0.802, created_at=_T0)
    rival = await _seed_scored(
        maker, hotkey=_SS58_A, composite=0.80, created_at=_T0 + timedelta(hours=1)
    )
    async with maker() as session, session.begin():
        review = await session.scalar(
            select(AthReview).where(AthReview.agent_id == held[0])
        )
        assert review is not None
        review.original_evidence = {
            **review.original_evidence,
            "previous_status": previous.value,
        }
    _install(app, maker)
    await _court_incumbency(app, maker)
    # A held exact legacy anchor must recover when cleared, never the rival
    # that provisionally won while it was absent.
    await _pin_for_court(maker, rival[0], legacy=True)
    async with maker() as session, session.begin():
        pin = await session.scalar(select(LedgerEpochSnapshot))
        assert pin is not None
        pin.champion_agent_id = held[0]
    rulings = [_ruling("clear", held[0], held[1])]
    preview = await client.post(_PREVIEW, json={"rulings": rulings}, headers=_HEADERS)
    assert preview.status_code == 200, preview.text
    assert preview.json()["items"][0]["would_change_crown"] is (
        previous == AgentStatus.SCORED
    )
    result = await _execute_court(client, preview.json(), rulings)
    assert result["items"][0]["status"] == "applied"
    assert await _status(maker, held[0]) == previous.value
    public = (await client.get(_LEADERBOARD)).json()["emissions"]
    assert result["board_after"]["champion_agent_id"] == public["champion_agent_id"]


async def test_court_diagnostic_does_not_materialize_or_rollback(
    app: FastAPI,
    maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _activate(maker)
    member = await _seed_scored(maker, hotkey=_SS58_A, composite=0.80, created_at=_T0)
    _install(app, maker)
    app.state.session_maker = maker
    now = datetime.now(UTC)
    async with maker() as session:
        context = await scoring_mod.resolve_ledger_context(app.state, session, now=now)
        context = replace(
            context,
            policy=replace(
                context.policy,
                efficiency=replace(
                    context.policy.efficiency, enabled=True, fold_enabled=False
                ),
                reward_eligibility=ResolvedEligibilityPolicy(
                    settings=EmissionEligibilitySettings(enforcement="shadow")
                ),
            ),
        )

        async def forbidden(*_args: object, **_kwargs: object) -> None:
            pytest.fail("diagnostic invoked an authority write or caller rollback")

        monkeypatch.setattr(scoring_mod, "ensure_current_efficiency_state", forbidden)
        monkeypatch.setattr(scoring_mod, "_record_eligibility_rehearsal", forbidden)
        monkeypatch.setattr(session, "rollback", forbidden)
        statements: list[str] = []

        def inspect_sql(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)
            assert (
                not statement.lstrip()
                .upper()
                .startswith(("INSERT", "UPDATE", "DELETE", "TRUNCATE"))
            )

        engine = session.bind
        assert engine is not None
        event.listen(engine.sync_engine, "before_cursor_execute", inspect_sql)
        try:
            snapshot = await scoring_mod.materialize_ledger_snapshot(
                app.state,
                session,
                context=context,
                now=now,
                requesting_validator_hotkey=None,
                diagnostic_read_only=True,
            )
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", inspect_sql)
        assert [entry.agent_id for entry in snapshot.entries] == [member[0]]
        assert statements and session.in_transaction()
        folding = replace(
            context,
            policy=replace(
                context.policy,
                efficiency=replace(context.policy.efficiency, fold_enabled=True),
            ),
        )
        with pytest.raises(HTTPException, match="frozen efficiency reference"):
            await scoring_mod.materialize_ledger_snapshot(
                app.state,
                session,
                context=folding,
                now=now,
                requesting_validator_hotkey=None,
                diagnostic_read_only=True,
            )
        enforcing = replace(
            context,
            reward_eligibility_fleet_ready=True,
            policy=replace(
                context.policy,
                reward_eligibility=ResolvedEligibilityPolicy(
                    settings=EmissionEligibilitySettings(enforcement="enforce"),
                ),
            ),
        )
        with pytest.raises(HTTPException, match="restoration unsupported"):
            await scoring_mod.materialize_ledger_snapshot(
                app.state,
                session,
                context=enforcing,
                now=now,
                requesting_validator_hotkey=None,
                diagnostic_read_only=True,
                diagnostic_scored_agent_ids=frozenset({member[0]}),
            )
        # Dirty caller state must fail before any autoflush can escape.
        session.add(
            Agent(
                agent_id=uuid4(),
                miner_hotkey="dirty",
                sha256="ab" * 32,
                status=AgentStatus.EVALUATING,
            )
        )
        with pytest.raises(ValueError, match="clean read-only session"):
            await scoring_mod.materialize_ledger_snapshot(
                app.state,
                session,
                context=context,
                now=now,
                requesting_validator_hotkey=None,
                diagnostic_read_only=True,
            )


async def test_court_rechecks_context_after_a_ruling_lands(
    app: FastAPI,
    client: httpx.AsyncClient,
    maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _activate(maker)
    champion = await _seed_scored(maker, hotkey=_SS58_A, composite=0.90, created_at=_T0)
    runner = await _seed_scored(
        maker, hotkey=_SS58_B, composite=0.80, created_at=_T0 + timedelta(hours=1)
    )
    _install(app, maker)
    await _court_incumbency(app, maker)
    await _pin_for_court(maker, champion[0])
    rulings = [
        _ruling("reject", champion[0], champion[1]),
        _ruling("reject", runner[0], runner[1]),
    ]
    preview = await client.post(_PREVIEW, json={"rulings": rulings}, headers=_HEADERS)
    original = rulings_mod._apply_step
    rotated = False

    async def rotate(*args, **kwargs):
        nonlocal rotated
        await original(*args, **kwargs)
        if not rotated:
            rotated = True
            await _pin_for_court(maker, runner[0], epoch=2)

    monkeypatch.setattr(rulings_mod, "_apply_step", rotate)
    result = await _execute_court(client, preview.json(), rulings)
    assert [item["status"] for item in result["items"]] == ["applied", "failed"]
    assert await _status(maker, champion[0]) == AgentStatus.BANNED.value
    assert await _status(maker, runner[0]) == AgentStatus.SCORED.value


@pytest.mark.parametrize("score_count", [1, 2])
async def test_court_does_not_crown_a_full_case_run_below_validator_quorum(
    app: FastAPI,
    client: httpx.AsyncClient,
    maker: async_sessionmaker[AsyncSession],
    score_count: int,
) -> None:
    await _activate(maker)
    finalized = await _seed_scored(
        maker, hotkey=_SS58_A, composite=0.80, created_at=_T0
    )
    partial = await _seed_scored(
        maker, hotkey=_SS58_B, composite=0.99, created_at=_T0, score_count=score_count
    )
    _install(app, maker)
    public = (await client.get(_LEADERBOARD)).json()["emissions"]
    preview = await client.post(
        _PREVIEW,
        headers=_HEADERS,
        json={
            "rulings": [
                _ruling("reject", partial[0], partial[1], score_count=score_count)
            ]
        },
    )
    assert preview.status_code == 200, preview.text
    assert (
        preview.json()["board"]["champion_agent_id"]
        == public["champion_agent_id"]
        == str(finalized[0])
    )
    assert preview.json()["items"][0]["would_change_crown"] is False


async def test_court_binds_the_successor_even_when_the_change_boolean_matches(
    app: FastAPI,
    client: httpx.AsyncClient,
    maker: async_sessionmaker[AsyncSession],
) -> None:
    await _activate(maker)
    champion = await _seed_scored(maker, hotkey=_SS58_A, composite=0.90, created_at=_T0)
    await _seed_scored(
        maker, hotkey=_SS58_B, composite=0.80, created_at=_T0 + timedelta(hours=1)
    )
    third = await _seed_scored(
        maker,
        hotkey=_SS58_VALIDATOR,
        composite=0.79,
        created_at=_T0 + timedelta(hours=2),
    )
    _install(app, maker)
    rulings = [_ruling("reject", champion[0], champion[1])]
    preview = await client.post(_PREVIEW, headers=_HEADERS, json={"rulings": rulings})
    assert preview.json()["items"][0]["would_change_crown"] is True
    async with maker() as session, session.begin():
        for score in await session.scalars(
            select(Score).where(Score.agent_id == third[0])
        ):
            score.composite = 0.85
    result = await _execute_court(client, preview.json(), rulings)
    assert result["items"][0]["would_change_crown"] is True
    assert result["items"][0]["status"] == "failed"
    assert await _status(maker, champion[0]) == AgentStatus.SCORED.value


async def test_court_registration_precedes_coldkey_owner_collapse(
    app: FastAPI,
    client: httpx.AsyncClient,
    maker: async_sessionmaker[AsyncSession],
) -> None:
    await _activate(maker)
    registered = await _seed_scored(
        maker, hotkey=_SS58_A, composite=0.80, created_at=_T0
    )
    deregistered = await _seed_scored(
        maker, hotkey=_SS58_B, composite=0.90, created_at=_T0 + timedelta(hours=1)
    )
    async with maker() as session, session.begin():
        for index, (identity, hotkey) in enumerate(
            [(registered, _SS58_A), (deregistered, _SS58_B)]
        ):
            session.add(
                EvaluationPayment(
                    block_hash=f"0x{index + 1:064x}",
                    extrinsic_index=0,
                    agent_id=identity[0],
                    miner_hotkey=hotkey,
                    miner_coldkey="shared-registration-owner",
                    amount_rao=1,
                    dest_address="5Treasury",
                    timestamp=_T0,
                )
            )
    _install(app, maker)
    app.state.chain = SimpleNamespace(
        get_recent_neurons=AsyncMock(
            return_value=[SimpleNamespace(hotkey=_SS58_A, uid=1)]
        )
    )
    public = (await client.get(_LEADERBOARD)).json()["emissions"]
    preview = await client.post(
        _PREVIEW,
        headers=_HEADERS,
        json={"rulings": [_ruling("reject", deregistered[0], deregistered[1])]},
    )
    assert preview.status_code == 200, preview.text
    assert (
        preview.json()["board"]["champion_agent_id"]
        == public["champion_agent_id"]
        == str(registered[0])
    )
    assert preview.json()["items"][0]["would_change_crown"] is False


async def test_preview_mirrors_the_resolve_guards_on_a_drifted_hold(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    """A hold whose evidence or reason moved under its review is a conflict.

    ``resolve_copy_review`` refuses those with a 409 the old preview could not
    see, so a "ready" clear or reject failed only at execute.
    """
    await _activate(maker)
    evidence_drift = await _seed_held(
        maker, hotkey="5Drift", composite=0.7, created_at=_T0 - timedelta(hours=1)
    )
    reason_drift = await _seed_held(
        maker, hotkey="5Reason", composite=0.6, created_at=_T0 - timedelta(hours=1)
    )
    intact = await _seed_held(
        maker, hotkey="5Intact", composite=0.5, created_at=_T0 - timedelta(hours=1)
    )
    async with maker() as session, session.begin():
        drifted = await session.get(Agent, evidence_drift[0])
        assert drifted is not None
        # Re-matched against another row after the review was opened.
        drifted.duplicate_of = intact[0]
        reworded = await session.get(Agent, reason_drift[0])
        assert reworded is not None
        reworded.review_reason = "Re-held under a different finding"
    _install(app, maker)

    rulings = [
        _ruling("clear", evidence_drift[0], evidence_drift[1], refs=()),
        _ruling("reject", reason_drift[0], reason_drift[1]),
        _ruling("clear", intact[0], intact[1], refs=()),
    ]
    preview = await client.post(_PREVIEW, json={"rulings": rulings}, headers=_HEADERS)
    assert preview.status_code == 200, preview.text
    items = preview.json()["items"]
    assert [item["disposition"] for item in items] == ["conflict", "conflict", "ready"]
    assert items[0]["conflict_reason"] == "agent hold evidence no longer matches review"
    assert items[1]["conflict_reason"] == "agent hold reason no longer matches review"
    assert (
        items[0]["message"]
        == items[1]["message"]
        == ("resolve_ath_review would answer 409")
    )

    # And execute agrees: the same two refusals, the intact clear lands.
    executed = await client.post(
        _EXECUTE,
        json={
            "preview_token": preview.json()["preview_token"],
            "confirmation": ATH_RULINGS_CONFIRMATION,
            "rulings": rulings,
        },
        headers=_HEADERS,
    )
    assert executed.status_code == 200, executed.text
    assert [item["status"] for item in executed.json()["items"]] == [
        "failed",
        "failed",
        "applied",
    ]
    assert executed.json()["items"][0]["message"] == (
        "agent hold evidence no longer matches review"
    )
    assert await _status(maker, evidence_drift[0]) == (
        AgentStatus.ATH_PENDING_REVIEW.value
    )
    assert await _status(maker, reason_drift[0]) == (
        AgentStatus.ATH_PENDING_REVIEW.value
    )
    assert await _status(maker, intact[0]) == AgentStatus.SCORED.value


async def test_execute_reports_applied_when_only_the_annotation_fails(
    app: FastAPI,
    client: httpx.AsyncClient,
    maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ruling that landed is never reported as failed.

    The audit annotation runs after ``open`` / ``resolve`` committed; if it
    blows up the row must still say applied (with ``annotated`` false) or the
    operator re-runs a ban that already happened.
    """
    await _activate(maker)
    agent = await _seed_scored(
        maker, hotkey="5A", name="a", composite=0.9, created_at=_T0
    )
    _install(app, maker)

    async def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("annotation store unavailable")

    monkeypatch.setattr(rulings_mod, "_annotate_batch_audit", _boom)
    rulings = [_ruling("reject", agent[0], agent[1])]
    preview = await client.post(_PREVIEW, json={"rulings": rulings}, headers=_HEADERS)
    assert preview.status_code == 200, preview.text
    executed = await client.post(
        _EXECUTE,
        json={
            "preview_token": preview.json()["preview_token"],
            "confirmation": ATH_RULINGS_CONFIRMATION,
            "rulings": rulings,
        },
        headers=_HEADERS,
    )

    assert executed.status_code == 200, executed.text
    item = executed.json()["items"][0]
    assert item["status"] == "applied"
    assert item["annotated"] is False
    assert item["steps_applied"] == ["open", "reject"]
    assert item["agent_status"] == AgentStatus.BANNED.value
    assert item["message"] == (
        "ruling applied; audit annotation failed: annotation store unavailable"
    )
    assert executed.json()["applied_count"] == 1
    assert executed.json()["failed_count"] == 0
    assert executed.json()["board_after"]["champion_agent_id"] is None
    assert await _status(maker, agent[0]) == AgentStatus.BANNED.value
    review, actions = await _review(maker, agent[0])
    assert review is not None and review.resolution == "reject"
    assert "batch_ruling" not in actions[-1].evidence


async def _seed_evaluating(
    maker: async_sessionmaker[AsyncSession],
    *,
    score_count: int,
    live_ticket: bool,
) -> tuple[UUID, str]:
    """An agent below scoring quorum, optionally with one validator run in flight."""
    agent_id = uuid4()
    sha256 = agent_id.hex * 2
    now = datetime.now(UTC)
    async with maker() as session, session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey="5Evaluating",
                name="evaluating",
                sha256=sha256,
                status=AgentStatus.EVALUATING,
                screening_policy_version=13,
                created_at=_T0,
            )
        )
        session.add_all(_scores(agent_id, 0.77, score_count))
        if live_ticket:
            session.add(
                ValidatorTicket(
                    agent_id=agent_id,
                    bench_version=MIN_SCOREABLE_BENCH_VERSION,
                    validator_hotkey="validator-live",
                    slot_id="slot-1",
                    status=TicketStatus.ISSUED,
                    issued_at=now - timedelta(minutes=5),
                    deadline=now + timedelta(minutes=60),
                    attempt_count=1,
                )
            )
    return agent_id, sha256


async def _ticket_statuses(
    maker: async_sessionmaker[AsyncSession], agent_id: UUID
) -> list[TicketStatus]:
    async with maker() as session:
        return list(
            (
                await session.scalars(
                    select(ValidatorTicket.status).where(
                        ValidatorTicket.agent_id == agent_id
                    )
                )
            ).all()
        )


async def _preview_and_execute(
    client: httpx.AsyncClient, rulings: list[dict[str, object]]
) -> tuple[dict, dict]:
    preview = await client.post(_PREVIEW, json={"rulings": rulings}, headers=_HEADERS)
    assert preview.status_code == 200, preview.text
    execute = await client.post(
        _EXECUTE,
        json={
            "preview_token": preview.json()["preview_token"],
            "confirmation": ATH_RULINGS_CONFIRMATION,
            "rulings": rulings,
        },
        headers=_HEADERS,
    )
    assert execute.status_code == 200, execute.text
    return preview.json(), execute.json()


@pytest.mark.parametrize("score_count", [0, 2])
async def test_reject_below_quorum_bans_and_stops_the_remaining_runs(
    app: FastAPI,
    client: httpx.AsyncClient,
    maker: async_sessionmaker[AsyncSession],
    score_count: int,
) -> None:
    """A proven violation is rejected without waiting for, or paying for, quorum."""
    await _activate(maker)
    agent_id, sha256 = await _seed_evaluating(
        maker, score_count=score_count, live_ticket=True
    )
    _install(app, maker)

    preview, execute = await _preview_and_execute(
        client, [_ruling("reject", agent_id, sha256, score_count=score_count)]
    )

    [item] = preview["items"]
    assert item["disposition"] == "ready"
    assert item["agent_status"] == "evaluating"
    assert item["steps"] == ["open", "reject"]
    # Not on the board yet, so it cannot move the crown.
    assert item["would_change_crown"] is False
    [applied] = execute["items"]
    assert applied["status"] == "applied", applied
    assert applied["steps_applied"] == ["open", "reject"]
    assert await _status(maker, agent_id) == "banned"
    # The in-flight validator run is ended, not left to finish and bill.
    assert await _ticket_statuses(maker, agent_id) == [TicketStatus.EXPIRED]
    review, actions = await _review(maker, agent_id)
    assert review is not None and review.resolution == "reject"
    assert review.original_evidence["previous_status"] == "evaluating"
    assert review.original_evidence["score_count"] == score_count
    assert [action.action for action in actions] == ["reject"]


async def test_below_quorum_guards_and_hold_stay_strict(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    """A score landing after review is a stale guard; a bare hold still needs quorum."""
    await _activate(maker)
    agent_id, sha256 = await _seed_evaluating(maker, score_count=2, live_ticket=False)
    _install(app, maker)

    items = []
    for ruling in (
        _ruling("reject", agent_id, sha256, score_count=1),
        _ruling("open", agent_id, sha256, score_count=2, refs=()),
    ):
        response = await client.post(
            _PREVIEW, json={"rulings": [ruling]}, headers=_HEADERS
        )
        assert response.status_code == 200, response.text
        items.extend(response.json()["items"])
    stale, hold = items
    assert stale["disposition"] == "stale_guard"
    assert stale["conflict_reason"] == "score count changed"
    assert hold["disposition"] == "conflict"
    assert hold["conflict_reason"] == "agent is evaluating, not scored or live"
    # The standalone open endpoint keeps its scored/live contract too.
    opened = await client.post(
        f"/api/v1/admin/copy-reviews/{agent_id}/open",
        json={
            "expected_sha256": sha256,
            "expected_score_count": 2,
            "reason": "Manual benchmark-overfit review",
        },
        headers=_HEADERS,
    )
    assert opened.status_code == 409, opened.text
    assert await _status(maker, agent_id) == "evaluating"


async def test_reconsidered_below_quorum_reject_returns_to_the_queue(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    """Clearing a reconsidered below-quorum reject never invents a scored status."""
    await _activate(maker)
    agent_id, sha256 = await _seed_evaluating(maker, score_count=2, live_ticket=False)
    _install(app, maker)
    await _preview_and_execute(
        client, [_ruling("reject", agent_id, sha256, score_count=2)]
    )
    assert await _status(maker, agent_id) == "banned"

    reopened = await client.post(
        f"/api/v1/admin/copy-reviews/{agent_id}/open",
        json={
            "expected_sha256": sha256,
            "expected_score_count": 2,
            "reason": "Reconsider: the cited mechanism is not served",
        },
        headers=_HEADERS,
    )
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["reopened"] is True
    clear = _ruling(
        "clear",
        agent_id,
        sha256,
        score_count=2,
        reason="Reconsidered: no violation",
        refs=(),
    )
    _preview, cleared = await _preview_and_execute(client, [clear])
    assert cleared["items"][0]["status"] == "applied", cleared
    assert await _status(maker, agent_id) == "evaluating"
    # Replaying the same clear is idempotent, not a conflict.
    replay = await client.post(_PREVIEW, json={"rulings": [clear]}, headers=_HEADERS)
    assert replay.status_code == 200, replay.text
    assert replay.json()["items"][0]["disposition"] == "already_applied"


async def _seed_live(
    maker: async_sessionmaker[AsyncSession], *, hotkey: str, composite: float
) -> tuple[UUID, str]:
    agent_id, sha256 = await _seed_scored(
        maker, hotkey=hotkey, composite=composite, created_at=_T0, name="live"
    )
    async with maker() as session, session.begin():
        agent = await session.get(Agent, agent_id)
        assert agent is not None
        agent.status = AgentStatus.LIVE
    return agent_id, sha256


async def test_clear_on_the_unheld_champion_attests_without_moving_the_crown(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    """A board-review clear on a paid row never takes it off the board."""
    board = await _seed_board(maker)
    _install(app, maker)
    champion_id, champion_sha = board["champion"]
    clear = _ruling(
        "clear",
        champion_id,
        champion_sha,
        reason="Policy v13 CLEAR: served path intact; no I1-I8 or S1-S3 ground",
        refs=("app/agent.py:150",),
    )

    preview, execute = await _preview_and_execute(client, [clear])

    [item] = preview["items"]
    assert item["disposition"] == "ready"
    assert item["steps"] == ["attest"]
    assert item["would_change_crown"] is False
    assert preview["crown_moving_count"] == 0
    [applied] = execute["items"]
    assert applied["status"] == "applied", applied
    assert applied["steps_applied"] == ["attest"]
    assert applied["agent_status"] == "scored"
    assert applied["annotated"] is True
    # The champion never left the board: same crown and fold before and after.
    assert execute["board_before"]["champion_agent_id"] == str(champion_id)
    assert execute["board_after"]["champion_agent_id"] == str(champion_id)
    assert (
        execute["board_after"]["fingerprint"] == execute["board_before"]["fingerprint"]
    )
    assert await _status(maker, champion_id) == "scored"

    review, actions = await _review(maker, champion_id)
    assert review is not None
    assert review.status == "resolved"
    assert review.resolution == "clear"
    assert review.resolution_reason == clear["reason"]
    assert review.resolved_by == _ACTOR
    assert review.original_evidence["sha256"] == champion_sha
    assert review.original_evidence["score_count"] == 3
    assert review.original_evidence["previous_status"] == "scored"
    assert review.algorithm_provenance["hold_free"] is True
    assert review.algorithm_provenance["review_kind"] == "benchmark_overfit"
    assert [action.action for action in actions] == ["clear"]
    assert actions[0].evidence["hold_free"] is True
    assert actions[0].evidence["batch_ruling"]["evidence_references"] == [
        "app/agent.py:150"
    ]

    # Replaying the same clear is idempotent.
    replay = await client.post(_PREVIEW, json={"rulings": [clear]}, headers=_HEADERS)
    assert replay.status_code == 200, replay.text
    assert replay.json()["items"][0]["disposition"] == "already_applied"
    # The attestation is case law and readable like any resolved review.
    audit = await client.get(
        f"/api/v1/admin/copy-reviews/{champion_id}", headers=_HEADERS
    )
    assert audit.status_code == 200, audit.text
    assert audit.json()["status"] == "resolved"
    assert audit.json()["resolution"] == "clear"


async def test_a_later_reject_supersedes_an_attested_clear(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    """Attesting never blocks a later hold or reject of the same exact agent."""
    board = await _seed_board(maker)
    _install(app, maker)
    runner_id, runner_sha = board["runner"]
    await _preview_and_execute(
        client,
        [_ruling("clear", runner_id, runner_sha, reason="Reviewed: clean", refs=())],
    )

    preview, execute = await _preview_and_execute(
        client, [_ruling("reject", runner_id, runner_sha)]
    )

    [item] = preview["items"]
    assert item["steps"] == ["open", "reject"]
    assert execute["items"][0]["status"] == "applied", execute
    assert await _status(maker, runner_id) == "banned"
    review, actions = await _review(maker, runner_id)
    assert review is not None and review.resolution == "reject"
    assert [action.action for action in actions] == ["clear", "reopen", "reject"]


async def test_attest_keeps_a_live_agent_live(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    await _activate(maker)
    agent_id, sha256 = await _seed_live(maker, hotkey="5Live", composite=0.7)
    _install(app, maker)

    preview, execute = await _preview_and_execute(
        client, [_ruling("clear", agent_id, sha256, reason="Reviewed: clean", refs=())]
    )

    assert preview["items"][0]["steps"] == ["attest"]
    assert execute["items"][0]["agent_status"] == "live"
    assert await _status(maker, agent_id) == "live"
    review, _actions = await _review(maker, agent_id)
    assert review is not None
    assert review.original_evidence["previous_status"] == "live"


async def test_attest_refuses_what_it_must_not_resolve(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    """Below quorum or a moved guard stays a conflict; a reject is reconsidered."""
    board = await _seed_board(maker)
    evaluating = await _seed_evaluating(maker, score_count=2, live_ticket=False)
    _install(app, maker)
    runner_id, runner_sha = board["runner"]
    await _preview_and_execute(client, [_ruling("reject", runner_id, runner_sha)])
    stale_id, stale_sha = board["stale"]

    response = await client.post(
        _PREVIEW,
        json={
            "rulings": [
                _ruling(
                    "clear",
                    evaluating[0],
                    evaluating[1],
                    score_count=2,
                    reason="Reviewed: clean",
                    refs=(),
                ),
                _ruling("clear", runner_id, runner_sha, reason="Reviewed", refs=()),
                _ruling(
                    "clear",
                    stale_id,
                    stale_sha,
                    score_count=2,
                    reason="Reviewed: clean",
                    refs=(),
                ),
            ]
        },
        headers=_HEADERS,
    )

    assert response.status_code == 200, response.text
    below_quorum, banned, stale = response.json()["items"]
    assert below_quorum["disposition"] == "conflict"
    assert below_quorum["conflict_reason"] == "agent is evaluating, not held"
    # Never attested: a rejected agent is reopened, then cleared, on the record.
    assert banned["disposition"] == "ready"
    assert banned["steps"] == ["open", "clear"]
    assert stale["disposition"] == "stale_guard"
    assert stale["conflict_reason"] == "score count changed"
    assert await _status(maker, evaluating[0]) == "evaluating"
    assert await _status(maker, runner_id) == "banned"
    assert (await _review(maker, evaluating[0]))[0] is None
    assert (await _review(maker, stale_id))[0] is None


async def test_single_row_attest_route_is_guarded_and_idempotent(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    board = await _seed_board(maker)
    _install(app, maker)
    champion_id, champion_sha = board["champion"]
    held_id, held_sha = board["held"]
    url = "/api/v1/admin/copy-reviews/{}/attest"
    payload = {
        "expected_sha256": champion_sha,
        "expected_score_count": 3,
        "reason": "Reviewed: clean",
    }

    moved = await client.post(
        url.format(champion_id),
        json={**payload, "expected_score_count": 2},
        headers=_HEADERS,
    )
    assert moved.status_code == 409, moved.text
    assert "score count changed" in moved.text
    no_actor = await client.post(
        url.format(champion_id),
        json=payload,
        headers={"Authorization": _HEADERS["Authorization"]},
    )
    assert no_actor.status_code == 422, no_actor.text
    assert (await _review(maker, champion_id))[0] is None
    payer = "5ChampionOwner"
    async with maker() as session, session.begin():
        session.add(
            EvaluationPayment(
                block_hash=f"0x{1:064x}",
                extrinsic_index=0,
                agent_id=champion_id,
                miner_hotkey="5Champion",
                miner_coldkey=payer,
                amount_rao=1,
                dest_address="5Treasury",
                timestamp=_T0,
            )
        )

    first = await client.post(url.format(champion_id), json=payload, headers=_HEADERS)
    assert first.status_code == 200, first.text
    assert first.json()["idempotent"] is False
    assert first.json()["agent_status"] == "scored"
    assert first.json()["review"]["resolution"] == "clear"
    assert first.json()["review"]["miner_coldkey"] == payer
    again = await client.post(url.format(champion_id), json=payload, headers=_HEADERS)
    assert again.status_code == 200, again.text
    assert again.json()["idempotent"] is True
    # A replay answers what the first write answered, coldkey included.
    assert again.json()["review"] == first.json()["review"]
    _review_row, actions = await _review(maker, champion_id)
    assert [action.action for action in actions] == ["clear"]

    # A pending hold is resolved through resolve, never attested over.
    pending = await client.post(
        url.format(held_id),
        json={**payload, "expected_sha256": held_sha},
        headers=_HEADERS,
    )
    assert pending.status_code == 409, pending.text
    assert "ATH review is pending; resolve the hold instead" in pending.text
    assert await _status(maker, held_id) == "ath_pending_review"


async def test_a_withdrawn_reject_is_reconsidered_back_to_the_board(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    """A clear on a rejected (banned) agent reopens the review, then restores it."""
    board = await _seed_board(maker)
    _install(app, maker)
    champion_id, champion_sha = board["champion"]
    await _preview_and_execute(client, [_ruling("reject", champion_id, champion_sha)])
    assert await _status(maker, champion_id) == "banned"

    clear = _ruling(
        "clear",
        champion_id,
        champion_sha,
        reason="Reconsidered: the 6k cut alone is not a ground; no second factor",
        refs=(),
    )
    preview, execute = await _preview_and_execute(client, [clear])

    [item] = preview["items"]
    assert item["disposition"] == "ready", item
    assert item["agent_status"] == "banned"
    assert item["steps"] == ["open", "clear"]
    assert "pre-reject status" in item["message"]
    # Restoring the top composite takes the crown back, and the preview says so.
    assert item["would_change_crown"] is True
    [applied] = execute["items"]
    assert applied["status"] == "applied", applied
    assert applied["steps_applied"] == ["open", "clear"]
    assert applied["agent_status"] == "scored"
    assert execute["board_after"]["champion_agent_id"] == str(champion_id)
    assert await _status(maker, champion_id) == "scored"

    review, actions = await _review(maker, champion_id)
    assert review is not None
    assert review.status == "resolved" and review.resolution == "clear"
    assert review.resolution_reason == clear["reason"]
    # The reject stays on the record; the reconsideration is appended after it.
    assert [action.action for action in actions] == ["reject", "reopen", "clear"]
    reopen = actions[1]
    assert reopen.reason == clear["reason"]
    assert reopen.evidence["previous_status"] == "scored"
    assert reopen.evidence["sha256"] == champion_sha
    assert actions[2].evidence["batch_ruling"]["evidence_references"] == []

    replay = await client.post(_PREVIEW, json={"rulings": [clear]}, headers=_HEADERS)
    assert replay.status_code == 200, replay.text
    assert replay.json()["items"][0]["disposition"] == "already_applied"


async def test_reconsidering_a_live_reject_restores_live_and_open_only_holds(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    await _activate(maker)
    agent_id, sha256 = await _seed_live(maker, hotkey="5Live", composite=0.7)
    _install(app, maker)
    await _preview_and_execute(client, [_ruling("reject", agent_id, sha256)])
    assert await _status(maker, agent_id) == "banned"

    preview, execute = await _preview_and_execute(
        client,
        [_ruling("open", agent_id, sha256, reason="Reconsider the I2 ruling", refs=())],
    )
    assert preview["items"][0]["steps"] == ["open"]
    assert preview["items"][0]["would_change_crown"] is False
    assert execute["items"][0]["status"] == "applied", execute
    assert await _status(maker, agent_id) == "ath_pending_review"

    # The held reconsideration resolves like any hold, back to its old status.
    _preview, cleared = await _preview_and_execute(
        client,
        [_ruling("clear", agent_id, sha256, reason="Reconsidered: clean", refs=())],
    )
    assert cleared["items"][0]["steps_applied"] == ["clear"]
    assert await _status(maker, agent_id) == "live"


async def test_a_ban_without_a_rejected_review_is_not_reconsidered(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    """Only an ATH reject is reversible here; other bans and guards stay strict."""
    await _activate(maker)
    agent_id, sha256 = await _seed_scored(
        maker, hotkey="5Banned", composite=0.7, created_at=_T0
    )
    async with maker() as session, session.begin():
        agent = await session.get(Agent, agent_id)
        assert agent is not None
        agent.status = AgentStatus.BANNED
    rejected_id, rejected_sha = await _seed_scored(
        maker, hotkey="5Rejected", composite=0.6, created_at=_T0
    )
    _install(app, maker)
    await _preview_and_execute(client, [_ruling("reject", rejected_id, rejected_sha)])

    items = []
    for ruling in (
        _ruling("clear", agent_id, sha256, refs=()),
        _ruling("open", agent_id, sha256, refs=()),
        _ruling("clear", rejected_id, rejected_sha, score_count=2, refs=()),
    ):
        response = await client.post(
            _PREVIEW, json={"rulings": [ruling]}, headers=_HEADERS
        )
        assert response.status_code == 200, response.text
        items.extend(response.json()["items"])
    clear, hold, stale = items
    assert clear["disposition"] == "conflict"
    assert clear["conflict_reason"] == "agent is banned, not held"
    assert hold["disposition"] == "conflict"
    assert hold["conflict_reason"] == "agent is banned, not scored or live"
    assert stale["disposition"] == "stale_guard"
    assert await _status(maker, agent_id) == "banned"
    assert await _status(maker, rejected_id) == "banned"


@pytest.mark.parametrize("fail_on", ["post_commit", "final_read"])
async def test_court_preserves_committed_ruling_outcomes_when_board_read_fails(
    app: FastAPI,
    client: httpx.AsyncClient,
    maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    fail_on: str,
) -> None:
    board = await _seed_board(maker)
    _install(app, maker)
    agent_id, sha256 = board["champion"]
    rulings = [_ruling("reject", agent_id, sha256)]
    preview = await client.post(_PREVIEW, json={"rulings": rulings}, headers=_HEADERS)
    assert preview.status_code == 200, preview.text
    original = rulings_mod.read_board_snapshot
    calls = 0

    async def fail_after_write(*args, **kwargs):
        nonlocal calls
        calls += 1
        # Execute reads the initial board, then after the committed item,
        # then once more for the response.
        if calls >= (2 if fail_on == "post_commit" else 3):
            raise RuntimeError("private read failure must not leak")
        return await original(*args, **kwargs)

    monkeypatch.setattr(rulings_mod, "read_board_snapshot", fail_after_write)
    response = await client.post(
        _EXECUTE,
        headers=_HEADERS,
        json={
            "preview_token": preview.json()["preview_token"],
            "confirmation": ATH_RULINGS_CONFIRMATION,
            "rulings": rulings,
        },
    )
    assert response.status_code == 503, response.text
    detail = response.json()["detail"]
    assert detail["applied_count"] == 1 and detail["failed_count"] == 0
    assert detail["already_applied_count"] == 0
    assert detail["items"][0]["status"] == "applied"
    assert detail["items"][0]["steps_applied"] == ["open", "reject"]
    assert "inspect all item outcomes before any retry" in response.json()["message"]
    assert "private read failure" not in response.text
    assert await _status(maker, agent_id) == "banned"


@pytest.mark.parametrize("active_reference", [False, True])
@pytest.mark.parametrize("fail_materialization", [False, True])
async def test_court_restoration_materializes_after_commit_outside_poll_ttl(
    app: FastAPI,
    client: httpx.AsyncClient,
    maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    fail_materialization: bool,
    active_reference: bool,
) -> None:
    from ditto.api_server import efficiency as efficiency_mod
    from ditto.api_server.config import EfficiencyBonusConfig
    from ditto.api_server.efficiency_settings import EfficiencyBonusSettingsResolver

    board = await _seed_board(maker)
    _install(app, maker)
    agent_id, sha256 = board["champion"]
    await _preview_and_execute(client, [_ruling("reject", agent_id, sha256)])
    if active_reference:
        async with maker() as session, session.begin():
            for score in await session.scalars(select(Score)):
                score.details = {
                    **(score.details or {}),
                    "token_usage": {
                        "status": "complete",
                        "usage_unavailable": 0,
                        "total_tokens": 1_000 if score.agent_id == agent_id else 10_000,
                    },
                }
    config = EfficiencyBonusConfig(enabled=True, fold_enabled=True, min_cohort=2)
    app.state.session_maker = maker
    app.state.efficiency_settings = EfficiencyBonusSettingsResolver(config)
    app.state.efficiency_materializer = efficiency_mod.EfficiencyStateMaterializer(
        poll_seconds=3600,
    )
    # A current inactive reference is valid. Warm the ordinary coordinator
    # while the restoration target is still banned, then prohibit using it.
    async with maker() as session:
        await efficiency_mod.ensure_current_efficiency_state(
            app.state,
            session,
            config,
            now=datetime.now(UTC),
        )
    from ditto.db.queries.efficiency import get_bonus_rows, get_snapshot

    epoch = efficiency_mod.epoch_index_for(datetime.now(UTC), 24)
    async with maker() as session:
        snapshot = await get_snapshot(
            session, bench_version=7, run_size="full", epoch_index=epoch
        )
        assert snapshot is not None and snapshot.active is active_reference
        snapshot_id = snapshot.snapshot_id
        assert (
            await get_bonus_rows(
                session, [agent_id], bench_versions={agent_id: 7}, epoch_index=epoch
            )
            == {}
        )
    original = efficiency_mod.ensure_efficiency_state
    seen: list[str] = []

    async def materialize_after_commit(session, config, *, now):
        assert not session.in_transaction()
        seen.append(await _status(maker, agent_id))
        if fail_materialization:
            raise RuntimeError("private materialization failure")
        await original(session, config, now=now)
        assignments = await get_bonus_rows(
            session, [agent_id], bench_versions={agent_id: 7}, epoch_index=epoch
        )
        if active_reference:
            assert assignments[agent_id].snapshot_id == snapshot_id
            assert assignments[agent_id].bonus > 0
        else:
            assert assignments == {}

    async def forbidden(*_args, **_kwargs):
        pytest.fail("court used the cached polling coordinator")

    monkeypatch.setattr(
        efficiency_mod, "ensure_efficiency_state", materialize_after_commit
    )
    monkeypatch.setattr(app.state.efficiency_materializer, "ensure", forbidden)
    rulings = [
        _ruling("clear", agent_id, sha256, reason="Withdrawn ground restored", refs=())
    ]
    if fail_materialization:
        held_id, held_sha = board["held"]
        rulings.append(
            _ruling("clear", held_id, held_sha, reason="Clean source", refs=())
        )
    preview = await client.post(_PREVIEW, json={"rulings": rulings}, headers=_HEADERS)
    assert preview.status_code == 200, preview.text
    assert seen == []
    response = await client.post(
        _EXECUTE,
        headers=_HEADERS,
        json={
            "preview_token": preview.json()["preview_token"],
            "confirmation": ATH_RULINGS_CONFIRMATION,
            "rulings": rulings,
        },
    )
    assert seen == ["scored"]
    assert await _status(maker, agent_id) == "scored"
    assert response.status_code == (503 if fail_materialization else 200), response.text
    result = response.json()["detail"] if fail_materialization else response.json()
    assert result["applied_count"] == 1 and result["items"][0]["status"] == "applied"
    assert result["items"][0]["steps_applied"] == ["open", "clear"]
    assert "private materialization failure" not in response.text
    if fail_materialization:
        assert result["failed_count"] == 1
        assert result["items"][1]["status"] == "failed"
        assert result["items"][1]["steps_applied"] == []
        assert await _status(maker, board["held"][0]) == "ath_pending_review"


@pytest.mark.parametrize("changed_guard", ["context", "values"])
async def test_court_refuses_changed_efficiency_guard_with_unchanged_crown_ids(
    app: FastAPI,
    client: httpx.AsyncClient,
    maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    changed_guard: str,
) -> None:
    board = await _seed_board(maker)
    _install(app, maker)
    agent_id, sha256 = board["champion"]
    await _preview_and_execute(client, [_ruling("reject", agent_id, sha256)])
    original = rulings_mod._project_board
    rotated = False
    identities: list[tuple[UUID | None, UUID | None]] = []

    async def efficiency_guard(*args, **kwargs):
        snapshot = await original(*args, **kwargs)
        if kwargs["restored_agent_ids"]:
            identities.append(
                (snapshot.champion_agent_id, snapshot.raw_leader_agent_id)
            )
            if rotated:
                return replace(
                    snapshot,
                    **{
                        "efficiency_context"
                        if changed_guard == "context"
                        else "efficiency_values_digest": "changed",
                    },
                )
        return snapshot

    monkeypatch.setattr(rulings_mod, "_project_board", efficiency_guard)
    rulings = [
        _ruling("clear", agent_id, sha256, reason="Withdrawn ground restored", refs=())
    ]
    preview = await client.post(_PREVIEW, json={"rulings": rulings}, headers=_HEADERS)
    assert preview.status_code == 200, preview.text
    rotated = True
    result = await _execute_court(client, preview.json(), rulings)
    assert len(identities) == 2 and identities[0] == identities[1]
    assert result["items"][0]["status"] == "failed"
    assert result["items"][0]["steps_applied"] == []
    assert "crown arithmetic moved" in result["items"][0]["message"]
    assert await _status(maker, agent_id) == "banned"


@pytest.mark.parametrize("failure_stage", ["readback", "next_preview"])
async def test_court_stops_later_simulation_without_losing_committed_items(
    app: FastAPI,
    client: httpx.AsyncClient,
    maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    board = await _seed_board(maker)
    _install(app, maker)
    first_id, first_sha = board["champion"]
    next_id, next_sha = board["runner"]
    rulings = [
        _ruling("reject", first_id, first_sha),
        _ruling("reject", next_id, next_sha),
    ]
    preview = await client.post(_PREVIEW, json={"rulings": rulings}, headers=_HEADERS)
    assert preview.status_code == 200, preview.text
    original_read = rulings_mod.read_board_snapshot
    original_preview = rulings_mod._preview_ruling
    reads = 0

    async def fail_read(*args, **kwargs):
        nonlocal reads
        reads += 1
        if failure_stage == "readback" and reads == 2:
            raise RuntimeError("private board-read failure")
        return await original_read(*args, **kwargs)

    async def fail_preview(*args, **kwargs):
        if kwargs["index"] == 1:
            if failure_stage == "readback":
                pytest.fail("must not simulate a later item after known read failure")
            raise RuntimeError("private next-item failure")
        return await original_preview(*args, **kwargs)

    monkeypatch.setattr(rulings_mod, "read_board_snapshot", fail_read)
    monkeypatch.setattr(rulings_mod, "_preview_ruling", fail_preview)
    response = await client.post(
        _EXECUTE,
        headers=_HEADERS,
        json={
            "preview_token": preview.json()["preview_token"],
            "confirmation": ATH_RULINGS_CONFIRMATION,
            "rulings": rulings,
        },
    )
    assert response.status_code == 503, response.text
    detail = response.json()["detail"]
    assert detail["applied_count"] == 1 and detail["failed_count"] == 1
    assert [item["status"] for item in detail["items"]] == ["applied", "failed"]
    assert detail["items"][0]["steps_applied"] == ["open", "reject"]
    assert detail["items"][1]["steps_applied"] == []
    assert detail["items"][1]["would_change_crown"] is None
    assert "private" not in response.text
    assert await _status(maker, first_id) == "banned"
    assert await _status(maker, next_id) == "scored"


async def test_court_keeps_uncertain_outcome_when_writer_commits_then_raises(
    app: FastAPI,
    client: httpx.AsyncClient,
    maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = await _seed_board(maker)
    _install(app, maker)
    first_id, first_sha = board["champion"]
    next_id, next_sha = board["runner"]
    rulings = [
        _ruling("reject", first_id, first_sha),
        _ruling("reject", next_id, next_sha),
    ]
    preview = await client.post(_PREVIEW, json={"rulings": rulings}, headers=_HEADERS)
    assert preview.status_code == 200, preview.text
    original_apply = rulings_mod._apply_step
    original_preview = rulings_mod._preview_ruling

    async def committed_then_failed(*args, **kwargs):
        await original_apply(*args, **kwargs)
        raise RuntimeError("private postcommit coldkey-read failure")

    async def no_later_preview(*args, **kwargs):
        if kwargs["index"] == 1:
            pytest.fail("an uncertain write must stop later simulation")
        return await original_preview(*args, **kwargs)

    monkeypatch.setattr(rulings_mod, "_apply_step", committed_then_failed)
    monkeypatch.setattr(rulings_mod, "_preview_ruling", no_later_preview)
    response = await client.post(
        _EXECUTE,
        headers=_HEADERS,
        json={
            "preview_token": preview.json()["preview_token"],
            "confirmation": ATH_RULINGS_CONFIRMATION,
            "rulings": rulings,
        },
    )
    assert response.status_code == 503, response.text
    first, later = response.json()["detail"]["items"]
    assert first["status"] == "failed" and first["steps_applied"] == []
    assert first["write_outcome_unknown"] is True
    assert "inspect current state before retry" in first["message"]
    assert later["status"] == "failed" and later["steps_applied"] == []
    assert later["write_outcome_unknown"] is False
    assert later["would_change_crown"] is None
    assert "private" not in response.text
    assert await _status(maker, first_id) == "ath_pending_review"
    assert await _status(maker, next_id) == "scored"
