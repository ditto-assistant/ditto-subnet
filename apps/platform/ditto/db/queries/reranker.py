"""Queries for the Season 2 reranker track (shadow; SN-207 intake, SN-209 scoring).

``reranker_submissions`` and ``reranker_champions`` are the whole reranker
track's state. Neither joins ``agents``, so nothing here can reach the
screener queue, validator allocation, the leaderboard, anti-copy, weights,
emissions or payments.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.exc import IntegrityError

from ditto.db.models import RerankerChampion, RerankerSubmission

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

RERANKER_STATUSES = ("accepted", "scoring", "invalid", "scored", "failed")
# Statuses a scorer may write when it finishes a claim.
RERANKER_FINISHED_STATUSES = frozenset({"invalid", "scored", "failed"})
# Short, allowlisted failure codes. Never paths, secrets or exception text.
RERANKER_FAILURE_CODES = frozenset(
    {
        "artifact_unavailable",
        "artifact_mismatch",
        "chain_unavailable",
        "pool_generation_failed",
        "pools_invalid",
        "champion_unavailable",
        "scoring_error",
        "attempts_exhausted",
        "internal_error",
    }
)
_CHAMPION_LOCK_KEY = "reranker-champion"
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


# --- Scorer lease (SN-209) -------------------------------------------------


class StaleRerankerClaimError(Exception):
    """The claim's token no longer owns the row (reclaimed or finished)."""


class RerankerChampionChangedError(Exception):
    """The crown moved after the challenger was compared against it."""


@dataclass(frozen=True)
class RerankerScoringClaim:
    submission_id: UUID
    token: UUID
    miner_hotkey: str
    artifact_sha256: str
    attempts: int
    lease_expires_at: datetime
    # Pinned on a previous attempt; reused so a retry never re-derives.
    seed: int | None
    seed_block: int | None
    seed_block_hash: str | None


def _aware(now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("scorer timestamps must be timezone-aware")


async def claim_reranker_submission(
    session: AsyncSession,
    *,
    now: datetime,
    lease: timedelta,
    max_attempts: int,
) -> RerankerScoringClaim | None:
    """Lease the oldest scorable submission with ``SKIP LOCKED``.

    Scorable means ``accepted``, or ``scoring`` with an expired lease and
    attempts left (a crashed worker). An expired lease with no attempts left
    is failed with ``attempts_exhausted`` first, so it stays visible instead
    of being retried forever. Caller owns the transaction and must commit it
    before any slow work.
    """
    _aware(now)
    if max_attempts < 1 or lease <= timedelta(0):
        raise ValueError("lease and max_attempts must be positive")
    await session.execute(
        update(RerankerSubmission)
        .where(
            RerankerSubmission.status == "scoring",
            RerankerSubmission.scoring_lease_expires_at <= now,
            RerankerSubmission.scoring_attempts >= max_attempts,
        )
        .values(
            status="failed",
            failure_reason="attempts_exhausted",
            scoring_token=None,
            scoring_lease_expires_at=None,
        )
    )
    row = await session.scalar(
        select(RerankerSubmission)
        .where(
            or_(
                RerankerSubmission.status == "accepted",
                (RerankerSubmission.status == "scoring")
                & (RerankerSubmission.scoring_lease_expires_at <= now)
                & (RerankerSubmission.scoring_attempts < max_attempts),
            )
        )
        .order_by(RerankerSubmission.created_at, RerankerSubmission.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if row is None:
        return None
    row.status = "scoring"
    row.scoring_token = uuid4()
    row.scoring_lease_expires_at = now + lease
    row.scoring_attempts += 1
    row.failure_reason = None
    await session.flush()
    assert row.scoring_token is not None
    assert row.scoring_lease_expires_at is not None
    return RerankerScoringClaim(
        submission_id=row.id,
        token=row.scoring_token,
        miner_hotkey=row.miner_hotkey,
        artifact_sha256=row.artifact_sha256,
        attempts=row.scoring_attempts,
        lease_expires_at=row.scoring_lease_expires_at,
        seed=row.seed,
        seed_block=row.seed_block,
        seed_block_hash=row.seed_block_hash,
    )


async def _owned_row(
    session: AsyncSession, claim: RerankerScoringClaim
) -> RerankerSubmission:
    row = await session.scalar(
        select(RerankerSubmission)
        .where(
            RerankerSubmission.id == claim.submission_id,
            RerankerSubmission.status == "scoring",
            RerankerSubmission.scoring_token == claim.token,
        )
        .with_for_update()
    )
    if row is None:
        raise StaleRerankerClaimError(str(claim.submission_id))
    return row


async def pin_reranker_seed(
    session: AsyncSession,
    *,
    claim: RerankerScoringClaim,
    seed: int,
    seed_block: int,
    seed_block_hash: str,
) -> None:
    """Record the post-commit seed once. A pinned seed is never replaced."""
    if seed < 0 or seed_block < 0 or not seed_block_hash:
        raise ValueError("seed, block and block hash are required")
    row = await _owned_row(session, claim)
    if row.seed is not None:
        if (row.seed, row.seed_block, row.seed_block_hash) != (
            seed,
            seed_block,
            seed_block_hash,
        ):
            raise StaleRerankerClaimError(str(claim.submission_id))
        return
    row.seed = seed
    row.seed_block = seed_block
    row.seed_block_hash = seed_block_hash
    await session.flush()


async def finish_reranker_submission(
    session: AsyncSession,
    *,
    claim: RerankerScoringClaim,
    now: datetime,
    status: str,
    score: dict[str, Any] | None = None,
    invalid_reason: str | None = None,
    failure_reason: str | None = None,
) -> None:
    """Close a claim as ``scored``, ``invalid`` or ``failed`` (token-fenced)."""
    _aware(now)
    if status not in RERANKER_FINISHED_STATUSES:
        raise ValueError(f"cannot finish a claim as {status!r}")
    if status == "failed" and failure_reason not in RERANKER_FAILURE_CODES:
        raise ValueError("failed rows need an allowlisted failure code")
    if status != "failed" and failure_reason is not None:
        raise ValueError("only failed rows carry a failure code")
    if status == "invalid" and not invalid_reason:
        raise ValueError("invalid rows need a reason")
    row = await _owned_row(session, claim)
    row.status = status
    row.scoring_token = None
    row.scoring_lease_expires_at = None
    row.failure_reason = failure_reason
    row.invalid_reason = invalid_reason[:500] if invalid_reason else None
    if score is not None:
        row.score = score
        row.scored_at = now
    await session.flush()


async def release_reranker_claim(
    session: AsyncSession, *, claim: RerankerScoringClaim
) -> None:
    """Return a claim to ``accepted`` for a later attempt (pinned seed kept)."""
    row = await _owned_row(session, claim)
    row.status = "accepted"
    row.scoring_token = None
    row.scoring_lease_expires_at = None
    await session.flush()


# --- Shadow champion history (SN-209) -------------------------------------


async def lock_reranker_champion(session: AsyncSession) -> None:
    """Serialize crown changes inside the caller's transaction."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": _CHAMPION_LOCK_KEY},
    )


async def get_current_reranker_champion(
    session: AsyncSession,
) -> RerankerChampion | None:
    """The current shadow champion, or ``None`` when the incumbent holds it."""
    return await session.scalar(
        select(RerankerChampion).where(RerankerChampion.superseded_at.is_(None))
    )


async def crown_reranker_champion(
    session: AsyncSession,
    *,
    submission_id: UUID,
    expected_previous: UUID | None,
    contract_sha256: str,
    pools_sha256: str,
    comparison: dict[str, Any],
    now: datetime,
) -> RerankerChampion:
    """Append a crown row, superseding the current one (shadow only).

    ``expected_previous`` is the champion the challenger was compared against
    (``None`` for the incumbent). If the crown moved in the meantime this
    raises :class:`RerankerChampionChangedError` and writes nothing.
    """
    _aware(now)
    await lock_reranker_champion(session)
    current = await get_current_reranker_champion(session)
    current_id = current.submission_id if current is not None else None
    if current_id != expected_previous:
        raise RerankerChampionChangedError(str(submission_id))
    if current is not None:
        current.superseded_at = now
        await session.flush()
    row = RerankerChampion(
        submission_id=submission_id,
        previous_submission_id=expected_previous,
        crowned_at=now,
        contract_sha256=contract_sha256,
        pools_sha256=pools_sha256,
        comparison=comparison,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def list_reranker_champions(
    session: AsyncSession, *, limit: int, offset: int = 0
) -> list[RerankerChampion]:
    """Crown history, newest first."""
    stmt = select(RerankerChampion).order_by(
        RerankerChampion.crowned_at.desc(), RerankerChampion.id.desc()
    )
    return list(await session.scalars(stmt.limit(limit).offset(offset)))


async def count_reranker_champions(session: AsyncSession) -> int:
    stmt = select(func.count()).select_from(RerankerChampion)
    return int(await session.scalar(stmt) or 0)


async def list_recent_scored_reranker_submissions(
    session: AsyncSession, *, limit: int
) -> list[RerankerSubmission]:
    """Most recently scored rows that carry a score (``scored`` or ``invalid``)."""
    stmt = (
        select(RerankerSubmission)
        .where(
            RerankerSubmission.status.in_(("scored", "invalid")),
            RerankerSubmission.score.is_not(None),
            RerankerSubmission.scored_at.is_not(None),
        )
        .order_by(RerankerSubmission.scored_at.desc(), RerankerSubmission.id.asc())
        .limit(limit)
    )
    return list(await session.scalars(stmt))
