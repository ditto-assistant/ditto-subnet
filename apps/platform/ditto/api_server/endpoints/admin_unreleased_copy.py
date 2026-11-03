"""Operator reads for the unreleased-copy signal (shadow mode).

Both routes are read-only. ``live=true`` recomputes against the current index
and reads the artifacts for evidence, but writes nothing.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.unreleased_copy import (
    AdminUnreleasedCopyCoverage,
    AdminUnreleasedCopyEffectiveSettings,
    AdminUnreleasedCopyIndex,
    AdminUnreleasedCopyOverview,
    AdminUnreleasedCopyReleasedOverlap,
    AdminUnreleasedCopyRuntime,
    AdminUnreleasedCopySettingsRequest,
    AdminUnreleasedCopySettingsResponse,
    AdminUnreleasedCopySettingsRevision,
    AdminUnreleasedCopySignal,
    AdminUnreleasedCopySignalList,
    AdminUnreleasedCopySummary,
    UnreleasedCopyEvaluation,
    UnreleasedCopySettings,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.unreleased_copy import SIGNAL_VERSION
from ditto.api_server.unreleased_copy_sentinel import (
    UnreleasedCopySentinel,
    inline_call,
    score_signal,
)
from ditto.db.models import (
    Agent,
    AgentDistinctiveItems,
    UnreleasedCopySettingsRevision,
    UnreleasedCopySignal,
)
from ditto.db.queries.unreleased_copy import (
    effective_settings,
    index_coverage,
    list_signals,
    load_context,
    settings_history,
    signal_payload,
)

router = APIRouter(prefix="/admin/unreleased-copy", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]


def _runtime(request: Request) -> AdminUnreleasedCopyRuntime | None:
    sentinel = getattr(request.app.state, "unreleased_copy_sentinel", None)
    if not isinstance(sentinel, UnreleasedCopySentinel):
        return None
    return AdminUnreleasedCopyRuntime.model_validate(sentinel.runtime())


@router.get("/agents/{agent_id}", response_model=AdminUnreleasedCopySignal)
async def get_unreleased_copy_signal(
    agent_id: UUID,
    request: Request,
    _admin: AdminDep,
    session: SessionDep,
    live: Annotated[bool, Query()] = False,
) -> AdminUnreleasedCopySignal:
    """One agent's recorded signal, shared items and matched agents."""
    signal = await read_signal(session, request, agent_id, live=live)
    if signal is None:
        raise HTTPException(status_code=404, detail="agent not found")
    return signal


async def read_signal(
    session: AsyncSession, request: Request, agent_id: UUID, *, live: bool
) -> AdminUnreleasedCopySignal | None:
    """The per-agent read, shared with the copy-court recommendation feed."""
    agent = await session.get(Agent, agent_id)
    if agent is None:
        return None
    index = await session.get(AgentDistinctiveItems, agent_id)
    recorded = await session.get(UnreleasedCopySignal, (agent_id, SIGNAL_VERSION))
    live_payload: dict[str, Any] | None = None
    live_reason: str | None = None
    released_overlap: list[AdminUnreleasedCopyReleasedOverlap] = []
    if live:
        state_sentinel = getattr(request.app.state, "unreleased_copy_sentinel", None)
        sentinel = (
            state_sentinel
            if isinstance(state_sentinel, UnreleasedCopySentinel)
            else None
        )
        if sentinel is not None:
            await sentinel.sync_worker_index()
        context = await load_context(
            session, agent, overlap=sentinel.overlap if sentinel is not None else None
        )
        if context is None:
            live_reason = (
                "artifact is not indexed yet"
                if index is None
                else "artifact could not be read"
            )
        else:
            settings = await effective_settings(session)
            signal, evidence_status = (
                await sentinel.score(context, threshold=settings.threshold)
                if sentinel is not None
                else await score_signal(
                    context,
                    getattr(request.app.state, "storage", None),
                    threshold=settings.threshold,
                    call=inline_call,
                )
            )
            live_payload = signal_payload(signal, evidence_status=evidence_status)
            released_overlap = _released_overlap(context)
    return AdminUnreleasedCopySignal(
        agent_id=agent.agent_id,
        agent_name=agent.name,
        agent_version=agent.version,
        miner_hotkey=agent.miner_hotkey,
        agent_status=agent.status.value,
        uploaded_at=agent.created_at,
        index=(
            AdminUnreleasedCopyIndex(
                items_version=index.items_version,
                item_count=index.item_count,
                error=index.error,
                indexed_at=index.indexed_at,
            )
            if index is not None
            else None
        ),
        mode=recorded.mode if recorded is not None else None,
        computed_at=recorded.computed_at if recorded is not None else None,
        recorded=(
            UnreleasedCopyEvaluation.model_validate(recorded.evidence)
            if recorded is not None
            else None
        ),
        live=(
            UnreleasedCopyEvaluation.model_validate(live_payload)
            if live_payload is not None
            else None
        ),
        live_unavailable_reason=live_reason,
        runtime=_runtime(request),
        released_overlap=released_overlap,
    )


def _released_overlap(context: Any) -> list[AdminUnreleasedCopyReleasedOverlap]:
    """Which released-before-upload agents remove the candidate's items."""
    t = context.candidate.uploaded_at
    rows = [
        AdminUnreleasedCopyReleasedOverlap(
            agent_id=other.agent_id,
            agent_name=other.name,
            agent_version=other.version,
            released_at=other.released_at,
            shared=len(context.overlaps.get(other.agent_id, ())),
        )
        for other in context.others
        if other.released_at is not None and other.released_at < t
    ]
    rows.sort(key=lambda r: r.shared, reverse=True)
    return rows[:10]


async def _summaries(
    session: AsyncSession, rows: list[tuple[UnreleasedCopySignal, Agent]]
) -> list[AdminUnreleasedCopySummary]:
    matched_ids = {s.top_matched_agent_id for s, _ in rows if s.top_matched_agent_id}
    matched = (
        {
            a.agent_id: a
            for a in (
                await session.scalars(
                    select(Agent).where(Agent.agent_id.in_(matched_ids))
                )
            ).all()
        }
        if matched_ids
        else {}
    )
    items = []
    for signal, agent in rows:
        top = (
            matched.get(signal.top_matched_agent_id)
            if signal.top_matched_agent_id
            else None
        )
        enforcement = (signal.evidence or {}).get("enforcement") or {}
        items.append(
            AdminUnreleasedCopySummary(
                agent_id=agent.agent_id,
                agent_name=agent.name,
                agent_version=agent.version,
                miner_hotkey=agent.miner_hotkey,
                agent_status=agent.status.value,
                uploaded_at=agent.created_at,
                mode=signal.mode,
                flagged=signal.flagged,
                top_score=signal.top_score,
                top_matched_agent_id=signal.top_matched_agent_id,
                top_matched_name=top.name if top is not None else None,
                top_matched_version=top.version if top is not None else None,
                computed_at=signal.computed_at,
                enforcement_outcome=enforcement.get("outcome"),
                excluded=(signal.evidence or {}).get("excluded"),
                candidate_item_count=signal.candidate_item_count,
                compared_agent_count=signal.compared_agent_count,
            )
        )
    return items


def _effective(settings: Any) -> AdminUnreleasedCopyEffectiveSettings:
    return AdminUnreleasedCopyEffectiveSettings(
        mode=settings.mode,
        threshold=settings.threshold,
        revision=settings.revision,
        enforce_since=settings.enforce_since,
    )


async def unreleased_copy_overview(
    session: AsyncSession, request: Request
) -> AdminUnreleasedCopyOverview:
    """Posture + coverage + top flags, embedded in the copy-court settings read."""
    _, rows = await list_signals(
        session,
        signal_version=SIGNAL_VERSION,
        flagged_only=True,
        min_score=0.0,
        limit=50,
        offset=0,
    )
    near = (
        await session.execute(
            select(UnreleasedCopySignal, Agent)
            .join(Agent, Agent.agent_id == UnreleasedCopySignal.agent_id)
            .where(
                UnreleasedCopySignal.signal_version == SIGNAL_VERSION,
                UnreleasedCopySignal.flagged.is_(False),
                UnreleasedCopySignal.top_score > 0,
            )
            .order_by(UnreleasedCopySignal.top_score.desc())
            .limit(25)
        )
    ).tuples()
    return AdminUnreleasedCopyOverview(
        effective=_effective(await effective_settings(session)),
        coverage=AdminUnreleasedCopyCoverage.model_validate(
            await index_coverage(session, signal_version=SIGNAL_VERSION)
        ),
        runtime=_runtime(request),
        flagged=await _summaries(session, list(rows)),
        near_misses=await _summaries(session, list(near.all())),
    )


@router.get("/signals", response_model=AdminUnreleasedCopySignalList)
async def list_unreleased_copy_signals(
    request: Request,
    _admin: AdminDep,
    session: SessionDep,
    flagged_only: Annotated[bool, Query()] = True,
    min_score: Annotated[float, Query(ge=0)] = 0.0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AdminUnreleasedCopySignalList:
    """Recorded signals by score: the operator review list, never public."""
    total, rows = await list_signals(
        session,
        signal_version=SIGNAL_VERSION,
        flagged_only=flagged_only,
        min_score=min_score,
        limit=limit,
        offset=offset,
    )
    coverage = await index_coverage(session, signal_version=SIGNAL_VERSION)
    return AdminUnreleasedCopySignalList(
        items=await _summaries(session, list(rows)),
        total=total,
        limit=limit,
        offset=offset,
        coverage=AdminUnreleasedCopyCoverage.model_validate(coverage),
        runtime=_runtime(request),
    )


def _revision(
    row: UnreleasedCopySettingsRevision,
) -> AdminUnreleasedCopySettingsRevision:
    return AdminUnreleasedCopySettingsRevision(
        revision=row.revision,
        parent_revision=row.parent_revision,
        settings=UnreleasedCopySettings(mode=row.mode, threshold=row.threshold),  # type: ignore[arg-type]
        reason=row.reason,
        actor=row.actor,
        created_at=row.created_at,
    )


@router.get("/settings", response_model=AdminUnreleasedCopySettingsResponse)
async def get_unreleased_copy_settings(
    request: Request,
    _admin: AdminDep,
    session: SessionDep,
    history_limit: Annotated[int, Query(ge=0, le=200)] = 20,
) -> AdminUnreleasedCopySettingsResponse:
    """Effective mode/threshold, append-only history (newest first), runtime."""
    rows = await settings_history(session, limit=max(history_limit, 1))
    return AdminUnreleasedCopySettingsResponse(
        effective=_effective(await effective_settings(session)),
        history=[_revision(row) for row in rows[:history_limit]],
        runtime=_runtime(request),
    )


@router.post("/settings", response_model=AdminUnreleasedCopySettingsRevision)
async def apply_unreleased_copy_settings(
    payload: AdminUnreleasedCopySettingsRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> AdminUnreleasedCopySettingsRevision:
    """Append one optimistic settings revision under an exact confirmation."""
    confirmation = f"APPLY UNRELEASED COPY {payload.settings.mode.upper()}"
    if payload.confirmation != confirmation:
        raise HTTPException(
            status_code=409, detail=f"confirmation must be exactly {confirmation}"
        )
    history = await settings_history(session, limit=1)
    current = history[0].revision if history else 0
    if payload.expected_revision != current:
        raise HTTPException(
            status_code=409,
            detail=(
                "unreleased-copy settings changed; refresh before applying "
                f"(expected {payload.expected_revision}, current {current})"
            ),
        )
    row = UnreleasedCopySettingsRevision(
        parent_revision=current,
        mode=payload.settings.mode,
        threshold=payload.settings.threshold,
        reason=payload.reason.strip(),
        actor=payload.actor.strip(),
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="unreleased-copy settings changed concurrently; refresh",
        ) from error
    await session.refresh(row)
    return _revision(row)
