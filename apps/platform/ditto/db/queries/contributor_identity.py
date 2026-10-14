"""Authoritative verified-account links and versioned consent reads."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select, text

from ditto.db.models import (
    ClaSettingsRevision,
    ClaSignature,
    ClaVersion,
    MinerGithubLink,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def lock_identity_writes(
    session: AsyncSession, *, namespace: str, shared: bool = False
) -> None:
    statement = (
        "SELECT pg_advisory_xact_lock_shared(hashtextextended(:namespace, 0))"
        if shared
        else "SELECT pg_advisory_xact_lock(hashtextextended(:namespace, 0))"
    )
    await session.execute(
        text(statement),
        {"namespace": namespace},
    )


async def active_github_link(
    session: AsyncSession,
    *,
    hotkey: str | None = None,
    github_user_id: int | None = None,
) -> MinerGithubLink | None:
    if hotkey is None and github_user_id is None:
        raise ValueError("a hotkey or numeric GitHub user id is required")
    # The signed proof retains its netuid, but the active identity mapping is
    # global, matching the partial unique indexes and the write lock.
    query = select(MinerGithubLink).where(MinerGithubLink.revoked_at.is_(None))
    if hotkey is not None:
        query = query.where(MinerGithubLink.hotkey == hotkey)
    if github_user_id is not None:
        query = query.where(MinerGithubLink.github_user_id == github_user_id)
    return await session.scalar(query)


async def current_cla_version(
    session: AsyncSession, *, now: datetime | None = None
) -> ClaVersion | None:
    return await session.scalar(
        select(ClaVersion)
        .where(ClaVersion.effective_at <= (now or datetime.now(UTC)))
        .order_by(ClaVersion.version.desc())
        .limit(1)
    )


async def current_cla_settings(session: AsyncSession) -> ClaSettingsRevision:
    row = await session.scalar(
        select(ClaSettingsRevision)
        .order_by(ClaSettingsRevision.revision.desc())
        .limit(1)
    )
    if row is None:
        raise RuntimeError("CLA settings migration seed is missing")
    return row


async def current_cla_signature(
    session: AsyncSession, *, netuid: int, hotkey: str, version: ClaVersion | None
) -> ClaSignature | None:
    if version is None:
        return None
    return await session.scalar(
        select(ClaSignature)
        .where(
            ClaSignature.netuid == netuid,
            ClaSignature.hotkey == hotkey,
            ClaSignature.cla_version == version.version,
            ClaSignature.cla_sha256 == version.sha256,
        )
        .order_by(ClaSignature.signed_at.desc(), ClaSignature.signature_id)
        .limit(1)
    )
