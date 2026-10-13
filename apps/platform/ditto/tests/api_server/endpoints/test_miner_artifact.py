"""Owner downloads must never disclose another miner's private source."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_server.dependencies import get_storage_client
from ditto.api_server.miner_session import hash_secret
from ditto.api_server.storage import (
    create_storage_client,
    parse_storage_config_from_env,
)
from ditto.api_server.storage.errors import ObjectUploadFailedError
from ditto.db.models import Agent, ArtifactFetchAudit, MinerSession
from ditto.db.queries.miner_sessions import create_session, issue_session_token
from ditto.tests.api_server.endpoints.test_miner_auth import _install

HOTKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
OTHER = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
SHA = "ab" * 32


async def seed_access(
    app, maker, *, status=AgentStatus.REJECTED, scopes="download,read", version=3
):
    _install(app, maker)
    now = datetime.now(UTC)
    agent_id = uuid4()
    token = "ditto_ms_" + uuid4().hex
    async with maker() as session, session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=HOTKEY,
                name="alpha",
                version=version,
                sha256=SHA,
                status=status,
            )
        )
        row = await create_session(
            session,
            miner_hotkey=HOTKEY,
            scopes=scopes,
            label="mcp",
            now=now,
            expires_at=now + timedelta(hours=1),
        )
        await issue_session_token(
            session,
            session_id=row.session_id,
            token_hash=hash_secret(token),
            now=now,
        )
    storage = app.state.storage
    storage.presigned_get_url = AsyncMock(return_value="https://storage.example/source")
    return agent_id, {"authorization": f"Bearer {token}"}, storage, row.session_id


@pytest.mark.parametrize("status", list(AgentStatus))
async def test_owner_download_is_status_independent_and_audited(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    status: AgentStatus,
) -> None:
    agent_id, headers, storage, session_id = await seed_access(
        app,
        session_maker,
        status=status,
    )
    before = datetime.now(UTC)
    got = await client.get(f"/api/v1/me/agents/{agent_id}/artifact", headers=headers)
    assert got.status_code == 200, got.text
    assert got.headers["cache-control"] == "private, no-store"
    body = got.json()
    assert body["agent_id"] == str(agent_id)
    assert body["sha256"] == SHA
    assert body["version"] == 3
    assert "bench_version" not in body and "finalized_at" not in body
    expires = datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00"))
    assert (
        before + timedelta(seconds=299)
        <= expires
        <= datetime.now(UTC) + timedelta(seconds=300)
    )
    storage.presigned_get_url.assert_awaited_once_with(
        key=f"{agent_id}/agent.tar.gz",
        expires_in=300,
        attachment_filename=f"ditto-agent-{agent_id}-v3.tar.gz",
    )
    async with session_maker() as session:
        rows = (await session.scalars(select(ArtifactFetchAudit))).all()
    assert len(rows) == 1
    assert rows[0].endpoint == "miner_me.my_artifact"
    assert rows[0].requester_kind == "miner"
    assert rows[0].requester_id == HOTKEY
    assert rows[0].artifact_sha256 == SHA
    assert rows[0].detail is not None
    assert rows[0].detail["session_id"] == str(session_id)
    assert rows[0].detail["version"] == 3
    # Owner access is never a public release, even for a scored/live submission.
    denied = await client.get(f"/api/v1/public/agent/{agent_id}/artifact")
    assert denied.status_code != 200
    storage.presigned_get_url.assert_awaited_once()


async def test_other_owner_and_missing_agent_have_identical_404s(
    app, client, session_maker
):
    agent_id, headers, storage, _ = await seed_access(app, session_maker)
    async with session_maker() as session, session.begin():
        agent = await session.get(Agent, agent_id)
        agent.miner_hotkey = OTHER
    replies = [
        await client.get(f"/api/v1/me/agents/{aid}/artifact", headers=headers)
        for aid in (agent_id, uuid4())
    ]
    assert [reply.status_code for reply in replies] == [404, 404]
    assert {k: v for k, v in replies[0].json().items() if k != "request_id"} == {
        k: v for k, v in replies[1].json().items() if k != "request_id"
    }
    storage.presigned_get_url.assert_not_awaited()
    async with session_maker() as session:
        assert (await session.scalars(select(ArtifactFetchAudit))).all() == []


@pytest.mark.parametrize("denial", ["missing", "expired", "revoked", "scope"])
async def test_download_requires_a_valid_read_session(
    app, client, session_maker, denial
):
    agent_id, headers, storage, sid = await seed_access(
        app,
        session_maker,
        scopes="read" if denial == "scope" else "download,read",
    )
    if denial in ("expired", "revoked"):
        async with session_maker() as session, session.begin():
            row = await session.get(MinerSession, sid)
            if denial == "expired":
                row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            else:
                row.revoked_at = datetime.now(UTC)
    got = await client.get(
        f"/api/v1/me/agents/{agent_id}/artifact",
        headers={} if denial == "missing" else headers,
    )
    assert got.status_code == (403 if denial == "scope" else 401), got.text
    storage.presigned_get_url.assert_not_awaited()


async def call_mcp(client, headers, name, arguments=None):
    reply = await client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
    )
    assert reply.status_code == 200, reply.text
    result = reply.json()["result"]
    return result["isError"], json.loads(result["content"][0]["text"])


async def test_mcp_download_and_submission_versions(app, client, session_maker):
    agent_id, headers, storage, _ = await seed_access(app, session_maker, version=7)
    got = await client.get("/api/v1/me/submissions", headers=headers)
    assert got.json()[0]["version"] == 7
    error, submissions = await call_mcp(client, headers, "list_my_submissions")
    assert not error and submissions[0]["version"] == 7
    error, body = await call_mcp(
        client, headers, "get_my_submission_artifact", {"agent_id": str(agent_id)}
    )
    assert not error and body["version"] == 7 and body["sha256"] == SHA
    storage.presigned_get_url.assert_awaited_once()
    async with session_maker() as session:
        audit = (await session.scalars(select(ArtifactFetchAudit))).one()
        assert audit.endpoint == "miner_mcp.get_my_submission_artifact"
        assert audit.requester_id == HOTKEY and audit.requester_kind == "miner"
    # MCP shares the same private ownership gate, including nonexistent UUIDs.
    async with session_maker() as session, session.begin():
        agent = await session.get(Agent, agent_id)
        agent.miner_hotkey = OTHER
    for aid in (agent_id, uuid4()):
        error, body = await call_mcp(
            client, headers, "get_my_submission_artifact", {"agent_id": str(aid)}
        )
        assert error and body == {"error": "no such agent for this hotkey"}
    error, body = await call_mcp(
        client, headers, "get_my_submission_artifact", {"agent_id": "bad"}
    )
    assert error and body == {"error": "agent_id is not a UUID"}
    storage.presigned_get_url.assert_awaited_once()


async def test_legacy_version_and_signing_failure(app, client, session_maker):
    agent_id, headers, storage, _ = await seed_access(app, session_maker, version=None)
    got = await client.get("/api/v1/me/submissions", headers=headers)
    assert got.json()[0]["version"] is None
    got = await client.get(f"/api/v1/me/agents/{agent_id}/artifact", headers=headers)
    assert got.status_code == 200 and got.json()["version"] is None
    assert (
        storage.presigned_get_url.call_args.kwargs["attachment_filename"]
        == f"ditto-agent-{agent_id}.tar.gz"
    )
    storage.presigned_get_url.side_effect = ObjectUploadFailedError(
        "private store failure"
    )
    got = await client.get(f"/api/v1/me/agents/{agent_id}/artifact", headers=headers)
    assert got.status_code == 502
    assert "private store failure" not in got.text
    async with session_maker() as session:
        assert len((await session.scalars(select(ArtifactFetchAudit))).all()) == 1


@pytest.mark.integration
async def test_owner_link_downloads_original_bytes_as_attachment(
    app, client, session_maker, object_storage
):
    del object_storage
    agent_id, headers, _, _ = await seed_access(app, session_maker)
    original = b"original submitted source archive"
    async with session_maker() as session, session.begin():
        agent = await session.get(Agent, agent_id)
        agent.sha256 = hashlib.sha256(original).hexdigest()
    async with create_storage_client(parse_storage_config_from_env()) as storage:
        await storage.put_object(key=f"{agent_id}/agent.tar.gz", body=original)

        async def get_storage():
            return storage

        app.dependency_overrides[get_storage_client] = get_storage
        got = await client.get(
            f"/api/v1/me/agents/{agent_id}/artifact", headers=headers
        )
        assert got.status_code == 200, got.text
        async with httpx.AsyncClient() as downloader:
            archive = await downloader.get(got.json()["download_url"])
        assert archive.status_code == 200
        assert archive.content == original
        assert (
            f"ditto-agent-{agent_id}-v3.tar.gz"
            in archive.headers["content-disposition"]
        )
        assert archive.headers["content-disposition"].startswith("attachment;")
        # The API digest describes the uploaded bytes, not a screened image.
        assert hashlib.sha256(archive.content).hexdigest() == got.json()["sha256"]
