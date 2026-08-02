"""Eligibility-aware cleanup for large screener-built image archives."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_server.storage import S3StorageClient
from ditto.db.models import Agent
from ditto.db.queries.scores import list_eligible_ledger

_ABANDONED_AFTER = timedelta(days=1)
_SUPERSEDED_AFTER = timedelta(days=30)
_IMAGE_MARKER = "/screened-images/"


@dataclass(frozen=True)
class CleanupResult:
    """Counts emitted by one idempotent cleanup pass."""

    aborted_multipart: int
    deleted_orphans: int
    deleted_superseded: int


def screened_image_key(agent_id: object, upload_id: object) -> str:
    """Build the canonical immutable archive key without accepting raw paths."""
    return f"{agent_id}/screened-images/{upload_id}.tar"


async def cleanup_screened_images(
    session_maker: async_sessionmaker[AsyncSession],
    storage: S3StorageClient,
    *,
    now: datetime | None = None,
) -> CleanupResult:
    """Abort abandoned uploads and delete only DB-ineligible image objects.

    Evaluating agents and each miner's current best eligible scored agent are
    retained without an age limit for validator retries and future rescoring.
    Older non-champion images are explicitly detached from the DB first and only
    then deleted, so a validator can never fetch a screened image whose archive
    has already been removed. There is deliberately no source-build fallback for
    the current benchmark era (v3+): a detached agent is simply not re-scorable
    until it is re-screened, which is correct — a validator must never rebuild
    untrusted miner source. Completed objects never accepted by a verdict are
    removed after one day.
    """
    now = now or datetime.now(UTC)
    abandoned_before = now - _ABANDONED_AFTER
    superseded_before = now - _SUPERSEDED_AFTER

    aborted = 0
    for upload in await storage.list_multipart_uploads(prefix=""):
        initiated = upload.initiated_at
        if initiated.tzinfo is None:
            initiated = initiated.replace(tzinfo=UTC)
        if _IMAGE_MARKER in upload.key and initiated < abandoned_before:
            await storage.abort_multipart_upload(
                key=upload.key, upload_id=upload.upload_id
            )
            aborted += 1

    async with session_maker() as session, session.begin():
        champions = {
            row.agent_id
            for row in await list_eligible_ledger(
                session, include_fingerprints=False, include_details=False
            )
        }
        rows = (
            await session.scalars(
                select(Agent).where(Agent.screened_image_upload_id.is_not(None))
            )
        ).all()
        accepted_keys = {
            screened_image_key(agent.agent_id, agent.screened_image_upload_id)
            for agent in rows
        }
        superseded: list[tuple[Agent, str]] = []
        for agent in rows:
            created_at = agent.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
            if (
                agent.status == AgentStatus.EVALUATING
                or agent.agent_id in champions
                or created_at >= superseded_before
            ):
                continue
            key = screened_image_key(agent.agent_id, agent.screened_image_upload_id)
            superseded.append((agent, key))

        for agent, _key in superseded:
            agent.screened_image_sha256 = None
            agent.screened_image_size_bytes = None
            agent.screened_image_id = None
            agent.screened_image_ref = None
            agent.screened_image_upload_id = None
            agent.screened_image_verified_at = None

    for _agent, key in superseded:
        await storage.delete_object(key=key)

    deleted_orphans = 0
    for item in await storage.list_objects(prefix=""):
        modified = item.last_modified
        if modified.tzinfo is None:
            modified = modified.replace(tzinfo=UTC)
        if (
            _IMAGE_MARKER in item.key
            and item.key not in accepted_keys
            and modified < abandoned_before
        ):
            await storage.delete_object(key=item.key)
            deleted_orphans += 1

    return CleanupResult(
        aborted_multipart=aborted,
        deleted_orphans=deleted_orphans,
        deleted_superseded=len(superseded),
    )
