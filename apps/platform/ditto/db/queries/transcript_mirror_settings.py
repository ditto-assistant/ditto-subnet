"""Effective public-transcript mirror policy. Default off."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.db.models import TranscriptMirrorSettingsRevision


async def latest_transcript_mirror_settings(
    session: AsyncSession,
) -> TranscriptMirrorSettingsRevision | None:
    return await session.scalar(
        select(TranscriptMirrorSettingsRevision)
        .order_by(TranscriptMirrorSettingsRevision.revision.desc())
        .limit(1)
    )


async def transcript_mirror_enabled(session: AsyncSession) -> bool:
    latest = await latest_transcript_mirror_settings(session)
    return bool(latest.enabled) if latest is not None else False
