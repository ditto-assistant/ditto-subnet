"""Generator competition reads and writes.

Backs ``ditto.api_server.generator_competition`` and its endpoints. Settings
revisions and probe epochs are append-only. A submission is leased by
creating a ``generator_runs`` row and stamping the lease on the submission;
the lease query uses ``FOR UPDATE SKIP LOCKED`` so concurrent scorers never
receive the same submission.
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, update

from ditto.db.models import (
    GeneratorCaseVerdict,
    GeneratorCompetitionSettingsRevision,
    GeneratorProbeEpoch,
    GeneratorProbeResult,
    GeneratorRun,
    GeneratorSubmission,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

GLOBAL_SCOPE = "*"


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


# --- submissions ---------------------------------------------------------


async def get_submission(
    session: AsyncSession, submission_id: UUID, *, for_update: bool = False
) -> GeneratorSubmission | None:
    stmt = select(GeneratorSubmission).where(
        GeneratorSubmission.submission_id == submission_id
    )
    if for_update:
        stmt = stmt.with_for_update()
    return await session.scalar(stmt)


async def get_submission_by_sha(
    session: AsyncSession, source_sha256: str
) -> GeneratorSubmission | None:
    return await session.scalar(
        select(GeneratorSubmission).where(
            GeneratorSubmission.source_sha256 == source_sha256
        )
    )


async def get_submission_by_tree(
    session: AsyncSession, tree_sha256: str
) -> GeneratorSubmission | None:
    """The earliest submission of this exact source tree, if any."""
    return await session.scalar(
        select(GeneratorSubmission)
        .where(GeneratorSubmission.tree_sha256 == tree_sha256)
        .order_by(GeneratorSubmission.submitted_at)
        .limit(1)
    )


async def latest_submitted_at(
    session: AsyncSession, *, miner_hotkey: str
) -> datetime | None:
    return await session.scalar(
        select(func.max(GeneratorSubmission.submitted_at)).where(
            GeneratorSubmission.miner_hotkey == miner_hotkey
        )
    )


async def insert_submission(
    session: AsyncSession,
    *,
    miner_hotkey: str,
    owner_key: str,
    source_sha256: str,
    tree_sha256: str,
    source_bytes: bytes,
    module: str,
    go_version: str,
    file_count: int,
    now: datetime,
) -> GeneratorSubmission:
    row = GeneratorSubmission(
        submission_id=uuid4(),
        miner_hotkey=miner_hotkey,
        owner_key=owner_key,
        source_sha256=source_sha256,
        tree_sha256=tree_sha256,
        source_bytes=source_bytes,
        size_bytes=len(source_bytes),
        module=module,
        go_version=go_version,
        file_count=file_count,
        status="active",
        attempts=0,
        next_run_at=now,
        submitted_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.flush()
    return row


async def supersede_active(
    session: AsyncSession, *, miner_hotkey: str, keep: UUID, now: datetime
) -> list[UUID]:
    """Supersede the hotkey's other active submissions. An in-flight run may
    still report; a superseded submission is simply never leased again."""
    rows = await session.scalars(
        update(GeneratorSubmission)
        .where(
            GeneratorSubmission.miner_hotkey == miner_hotkey,
            GeneratorSubmission.status == "active",
            GeneratorSubmission.submission_id != keep,
        )
        .values(
            status="superseded", status_reason=f"superseded by {keep}", updated_at=now
        )
        .returning(GeneratorSubmission.submission_id)
    )
    return list(rows)


async def lock_next_runnable_submission(
    session: AsyncSession, *, now: datetime
) -> GeneratorSubmission | None:
    """The active submission whose next run is most overdue: unleased past
    ``next_run_at``, or leased with an expired lease. Row-locked and skipped by
    concurrent callers."""
    return await session.scalar(
        select(GeneratorSubmission)
        .where(
            GeneratorSubmission.status == "active",
            or_(
                and_(
                    GeneratorSubmission.lease_id.is_(None),
                    GeneratorSubmission.next_run_at <= now,
                ),
                GeneratorSubmission.lease_expires_at <= now,
            ),
        )
        .order_by(GeneratorSubmission.next_run_at, GeneratorSubmission.submitted_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )


def clear_lease(row: GeneratorSubmission, *, now: datetime) -> None:
    row.lease_id = None
    row.lease_run_id = None
    row.lease_expires_at = None
    row.updated_at = now


def new_seeds(count: int) -> list[str]:
    """Fresh 32-byte secret seeds, as lowercase hex."""
    return [secrets.token_hex(32) for _ in range(count)]


async def start_run(
    session: AsyncSession,
    row: GeneratorSubmission,
    *,
    now: datetime,
    ttl: timedelta,
    seeds: list[str],
    probe_epoch: int,
    cases_per_seed: int,
    panel: list[dict],
    settings_revision: int,
) -> GeneratorRun:
    """Create a run on fresh seeds and lease the submission to it."""
    row.attempts = row.attempts + 1
    run = GeneratorRun(
        run_id=uuid4(),
        submission_id=row.submission_id,
        attempt=row.attempts,
        seeds=seeds,
        probe_epoch=probe_epoch,
        cases_per_seed=cases_per_seed,
        panel=panel,
        settings_revision=settings_revision,
        status="leased",
        leased_at=now,
    )
    session.add(run)
    row.lease_id = uuid4()
    row.lease_run_id = run.run_id
    row.lease_expires_at = now + ttl
    row.updated_at = now
    await session.flush()
    return run


async def get_run(
    session: AsyncSession, run_id: UUID, *, for_update: bool = False
) -> GeneratorRun | None:
    stmt = select(GeneratorRun).where(GeneratorRun.run_id == run_id)
    if for_update:
        stmt = stmt.with_for_update()
    return await session.scalar(stmt)


def finish_run(
    run: GeneratorRun,
    *,
    status: str,
    outcome: str,
    now: datetime,
    score: float | None = None,
) -> None:
    run.status = status
    run.outcome = outcome
    run.score = score
    run.finished_at = now


async def abandon_run(session: AsyncSession, run_id: UUID, *, now: datetime) -> None:
    run = await get_run(session, run_id, for_update=True)
    if run is not None and run.status == "leased":
        finish_run(run, status="abandoned", outcome="lease_expired", now=now)


# --- probe epochs ----------------------------------------------------------


async def current_probe_epoch(
    session: AsyncSession, *, create: bool = True
) -> GeneratorProbeEpoch | None:
    """The current probe epoch; the first call creates epoch 1."""
    row = await session.scalar(
        select(GeneratorProbeEpoch).where(GeneratorProbeEpoch.retired_at.is_(None))
    )
    if row is None and create:
        row = GeneratorProbeEpoch(
            seed=secrets.token_hex(32),
            reason="initial probe epoch",
            actor="platform",
        )
        session.add(row)
        await session.flush()
    return row


async def rotate_probe_epoch(
    session: AsyncSession, *, reason: str, actor: str, now: datetime
) -> GeneratorProbeEpoch:
    """Retire the current epoch and start a new one with a fresh seed.

    Copy detection only compares submissions probed in the same epoch; every
    active submission is re-probed on its next run.
    """
    current = await current_probe_epoch(session, create=False)
    if current is not None:
        current.retired_at = now
        await session.flush()
    row = GeneratorProbeEpoch(seed=secrets.token_hex(32), reason=reason, actor=actor)
    session.add(row)
    await session.flush()
    return row


async def list_probe_epochs(
    session: AsyncSession, *, limit: int = 50
) -> list[tuple[GeneratorProbeEpoch, int]]:
    counts = (
        select(GeneratorProbeResult.epoch, func.count().label("probed"))
        .group_by(GeneratorProbeResult.epoch)
        .subquery()
    )
    rows = await session.execute(
        select(GeneratorProbeEpoch, func.coalesce(counts.c.probed, 0))
        .outerjoin(counts, counts.c.epoch == GeneratorProbeEpoch.epoch)
        .order_by(GeneratorProbeEpoch.epoch.desc())
        .limit(limit)
    )
    return [(epoch, int(probed)) for epoch, probed in rows.tuples()]


async def upsert_probe_result(
    session: AsyncSession,
    *,
    submission_id: UUID,
    epoch: int,
    run_id: UUID,
    fingerprints: list[str],
    now: datetime,
) -> None:
    row = await session.get(GeneratorProbeResult, (submission_id, epoch))
    if row is None:
        session.add(
            GeneratorProbeResult(
                submission_id=submission_id,
                epoch=epoch,
                run_id=run_id,
                fingerprints=fingerprints,
                recorded_at=now,
            )
        )
    else:
        row.run_id, row.fingerprints, row.recorded_at = run_id, fingerprints, now
    await session.flush()


async def earlier_probe_results(
    session: AsyncSession,
    *,
    epoch: int,
    exclude_owner_key: str,
    submitted_before: datetime,
) -> list[tuple[GeneratorSubmission, list[str]]]:
    """Probe fingerprints of earlier submissions by other owners in an epoch.

    Rejected submissions count too: copying a generator that was later
    rejected is still copying.
    """
    rows = await session.execute(
        select(GeneratorSubmission, GeneratorProbeResult.fingerprints)
        .join(
            GeneratorProbeResult,
            GeneratorProbeResult.submission_id == GeneratorSubmission.submission_id,
        )
        .where(
            GeneratorProbeResult.epoch == epoch,
            GeneratorSubmission.owner_key != exclude_owner_key,
            GeneratorSubmission.submitted_at < submitted_before,
        )
        .order_by(GeneratorSubmission.submitted_at)
    )
    return [(sub, list(fps)) for sub, fps in rows.tuples()]


# --- scoring ---------------------------------------------------------------


async def claimed_fingerprints(
    session: AsyncSession, *, fingerprints: Sequence[str], exclude_run_id: UUID
) -> list[str]:
    """Fingerprints an earlier scored run (of any generator) already holds."""
    if not fingerprints:
        return []
    rows = await session.scalars(
        select(GeneratorCaseVerdict.fingerprint)
        .join(GeneratorRun, GeneratorRun.run_id == GeneratorCaseVerdict.run_id)
        .where(
            GeneratorCaseVerdict.fingerprint.in_(list(fingerprints)),
            GeneratorCaseVerdict.status != "duplicate",
            GeneratorRun.status == "scored",
            GeneratorRun.run_id != exclude_run_id,
        )
        .distinct()
    )
    return sorted(rows)


# --- listings --------------------------------------------------------------


async def list_submissions(
    session: AsyncSession,
    *,
    miner_hotkey: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> Sequence[GeneratorSubmission]:
    stmt = (
        select(GeneratorSubmission)
        .order_by(GeneratorSubmission.submitted_at.desc())
        .limit(limit)
    )
    if miner_hotkey is not None:
        stmt = stmt.where(GeneratorSubmission.miner_hotkey == miner_hotkey)
    if status is not None:
        stmt = stmt.where(GeneratorSubmission.status == status)
    return list(await session.scalars(stmt))


async def submission_counts_by_status(session: AsyncSession) -> dict[str, int]:
    rows = await session.execute(
        select(GeneratorSubmission.status, func.count()).group_by(
            GeneratorSubmission.status
        )
    )
    return {status: int(count) for status, count in rows}


async def latest_runs(
    session: AsyncSession, submission_ids: Sequence[UUID]
) -> dict[UUID, GeneratorRun]:
    """Each submission's most recent run that is not abandoned."""
    if not submission_ids:
        return {}
    rows = await session.scalars(
        select(GeneratorRun)
        .where(
            GeneratorRun.submission_id.in_(list(submission_ids)),
            GeneratorRun.status != "abandoned",
        )
        .order_by(GeneratorRun.submission_id, GeneratorRun.leased_at.desc())
        .distinct(GeneratorRun.submission_id)
    )
    return {run.submission_id: run for run in rows}


async def list_runs(
    session: AsyncSession, submission_id: UUID, *, limit: int = 50
) -> Sequence[GeneratorRun]:
    return list(
        await session.scalars(
            select(GeneratorRun)
            .where(GeneratorRun.submission_id == submission_id)
            .order_by(GeneratorRun.leased_at.desc())
            .limit(limit)
        )
    )


async def case_status_counts(
    session: AsyncSession, run_ids: Sequence[UUID]
) -> dict[UUID, dict[str, int]]:
    if not run_ids:
        return {}
    rows = await session.execute(
        select(GeneratorCaseVerdict.run_id, GeneratorCaseVerdict.status, func.count())
        .where(GeneratorCaseVerdict.run_id.in_(list(run_ids)))
        .group_by(GeneratorCaseVerdict.run_id, GeneratorCaseVerdict.status)
    )
    out: dict[UUID, dict[str, int]] = {}
    for run_id, status, count in rows:
        out.setdefault(run_id, {})[status] = int(count)
    return out


async def list_verdicts(
    session: AsyncSession, run_id: UUID
) -> Sequence[GeneratorCaseVerdict]:
    return list(
        await session.scalars(
            select(GeneratorCaseVerdict)
            .where(GeneratorCaseVerdict.run_id == run_id)
            .order_by(GeneratorCaseVerdict.seed_index, GeneratorCaseVerdict.position)
        )
    )


async def latest_scored_runs(
    session: AsyncSession, *, since: datetime
) -> list[tuple[GeneratorRun, GeneratorSubmission]]:
    """Each hotkey's latest run scored since ``since``, best score first.

    Runs of rejected submissions never count; a superseded submission's run
    counts until a newer submission of the hotkey is scored.
    """
    latest = (
        select(GeneratorRun.run_id)
        .join(
            GeneratorSubmission,
            GeneratorSubmission.submission_id == GeneratorRun.submission_id,
        )
        .where(
            GeneratorRun.status == "scored",
            GeneratorRun.finished_at >= since,
            GeneratorSubmission.status != "rejected",
        )
        .order_by(
            GeneratorSubmission.miner_hotkey,
            GeneratorRun.finished_at.desc(),
            GeneratorRun.run_id,
        )
        .distinct(GeneratorSubmission.miner_hotkey)
        .subquery()
    )
    rows = await session.execute(
        select(GeneratorRun, GeneratorSubmission)
        .join(
            GeneratorSubmission,
            GeneratorSubmission.submission_id == GeneratorRun.submission_id,
        )
        .join(latest, latest.c.run_id == GeneratorRun.run_id)
        .order_by(
            GeneratorRun.score.desc(),
            GeneratorSubmission.submitted_at.asc(),
            GeneratorRun.run_id.asc(),
        )
    )
    return [(run, sub) for run, sub in rows.tuples()]


async def list_promotion_candidates(
    session: AsyncSession, *, limit: int = 100
) -> Sequence[tuple[GeneratorCaseVerdict, GeneratorRun, GeneratorSubmission]]:
    rows = await session.execute(
        select(GeneratorCaseVerdict, GeneratorRun, GeneratorSubmission)
        .join(GeneratorRun, GeneratorRun.run_id == GeneratorCaseVerdict.run_id)
        .join(
            GeneratorSubmission,
            GeneratorSubmission.submission_id == GeneratorRun.submission_id,
        )
        .where(
            GeneratorCaseVerdict.status == "discriminating",
            GeneratorRun.status == "scored",
            GeneratorSubmission.status != "rejected",
        )
        .order_by(
            GeneratorCaseVerdict.credit.desc(),
            GeneratorRun.finished_at.desc(),
            GeneratorCaseVerdict.case_id,
        )
        .limit(limit)
    )
    return [(v, r, s) for v, r, s in rows.tuples()]
