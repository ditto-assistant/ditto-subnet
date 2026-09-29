"""Fail-closed boundaries for the public, non-miner source fixture."""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import tarfile
import time
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.l2_report_canary import (
    CanonicalFixtureRegisterRequest,
    CanonicalFixtureReviewRequest,
    CanonicalFixtureScheduleRequest,
)
from ditto.api_server import canonical_starter_control as starter
from ditto.api_server.endpoints import l2_report_canary as endpoints
from ditto.api_server.operator_proof import require_operator_proof
from ditto.api_server.storage import S3StorageClient
from ditto.api_server.storage.models import VerifiedObject
from ditto.db.models import ScreenerHeartbeat, ScreenerNode

SECRET = "fixture-proof-test-secret-with-32-bytes-minimum"
BASE = "/api/v1/admin/screener-l2-report-canaries/fixture"


def _request(
    path: str, body: dict, actor: str, *, proof_actor: str | None = None
) -> Request:
    raw = json.dumps(body, separators=(",", ":")).encode()
    now = int(time.time())
    message = "\n".join(
        (str(now), proof_actor or actor, "POST", path, hashlib.sha256(raw).hexdigest())
    )
    signature = hmac.new(SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()
    headers = [
        (b"x-admin-actor", actor.encode()),
        (b"x-backroom-operator-proof", f"{now}:{signature}".encode()),
    ]

    async def receive() -> dict:
        return {"type": "http.request", "body": raw, "more_body": False}

    return Request(
        {"type": "http", "method": "POST", "path": path, "headers": headers},
        receive,
    )


def test_archive_is_exact_released_regular_file_payload() -> None:
    data = starter.archive_bytes()
    assert len(data) == starter.ARCHIVE_BYTES
    assert hashlib.sha256(data).hexdigest() == starter.ARCHIVE_SHA256
    with tarfile.open(fileobj=BytesIO(data), mode="r:gz") as archive:
        members = archive.getmembers()
        names = {member.name for member in members}
        assert "Dockerfile" in names
        assert "Cargo.toml" in names
        assert all(member.isfile() or member.isdir() for member in members)
        assert not any(name.startswith((".agents/", ".claude/")) for name in names)


def test_rollback_refuses_to_drop_registered_fixture_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = (
        Path(__file__).resolve().parents[4]
        / "alembic/versions/2026_09_29_add_public_source_control_fixture.py"
    )
    spec = importlib.util.spec_from_file_location("fixture_migration", migration)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    bind = SimpleNamespace(execute=lambda _query: SimpleNamespace(first=lambda: (1,)))
    monkeypatch.setattr(module.op, "get_bind", lambda: bind)
    with pytest.raises(RuntimeError, match="preserve its report and operator audit"):
        module.downgrade()


@pytest.mark.asyncio
async def test_operator_proof_rejects_forged_actor_and_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BACKROOM_PLATFORM_OPERATOR_PROOF_SECRET", SECRET)
    path = f"{BASE}/register"
    body = {
        "request_id": str(uuid4()),
        "target_node_id": "node",
        "confirm_report_only": True,
    }
    valid = _request(path, body, "registrar@omniaura.ai")
    assert await require_operator_proof(valid) == "registrar@omniaura.ai"
    forged = _request(
        path, body, "forged@omniaura.ai", proof_actor="registrar@omniaura.ai"
    )
    with pytest.raises(HTTPException) as error:
        await require_operator_proof(forged)
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_fixture_capability_uses_signed_protocol_not_version_string(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    node_id = f"fixture-cap-{uuid4().hex[:12]}"
    hotkey = f"hotkey-{node_id}"
    instance = f"{node_id}-worker-1"
    async with session_maker() as session, session.begin():
        node = ScreenerNode(
            environment="prod",
            node_id=node_id,
            provider="hetzner",
            provider_resource_id=node_id,
            screener_hotkey=hotkey,
            token_hash="f" * 64,
            token_expires_at=now + timedelta(hours=1),
            status="active",
            capacity=1,
        )
        session.add(node)
        session.add(
            ScreenerHeartbeat(
                screener_hotkey=hotkey,
                instance_id=instance,
                software_version="999.0.0",
                protocol_version=7,
                policy_version=13,
                state="polling",
                reported_at=now,
                seen_at=now,
                signature="f" * 128,
                system_metrics={
                    "release": {
                        "builtin_policy_version": 13,
                        "version": "999.0.0",
                        "source_fixture_v1": True,
                    }
                },
            )
        )
    async with session_maker() as session:
        node_before = await session.get(ScreenerNode, node_id)
        assert node_before is not None
        assert not await endpoints._fixture_worker_ready(
            session, node=node_before, now=now, instance_id=instance
        )
    async with session_maker() as session, session.begin():
        heartbeat = await session.get(ScreenerHeartbeat, (hotkey, instance))
        assert heartbeat is not None
        heartbeat.protocol_version = 8
    async with session_maker() as session:
        node_after = await session.get(ScreenerNode, node_id)
        assert node_after is not None
        assert await endpoints._fixture_worker_ready(
            session, node=node_after, now=now, instance_id=instance
        )


@pytest.mark.asyncio
async def test_fixture_requires_distinct_review_and_one_schedule(
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BACKROOM_PLATFORM_OPERATOR_PROOF_SECRET", SECRET)
    monkeypatch.setattr(
        endpoints, "_fixture_worker_ready", AsyncMock(return_value=True)
    )
    now = datetime.now(UTC)
    node_id = f"fixture-test-{uuid4().hex[:12]}"
    async with session_maker() as session, session.begin():
        session.add(
            ScreenerNode(
                environment="prod",
                node_id=node_id,
                provider="hetzner",
                provider_resource_id=node_id,
                screener_hotkey=f"hotkey-{node_id}",
                token_hash="f" * 64,
                token_expires_at=now + timedelta(hours=1),
                status="active",
                capacity=1,
            )
        )
    storage = cast(
        S3StorageClient,
        SimpleNamespace(
            put_object=AsyncMock(),
            verify_object_sha256=AsyncMock(
                return_value=VerifiedObject(
                    size_bytes=starter.ARCHIVE_BYTES, sha256=starter.ARCHIVE_SHA256
                )
            ),
        ),
    )
    register_body = {
        "request_id": str(uuid4()),
        "target_node_id": node_id,
        "confirm_report_only": True,
    }
    async with session_maker() as session:
        registered = await endpoints.register_canonical_fixture(
            CanonicalFixtureRegisterRequest.model_validate(register_body),
            _request(f"{BASE}/register", register_body, "registrar@omniaura.ai"),
            None,
            session,
            storage,
        )
    assert registered.status == "awaiting_review"
    assert registered.agent_id is None and registered.source_attempt_id is None
    assert registered.source_attestation is not None
    assert registered.source_attestation["source_tree"] == starter.SOURCE_TREE
    review_body = {
        "reviewer_evidence_sha256": "a" * 64,
        "reviewed_archive_sha256": starter.ARCHIVE_SHA256,
        "reviewed_dockerfile_sha256": starter.DOCKERFILE_SHA256,
        "built_image_digest": "sha256:" + "b" * 64,
        "reviewer_evidence_url": "https://github.com/ditto-assistant/ditto-subnet/issues/2515#issuecomment-1",
        "confirm_candidate_review": True,
    }
    path = f"{BASE}/{registered.canary_id}/review"
    async with session_maker() as session:
        with pytest.raises(HTTPException, match="independent reviewer"):
            await endpoints.review_canonical_fixture(
                registered.canary_id,
                CanonicalFixtureReviewRequest.model_validate(review_body),
                _request(path, review_body, "registrar@omniaura.ai"),
                None,
                session,
                storage,
            )
    async with session_maker() as session:
        reviewed = await endpoints.review_canonical_fixture(
            registered.canary_id,
            CanonicalFixtureReviewRequest.model_validate(review_body),
            _request(path, review_body, "reviewer@omniaura.ai"),
            None,
            session,
            storage,
        )
    assert reviewed.status == "ready"
    schedule_body = {"confirm_report_only": True}
    schedule_path = f"{BASE}/{registered.canary_id}/schedule"
    async with session_maker() as session:
        with pytest.raises(HTTPException, match="reviewer cannot schedule"):
            await endpoints.schedule_canonical_fixture(
                registered.canary_id,
                CanonicalFixtureScheduleRequest.model_validate(schedule_body),
                _request(schedule_path, schedule_body, "reviewer@omniaura.ai"),
                None,
                session,
                storage,
            )
    async with session_maker() as session:
        queued = await endpoints.schedule_canonical_fixture(
            registered.canary_id,
            CanonicalFixtureScheduleRequest.model_validate(schedule_body),
            _request(schedule_path, schedule_body, "registrar@omniaura.ai"),
            None,
            session,
            storage,
        )
    assert queued.status == "queued"
    assert queued.review_label == "candidate_clear"
    assert queued.agent_id is None and queued.source_attempt_id is None
