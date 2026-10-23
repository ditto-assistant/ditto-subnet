"""Generator competition reads and writes.

Backs ``ditto.api_server.generator_competition`` and its endpoints. Settings
revisions are append-only. Packs move through
``queued -> leased -> scored | invalid | failed`` by explicit transitions here;
the lease query uses ``FOR UPDATE SKIP LOCKED`` so concurrent scorers never
receive the same pack.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select

from ditto.db.models import (
    GeneratorCaseVerdict,
    GeneratorCompetitionSettingsRevision,
    GeneratorPack,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

GLOBAL_SCOPE = "*"
PENDING_STATUSES = ("queued", "leased")


async def latest_generator_settings_revision(
    session: AsyncSession,
) -> GeneratorCompetitionSettingsRevision | None:
    return await session.scalar(
        select(GeneratorCompetitionSettingsRevision)
        .where(GeneratorCompetitionSettingsRevision.scope == GLOBAL_SCOPE)
        .order_by(GeneratorCompetitionSettingsRevision.revision.desc())
        .limit(1)
    )


async def list_generator_settings_revisions(
    session: AsyncSession, *, limit: int = 200
) -> Sequence[GeneratorCompetitionSettingsRevision]:
    return list(
        await session.scalars(
            select(GeneratorCompetitionSettingsRevision)
            .order_by(GeneratorCompetitionSettingsRevision.revision.desc())
            .limit(limit)
        )
    )


async def insert_generator_settings_revision(
    session: AsyncSession,
    *,
    parent_revision: int,
    settings: dict,
    checksum: str,
    reason: str,
    actor: str,
) -> GeneratorCompetitionSettingsRevision:
    """Append one revision; a racing write surfaces as ``IntegrityError``."""
    row = GeneratorCompetitionSettingsRevision(
        parent_revision=parent_revision,
        scope=GLOBAL_SCOPE,
        settings=settings,
        checksum=checksum,
        reason=reason,
        actor=actor,
    )
    session.add(row)
    await session.flush()
    return row


async def count_pending_packs(session: AsyncSession, *, miner_hotkey: str) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(GeneratorPack)
            .where(
                GeneratorPack.miner_hotkey == miner_hotkey,
                GeneratorPack.status.in_(PENDING_STATUSES),
            )
        )
        or 0
    )


async def latest_pack_submitted_at(
    session: AsyncSession, *, miner_hotkey: str
) -> datetime | None:
    return await session.scalar(
        select(func.max(GeneratorPack.submitted_at)).where(
            GeneratorPack.miner_hotkey == miner_hotkey
        )
    )


async def get_pack_by_sha(
    session: AsyncSession, pack_sha256: str
) -> GeneratorPack | None:
    return await session.scalar(
        select(GeneratorPack).where(GeneratorPack.pack_sha256 == pack_sha256)
    )


async def get_pack(
    session: AsyncSession, pack_id: UUID, *, for_update: bool = False
) -> GeneratorPack | None:
    stmt = select(GeneratorPack).where(GeneratorPack.pack_id == pack_id)
    if for_update:
        stmt = stmt.with_for_update()
    return await session.scalar(stmt)


async def insert_pack(
    session: AsyncSession,
    *,
    miner_hotkey: str,
    owner_key: str,
    pack_sha256: str,
    pack_bytes: bytes,
    case_count: int,
    generator_name: str,
    generator_version: str,
    now: datetime,
) -> GeneratorPack:
    row = GeneratorPack(
        pack_id=uuid4(),
        miner_hotkey=miner_hotkey,
        owner_key=owner_key,
        pack_sha256=pack_sha256,
        pack_bytes=pack_bytes,
        size_bytes=len(pack_bytes),
        case_count=case_count,
        generator_name=generator_name,
        generator_version=generator_version,
        status="queued",
        attempts=0,
        not_before=now,
        submitted_at=now,
    )
    session.add(row)
    await session.flush()
    return row


async def lock_next_leasable_pack(
    session: AsyncSession, *, now: datetime
) -> GeneratorPack | None:
    """The oldest pack ready for a lease: queued past its backoff, or leased
    with an expired lease. Row-locked and skipped by concurrent callers."""
    return await session.scalar(
        select(GeneratorPack)
        .where(
            or_(
                and_(GeneratorPack.status == "queued", GeneratorPack.not_before <= now),
                and_(
                    GeneratorPack.status == "leased",
                    GeneratorPack.lease_expires_at <= now,
                ),
            )
        )
        .order_by(GeneratorPack.not_before, GeneratorPack.submitted_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )


def lease_pack(
    row: GeneratorPack, *, now: datetime, ttl: timedelta, panel: list[dict]
) -> None:
    row.status = "leased"
    row.lease_id = uuid4()
    row.lease_expires_at = now + ttl
    row.attempts = row.attempts + 1
    row.panel = panel


def finish_pack(row: GeneratorPack, *, status: str, outcome: str) -> None:
    """Leave the leased state for a terminal (or re-queued) status."""
    row.status = status
    row.last_outcome = outcome
    row.lease_id = None
    row.lease_expires_at = None


async def claimed_fingerprints(
    session: AsyncSession, *, fingerprints: Sequence[str], exclude_pack_id: UUID
) -> list[str]:
    """Fingerprints an earlier scored pack already holds credit for."""
    if not fingerprints:
        return []
    rows = await session.scalars(
        select(GeneratorCaseVerdict.fingerprint)
        .join(GeneratorPack, GeneratorPack.pack_id == GeneratorCaseVerdict.pack_id)
        .where(
            GeneratorCaseVerdict.fingerprint.in_(list(fingerprints)),
            GeneratorCaseVerdict.status != "duplicate",
            GeneratorPack.status == "scored",
            GeneratorPack.pack_id != exclude_pack_id,
        )
        .distinct()
    )
    return sorted(rows)


async def list_packs(
    session: AsyncSession,
    *,
    miner_hotkey: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> Sequence[GeneratorPack]:
    stmt = (
        select(GeneratorPack).order_by(GeneratorPack.submitted_at.desc()).limit(limit)
    )
    if miner_hotkey is not None:
        stmt = stmt.where(GeneratorPack.miner_hotkey == miner_hotkey)
    if status is not None:
        stmt = stmt.where(GeneratorPack.status == status)
    return list(await session.scalars(stmt))


async def case_status_counts(
    session: AsyncSession, pack_ids: Sequence[UUID]
) -> dict[UUID, dict[str, int]]:
    if not pack_ids:
        return {}
    rows = await session.execute(
        select(
            GeneratorCaseVerdict.pack_id,
            GeneratorCaseVerdict.status,
            func.count(),
        )
        .where(GeneratorCaseVerdict.pack_id.in_(list(pack_ids)))
        .group_by(GeneratorCaseVerdict.pack_id, GeneratorCaseVerdict.status)
    )
    out: dict[UUID, dict[str, int]] = {}
    for pack_id, status, count in rows:
        out.setdefault(pack_id, {})[status] = int(count)
    return out


async def pack_counts_by_status(session: AsyncSession) -> dict[str, int]:
    rows = await session.execute(
        select(GeneratorPack.status, func.count()).group_by(GeneratorPack.status)
    )
    return {status: int(count) for status, count in rows}


async def list_verdicts(
    session: AsyncSession, pack_id: UUID
) -> Sequence[GeneratorCaseVerdict]:
    return list(
        await session.scalars(
            select(GeneratorCaseVerdict)
            .where(GeneratorCaseVerdict.pack_id == pack_id)
            .order_by(GeneratorCaseVerdict.position)
        )
    )


async def best_scored_packs(
    session: AsyncSession, *, since: datetime
) -> Sequence[GeneratorPack]:
    """Each hotkey's best pack scored since ``since``, best score first.

    Ties keep the earliest-scored pack, so re-uploading an equal pack never
    displaces the original.
    """
    best = (
        select(GeneratorPack)
        .where(GeneratorPack.status == "scored", GeneratorPack.scored_at >= since)
        .order_by(
            GeneratorPack.miner_hotkey,
            GeneratorPack.score.desc(),
            GeneratorPack.scored_at.asc(),
            GeneratorPack.pack_id.asc(),
        )
        .distinct(GeneratorPack.miner_hotkey)
        .subquery()
    )
    rows = await session.execute(
        select(GeneratorPack)
        .join(best, best.c.pack_id == GeneratorPack.pack_id)
        .order_by(
            GeneratorPack.score.desc(),
            GeneratorPack.submitted_at.asc(),
            GeneratorPack.pack_id.asc(),
        )
    )
    return list(rows.scalars())


async def list_promotion_candidates(
    session: AsyncSession, *, limit: int = 100
) -> Sequence[tuple[GeneratorCaseVerdict, GeneratorPack]]:
    rows = await session.execute(
        select(GeneratorCaseVerdict, GeneratorPack)
        .join(GeneratorPack, GeneratorPack.pack_id == GeneratorCaseVerdict.pack_id)
        .where(
            GeneratorCaseVerdict.status == "discriminating",
            GeneratorPack.status == "scored",
        )
        .order_by(
            GeneratorCaseVerdict.credit.desc(),
            GeneratorPack.scored_at.desc(),
            GeneratorCaseVerdict.case_id,
        )
        .limit(limit)
    )
    return [(verdict, pack) for verdict, pack in rows.tuples()]
