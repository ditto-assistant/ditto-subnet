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
        PublicTreasuryEvent(
            id=row.id,
            payment_id=row.payment_id,
            event_kind=row.event_kind,
            state=row.state,
            event_at=row.event_at,
            recorded_at=row.recorded_at,
            policy_revision=row.policy_revision,
            burn_revision=row.burn_revision,
            denominator=row.denominator,
            allocation_bps=row.allocation_bps,
            allocated_alpha_rao=str(row.allocated_alpha_rao),
            route=row.route,
            asset=row.asset,
            gross_amount_atomic=str(row.gross_amount_atomic),
            realized_amount_atomic=(
                str(row.realized_amount_atomic)
                if row.realized_amount_atomic is not None
                else None
            ),
            public_sender=row.public_sender,
            public_recipient=row.public_recipient,
            block_hash=row.block_hash,
            extrinsic_index=row.extrinsic_index,
            event_index=row.event_index,
            actor_provenance=row.actor_provenance,
            verification_source=row.verification_source,
        )
        for row in rows[:limit]
    ]
    response.headers["Cache-Control"] = "public, max-age=5"
    return PublicTreasuryEventPage(
        items=items, next_before=items[-1].id if len(rows) > limit else None
    )
