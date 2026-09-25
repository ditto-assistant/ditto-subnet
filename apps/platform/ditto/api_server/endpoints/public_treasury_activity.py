"""Public finalized treasury receipt history, separate from admin requests."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.treasury_activity import (
    PublicTreasuryEvent,
    PublicTreasuryEventPage,
)
from ditto.api_server.dependencies import get_session
from ditto.db.models import TreasuryPublicEvent

router = APIRouter(prefix="/public/treasury-activity", tags=["public"])


@router.get("", response_model=PublicTreasuryEventPage)
async def list_treasury_activity(
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    before: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> PublicTreasuryEventPage:
    statement = select(TreasuryPublicEvent)
    if before is not None:
        statement = statement.where(TreasuryPublicEvent.id < before)
    rows = (
        (
            await session.execute(
                statement.order_by(TreasuryPublicEvent.id.desc()).limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    items = [
        PublicTreasuryEvent.model_validate(row, from_attributes=True)
        for row in rows[:limit]
    ]
    response.headers["Cache-Control"] = "public, max-age=5"
    return PublicTreasuryEventPage(
        items=items, next_before=items[-1].id if len(rows) > limit else None
    )
