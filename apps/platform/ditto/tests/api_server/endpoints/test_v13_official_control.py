"""Real Postgres/S3 controls without invented miner or screening records."""

from __future__ import annotations

import hashlib
import hmac
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_server import canonical_starter_control as starter
from ditto.api_server.storage import create_storage_client
from ditto.db.models import (
    ScreenerL2ReportCanary,
    ScreenerNode,
    V13OfficialControlImage,
)
from ditto.tests.api_server.endpoints.test_admin_v13_private_generation import _install

pytestmark = pytest.mark.asyncio
_BASE = "/api/v1/admin/v13-official-controls"
_SECRET = "synthetic-official-control-proof-value-for-tests"
_REGISTRAR = "registrar@omniaura.ai"
_REVIEWER = "reviewer@omniaura.ai"
_IMAGE_ID = "sha256:" + "a" * 64
_IMAGE = b"synthetic archive bytes; no executed image identity is claimed"
_SHA = hashlib.sha256(_IMAGE).hexdigest()


async def _artifact(
    client: httpx.AsyncClient,
    control_id: str,
    actor: str = _REVIEWER,
    *,
    signed: bool = True,
) -> httpx.Response:
    path = f"{_BASE}/{control_id}/artifact"
    now = str(int(time.time()))
    message = "\n".join((now, actor, "GET", path, hashlib.sha256(b"").hexdigest()))
    signature = hmac.new(_SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()
    headers = {
        "Authorization": "Bearer test-admin-token-at-least-32-characters",
        "X-Admin-Actor": actor,
    }
    if signed:
        headers["X-Backroom-Operator-Proof"] = f"{now}:{signature}"
    return await client.get(path, headers=headers)


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
    control_id = uuid4()
    key = f"synthetic-official-history/{control_id}.tar"
    metadata = {
        "control-id": str(control_id),
        "fixture-id": str(fixture_id),
        "artifact-sha256": starter.ARCHIVE_SHA256,
        "image-sha256": _SHA,
        "image-id": _IMAGE_ID,
    }
    # Seed historical state directly; the retired HTTP writes cannot create it.
    async with session_maker() as session, session.begin():
        session.add(
            V13OfficialControlImage(
                control_id=control_id,
                request_id=uuid4(),
                fixture_id=fixture_id,
                artifact_sha256=starter.ARCHIVE_SHA256,
                image_sha256=_SHA,
                image_id=_IMAGE_ID,
                size_bytes=len(_IMAGE),
                producer=_REGISTRAR,
                staging_id=uuid4(),
                verified_storage_key=key,
                verified_at=now,
                created_at=now,
            )
        )
    url = await storage.presigned_put_url(
        key=key,
        size_bytes=len(_IMAGE),
        metadata=metadata,
        expires_in=60,
    )
    async with httpx.AsyncClient() as uploader:
        result = await uploader.put(
            url,
            content=_IMAGE,
            headers={
                "Content-Type": "application/x-tar",
                **{f"x-amz-meta-{key}": value for key, value in metadata.items()},
            },
        )
    assert result.status_code == 200, result.text
    return {"control_id": str(control_id)}


@pytest.mark.parametrize("tamper", ["bytes", "metadata", "unavailable"])
async def test_artifact_download_rechecks_sealed_object(
    official_control: dict,
    client: httpx.AsyncClient,
    app: FastAPI,
    session_maker: async_sessionmaker[AsyncSession],
    tamper: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ident = official_control["control_id"]
    async with session_maker() as session:
        row = await session.get(V13OfficialControlImage, UUID(ident))
        assert row is not None and row.verified_storage_key is not None
        key = row.verified_storage_key
    if tamper == "unavailable":

        async def unavailable(**_kwargs):
            raise OSError("synthetic storage outage")

        monkeypatch.setattr(app.state.storage, "head_object", unavailable)
    else:
        head = await app.state.storage.head_object(key=key)
        metadata = head.metadata if tamper == "bytes" else {"changed": "true"}
        url = await app.state.storage.presigned_put_url(
            key=key,
            size_bytes=len(_IMAGE),
            metadata=metadata,
            expires_in=60,
        )
        async with httpx.AsyncClient() as uploader:
            changed = await uploader.put(
                url,
                content=b"X" * len(_IMAGE) if tamper == "bytes" else _IMAGE,
                headers={
                    "Content-Type": "application/x-tar",
                    **{f"x-amz-meta-{key}": value for key, value in metadata.items()},
                },
            )
        assert changed.status_code == 200, changed.text
    download = await _artifact(client, ident)
    assert download.status_code == (503 if tamper == "unavailable" else 409)
    assert "download_url" not in download.text


async def test_historical_official_image_remains_readable_and_authenticated(
    official_control: dict,
    client: httpx.AsyncClient,
) -> None:
    ident = official_control["control_id"]
    headers = {"Authorization": "Bearer test-admin-token-at-least-32-characters"}
    assert (await client.get(_BASE)).status_code == 401
    listed = await client.get(_BASE, headers=headers)
    assert listed.status_code == 200
    assert listed.headers["Cache-Control"] == "no-store"
    assert listed.json()[0]["control_id"] == ident
    assert "storage_key" not in listed.text
    assert listed.json()[0]["policy_verification_complete"] is False
    assert (await _artifact(client, ident, signed=False)).status_code == 403
    assert (await _artifact(client, ident, "unrelated@omniaura.ai")).status_code == 403
    download = await _artifact(client, ident)
    assert download.status_code == 200, download.text
    assert download.headers["Cache-Control"] == "no-store"
    async with httpx.AsyncClient() as reader:
        result = await reader.get(download.json()["download_url"])
    assert result.content == _IMAGE


async def test_historical_official_image_identity_is_still_database_immutable(
    official_control: dict,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    for statement in (
        "UPDATE v13_official_control_images SET image_sha256=:sha WHERE control_id=:id",
        "UPDATE v13_official_control_images SET verified_storage_key='other' "
        "WHERE control_id=:id",
        "DELETE FROM v13_official_control_images WHERE control_id=:id",
    ):
        with pytest.raises(DBAPIError, match="immutable|retained audit evidence"):
            async with session_maker() as session, session.begin():
                await session.execute(
                    text(statement),
                    {"id": UUID(official_control["control_id"]), "sha": "f" * 64},
                )
