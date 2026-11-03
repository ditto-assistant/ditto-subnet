"""Owner-only source downloads shared by the miner console and MCP."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.miner_session import MinerArtifactDownload
from ditto.api_server.artifact_audit import client_ip, request_detail
from ditto.api_server.endpoints.miner_auth import require_scope, resolve_miner_session
from ditto.api_server.storage import S3StorageClient
from ditto.api_server.storage.errors import ObjectUploadFailedError
from ditto.db.models import Agent
from ditto.db.queries.artifact_fetch_audit import (
    ENDPOINT_MINER_ARTIFACT,
    record_artifact_fetch,
)

DOWNLOAD_TTL_SECONDS = 300


async def prepare_owned_artifact_download(
    request: Request,
    session: AsyncSession,
    storage: S3StorageClient,
    *,
    agent_id: UUID,
    endpoint: str = ENDPOINT_MINER_ARTIFACT,
) -> MinerArtifactDownload:
    async with session.begin():
        row, _token = await resolve_miner_session(request, session)
        require_scope(row, "download")
        agent = await session.get(Agent, agent_id)
        if agent is None or agent.miner_hotkey != row.miner_hotkey:
            raise HTTPException(status_code=404, detail="no such agent for this hotkey")
        hotkey, session_id = row.miner_hotkey, row.session_id
        version, sha256 = agent.version, agent.sha256

    # Ownership is sufficient regardless of scoring status or public embargo.
    # Uploads store the original archive under this key before creating Agent.
    now = datetime.now(UTC)
    revision = f"-v{version}" if version is not None else ""
    try:
        download_url = await storage.presigned_get_url(
            key=f"{agent_id}/agent.tar.gz",
            expires_in=DOWNLOAD_TTL_SECONDS,
            attachment_filename=f"ditto-agent-{agent_id}{revision}.tar.gz",
        )
    except ObjectUploadFailedError as exc:
        raise HTTPException(
            status_code=502, detail="Could not prepare the source download. Try again."
        ) from exc
    await record_artifact_fetch(
        session,
        agent_id=agent_id,
        endpoint=endpoint,
        requester_kind="miner",
        requester_id=hotkey,
        artifact_sha256=sha256,
        source_ip=client_ip(request),
        detail=request_detail(
            request,
            session_id=str(session_id),
            version=version,
            request_id=getattr(request.state, "request_id", None),
        ),
    )
    return MinerArtifactDownload(
        agent_id=agent_id,
        version=version,
        sha256=sha256,
        download_url=download_url,
        expires_at=now + timedelta(seconds=DOWNLOAD_TTL_SECONDS),
    )
