"""Public finalized treasury receipt history, separate from admin requests."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.treasury_activity import (
    PublicTreasuryEvent,
    PublicTreasuryEventPage,
)
from ditto.api_server.dependencies import get_session
from ditto.db.models import TreasuryPublicEvent, TreasuryVerifiedReceipt

router = APIRouter(prefix="/public/treasury-activity", tags=["public"])


@router.get("", response_model=PublicTreasuryEventPage)
async def list_treasury_activity(
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    before: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> PublicTreasuryEventPage:
    statement = select(
        TreasuryPublicEvent,
        TreasuryVerifiedReceipt.proof["extrinsic_hash"].as_string(),
    ).outerjoin(
        TreasuryVerifiedReceipt,
        and_(
            TreasuryVerifiedReceipt.receipt_id == TreasuryPublicEvent.payment_id,
            TreasuryVerifiedReceipt.public_event_id == TreasuryPublicEvent.id,
            TreasuryVerifiedReceipt.published.is_(True),
            TreasuryVerifiedReceipt.block_hash == TreasuryPublicEvent.block_hash,
            TreasuryVerifiedReceipt.extrinsic_index
            == TreasuryPublicEvent.extrinsic_index,
        ),
    )
    if before is not None:
        statement = statement.where(TreasuryPublicEvent.id < before)
    rows = (
        await session.execute(
            statement.order_by(TreasuryPublicEvent.id.desc()).limit(limit + 1)
        )
    ).all()
    items = [
        PublicTreasuryEvent.model_validate(
            {
                "id": row.id,
                "payment_id": row.payment_id,
                "bucket_id": row.bucket_id,
                "policy_digest": row.policy_digest,
                "epoch_index": row.epoch_index,
                "event_kind": row.event_kind,
                "state": row.state,
                "finalized_event_id": row.finalized_event_id,
                "event_at": row.event_at,
                "recorded_at": row.recorded_at,
                "policy_revision": row.policy_revision,
                "burn_revision": row.burn_revision,
                "burn_share_micros": row.burn_share_micros,
                "denominator": row.denominator,
                "maintenance_bps": row.maintenance_bps,
                "gm_bps": row.gm_bps,
                "allocation_bps": row.allocation_bps,
                "allocated_alpha_rao": str(row.allocated_alpha_rao),
                "source_alpha_rao": str(row.source_alpha_rao),
                "route": row.route,
                "deposit_asset": row.deposit_asset,
                "deposit_amount_atomic": str(row.deposit_amount_atomic),
                "credited_usd_nano": (
                    str(row.credited_usd_nano)
                    if row.credited_usd_nano is not None
                    else None
                ),
                "bounty_award_id": row.bounty_award_id,
                "accepted_work_ref": row.accepted_work_ref,
                "public_sender": row.public_sender,
                "public_recipient": row.public_recipient,
                "block_hash": row.block_hash,
                "extrinsic_index": row.extrinsic_index,
                "extrinsic_hash": extrinsic_hash,
                "event_index": row.event_index,
                "actor_provenance": row.actor_provenance,
                "actor_public_id": row.actor_public_id,
                "verification_source": row.verification_source,
            }
        )
        for row, extrinsic_hash in rows[:limit]
    ]
    response.headers["Cache-Control"] = "public, max-age=5"
    return PublicTreasuryEventPage(
        items=items, next_before=items[-1].id if len(rows) > limit else None
    )
