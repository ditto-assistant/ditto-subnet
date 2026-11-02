"""Feedback Track: credit Ditto feedback contributions to a miner's Ditto account.

This is identity plumbing. The Ditto backend (operator bearer) records that a
Ditto account filed a report, followed up, or had its report shipped; miners
see their own contributions through the account linked to their hotkey; the
public read returns counts only. Nothing here defines a reward, a weight
policy, or an emission split — ``weight`` is stored for a future policy and
consumed by nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.feedback_track import (
    FeedbackTrackContributionRequest,
    FeedbackTrackContributionResponse,
    FeedbackTrackContributionView,
    FeedbackTrackMeResponse,
    PublicFeedbackTrackResponse,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.endpoints.miner_auth import (
    require_scope,
    resolve_miner_session,
)
from ditto.db.models import FeedbackTrackContribution
from ditto.db.queries.feedback_track import (
    list_contributions_for_user,
    summarize_contributions_for_hotkey,
    upsert_contribution,
)
from ditto.db.queries.miner_ditto_links import get_active_link, get_link

router = APIRouter(tags=["feedback-track"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]


def _view(row: FeedbackTrackContribution) -> FeedbackTrackContributionView:
    return FeedbackTrackContributionView(
        contribution_id=row.contribution_id,
        source=row.source,  # type: ignore[arg-type]
        external_ref=row.external_ref,
        kind=row.kind,  # type: ignore[arg-type]
        weight=row.weight,
        note=row.note,
        recorded_at=row.recorded_at,
    )


@router.post(
    "/feedback-track/contributions",
    response_model=FeedbackTrackContributionResponse,
    dependencies=[Depends(require_admin)],
)
async def record_contribution(
    body: FeedbackTrackContributionRequest,
    request: Request,
    response: Response,
    session: SessionDep,
) -> FeedbackTrackContributionResponse:
    """Operator write. Idempotent on (source, external_ref, kind)."""
    response.headers["Cache-Control"] = "no-store"
    actor = (request.headers.get("x-admin-actor") or "ditto-backend").strip()[:120]
    now = datetime.now(UTC)
    async with session.begin():
        row, created = await upsert_contribution(
            session,
            ditto_user_id=body.ditto_user_id,
            source=body.source,
            external_ref=body.external_ref,
            kind=body.kind,
            weight=body.weight,
            note=body.note,
            recorded_by=actor,
            now=now,
        )
        view = _view(row)
    response.status_code = 201 if created else 200
    return FeedbackTrackContributionResponse(created=created, contribution=view)


@router.get("/me/feedback-track", response_model=FeedbackTrackMeResponse)
async def my_contributions(
    request: Request, response: Response, session: SessionDep
) -> FeedbackTrackMeResponse:
    response.headers["Cache-Control"] = "no-store"
    async with session.begin():
        row, _token = await resolve_miner_session(request, session)
        require_scope(row, "read")
        link = await get_active_link(session, hotkey=row.miner_hotkey)
        if link is None:
            return FeedbackTrackMeResponse(linked=False, contributions=[], counts={})
        rows = await list_contributions_for_user(
            session, ditto_user_id=link.ditto_user_id
        )
        counts = await summarize_contributions_for_hotkey(
            session, hotkey=row.miner_hotkey
        )
        views = [_view(r) for r in rows]
        user_id = link.ditto_user_id
    return FeedbackTrackMeResponse(
        linked=True, ditto_user_id=user_id, contributions=views, counts=counts
    )


@router.get(
    "/public/feedback-track/{hotkey}", response_model=PublicFeedbackTrackResponse
)
async def public_contributions(
    hotkey: str, response: Response, session: SessionDep
) -> PublicFeedbackTrackResponse:
    """Counts by kind for one hotkey. Never names the account."""
    if not (40 <= len(hotkey) <= 64) or not hotkey.isalnum():
        raise HTTPException(status_code=404, detail="unknown miner")
    response.headers["Cache-Control"] = "public, max-age=60"
    async with session.begin():
        link = await get_link(session, hotkey=hotkey)
        linked = link is not None and link.revoked_at is None
        counts = (
            await summarize_contributions_for_hotkey(session, hotkey=hotkey)
            if linked
            else {}
        )
    return PublicFeedbackTrackResponse(
        miner_hotkey=hotkey, linked=linked, counts=counts, total=sum(counts.values())
    )
