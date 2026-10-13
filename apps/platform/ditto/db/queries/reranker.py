"""Queries against ``reranker_submissions`` (Season 2 shadow intake, SN-207).

This table is the whole reranker track's state. It never joins ``agents``,
so nothing here can reach the screener queue, validator allocation, the
leaderboard, anti-copy or payments.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from ditto.db.models import RerankerSubmission

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

RERANKER_STATUSES = ("accepted", "invalid", "scored", "failed")
_ARTIFACT_UNIQUE = "reranker_submissions_artifact_sha256_key"


class DuplicateRerankerArtifactError(Exception):
    """An earlier submission already holds this ``artifact_sha256``."""


async def insert_reranker_submission(
    session: AsyncSession,
    *,
    submission_id: UUID,
    miner_hotkey: str,
    miner_coldkey: str | None,
    upload_sha256: str,
    artifact_sha256: str,
    contract_sha256: str,
    size_bytes: int,
    base_model: str,
    base_model_license: str,
) -> RerankerSubmission:
    """Insert one ``accepted`` row. Caller owns the transaction.

    Raises :class:`DuplicateRerankerArtifactError` when the unique artifact
    digest is already taken (the first submitter wins). The insert runs in a
    savepoint so the caller's transaction survives the conflict.
    """
    row = RerankerSubmission(
        id=submission_id,
        miner_hotkey=miner_hotkey,
        miner_coldkey=miner_coldkey,
        upload_sha256=upload_sha256,
        artifact_sha256=artifact_sha256,
        contract_sha256=contract_sha256,
        size_bytes=size_bytes,
        base_model=base_model,
        base_model_license=base_model_license,
        status="accepted",
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError as exc:
        if _ARTIFACT_UNIQUE in str(exc.orig):
            raise DuplicateRerankerArtifactError(artifact_sha256) from exc
        raise
    await session.refresh(row)
    return row


async def get_reranker_submission(
    session: AsyncSession, *, submission_id: UUID
) -> RerankerSubmission | None:
    return await session.get(RerankerSubmission, submission_id)


async def get_reranker_submission_by_artifact(
    session: AsyncSession, *, artifact_sha256: str
) -> RerankerSubmission | None:
    return await session.scalar(
        select(RerankerSubmission).where(
            RerankerSubmission.artifact_sha256 == artifact_sha256
        )
    )


async def list_reranker_submissions(
    session: AsyncSession,
    *,
    limit: int,
    offset: int,
    miner_hotkey: str | None = None,
    status: str | None = None,
) -> list[RerankerSubmission]:
    """Newest first, with a stable id tiebreaker, optionally for one hotkey."""
    stmt = select(RerankerSubmission)
    if miner_hotkey is not None:
        stmt = stmt.where(RerankerSubmission.miner_hotkey == miner_hotkey)
    if status is not None:
        stmt = stmt.where(RerankerSubmission.status == status)
    stmt = stmt.order_by(
        RerankerSubmission.created_at.desc(), RerankerSubmission.id.asc()
    )
    return list(await session.scalars(stmt.limit(limit).offset(offset)))


async def count_reranker_submissions(
    session: AsyncSession,
    *,
    miner_hotkey: str | None = None,
    status: str | None = None,
) -> int:
    stmt = select(func.count()).select_from(RerankerSubmission)
    if miner_hotkey is not None:
        stmt = stmt.where(RerankerSubmission.miner_hotkey == miner_hotkey)
    if status is not None:
        stmt = stmt.where(RerankerSubmission.status == status)
    return int(await session.scalar(stmt) or 0)


async def count_reranker_submissions_by_status(
    session: AsyncSession,
) -> dict[str, int]:
    """Every status with its row count, zero-filled."""
    rows = await session.execute(
        select(RerankerSubmission.status, func.count()).group_by(
            RerankerSubmission.status
        )
    )
    counts = dict.fromkeys(RERANKER_STATUSES, 0)
    for status, count in rows.all():
        counts[status] = int(count)
    return counts


async def count_recent_reranker_submissions(
    session: AsyncSession,
    *,
    since: datetime,
    miner_hotkey: str,
    miner_coldkey: str | None,
) -> int:
    """Rows since ``since`` from this hotkey or, when known, its coldkey.

    The coldkey is the owner, so one owner rotating hotkeys shares one budget.
    """
    owner = RerankerSubmission.miner_hotkey == miner_hotkey
    if miner_coldkey is not None:
        owner = owner | (RerankerSubmission.miner_coldkey == miner_coldkey)
    stmt = (
        select(func.count())
        .select_from(RerankerSubmission)
        .where(owner, RerankerSubmission.created_at >= since)
    )
    return int(await session.scalar(stmt) or 0)


async def oldest_recent_reranker_submission_at(
    session: AsyncSession,
    *,
    since: datetime,
    miner_hotkey: str,
    miner_coldkey: str | None,
) -> datetime | None:
    """``created_at`` of the oldest row inside the window, for a retry hint."""
    owner = RerankerSubmission.miner_hotkey == miner_hotkey
    if miner_coldkey is not None:
        owner = owner | (RerankerSubmission.miner_coldkey == miner_coldkey)
    return await session.scalar(
        select(func.min(RerankerSubmission.created_at)).where(
            owner, RerankerSubmission.created_at >= since
        )
    )


async def lock_reranker_owner(
    session: AsyncSession, *, miner_hotkey: str, miner_coldkey: str | None
) -> None:
    """Serialize concurrent uploads from one owner inside the caller's tx.

    Without it two simultaneous uploads could both pass the window count.
    """
    key = f"reranker-upload:{miner_coldkey or miner_hotkey}"
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": key},
    )
