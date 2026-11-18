"""The model_context (CPM) competition: settings, seasons, screening, scoring.

Build contract: ``docs/research/cpm/competition-v1.md`` (sections 3-5). The
endpoints in ``endpoints/model_context*.py`` and
``endpoints/admin_model_context.py`` stay thin; the decisions live here.

* :func:`load_settings` resolves the governing revision; an empty table or a
  corrupt row reads as ``mode=off``.
* Admission (the endpoint) checks the signature, ban, registration, manifest
  and on-chain commitment, then inserts the submission as ``screening``.
  :class:`ModelContextLoop` claims it, streams the pinned revision from
  Hugging Face (:mod:`ditto.model_context_artifacts`), screens it, and either
  rejects it or makes it the hotkey's active submission (``queued``) with a
  run in the current season.
* Seasons (section 4.1) open when none is open or the current one is
  ``season_days`` old. Opening reads the finalized head, derives the three
  seeds from its hash, and queues a reference run per allowlisted base (the
  floors) plus a run for every active submission.
* The scorer leases runs (reference runs first), posts results checked
  against the season's seeds and the section 4.4 formula, or reports a fault.
* :func:`build_ledger` serves the current season's scored active submissions.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import os
import shutil
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.sql.elements import ColumnElement

from ditto.api_models.model_context import (
    CPM_SCORE_VERSION,
    MAX_FAULT_DETAIL_CHARS,
    MAX_REJECT_REASON_CHARS,
    CpmManifest,
    ModelContextBoardEntry,
    ModelContextBoardResponse,
    ModelContextFaultRequest,
    ModelContextLedgerEntry,
    ModelContextLedgerResponse,
    ModelContextResultRequest,
    ModelContextRunSummary,
    ModelContextSeasonView,
    ModelContextSettings,
    ModelContextSubmissionSummary,
    season_seeds,
    seed_score,
)
from ditto.db.models import (
    ModelContextRun,
    ModelContextScorerHeartbeat,
    ModelContextSeason,
    ModelContextSettingsRevision,
    ModelContextSubmission,
)
from ditto.model_context_artifacts import (
    ArtifactFetcher,
    ArtifactRejected,
    ArtifactUnavailable,
    HuggingFaceFetcher,
    ScreenResult,
    fetch_and_verify,
    screen_files,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from ditto.chain.models import BlockInfo

logger = logging.getLogger(__name__)

GLOBAL_SCOPE = "*"
DEFAULT_SETTINGS = ModelContextSettings()
ACTIVE_STATUSES = ("queued", "scored")
OPEN_RUN_STATUSES = ("queued", "leased")
SCREENING_LEASE = timedelta(hours=1)
SCREENING_RETRY_BACKOFF = timedelta(minutes=10)
MAX_SCREENING_ATTEMPTS = 5
SCORE_TOLERANCE = 1e-6
# Free space screening leaves on the host beyond the files it downloads.
SCREENING_DISK_HEADROOM_BYTES = 2 << 30
SCREEN_DIR_ENV = "DITTO_MODEL_CONTEXT_SCREEN_DIR"
SCORER_LIVE_WINDOW = timedelta(minutes=10)
# Unrunnable runs one /work poll closes before answering; the rest close on
# the next poll.
_MAX_LEASE_SCAN = 64


def _lock_key(name: str) -> int:
    return int.from_bytes(
        hashlib.sha256(name.encode()).digest()[:8], "big", signed=True
    )


_SEASON_LOCK_KEY = _lock_key("model-context-season")


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


# --- settings ----------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedSettings:
    settings: ModelContextSettings
    revision: int
    checksum: str
    valid: bool


def settings_checksum(settings: ModelContextSettings) -> str:
    encoded = json.dumps(
        settings.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def settings_from_row(
    row: ModelContextSettingsRevision | None,
) -> tuple[ModelContextSettings, bool]:
    """Decode a revision; a corrupt payload falls back to the closed default."""
    if row is None:
        return DEFAULT_SETTINGS, True
    try:
        return ModelContextSettings.model_validate(row.settings), True
    except ValidationError:
        logger.warning(
            "model_context settings revision %s is invalid; lane closed",
            row.revision,
            exc_info=True,
        )
        return DEFAULT_SETTINGS, False


async def latest_settings_revision(
    session: AsyncSession,
) -> ModelContextSettingsRevision | None:
    return await session.scalar(
        select(ModelContextSettingsRevision)
        .where(ModelContextSettingsRevision.scope == GLOBAL_SCOPE)
        .order_by(ModelContextSettingsRevision.revision.desc())
        .limit(1)
    )


async def list_settings_revisions(
    session: AsyncSession, *, limit: int = 100
) -> Sequence[ModelContextSettingsRevision]:
    return list(
        await session.scalars(
            select(ModelContextSettingsRevision)
            .order_by(ModelContextSettingsRevision.revision.desc())
            .limit(limit)
        )
    )


async def load_settings(session: AsyncSession) -> ResolvedSettings:
    row = await latest_settings_revision(session)
    settings, valid = settings_from_row(row)
    return ResolvedSettings(
        settings=settings,
        revision=row.revision if row is not None else 0,
        checksum=row.checksum if row is not None else "",
        valid=valid,
    )


# --- submissions -------------------------------------------------------------


async def lock_hotkey(session: AsyncSession, hotkey: str) -> None:
    """Serialize one hotkey's submissions for the rest of the transaction."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"model-context-upload:{hotkey}"},
    )


async def latest_submitted_at(session: AsyncSession, hotkey: str) -> datetime | None:
    """The hotkey's last submission that counts toward the cooldown.

    A submission refused during screening still counts: the cooldown bounds
    how often a hotkey makes Platform download a model."""
    return await session.scalar(
        select(func.max(ModelContextSubmission.created_at)).where(
            ModelContextSubmission.hotkey == hotkey
        )
    )


def cooldown_until(
    last: datetime | None, settings: ModelContextSettings, now: datetime
) -> datetime | None:
    if last is None:
        return None
    until = _aware(last) + timedelta(seconds=settings.upload_cooldown_seconds)
    return until if until > now else None


async def same_manifest(
    session: AsyncSession, *, hotkey: str, manifest_sha256: str
) -> ModelContextSubmission | None:
    """The hotkey's earlier, still-standing submission of this exact manifest."""
    return await session.scalar(
        select(ModelContextSubmission)
        .where(
            ModelContextSubmission.hotkey == hotkey,
            ModelContextSubmission.manifest_sha256 == manifest_sha256,
            ModelContextSubmission.status != "rejected",
        )
        .limit(1)
    )


async def insert_submission(
    session: AsyncSession,
    *,
    hotkey: str,
    hf_repo: str,
    revision: str,
    manifest: CpmManifest,
    manifest_sha256: str,
    commitment_block: int,
    now: datetime,
) -> ModelContextSubmission:
    row = ModelContextSubmission(
        submission_id=uuid4(),
        hotkey=hotkey,
        hf_repo=hf_repo,
        revision=revision,
        manifest=manifest.model_dump(mode="json", by_alias=True),
        manifest_sha256=manifest_sha256,
        base_id=manifest.base_id,
        kind=manifest.kind,
        commitment_block=commitment_block,
        total_bytes=manifest.total_bytes,
        status="screening",
        screening_attempts=0,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.flush()
    return row


def _bounded(reason: str, limit: int = MAX_REJECT_REASON_CHARS) -> str:
    return reason if len(reason) <= limit else reason[: limit - 1] + "…"


async def abandon_open_runs(
    session: AsyncSession,
    *,
    submission_id: UUID | None = None,
    season: int | None = None,
    fault: str,
    now: datetime,
) -> int:
    conditions: list[ColumnElement[bool]] = [
        ModelContextRun.status.in_(OPEN_RUN_STATUSES)
    ]
    if submission_id is not None:
        conditions.append(ModelContextRun.submission_id == submission_id)
    if season is not None:
        conditions.append(ModelContextRun.season == season)
    result = await session.execute(
        update(ModelContextRun)
        .where(*conditions)
        .values(
            status="abandoned",
            lease_id=None,
            lease_expires_at=None,
            finished_at=now,
            fault=_bounded(fault, MAX_FAULT_DETAIL_CHARS),
        )
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def reject_submission(
    session: AsyncSession, row: ModelContextSubmission, reason: str, *, now: datetime
) -> None:
    row.status = "rejected"
    row.reject_reason = _bounded(reason)
    row.screening_lease_until = None
    row.updated_at = now
    await abandon_open_runs(
        session, submission_id=row.submission_id, fault=f"rejected: {reason}", now=now
    )


async def supersede_active(
    session: AsyncSession, *, hotkey: str, keep: UUID, now: datetime
) -> list[UUID]:
    """The hotkey's other active submissions become ``superseded``."""
    ids = list(
        await session.scalars(
            update(ModelContextSubmission)
            .where(
                ModelContextSubmission.hotkey == hotkey,
                ModelContextSubmission.status.in_(ACTIVE_STATUSES),
                ModelContextSubmission.submission_id != keep,
            )
            .values(status="superseded", updated_at=now)
            .returning(ModelContextSubmission.submission_id)
        )
    )
    for submission_id in ids:
        await abandon_open_runs(
            session,
            submission_id=submission_id,
            fault=f"superseded by {keep}",
            now=now,
        )
    return ids


def _priority(row: ModelContextSubmission) -> tuple[int, datetime, str]:
    return (row.commitment_block, _aware(row.created_at), str(row.submission_id))


async def accept_screened(
    session: AsyncSession,
    row: ModelContextSubmission,
    result: ScreenResult,
    *,
    now: datetime,
) -> str:
    """Apply a passed screen to a row-locked ``screening`` submission.

    Duplicate weights (section 3.3): the earlier commitment block owns a
    ``weights_sha256``; a later holder is rejected ``duplicate_weights``,
    whether it is this submission or one admitted before it. Otherwise this
    submission supersedes the hotkey's previous active one and is queued in
    the current season. Returns the new status.
    """
    await lock_hotkey(session, row.hotkey)
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"model-context-weights:{result.weights_sha256}"},
    )
    row.weights_sha256 = result.weights_sha256
    row.screening_detail = result.detail
    row.screening_lease_until = None
    row.updated_at = now
    holders = list(
        await session.scalars(
            select(ModelContextSubmission)
            .where(
                ModelContextSubmission.weights_sha256 == result.weights_sha256,
                ModelContextSubmission.submission_id != row.submission_id,
                ModelContextSubmission.status != "rejected",
            )
            .with_for_update()
        )
    )
    owner = min([row, *holders], key=_priority)
    if owner is not row:
        await reject_submission(
            session,
            row,
            f"duplicate_weights: owned by submission {owner.submission_id} "
            f"(commitment block {owner.commitment_block})",
            now=now,
        )
        return row.status
    for later in holders:
        await reject_submission(
            session,
            later,
            f"duplicate_weights: owned by submission {row.submission_id} "
            f"(commitment block {row.commitment_block})",
            now=now,
        )
    row.status = "queued"
    await supersede_active(session, hotkey=row.hotkey, keep=row.submission_id, now=now)
    season = await current_season(session)
    if season is not None:
        await enqueue_submission_run(session, row, season.season, now=now)
    return row.status


# --- seasons -----------------------------------------------------------------


async def current_season(
    session: AsyncSession, *, for_update: bool = False
) -> ModelContextSeason | None:
    query = select(ModelContextSeason).where(ModelContextSeason.closed_at.is_(None))
    if for_update:
        query = query.with_for_update()
    return await session.scalar(query)


async def list_seasons(
    session: AsyncSession, *, limit: int = 20, offset: int = 0
) -> Sequence[ModelContextSeason]:
    return list(
        await session.scalars(
            select(ModelContextSeason)
            .order_by(ModelContextSeason.season.desc())
            .limit(limit)
            .offset(offset)
        )
    )


def normalize_block_hash(value: str) -> str:
    digits = value.lower().removeprefix("0x")
    if len(digits) != 64 or any(c not in "0123456789abcdef" for c in digits):
        raise ValueError(f"not a block hash: {value!r}")
    return "0x" + digits


async def enqueue_submission_run(
    session: AsyncSession, row: ModelContextSubmission, season: int, *, now: datetime
) -> bool:
    """Queue one run of ``row`` in ``season`` unless one is already open."""
    result = await session.execute(
        pg_insert(ModelContextRun)
        .values(
            run_id=uuid4(),
            kind="submission",
            submission_id=row.submission_id,
            base_id=row.base_id,
            season=season,
            cpm_score_version=CPM_SCORE_VERSION,
            status="queued",
            created_at=now,
        )
        .on_conflict_do_nothing(
            index_elements=["submission_id", "season"],
            index_where=and_(
                ModelContextRun.kind == "submission",
                ModelContextRun.status.in_(OPEN_RUN_STATUSES),
            ),
        )
    )
    return bool(getattr(result, "rowcount", 0))


async def enqueue_reference_run(
    session: AsyncSession, *, base_id: str, season: int, now: datetime
) -> bool:
    result = await session.execute(
        pg_insert(ModelContextRun)
        .values(
            run_id=uuid4(),
            kind="reference",
            submission_id=None,
            base_id=base_id,
            season=season,
            cpm_score_version=CPM_SCORE_VERSION,
            status="queued",
            created_at=now,
        )
        .on_conflict_do_nothing(
            index_elements=["season", "base_id"],
            index_where=and_(
                ModelContextRun.kind == "reference",
                ModelContextRun.status.in_(OPEN_RUN_STATUSES),
            ),
        )
    )
    return bool(getattr(result, "rowcount", 0))


def season_due(season: ModelContextSeason | None, now: datetime) -> bool:
    return season is None or _aware(season.closes_at) <= now


async def open_season(
    session: AsyncSession,
    *,
    resolved: ResolvedSettings,
    block: BlockInfo,
    now: datetime,
) -> ModelContextSeason:
    """Close the open season (if any) and open the next (caller commits).

    The seeds come from the finalized block Platform read just before, so
    nobody (Platform included) chooses them. Every active submission and every
    allowlisted base is queued for the new season: scores within a season are
    always on the same seeds.
    """
    settings = resolved.settings
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": _SEASON_LOCK_KEY}
    )
    previous = await current_season(session, for_update=True)
    last = await session.scalar(select(func.max(ModelContextSeason.season)))
    number = (last or 0) + 1
    if previous is not None:
        previous.closed_at = now
        await abandon_open_runs(
            session,
            season=previous.season,
            fault=f"season {previous.season} rotated",
            now=now,
        )
        await session.flush()
    block_hash = normalize_block_hash(block.hash)
    season = ModelContextSeason(
        season=number,
        open_block=block.number,
        open_block_hash=block_hash,
        seeds=season_seeds(number, block_hash),
        bench_version=settings.bench_version,
        cpm_score_version=CPM_SCORE_VERSION,
        base_floors={},
        settings_revision=resolved.revision,
        opened_at=now,
        closes_at=now + timedelta(days=settings.season_days),
    )
    session.add(season)
    await session.flush()
    for base in settings.base_allowlist:
        await enqueue_reference_run(
            session, base_id=base.base_id, season=number, now=now
        )
    active = await session.scalars(
        select(ModelContextSubmission).where(
            ModelContextSubmission.status.in_(ACTIVE_STATUSES)
        )
    )
    for row in active:
        await enqueue_submission_run(session, row, number, now=now)
    logger.info(
        "model_context season %d opened at block %d (bench v%d)",
        number,
        block.number,
        settings.bench_version,
    )
    return season


def season_view(row: ModelContextSeason) -> ModelContextSeasonView:
    return ModelContextSeasonView(
        season=row.season,
        open_block=row.open_block,
        open_block_hash=row.open_block_hash,
        seeds=[int(s) for s in row.seeds],
        seed_strings=[str(int(s)) for s in row.seeds],
        bench_version=row.bench_version,
        cpm_score_version=row.cpm_score_version,
        base_floors={k: float(v) for k, v in (row.base_floors or {}).items()},
        gpu_sku=row.gpu_sku,
        opened_at=row.opened_at,
        closes_at=row.closes_at,
        closed_at=row.closed_at,
    )


# --- screening ---------------------------------------------------------------


async def claim_screening(
    session: AsyncSession, *, now: datetime
) -> ModelContextSubmission | None:
    """Lease the oldest unleased ``screening`` submission (caller commits)."""
    candidate = (
        select(ModelContextSubmission.submission_id)
        .where(
            ModelContextSubmission.status == "screening",
            or_(
                ModelContextSubmission.screening_lease_until.is_(None),
                ModelContextSubmission.screening_lease_until <= now,
            ),
        )
        .order_by(ModelContextSubmission.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    return await session.scalar(
        update(ModelContextSubmission)
        .where(ModelContextSubmission.submission_id == candidate)
        .values(
            screening_lease_until=now + SCREENING_LEASE,
            screening_attempts=ModelContextSubmission.screening_attempts + 1,
            updated_at=now,
        )
        .returning(ModelContextSubmission)
    )


async def screen_artifacts(
    fetcher: ArtifactFetcher,
    row: ModelContextSubmission,
    settings: ModelContextSettings,
    *,
    workdir_root: str | None = None,
) -> ScreenResult:
    """Fetch, verify and statically screen one submission's pinned revision."""
    try:
        manifest = CpmManifest.model_validate(row.manifest)
    except ValidationError as exc:
        raise ArtifactRejected("stored manifest no longer validates") from exc
    base = settings.base(manifest.base_id)
    if base is None:
        raise ArtifactRejected(f"base_id {manifest.base_id!r} is no longer allowlisted")
    if manifest.total_bytes > settings.max_total_bytes:
        raise ArtifactRejected(
            f"files total {manifest.total_bytes} bytes, above the "
            f"{settings.max_total_bytes}-byte cap"
        )
    free = shutil.disk_usage(workdir_root or tempfile.gettempdir()).free
    if free < manifest.total_bytes + SCREENING_DISK_HEADROOM_BYTES:
        # The host's disk, not the miner's model: retry later.
        raise ArtifactUnavailable(
            f"screening needs {manifest.total_bytes} bytes plus headroom; "
            f"{free} bytes free"
        )
    workdir = Path(tempfile.mkdtemp(prefix="cpm-screen-", dir=workdir_root))
    try:
        paths = await fetch_and_verify(
            fetcher,
            hf_repo=row.hf_repo,
            revision=row.revision,
            manifest_sha256=row.manifest_sha256,
            manifest=manifest,
            workdir=workdir,
        )
        return await asyncio.to_thread(
            screen_files,
            manifest,
            paths,
            base=base,
            max_lora_rank=settings.max_lora_rank,
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def screen_next(
    session_maker: async_sessionmaker[AsyncSession],
    fetcher: ArtifactFetcher,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    workdir_root: str | None = None,
) -> tuple[UUID, str] | None:
    """Screen one pending submission end to end; ``(id, outcome)`` or ``None``.

    No transaction is held across the download: the claim commits a lease,
    the screen runs, and a second transaction applies the outcome only if
    the submission is still the same ``screening`` row.
    """
    async with session_maker() as session, session.begin():
        resolved = await load_settings(session)
        if resolved.settings.mode == "off":
            return None
        row = await claim_screening(session, now=now())
        if row is None:
            return None
        submission_id, attempts = row.submission_id, row.screening_attempts
    rejected: str | None = None
    unavailable: str | None = None
    result: ScreenResult | None = None
    try:
        result = await screen_artifacts(
            fetcher, row, resolved.settings, workdir_root=workdir_root
        )
    except ArtifactRejected as exc:
        rejected = str(exc)
    except ArtifactUnavailable as exc:
        unavailable = str(exc)
    except Exception as exc:  # noqa: BLE001 - host faults retry, then exhaust
        # An unexpected host error (OSError writing a download, a bug) is not
        # the miner's fault, but it still spends an attempt: it goes through
        # the same backoff and exhaustion accounting as an outage, so a
        # persistent failure ends in a rejection instead of looping forever.
        logger.exception(
            "model_context screening of %s failed unexpectedly", submission_id
        )
        unavailable = f"screening failed ({type(exc).__name__}): {exc}"[:500]
    async with session_maker() as session, session.begin():
        current = await session.get(
            ModelContextSubmission, submission_id, with_for_update=True
        )
        at = now()
        if current is None or current.status != "screening":
            return submission_id, "stale"
        if rejected is not None:
            await reject_submission(session, current, f"screening: {rejected}", now=at)
            outcome = "rejected"
        elif unavailable is not None:
            if attempts >= MAX_SCREENING_ATTEMPTS:
                await reject_submission(
                    session,
                    current,
                    f"screening: artifacts unavailable after {attempts} "
                    f"attempts: {unavailable}",
                    now=at,
                )
                outcome = "rejected"
            else:
                current.screening_lease_until = at + SCREENING_RETRY_BACKOFF
                current.updated_at = at
                outcome = "retry"
        else:
            assert result is not None
            outcome = await accept_screened(session, current, result, now=at)
    logger.info(
        "model_context screening submission_id=%s outcome=%s", submission_id, outcome
    )
    return submission_id, outcome


# --- scorer ------------------------------------------------------------------


async def record_heartbeat(
    session: AsyncSession,
    *,
    scorer_id: str,
    scorer_version: str | None,
    gpu_sku: str | None,
    poll: str,
    now: datetime,
) -> None:
    await session.execute(
        pg_insert(ModelContextScorerHeartbeat)
        .values(
            scorer_id=scorer_id,
            scorer_version=scorer_version,
            gpu_sku=gpu_sku,
            last_poll=poll,
            first_seen_at=now,
            last_seen_at=now,
        )
        .on_conflict_do_update(
            index_elements=["scorer_id"],
            set_={
                "scorer_version": scorer_version,
                "gpu_sku": gpu_sku,
                "last_poll": poll,
                "last_seen_at": now,
            },
        )
    )


async def expire_leases(
    session: AsyncSession, *, settings: ModelContextSettings, now: datetime
) -> None:
    """Re-queue runs whose lease lapsed; abandon them after ``max_attempts``."""
    expired = list(
        await session.scalars(
            select(ModelContextRun)
            .where(
                ModelContextRun.status == "leased",
                ModelContextRun.lease_expires_at <= now,
            )
            .with_for_update(skip_locked=True)
        )
    )
    for run in expired:
        run.lease_id = None
        run.lease_expires_at = None
        if run.attempts >= settings.max_attempts:
            run.status = "abandoned"
            run.finished_at = now
            run.fault = f"lease expired; {run.attempts} attempts exhausted"
        else:
            run.status = "queued"
            run.fault = "lease expired; re-queued"
    if expired:
        await session.flush()


@dataclass(frozen=True)
class Lease:
    run: ModelContextRun
    season: ModelContextSeason
    submission: ModelContextSubmission | None


async def lease_next_run(
    session: AsyncSession,
    *,
    resolved: ResolvedSettings,
    scorer_id: str,
    now: datetime,
) -> Lease | None:
    """Lease the next queued run of the open season (caller commits).

    Reference runs go first: no submission can be judged eligible before its
    base floor exists."""
    settings = resolved.settings
    if settings.mode == "off":
        return None
    season = await current_season(session)
    if season is None:
        return None
    await expire_leases(session, settings=settings, now=now)
    for _ in range(_MAX_LEASE_SCAN):
        run = await session.scalar(
            select(ModelContextRun)
            .where(
                ModelContextRun.season == season.season,
                ModelContextRun.status == "queued",
            )
            .order_by(
                (ModelContextRun.kind == "reference").desc(),
                ModelContextRun.created_at,
            )
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if run is None:
            return None
        # A run that can no longer be scored is closed with its reason, never
        # leased: a base dropped from the allowlist (the submission keeps its
        # status, so restoring the base re-queues it next season), or a
        # submission that is no longer active.
        unrunnable: str | None = None
        submission = None
        if settings.base(run.base_id) is None:
            unrunnable = f"base {run.base_id} is no longer allowlisted"
        elif run.submission_id is not None:
            submission = await session.get(ModelContextSubmission, run.submission_id)
            if submission is None or submission.status not in ACTIVE_STATUSES:
                unrunnable = "submission is no longer active"
        if unrunnable is not None:
            run.status = "abandoned"
            run.finished_at = now
            run.fault = unrunnable
            await session.flush()
            continue
        run.status = "leased"
        run.lease_id = uuid4()
        run.lease_expires_at = now + timedelta(seconds=settings.lease_ttl_seconds)
        run.attempts += 1
        run.started_at = now
        run.scorer_id = scorer_id
        run.fault = None
        await session.flush()
        return Lease(run=run, season=season, submission=submission)
    return None


class ResultRejected(ValueError):
    """A scorer post that does not match the current lease or the contract."""


async def _leased_run(
    session: AsyncSession, run_id: UUID, lease_id: UUID, *, now: datetime
) -> ModelContextRun:
    run = await session.get(ModelContextRun, run_id, with_for_update=True)
    if run is None:
        raise LookupError("unknown run")
    if run.status != "leased" or run.lease_id != lease_id:
        raise ResultRejected("lease is not current for this run")
    expires = run.lease_expires_at
    if expires is None or _aware(expires) <= now:
        raise ResultRejected("lease expired before the post arrived")
    return run


def check_result(payload: ModelContextResultRequest, seeds: Sequence[int]) -> None:
    """The result is internally consistent and on the season's seeds."""
    if payload.cpm_score_version != CPM_SCORE_VERSION:
        raise ResultRejected(
            f"cpm_score_version must be {CPM_SCORE_VERSION}, "
            f"not {payload.cpm_score_version}"
        )
    if [s.seed % (1 << 64) for s in payload.per_seed] != [int(s) for s in seeds]:
        raise ResultRejected("per_seed seeds do not match the season's seeds in order")
    for entry in payload.per_seed:
        expected = seed_score(
            entry.memory_recall, entry.distractor_rate, entry.tool_recall
        )
        if not math.isclose(entry.score, expected, abs_tol=SCORE_TOLERANCE):
            raise ResultRejected(
                f"seed {entry.seed} score {entry.score} does not match its "
                f"components ({expected})"
            )
    mean = sum(s.score for s in payload.per_seed) / len(payload.per_seed)
    if not math.isclose(payload.score, mean, abs_tol=SCORE_TOLERANCE):
        raise ResultRejected(f"score {payload.score} is not the per-seed mean {mean}")


@dataclass(frozen=True)
class RecordedResult:
    run: ModelContextRun
    submission_status: str | None
    base_floor: float | None


async def record_result(
    session: AsyncSession, payload: ModelContextResultRequest, *, now: datetime
) -> RecordedResult:
    """Apply one scorer result to its leased run (caller commits)."""
    run = await _leased_run(session, payload.run_id, payload.lease_id, now=now)
    season = await session.get(ModelContextSeason, run.season, with_for_update=True)
    assert season is not None
    if season.closed_at is not None:
        raise ResultRejected(f"season {season.season} is closed")
    check_result(payload, season.seeds)
    if season.gpu_sku is None:
        season.gpu_sku = payload.gpu_sku
    elif season.gpu_sku != payload.gpu_sku:
        raise ResultRejected(
            f"season {season.season} is scored on {season.gpu_sku}, "
            f"not {payload.gpu_sku}"
        )
    run.status = "scored"
    run.score = payload.score
    run.components = payload.components.model_dump(mode="json")
    run.per_seed = [s.model_dump(mode="json") for s in payload.per_seed]
    run.scorer_id = payload.scorer_id
    run.scorer_version = payload.scorer_version
    run.gpu_sku = payload.gpu_sku
    run.lease_id = None
    run.lease_expires_at = None
    run.finished_at = now
    run.fault = None
    submission_status: str | None = None
    base_floor: float | None = None
    if run.kind == "reference":
        floors = dict(season.base_floors or {})
        floors[run.base_id] = payload.score
        season.base_floors = floors
        base_floor = payload.score
    elif run.submission_id is not None:
        row = await session.get(
            ModelContextSubmission, run.submission_id, with_for_update=True
        )
        if row is not None:
            if row.status == "queued":
                row.status = "scored"
                row.updated_at = now
            submission_status = row.status
    await session.flush()
    return RecordedResult(
        run=run, submission_status=submission_status, base_floor=base_floor
    )


@dataclass(frozen=True)
class RecordedFault:
    run: ModelContextRun
    action: str
    submission_status: str | None


async def record_fault(
    session: AsyncSession,
    payload: ModelContextFaultRequest,
    *,
    settings: ModelContextSettings,
    now: datetime,
) -> RecordedFault:
    """Apply a scorer fault to its leased run (caller commits).

    ``infra`` re-queues the run until ``max_attempts``; ``nondeterministic``
    fails the run (contract section 2.4); ``model_load`` and ``invalid_model``
    fail it and reject the submission. A reference run never rejects anything.
    """
    run = await _leased_run(session, payload.run_id, payload.lease_id, now=now)
    # The request bounds detail so this fits MAX_FAULT_DETAIL_CHARS intact.
    detail = (
        f"{payload.error_kind}: {payload.detail}"
        if payload.detail
        else payload.error_kind
    )
    run.lease_id = None
    run.lease_expires_at = None
    run.scorer_id = payload.scorer_id
    run.scorer_version = payload.scorer_version
    run.fault = detail
    submission_status: str | None = None
    if payload.error_kind == "infra":
        if run.attempts >= settings.max_attempts:
            run.status = "abandoned"
            run.finished_at = now
            action = "abandoned"
        else:
            run.status = "queued"
            action = "released"
    else:
        run.status = "failed"
        run.finished_at = now
        action = "failed"
        if run.submission_id is not None:
            row = await session.get(
                ModelContextSubmission, run.submission_id, with_for_update=True
            )
            if row is not None:
                if payload.error_kind != "nondeterministic" and row.status in (
                    ACTIVE_STATUSES
                ):
                    row.status = "rejected"
                    row.reject_reason = _bounded(f"scorer: {detail}")
                    row.updated_at = now
                submission_status = row.status
    await session.flush()
    return RecordedFault(run=run, action=action, submission_status=submission_status)


# --- ledger and board --------------------------------------------------------


@dataclass(frozen=True)
class _Standing:
    submission: ModelContextSubmission
    run: ModelContextRun
    eligible: bool
    floor: float | None


async def _standings(
    session: AsyncSession,
    *,
    settings: ModelContextSettings,
    season: ModelContextSeason,
) -> list[_Standing]:
    latest = (
        select(ModelContextRun)
        .where(
            ModelContextRun.season == season.season,
            ModelContextRun.kind == "submission",
            ModelContextRun.status == "scored",
        )
        .order_by(ModelContextRun.submission_id, ModelContextRun.finished_at.desc())
        .distinct(ModelContextRun.submission_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(ModelContextSubmission, ModelContextRun)
            .join(
                latest, latest.c.submission_id == ModelContextSubmission.submission_id
            )
            .join(ModelContextRun, ModelContextRun.run_id == latest.c.run_id)
            .where(ModelContextSubmission.status.in_(ACTIVE_STATUSES))
        )
    ).all()
    floors = {k: float(v) for k, v in (season.base_floors or {}).items()}
    standings = []
    for submission, run in rows:
        floor = floors.get(submission.base_id)
        score = float(run.score or 0.0)
        eligible = floor is not None and score >= floor + settings.floor_margin
        standings.append(
            _Standing(submission=submission, run=run, eligible=eligible, floor=floor)
        )
    standings.sort(
        key=lambda s: (
            -float(s.run.score or 0.0),
            s.submission.commitment_block,
            str(s.submission.submission_id),
        )
    )
    return standings


async def build_ledger(
    session: AsyncSession, *, resolved: ResolvedSettings, now: datetime
) -> ModelContextLedgerResponse:
    settings = resolved.settings
    empty = ModelContextLedgerResponse(
        mode=settings.mode, rank_shares=list(settings.rank_shares), generated_at=now
    )
    if settings.mode == "off":
        return empty
    season = await current_season(session)
    if season is None:
        return empty
    entries = [
        ModelContextLedgerEntry(
            hotkey=s.submission.hotkey,
            submission_id=s.submission.submission_id,
            score=float(s.run.score or 0.0),
            eligible=s.eligible,
            commitment_block=s.submission.commitment_block,
            season=season.season,
        )
        for s in await _standings(session, settings=settings, season=season)
    ]
    return empty.model_copy(
        update={"entries": entries, "count": len(entries), "season": season.season}
    )


def model_url(row: ModelContextSubmission) -> str:
    if row.mirror_repo:
        return f"https://huggingface.co/{row.mirror_repo}"
    return f"https://huggingface.co/{row.hf_repo}/tree/{row.revision}"


async def build_board(
    session: AsyncSession, *, resolved: ResolvedSettings, now: datetime
) -> ModelContextBoardResponse:
    settings = resolved.settings
    season = await current_season(session)
    standings = (
        await _standings(session, settings=settings, season=season)
        if season is not None
        else []
    )
    return ModelContextBoardResponse(
        entries=[
            ModelContextBoardEntry(
                rank=index + 1,
                hotkey=s.submission.hotkey,
                submission_id=s.submission.submission_id,
                score=float(s.run.score or 0.0),
                eligible=s.eligible,
                commitment_block=s.submission.commitment_block,
                season=s.run.season,
                base_id=s.submission.base_id,
                kind=s.submission.kind,  # type: ignore[arg-type]
                hf_repo=s.submission.hf_repo,
                revision=s.submission.revision,
                model_url=model_url(s.submission),
                components=dict(s.run.components or {}),
                floor=s.floor,
            )
            for index, s in enumerate(standings)
        ],
        count=len(standings),
        season=season.season if season is not None else None,
        mode=settings.mode,
        floor_margin=settings.floor_margin,
        base_floors=(
            {k: float(v) for k, v in (season.base_floors or {}).items()}
            if season is not None
            else {}
        ),
        generated_at=now,
    )


def run_summary(run: ModelContextRun | None) -> ModelContextRunSummary | None:
    if run is None:
        return None
    return ModelContextRunSummary(
        run_id=run.run_id,
        season=run.season,
        cpm_score_version=run.cpm_score_version,
        status=run.status,  # type: ignore[arg-type]
        score=run.score,
        fault=run.fault,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


async def latest_runs(
    session: AsyncSession, submission_ids: Sequence[UUID]
) -> dict[UUID, ModelContextRun]:
    if not submission_ids:
        return {}
    rows = await session.scalars(
        select(ModelContextRun)
        .where(ModelContextRun.submission_id.in_(submission_ids))
        .order_by(ModelContextRun.submission_id, ModelContextRun.created_at.desc())
        .distinct(ModelContextRun.submission_id)
    )
    return {row.submission_id: row for row in rows if row.submission_id is not None}


def submission_summary(
    row: ModelContextSubmission, latest: ModelContextRun | None
) -> ModelContextSubmissionSummary:
    return ModelContextSubmissionSummary(
        submission_id=row.submission_id,
        hotkey=row.hotkey,
        hf_repo=row.hf_repo,
        revision=row.revision,
        manifest_sha256=row.manifest_sha256,
        weights_sha256=row.weights_sha256,
        base_id=row.base_id,
        kind=row.kind,  # type: ignore[arg-type]
        commitment_block=row.commitment_block,
        status=row.status,  # type: ignore[arg-type]
        reject_reason=row.reject_reason,
        mirror_repo=row.mirror_repo,
        model_url=model_url(row),
        created_at=row.created_at,
        latest_run=run_summary(latest),
    )


# --- background loop ---------------------------------------------------------


class ModelContextLoop:
    """Open and rotate seasons, and screen pending submissions.

    One loop per platform role. With ``mode=off`` each tick is one settings
    read. A chain read failure changes nothing: a season never opens on a
    guessed block. ``app_state.model_context_fetcher`` overrides the Hugging
    Face fetcher (tests inject one). Downloads land under
    ``DITTO_MODEL_CONTEXT_SCREEN_DIR`` (default: the system temp directory)
    and are removed after each screen; a host short of space retries later.
    """

    def __init__(
        self,
        *,
        app_state: Any,
        session_maker: async_sessionmaker[AsyncSession],
        interval_seconds: float = 30.0,
        max_screens_per_tick: int = 4,
    ) -> None:
        self._app_state = app_state
        self._session_maker = session_maker
        self._interval = interval_seconds
        self._max_screens = max_screens_per_tick
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.last_error: str | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="model-context-loop")

    async def aclose(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            await task

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
                self.last_error = None
            except Exception as exc:  # noqa: BLE001 - the loop survives any tick
                self.last_error = f"{type(exc).__name__}: {exc}"[:300]
                logger.exception("model_context loop tick failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)

    def _fetcher(self) -> ArtifactFetcher:
        fetcher = getattr(self._app_state, "model_context_fetcher", None)
        if fetcher is None:
            fetcher = HuggingFaceFetcher()
            self._app_state.model_context_fetcher = fetcher
        return fetcher

    async def _read_block(self) -> BlockInfo:
        chain = getattr(self._app_state, "chain", None)
        if chain is None:
            raise RuntimeError("chain client is unavailable")
        return await chain.get_finalized_block()

    async def tick(self) -> list[str]:
        actions: list[str] = []
        async with self._session_maker() as session:
            resolved = await load_settings(session)
            if resolved.settings.mode == "off":
                return actions
            due = season_due(await current_season(session), datetime.now(UTC))
        if due:
            season = await rotate_season(
                self._session_maker, read_block=self._read_block, force=False
            )
            if season is not None:
                actions.append(f"opened season {season}")
        for _ in range(self._max_screens):
            outcome = await screen_next(
                self._session_maker,
                self._fetcher(),
                workdir_root=os.environ.get(SCREEN_DIR_ENV) or None,
            )
            if outcome is None:
                break
            actions.append(f"screened {outcome[0]}: {outcome[1]}")
        return actions


async def rotate_in_session(
    session: AsyncSession, *, block: BlockInfo, force: bool
) -> int | None:
    """Open the next season when due (or ``force``) inside the caller's
    transaction; returns its number, or ``None`` (mode off, or not due).

    The open season is re-checked under the season lock, so two callers
    racing on the same boundary open one season."""
    resolved = await load_settings(session)
    if resolved.settings.mode == "off":
        return None
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": _SEASON_LOCK_KEY}
    )
    now = datetime.now(UTC)
    if not force and not season_due(await current_season(session), now):
        return None
    season = await open_season(session, resolved=resolved, block=block, now=now)
    return season.season


async def rotate_season(
    session_maker: async_sessionmaker[AsyncSession],
    *,
    read_block: Callable[[], Awaitable[BlockInfo]],
    force: bool,
) -> int | None:
    """Read the finalized head, then :func:`rotate_in_session` in one
    transaction (no transaction is held across the chain read)."""
    block = await read_block()
    async with session_maker() as session, session.begin():
        return await rotate_in_session(session, block=block, force=force)
