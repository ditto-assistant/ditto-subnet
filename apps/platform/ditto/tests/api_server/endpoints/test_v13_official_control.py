"""Real Postgres/S3 controls without invented miner or screening records."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_server import canonical_starter_control as starter
from ditto.api_server.storage import create_storage_client
from ditto.db.models import (
    Agent,
    ScreenerL2ReportCanary,
    ScreenerNode,
    ScreeningAttempt,
)
from ditto.tests.api_server.endpoints.test_admin_v13_private_generation import _install
from ditto_screening_protocol.v13_private_package import V13_PRIVATE_PROFILE_SHA256

pytestmark = pytest.mark.asyncio
_BASE = "/api/v1/admin/v13-official-controls"
_SECRET = "synthetic-official-control-proof-value-for-tests"
_REGISTRAR = "registrar@omniaura.ai"
_REVIEWER = "reviewer@omniaura.ai"
_IMAGE_ID = "sha256:" + "a" * 64
_IMAGE = b"synthetic archive bytes; no executed image identity is claimed"
_SHA = hashlib.sha256(_IMAGE).hexdigest()


async def _post(
    client: httpx.AsyncClient,
    path: str,
    payload: dict,
    actor: str = _REGISTRAR,
    *,
    signed: bool = True,
) -> httpx.Response:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    now = str(int(time.time()))
    message = "\n".join((now, actor, "POST", path, hashlib.sha256(raw).hexdigest()))
    signature = hmac.new(_SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()
    headers = {
        "Authorization": "Bearer test-admin-token-at-least-32-characters",
        "Content-Type": "application/json",
        "X-Admin-Actor": actor,
    }
    if signed:
        headers["X-Backroom-Operator-Proof"] = f"{now}:{signature}"
    return await client.post(path, content=raw, headers=headers)


@pytest.fixture
async def official_control(
    app: FastAPI,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    object_storage: None,
) -> dict:
    assert object_storage is None
    _install(app, session_maker)
    monkeypatch.setenv("BACKROOM_PLATFORM_OPERATOR_PROOF_SECRET", _SECRET)
    storage = create_storage_client()
    # Refuse any accidental real-service override in a developer environment.
    assert urlparse(storage._config.endpoint_url).hostname in {
        "127.0.0.1",
        "localhost",
        "minio",
    }
    app.state.storage = storage
    now = datetime.now(UTC)
    fixture_id = uuid4()
    node_id = "official-control-" + uuid4().hex[:12]
    async with session_maker() as session, session.begin():
        session.add(
            ScreenerNode(
                environment="prod",
                node_id=node_id,
                provider="hetzner",
                provider_resource_id=node_id,
                screener_hotkey="hotkey-" + node_id,
                token_hash="f" * 64,
                token_expires_at=now + timedelta(hours=1),
                status="active",
                capacity=1,
            )
        )
        await session.flush()
        session.add(
            ScreenerL2ReportCanary(
                canary_id=fixture_id,
                request_id=uuid4(),
                source_kind="canonical_starter_fixture",
                fixture_key=starter.OBJECT_KEY,
                artifact_sha256=starter.ARCHIVE_SHA256,
                policy_version=13,
                bench_version=13,
                target_node_id=node_id,
                review_label="candidate_clear",
                run_mode="source_only",
                status="ready",
                source_attestation={
                    "release": starter.RELEASE,
                    "release_commit": starter.RELEASE_COMMIT,
                    "source_tree": starter.SOURCE_TREE,
                    "archive_sha256": starter.ARCHIVE_SHA256,
                    "archive_size_bytes": starter.ARCHIVE_BYTES,
                    "reviewed_archive_sha256": starter.ARCHIVE_SHA256,
                    "reviewed_dockerfile_sha256": starter.DOCKERFILE_SHA256,
                    "registered_by": _REGISTRAR,
                    "reviewed_by": _REVIEWER,
                    "reviewer_built_image_digest": _IMAGE_ID,
                    "reviewer_evidence_sha256": "b" * 64,
                },
            )
        )
    return {
        "request_id": str(uuid4()),
        "fixture_id": str(fixture_id),
        "image_sha256": _SHA,
        "image_id": _IMAGE_ID,
        "size_bytes": len(_IMAGE),
    }


async def _uploaded(
    client: httpx.AsyncClient, payload: dict, *, body: bytes = _IMAGE
) -> dict:
    result = await _post(client, f"{_BASE}/images", payload)
    assert result.status_code == 200, result.text
    assert result.headers["Cache-Control"] == "no-store"
    data = result.json()
    async with httpx.AsyncClient() as uploader:
        uploaded = await uploader.put(
            data["upload_url"], content=body, headers=data["required_headers"]
        )
    assert uploaded.status_code == 200, uploaded.text
    return data


def _approval() -> dict:
    return {
        "image_sha256": _SHA,
        "image_id": _IMAGE_ID,
        "profile_sha256": V13_PRIVATE_PROFILE_SHA256,
        "review_evidence_sha256": "c" * 64,
        "reason": "Independently reviewed official image for report-only verification",
    }


async def test_official_control_roundtrip_has_no_miner_or_attempt_and_no_verdict(
    official_control: dict,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    staged = await _uploaded(client, {**official_control, "future_ignored_field": True})
    control_id = staged["control"]["control_id"]
    guard = {"image_sha256": _SHA, "image_id": _IMAGE_ID}
    verified = await _post(client, f"{_BASE}/{control_id}/verify", guard)
    assert verified.status_code == 200, verified.text
    assert verified.json()["verified_at"] is not None
    assert verified.json()["image_identity_execution_verified"] is False
    approved = await _post(
        client, f"{_BASE}/{control_id}/approvals", _approval(), _REVIEWER
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "recorded_unverified"
    assert approved.json()["policy_verification_complete"] is False
    assert approved.json()["fixture_id"] == official_control["fixture_id"]
    assert approved.json()["actor"] == _REVIEWER
    retry = await _post(
        client, f"{_BASE}/{control_id}/approvals", _approval(), _REVIEWER
    )
    assert retry.json() == approved.json()
    headers = {"Authorization": "Bearer test-admin-token-at-least-32-characters"}
    listing = await client.get(f"{_BASE}/{control_id}/approvals", headers=headers)
    assert listing.json() == [approved.json()]
    assert listing.headers["Cache-Control"] == "no-store"
    assert "upload_url" not in listing.text and _SECRET not in listing.text
    async with session_maker() as session:
        assert await session.scalar(select(func.count()).select_from(Agent)) == 0
        assert (
            await session.scalar(select(func.count()).select_from(ScreeningAttempt))
            == 0
        )
    # An already verified object cannot be replaced through a reminted PUT.
    restage = await _post(client, f"{_BASE}/images", official_control)
    assert restage.status_code == 409


async def test_official_control_requires_real_proof_and_separate_actor(
    official_control: dict, client: httpx.AsyncClient
) -> None:
    unsigned = await _post(client, f"{_BASE}/images", official_control, signed=False)
    assert unsigned.status_code == 403
    wrong_registrar = await _post(
        client, f"{_BASE}/images", official_control, _REVIEWER
    )
    assert wrong_registrar.status_code == 403
    staged = await _uploaded(client, official_control)
    control_id = staged["control"]["control_id"]
    guard = {"image_sha256": _SHA, "image_id": _IMAGE_ID}
    before_verified = await _post(
        client, f"{_BASE}/{control_id}/approvals", _approval(), _REVIEWER
    )
    assert before_verified.status_code == 409
    assert (
        await _post(client, f"{_BASE}/{control_id}/verify", guard)
    ).status_code == 200
    self_approval = await _post(client, f"{_BASE}/{control_id}/approvals", _approval())
    assert self_approval.status_code == 403
    third_actor = await _post(
        client, f"{_BASE}/{control_id}/approvals", _approval(), "unrelated@omniaura.ai"
    )
    assert third_actor.status_code == 403


async def test_official_control_hashes_actual_staged_bytes(
    official_control: dict, client: httpx.AsyncClient
) -> None:
    staged = await _uploaded(client, official_control, body=b"X" * len(_IMAGE))
    ident = staged["control"]["control_id"]
    verified = await _post(
        client, f"{_BASE}/{ident}/verify", {"image_sha256": _SHA, "image_id": _IMAGE_ID}
    )
    assert verified.status_code == 409
    approved = await _post(client, f"{_BASE}/{ident}/approvals", _approval(), _REVIEWER)
    assert approved.status_code == 409


async def test_official_control_refuses_mismatched_review_and_reused_request(
    official_control: dict, client: httpx.AsyncClient
) -> None:
    different_image = await _post(
        client,
        f"{_BASE}/images",
        {**official_control, "image_id": "sha256:" + "d" * 64},
    )
    assert different_image.status_code == 409
    staged = await _post(client, f"{_BASE}/images", official_control)
    assert staged.status_code == 200, staged.text
    retry = await _post(client, f"{_BASE}/images", official_control)
    assert (
        retry.json()["control"]["control_id"] == staged.json()["control"]["control_id"]
    )
    conflict = await _post(
        client, f"{_BASE}/images", {**official_control, "image_sha256": "e" * 64}
    )
    assert conflict.status_code == 409


async def test_official_control_identity_and_approval_are_database_immutable(
    official_control: dict,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    staged = await _uploaded(client, official_control)
    ident = UUID(staged["control"]["control_id"])
    with pytest.raises(DBAPIError, match="identity is immutable"):
        async with session_maker() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE v13_official_control_images SET image_sha256=:sha "
                    "WHERE control_id=:id"
                ),
                {"sha": "f" * 64, "id": ident},
            )
    assert (
        await _post(
            client,
            f"{_BASE}/{ident}/verify",
            {"image_sha256": _SHA, "image_id": _IMAGE_ID},
        )
    ).status_code == 200
    assert (
        await _post(client, f"{_BASE}/{ident}/approvals", _approval(), _REVIEWER)
    ).status_code == 200
    for statement in (
        "UPDATE v13_official_control_images SET verified_storage_key='other' "
        "WHERE control_id=:id",
        "DELETE FROM v13_official_control_images WHERE control_id=:id",
        "UPDATE v13_official_control_approvals SET reason='changed approval' "
        "WHERE control_id=:id",
        "DELETE FROM v13_official_control_approvals WHERE control_id=:id",
    ):
        with pytest.raises(DBAPIError, match="immutable|retained audit evidence"):
            async with session_maker() as session, session.begin():
                await session.execute(text(statement), {"id": ident})
