"""Authoritative verified-account links and versioned consent reads."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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


GITHUB_LINK_ATTEMPT_RETENTION = timedelta(days=30)
GITHUB_LINK_ATTEMPT_PRUNE_BATCH = 100


async def prune_github_link_attempts(
    session: AsyncSession,
    *,
    now: datetime,
    limit: int = GITHUB_LINK_ATTEMPT_PRUNE_BATCH,
) -> None:
    """Retire abandoned link attempts in bounded, lock-skipping batches.

    An attempt whose callback never arrives would otherwise keep its PKCE
    verifier until something reads it again. Expire those and clear their
    secrets, then delete unlinked attempts past retention. Linked attempts are
    the provenance of a link and are never deleted.
    """
    await session.execute(
        text(
            "WITH stale AS ("
            " SELECT attempt_id FROM miner_github_link_attempts"
            " WHERE status IN ('pending','identity_verified','accepted')"
            " AND expires_at <= :now ORDER BY expires_at LIMIT :limit"
            " FOR UPDATE SKIP LOCKED)"
            " UPDATE miner_github_link_attempts AS attempt"
            " SET status = 'expired', code_verifier = NULL, accept_token_hash = NULL"
            " FROM stale WHERE attempt.attempt_id = stale.attempt_id"
        ),
        {"now": now, "limit": limit},
    )
    await session.execute(
        text(
            "DELETE FROM miner_github_link_attempts WHERE attempt_id IN ("
            " SELECT attempt_id FROM miner_github_link_attempts"
            " WHERE status IN ('failed','expired') AND created_at < :cutoff"
            " ORDER BY created_at LIMIT :limit FOR UPDATE SKIP LOCKED)"
        ),
        {"cutoff": now - GITHUB_LINK_ATTEMPT_RETENTION, "limit": limit},
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
