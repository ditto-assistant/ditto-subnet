"""Operator reads and the audited rotation for dueling-bench seasons (v16+).

- ``GET /admin/generator-competition/dueling/seasons``: the dueling target
  (version, shadow, why nothing can open), the open season(s), the committed
  next season(s), and recent closed/revealed/void ones.
- ``GET /admin/generator-competition/dueling/seasons/{season_id}``: one season
  with its commitment, frozen columns, battery results, per-column means
  across in-season scores and slice counts.
- ``GET /admin/generator-competition/dueling/slices``: slice preparations with
  status, attempts, latency and digests (never the bytes).
- ``POST /admin/generator-competition/dueling/rotate``: close the open season
  at the next round boundary, or now with ``immediate``, under the
  confirmation ``ROTATE DUELING SEASON`` and an audited reason.

The season secret and seed are served only once a season is revealed.
"""

from __future__ import annotations

import statistics
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Float, func, select, true
from sqlalchemy import column as sa_column
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.dueling import (
    DUELING_COLUMN_FLOOR,
    ROTATE_DUELING_SEASON_CONFIRMATION,
    AdminDuelingRotateRequest,
    AdminDuelingSeasonsResponse,
    AdminDuelingSliceRetryRequest,
    AdminDuelingSlicesResponse,
    DuelingBatteryView,
    DuelingColumnMean,
    DuelingRotationView,
    DuelingSeasonView,
    DuelingSliceStatus,
    DuelingSliceView,
    DuelingTargetView,
    retry_dueling_slice_confirmation,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.dueling_seasons import (
    dueling_target,
    open_season,
    retry_slice,
    slice_retry_refusal,
)
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.generator_competition import load_settings
from ditto.db.models import (
    ConfirmationScore,
    DuelingSeason,
    DuelingSeasonBattery,
    DuelingSlicePreparation,
    Score,
    ScoreAuditEntry,
)
from ditto.db.queries.audit import (
    EVENT_SCORE_RETEST_QUEUED,
    EVENT_SCORE_RETEST_REQUESTED,
    SCORE_RETEST_EVENTS,
)
from ditto.db.queries.benchmark_rollout import SCORING_QUORUM
from ditto.db.queries.dueling_season_window import dueling_season_window
from ditto.db.queries.generator import record_operator_action
from ditto.db.queries.score_retests import rotation_request_id
from ditto_screening_protocol.dueling import DUELING_MIN_BENCH_VERSION

router = APIRouter(prefix="/admin/generator-competition/dueling", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]

_RECENT = 10
_PRIVATE_DETAIL_KEYS = frozenset({"fallback"})


async def _column_means(
    session: AsyncSession, season: DuelingSeason
) -> list[DuelingColumnMean]:
    """Each pinned column's mean case score across the season's scores."""
    if not season.columns:
        return []
    totals: dict[str, list[float]] = {
        column["column_id"]: [0, 0.0, 0] for column in season.columns
    }
    # Aggregated in PostgreSQL: one row per column, whatever the season's
    # score count.
    element = (
        func.jsonb_array_elements(Score.dueling["columns"])
        .table_valued(sa_column("value", JSONB))
        .alias("element")
    )
    column_id_label = element.c.value["column_id"].astext.label("column_id")
    for column_id, cases, total, scores in await session.execute(
        select(
            column_id_label,
            func.sum(element.c.value["n"].astext.cast(Float)),
            func.sum(element.c.value["sum"].astext.cast(Float)),
            func.count(),
        )
        .select_from(Score)
        .join(element, true())
        .where(Score.dueling_season_id == season.id, Score.dueling.is_not(None))
        .group_by(column_id_label)
    ):
        bucket = totals.get(str(column_id))
        if bucket is not None:
            bucket[0] = float(cases or 0)
            bucket[1] = float(total or 0.0)
            bucket[2] = int(scores or 0)
    means = {
        cid: (bucket[1] / bucket[0]) if bucket[0] else None
        for cid, bucket in totals.items()
    }
    defaults = [
        means[c["column_id"]]
        for c in season.columns
        if c.get("public_default") and means[c["column_id"]] is not None
    ]
    anchor = statistics.fmean(defaults) if defaults else None  # type: ignore[arg-type]
    out: list[DuelingColumnMean] = []
    for column in season.columns:
        cid = column["column_id"]
        mean = means[cid]
        out.append(
            DuelingColumnMean(
                column_id=cid,
                submission_id=str(column["submission_id"]),
                owner_key=str(column.get("owner_key") or ""),
                public_default=bool(column.get("public_default")),
                scores=int(totals[cid][2]),
                cases=int(totals[cid][0]),
                mean=mean,
                default_gap=(None if mean is None or anchor is None else mean - anchor),
                below_floor=mean is not None and mean < DUELING_COLUMN_FLOOR,
            )
        )
    return out


async def _rotation(
    session: AsyncSession, season: DuelingSeason
) -> DuelingRotationView:
    """The season-open re-scoring and its progress, from the audit log."""
    window = await dueling_season_window(session, season.bench_version)
    standing = (
        "not_opened"
        if season.status in ("committed", "void")
        else "current"
        if window is not None and window.current == season.id
        else "provisional"
        if window is not None and window.previous == season.id
        else "expired"
    )
    recorded = dict((season.detail or {}).get("rotation") or {})
    cohort = [
        UUID(str(member["agent_id"]))
        for member in recorded.get("cohort", ())
        if isinstance(member, dict) and member.get("agent_id")
    ]
    latest: dict[tuple[UUID, str | None], ScoreAuditEntry] = {}
    if cohort:
        for entry in await session.scalars(
            select(ScoreAuditEntry)
            .where(
                ScoreAuditEntry.agent_id.in_(cohort),
                ScoreAuditEntry.event.in_(SCORE_RETEST_EVENTS),
            )
            .order_by(ScoreAuditEntry.seq)
        ):
            latest[(entry.agent_id, entry.validator_hotkey)] = entry
    pending = running = closed = 0
    for (agent_id, validator_hotkey), entry in latest.items():
        payload = entry.payload or {}
        ours = str(rotation_request_id(season.id, agent_id, validator_hotkey or ""))
        if ours not in (payload.get("request_id"), payload.get("retest_request_id")):
            continue  # this season never queued it, or a later re-test replaced it
        if entry.event == EVENT_SCORE_RETEST_QUEUED:
            pending += 1
        elif entry.event == EVENT_SCORE_RETEST_REQUESTED:
            running += 1
        else:
            closed += 1
    per_agent = dict(
        (
            await session.execute(
                select(Score.agent_id, func.count())
                .where(
                    Score.dueling_season_id == season.id,
                    Score.agent_id.in_(cohort or [UUID(int=0)]),
                )
                .group_by(Score.agent_id)
            )
        )
        .tuples()
        .all()
    )
    scores_in_season = await session.scalar(
        select(func.count())
        .select_from(Score)
        .where(Score.dueling_season_id == season.id)
    )
    confirmations_in_season = await session.scalar(
        select(func.count())
        .select_from(ConfirmationScore)
        .where(ConfirmationScore.dueling_season_id == season.id)
    )
    return DuelingRotationView(
        standing=standing,  # type: ignore[arg-type]
        source=recorded.get("source"),
        cohort=list(recorded.get("cohort", ())),
        queued_at=recorded.get("queued_at"),
        queued=int(recorded.get("queued", 0)),
        already_current=int(recorded.get("already_current", 0)),
        busy=int(recorded.get("busy", 0)),
        error=recorded.get("error"),
        pending=pending,
        running=running,
        closed=closed,
        cohort_scores_in_season=sum(int(n) for n in per_agent.values()),
        cohort_quorum_in_season=sum(
            1 for n in per_agent.values() if int(n) >= SCORING_QUORUM
        ),
        scores_in_season=int(scores_in_season or 0),
        confirmation_scores_in_season=int(confirmations_in_season or 0),
    )


async def _view(
    session: AsyncSession, season: DuelingSeason, *, full: bool
) -> DuelingSeasonView:
    revealed = season.status == "revealed"
    detail = season.detail or {}
    view: dict[str, Any] = {
        "id": season.id,
        "bench_version": season.bench_version,
        "season_index": season.season_index,
        "shadow": season.shadow,
        "status": season.status,
        "commitment": season.commitment,
        "committed_at": season.committed_at,
        "rounds_per_season": season.rounds_per_season,
        "settings_revision": season.settings_revision,
        "anchor_block": season.anchor_block,
        "anchor_block_hash": season.anchor_block_hash,
        "opened_at": season.opened_at,
        "closes_at_round": season.closes_at_round,
        "closed_at": season.closed_at,
        "reveal_at_epoch": season.reveal_at_epoch,
        "revealed_at": season.revealed_at,
        "void_reason": season.void_reason,
        "rotation_requested_at": season.rotation_requested_at,
        "rotation_immediate": season.rotation_immediate,
        "rotation_actor": season.rotation_actor,
        "rotation_reason": season.rotation_reason,
        "columns": season.columns,
        "fallback": detail.get("fallback"),
        "detail": {
            key: value
            for key, value in detail.items()
            if key not in _PRIVATE_DETAIL_KEYS
        }
        or None,
        "secret_hex": season.secret.hex() if revealed else None,
        "season_seed": season.season_seed if revealed else None,
    }
    if full:
        view["batteries"] = [
            DuelingBatteryView(
                submission_id=row.submission_id,
                miner_hotkey=row.miner_hotkey,
                owner_key=row.owner_key,
                tree_sha256=row.tree_sha256,
                rank=row.rank,
                evidence=row.evidence,
                status=row.status,  # type: ignore[arg-type]
                attempts=row.attempts,
                grading_bench_version=row.grading_bench_version,
                error=row.error,
                scorer_version=row.scorer_version,
                finished_at=row.finished_at,
            )
            for row in await session.scalars(
                select(DuelingSeasonBattery)
                .where(DuelingSeasonBattery.season_id == season.id)
                .order_by(DuelingSeasonBattery.rank)
            )
        ]
        view["column_means"] = await _column_means(session, season)
        view["rotation"] = await _rotation(session, season)
        view["slices"] = {
            str(status): int(count)
            for status, count in await session.execute(
                select(DuelingSlicePreparation.status, func.count())
                .where(DuelingSlicePreparation.season_id == season.id)
                .group_by(DuelingSlicePreparation.status)
            )
        }
    return DuelingSeasonView(**view)


@router.get("/seasons", response_model=AdminDuelingSeasonsResponse)
async def admin_dueling_seasons(
    _admin: AdminDep, session: SessionDep
) -> AdminDuelingSeasonsResponse:
    """The dueling target, the open and committed seasons, and recent ones."""
    resolved = await load_settings(session)
    target = await dueling_target(session, resolved.settings)

    async def _status(*statuses: str, limit: int | None = None) -> list[DuelingSeason]:
        stmt = (
            select(DuelingSeason)
            .where(DuelingSeason.status.in_(statuses))
            .order_by(
                DuelingSeason.season_index.desc(), DuelingSeason.created_at.desc()
            )
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(await session.scalars(stmt))

    floor = DUELING_MIN_BENCH_VERSION
    missing = await session.scalar(
        select(func.count())
        .select_from(Score)
        .where(
            Score.bench_version >= floor,
            (Score.dueling_season_id.is_(None)) | (Score.dueling.is_(None)),
        )
    )
    missing_confirmations = await session.scalar(
        select(func.count())
        .select_from(ConfirmationScore)
        .where(
            ConfirmationScore.bench_version >= floor,
            (ConfirmationScore.dueling_season_id.is_(None))
            | (ConfirmationScore.dueling.is_(None)),
        )
    )
    return AdminDuelingSeasonsResponse(
        scores_without_dueling=int(missing or 0),
        confirmation_scores_without_dueling=int(missing_confirmations or 0),
        target=DuelingTargetView(
            bench_version=target.bench_version,
            shadow=target.shadow,
            blocked=target.blocked,
            live_versions=sorted(target.live_versions),
        ),
        current=[await _view(session, s, full=True) for s in await _status("open")],
        next=[await _view(session, s, full=True) for s in await _status("committed")],
        recent=[
            await _view(session, s, full=False)
            for s in await _status("closed", "revealed", "void", limit=_RECENT)
        ],
    )


@router.get("/seasons/{season_id}", response_model=DuelingSeasonView)
async def admin_dueling_season(
    season_id: UUID, _admin: AdminDep, session: SessionDep
) -> DuelingSeasonView:
    season = await session.get(DuelingSeason, season_id)
    if season is None:
        raise HTTPException(status_code=404, detail="unknown dueling season")
    return await _view(session, season, full=True)


@router.get("/slices", response_model=AdminDuelingSlicesResponse)
async def admin_dueling_slices(
    _admin: AdminDep,
    session: SessionDep,
    season_id: UUID | None = None,
    status: DuelingSliceStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
    preparation_id: UUID | None = None,
) -> AdminDuelingSlicesResponse:
    """Slice preparations, newest first, with latency, digests and whether a
    failed one can be retried (``retry_dueling_slice_preparation``)."""
    filters = []
    if preparation_id is not None:
        filters.append(DuelingSlicePreparation.id == preparation_id)
    if season_id is not None:
        filters.append(DuelingSlicePreparation.season_id == season_id)
    if status is not None:
        filters.append(DuelingSlicePreparation.status == status)
    rows = (
        await session.execute(
            select(
                DuelingSlicePreparation.id,
                DuelingSlicePreparation.season_id,
                DuelingSlicePreparation.run_seed,
                DuelingSlicePreparation.status,
                DuelingSlicePreparation.attempts,
                DuelingSlicePreparation.slice_sha256,
                func.octet_length(DuelingSlicePreparation.slice_bytes).label("size"),
                DuelingSlicePreparation.bytes_pruned_at,
                DuelingSlicePreparation.columns,
                DuelingSlicePreparation.error,
                DuelingSlicePreparation.scorer_version,
                DuelingSlicePreparation.created_at,
                DuelingSlicePreparation.first_leased_at,
                DuelingSlicePreparation.ready_at,
                DuelingSlicePreparation.failed_at,
                DuelingSlicePreparation.retries,
                DuelingSlicePreparation.last_retry_at,
                DuelingSlicePreparation.last_retry_actor,
                DuelingSlicePreparation.last_retry_reason,
                DuelingSeason.season_index,
                DuelingSeason.bench_version,
                DuelingSeason.status.label("season_status"),
            )
            .join(DuelingSeason, DuelingSeason.id == DuelingSlicePreparation.season_id)
            .where(*filters)
            .order_by(
                DuelingSlicePreparation.created_at.desc(), DuelingSlicePreparation.id
            )
            .offset(offset)
            .limit(limit)
        )
    ).all()
    counts = {
        str(state): int(count)
        for state, count in await session.execute(
            select(DuelingSlicePreparation.status, func.count())
            .where(*filters)
            .group_by(DuelingSlicePreparation.status)
        )
    }
    views: list[DuelingSliceView] = []
    latencies: list[float] = []
    for row in rows:
        finished = row.ready_at or row.failed_at
        latency = (
            (finished - row.created_at).total_seconds()
            if finished is not None
            else None
        )
        if row.ready_at is not None and latency is not None:
            latencies.append(latency)
        refusal = _retry_refusal(row.status, row.error, row.season_status, force=False)
        views.append(
            DuelingSliceView(
                id=row.id,
                season_id=row.season_id,
                season_index=row.season_index,
                bench_version=row.bench_version,
                run_seed=str(row.run_seed),
                status=row.status,
                attempts=row.attempts,
                slice_sha256=row.slice_sha256,
                size_bytes=row.size,
                bytes_pruned=row.bytes_pruned_at is not None,
                columns=row.columns,
                error=row.error,
                scorer_version=row.scorer_version,
                created_at=row.created_at,
                first_leased_at=row.first_leased_at,
                ready_at=row.ready_at,
                failed_at=row.failed_at,
                latency_seconds=latency,
                retries=row.retries,
                last_retry_at=row.last_retry_at,
                last_retry_actor=row.last_retry_actor,
                last_retry_reason=row.last_retry_reason,
                retryable=row.status == "failed" and refusal is None,
                retry_refusal=refusal if row.status == "failed" else None,
            )
        )
    p95 = None
    if latencies:
        ordered = sorted(latencies)
        p95 = ordered[min(len(ordered) - 1, int(0.95 * (len(ordered) - 1) + 0.5))]
    return AdminDuelingSlicesResponse(
        slices=views,
        counts=counts,
        latency_p50_seconds=statistics.median(latencies) if latencies else None,
        latency_p95_seconds=p95,
    )


@router.post("/rotate", response_model=DuelingSeasonView)
async def admin_rotate_dueling_season(
    payload: AdminDuelingRotateRequest, _admin: AdminDep, session: SessionDep
) -> DuelingSeasonView:
    """Rotate the open season: it closes at the next round boundary (or now,
    with ``immediate``) and the next season opens at the following boundary
    on a fresh battery. Never swaps columns in place."""
    if payload.confirmation != ROTATE_DUELING_SEASON_CONFIRMATION:
        raise HTTPException(
            status_code=409,
            detail=f"confirmation must be exactly {ROTATE_DUELING_SEASON_CONFIRMATION}",
        )
    reason = payload.reason.strip()
    actor = payload.actor.strip()
    if len(reason) < 8 or not actor:
        raise HTTPException(
            status_code=422, detail="a rotation needs a reason (8+ chars) and an actor"
        )
    if payload.season_id is not None:
        season = await session.scalar(
            select(DuelingSeason)
            .where(DuelingSeason.id == payload.season_id)
            .with_for_update()
        )
    else:
        resolved = await load_settings(session)
        target = await dueling_target(session, resolved.settings)
        season = (
            await open_season(session, target.bench_version, for_update=True)
            if target.bench_version is not None
            else None
        )
    if season is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "unknown dueling season"
                if payload.season_id is not None
                else "no open dueling season to rotate"
            ),
        )
    if season.status != "open":
        raise HTTPException(
            status_code=409,
            detail=f"season is {season.status}; only an open season rotates",
        )
    if season.rotation_requested_at is not None and (
        season.rotation_immediate or not payload.immediate
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                f"season already rotated by {season.rotation_actor} at "
                f"{season.rotation_requested_at.isoformat()}"
            ),
        )
    now = datetime.now(UTC)
    season.rotation_requested_at = now
    season.rotation_immediate = payload.immediate
    season.rotation_actor = actor[:120]
    season.rotation_reason = reason
    record_operator_action(
        session,
        action="rotate_dueling_season",
        subject=f"dueling_season:{season.id}",
        actor=actor,
        reason=reason,
        detail={
            "season_index": season.season_index,
            "bench_version": season.bench_version,
            "immediate": payload.immediate,
        },
    )
    await session.commit()
    await session.refresh(season)
    return await _view(session, season, full=True)


def _retry_refusal(
    status: str, error: str | None, season_status: str | None, *, force: bool
) -> str | None:
    """:func:`slice_retry_refusal` on already-read columns."""
    row = DuelingSlicePreparation(status=status, error=error)
    season = None if season_status is None else DuelingSeason(status=season_status)
    return slice_retry_refusal(row, season, force=force)


@router.post("/slices/{preparation_id}/retry", response_model=DuelingSliceView)
async def admin_retry_dueling_slice(
    preparation_id: UUID,
    payload: AdminDuelingSliceRetryRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> DuelingSliceView:
    """Re-queue exactly one failed slice preparation: status pending, attempts
    kept, error cleared, so the next lease of its seed reserves it again."""
    expected = retry_dueling_slice_confirmation(preparation_id)
    if payload.confirmation != expected:
        raise HTTPException(
            status_code=409, detail=f"confirmation must be exactly {expected}"
        )
    reason = payload.reason.strip()
    actor = payload.actor.strip()
    if len(reason) < 8 or not actor:
        raise HTTPException(
            status_code=422, detail="a retry needs a reason (8+ chars) and an actor"
        )
    row = await session.scalar(
        select(DuelingSlicePreparation)
        .where(DuelingSlicePreparation.id == preparation_id)
        .with_for_update()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="unknown slice preparation")
    if row.status != payload.expected_status or row.error != payload.expected_error:
        raise HTTPException(
            status_code=409,
            detail=(
                f"preparation is {row.status} with error {row.error!r}; re-read "
                "list_dueling_slices and retry against what it shows"
            ),
        )
    # Locked so a season closing concurrently cannot race the open check.
    season = await session.scalar(
        select(DuelingSeason).where(DuelingSeason.id == row.season_id).with_for_update()
    )
    refused = slice_retry_refusal(row, season, force=payload.force)
    if refused is not None:
        raise HTTPException(status_code=409, detail=refused)
    previous_error = row.error
    previous_columns = row.columns
    previous_scorer_version = row.scorer_version
    retry_slice(row, actor=actor[:120], reason=reason, now=datetime.now(UTC))
    record_operator_action(
        session,
        action="retry_dueling_slice_preparation",
        subject=f"dueling_slice:{row.id}",
        actor=actor,
        reason=reason,
        detail={
            "season_id": str(row.season_id),
            "run_seed": str(row.run_seed),
            "previous_error": previous_error,
            "previous_columns": previous_columns,
            "previous_scorer_version": previous_scorer_version,
            "attempts": row.attempts,
            "retries": row.retries,
            "force": payload.force,
        },
    )
    await session.commit()
    listing = await admin_dueling_slices(
        _admin,
        session,
        season_id=None,
        status=None,
        limit=1,
        offset=0,
        preparation_id=preparation_id,
    )
    return listing.slices[0]
