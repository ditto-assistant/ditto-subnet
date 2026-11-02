"""Reads and mutations for Feedback Track contributions (identity plumbing only)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from ditto.db.models import FeedbackTrackContribution, MinerDittoLink

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def upsert_contribution(
    session: AsyncSession,
    *,
    ditto_user_id: str,
    source: str,
    external_ref: str,
    kind: str,
    weight: Decimal | None,
    note: str | None,
    recorded_by: str,
    now: datetime,
) -> tuple[FeedbackTrackContribution, bool]:
    """Record one contribution; replaying the same (source, ref, kind) updates it.

    Returns the row and whether it was newly created, so callers can report
    idempotent replays honestly instead of counting them twice.
    """
    insert_stmt = insert(FeedbackTrackContribution).values(
        contribution_id=uuid4(),
        ditto_user_id=ditto_user_id,
        source=source,
        external_ref=external_ref,
        kind=kind,
        weight=weight,
        note=note,
        recorded_by=recorded_by,
        recorded_at=now,
        updated_at=now,
    )
    upsert_stmt = insert_stmt.on_conflict_do_update(
        constraint="feedback_track_dedupe_key",
        set_={
            "ditto_user_id": insert_stmt.excluded.ditto_user_id,
            "weight": insert_stmt.excluded.weight,
            "note": insert_stmt.excluded.note,
            "recorded_by": insert_stmt.excluded.recorded_by,
            "updated_at": insert_stmt.excluded.updated_at,
        },
    ).returning(
        FeedbackTrackContribution,
        (FeedbackTrackContribution.recorded_at == now).label("created"),
    )
    row = (await session.execute(upsert_stmt)).one()
    return row[0], bool(row[1])


async def list_contributions_for_user(
    session: AsyncSession, *, ditto_user_id: str, limit: int = 100
) -> list[FeedbackTrackContribution]:
    stmt = (
        select(FeedbackTrackContribution)
        .where(FeedbackTrackContribution.ditto_user_id == ditto_user_id)
        .order_by(FeedbackTrackContribution.recorded_at.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def summarize_contributions_for_hotkey(
    session: AsyncSession, *, hotkey: str
) -> dict[str, int]:
    """Counts by kind for the Ditto account currently linked to ``hotkey``.

    Resolved through the active link at read time: an unlinked or revoked
    hotkey has no contributions, whatever was recorded for the account.
    """
    stmt = (
        select(FeedbackTrackContribution.kind, func.count())
        .select_from(FeedbackTrackContribution)
        .join(
            MinerDittoLink,
            MinerDittoLink.ditto_user_id == FeedbackTrackContribution.ditto_user_id,
        )
        .where(
            MinerDittoLink.miner_hotkey == hotkey, MinerDittoLink.revoked_at.is_(None)
        )
        .group_by(FeedbackTrackContribution.kind)
    )
    return {kind: int(count) for kind, count in (await session.execute(stmt)).all()}
