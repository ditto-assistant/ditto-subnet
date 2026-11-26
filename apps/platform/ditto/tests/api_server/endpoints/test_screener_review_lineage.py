"""Lineage-scoped review: claim selects the parent, the packet binds to the attempt."""

from __future__ import annotations

import gzip
import hashlib
import io
import tarfile
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_server.dependencies import get_storage_client
from ditto.db.models import (
    AgentStatus,
    ScreeningAttempt,
    ScreeningReviewEvent,
)
from ditto.screener_policy_state import effective_screening_policy_version
from ditto.tests.api_server.endpoints.test_screener import (
    _AUTH_HEADER,
    _CLAIM_URL,
    _SCREENER_HOTKEY,
    _authenticate_screener_client,  # noqa: F401  (autouse: claim admission)
    _install_chain,
    _install_db,
    _seed_agent,
)

_MAIN_V1 = "fn main() {\n    answer();\n}\n\nfn answer() {\n    model();\n}\n"
_MAIN_V2 = "fn main() {\n    answer();\n}\n\nfn answer() {\n    model_with_note();\n}\n"


def _tarball(files: dict[str, str | bytes]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        for path, body in files.items():
            data = body.encode() if isinstance(body, str) else body
            info = tarfile.TarInfo(name=path)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return gzip.compress(raw.getvalue(), mtime=0)


def _install_objects(app: FastAPI, objects: dict[str, bytes]) -> MagicMock:
    storage = MagicMock()

    async def _get_object(*, key: str, max_bytes: int) -> bytes:
        del max_bytes
        return objects[key]

    storage.get_object = AsyncMock(side_effect=_get_object)
    app.dependency_overrides[get_storage_client] = lambda: storage
    return storage


async def _lineage(
    maker: async_sessionmaker[AsyncSession],
    app: FastAPI,
    *,
    parent_files: dict[str, str | bytes],
    child_files: dict[str, str | bytes],
    parent_review: bool = True,
    review_policy: int | None = None,
) -> tuple[UUID, UUID]:
    policy = review_policy or effective_screening_policy_version()
    parent_tar, child_tar = _tarball(parent_files), _tarball(child_files)
    old = datetime.now(UTC) - timedelta(days=1)
    parent_id = await _seed_agent(
        maker,
        status=AgentStatus.REJECTED,
        name="lineage-agent",
        version=1,
        sha256=hashlib.sha256(parent_tar).hexdigest(),
        screening_policy_version=policy,
        created_at=old,
    )
    child_id = await _seed_agent(
        maker,
        status=AgentStatus.UPLOADED,
        name="lineage-agent",
        version=2,
        sha256=hashlib.sha256(child_tar).hexdigest(),
    )
    if parent_review:
        attempt_id = uuid4()
        async with maker() as session, session.begin():
            session.add(
                ScreeningAttempt(
                    attempt_id=attempt_id,
                    agent_id=parent_id,
                    artifact_sha256=hashlib.sha256(parent_tar).hexdigest(),
                    screener_hotkey=_SCREENER_HOTKEY,
                    policy_version=policy,
                    status="rejected",
                    started_at=old,
                    deadline=old + timedelta(minutes=30),
                    finished_at=old + timedelta(minutes=5),
                )
            )
            await session.flush()
            session.add(
                ScreeningReviewEvent(
                    event_id=uuid4(),
                    agent_id=parent_id,
                    attempt_id=attempt_id,
                    event_kind="automated",
                    artifact_sha256=hashlib.sha256(parent_tar).hexdigest(),
                    policy_version=policy,
                    actor=f"screener:{_SCREENER_HOTKEY}",
                    reviewer_model="openai/gpt-5.6-sol",
                    outcome="reject",
                    effective_decision="reject",
                    reason_code="l2-policy-violation",
                    reason="answer() overrides the model's graded answer",
                    prior_agent_status="screening",
                    next_agent_status="rejected",
                    evidence={
                        "finding": {
                            "risk_level": "high",
                            "categories": ["answer_override"],
                            "summary": "host writes the graded slot",
                            "evidence": [
                                {
                                    "path": "src/main.rs",
                                    "line": 6,
                                    "category": "answer_override",
                                }
                            ],
                        },
                        "review_notes": [
                            {
                                "kind": "concern",
                                "category": "answer_override",
                                "path": "src/main.rs",
                                "line": 6,
                                "summary": "answer() bypasses the model",
                                "stage": "l2",
                            }
                        ],
                    },
                    created_at=old + timedelta(minutes=5),
                )
            )
    _install_objects(
        app,
        {
            f"{parent_id}/agent.tar.gz": parent_tar,
            f"{child_id}/agent.tar.gz": child_tar,
        },
    )
    return parent_id, child_id


@pytest.mark.asyncio
class TestReviewLineage:
    async def test_a_parent_from_another_lineage_is_refused(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # A parent id that is not an earlier version of this hotkey's agent
        # would put a foreign artifact and unrelated findings in the packet.
        _install_db(app, session_maker)
        _install_chain(app)
        _, child_id = await _lineage(
            session_maker,
            app,
            parent_files={"src/main.rs": _MAIN_V1},
            child_files={"src/main.rs": _MAIN_V2},
        )
        stranger = await _seed_agent(
            session_maker,
            status=AgentStatus.REJECTED,
            name="lineage-agent",
            version=1,
            miner_hotkey="5Stranger" + "x" * 39,
            sha256="ab" * 32,
            created_at=datetime.now(UTC) - timedelta(days=2),
        )
        item = (await client.post(_CLAIM_URL, headers=_AUTH_HEADER)).json()["items"][0]
        async with session_maker() as session, session.begin():
            attempt = await session.get(ScreeningAttempt, UUID(item["attempt_id"]))
            assert attempt is not None
            attempt.lineage_parent_agent_id = stranger
        response = await client.post(
            f"/api/v1/screener/agent/{child_id}/review-lineage",
            headers=_AUTH_HEADER,
            params={"attempt_id": item["attempt_id"]},
        )
        # The error envelope keeps the specific reason in the log; the worker
        # only needs the 409 to fall back to an ordinary full review.
        assert response.status_code == 409, response.text

    async def test_a_first_version_has_no_lineage(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        agent_id = await _seed_agent(
            session_maker, status=AgentStatus.UPLOADED, name="solo", version=1
        )
        item = (await client.post(_CLAIM_URL, headers=_AUTH_HEADER)).json()["items"][0]
        assert item["agent_id"] == str(agent_id)
        assert item["lineage_parent_agent_id"] is None
        response = await client.post(
            f"/api/v1/screener/agent/{agent_id}/review-lineage",
            headers=_AUTH_HEADER,
            params={"attempt_id": item["attempt_id"]},
        )
        assert response.status_code == 409, response.text
