"""Safe public projection of wallet policy. No account references or actors."""

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.treasury_allocation import (
    PublicServiceBucket,
    PublicTreasuryAllocation,
)
from ditto.api_models.treasury_settings import TreasurySettings, public_wallet_address
from ditto.api_server.burn_settings import settings_from_row
from ditto.api_server.dependencies import get_session
from ditto.db.models import TreasurySettingsRevision
from ditto.db.queries.burn_settings import latest_burn_settings_revision

router = APIRouter(prefix="/public/treasury-allocation", tags=["public"])


@router.get("", response_model=PublicTreasuryAllocation)
async def get_public_treasury_allocation(
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PublicTreasuryAllocation:
    row = await session.scalar(
        select(TreasurySettingsRevision)
        .order_by(TreasurySettingsRevision.revision.desc())
        .limit(1)
    )
    policy = (
        TreasurySettings.model_validate(row.settings) if row else TreasurySettings()
    )
    burn_row = await latest_burn_settings_revision(session)
    burn = settings_from_row(burn_row).burn_share
    service_bps = 10_000 - policy.miner_bps
    fraction = service_bps / 10_000
    # v1 retains its released-share denominator, v2 is carved before burn.
    service = fraction if policy.allocation_version == 2 else fraction * (1 - burn)
    forecast_burn = (1 - fraction) * burn if policy.allocation_version == 2 else burn
    response.headers["Cache-Control"] = "public, max-age=5"
    return PublicTreasuryAllocation(
        policy_revision=row.revision if row else 0,
        allocation_version=policy.allocation_version,
        denominator="miner_emission"
        if policy.allocation_version == 2
        else "released_miner_emission",
        service_bps=service_bps,
        forecast_service_share=service,
        forecast_burn_share=forecast_burn,
        forecast_miner_share=(1 - fraction) * (1 - burn),
        burn_revision=burn_row.revision if burn_row else 0,
        burn_of_miner_remainder=burn,
        collector_hotkey=public_wallet_address(policy.treasury_hotkey),
        collector_coldkey=public_wallet_address(policy.treasury_coldkey),
        sweep_interval_hours=policy.sweep_interval_hours,
        buckets=[
            PublicServiceBucket(
                bucket_id=b.bucket_id,
                purpose=b.purpose,
                allocation_bps=b.allocation_bps,
                holding_coldkey=b.holding_coldkey,
                publish_payments=b.publish_payments,
                payee_rules=b.payee_rules,
            )
            for b in policy.service_buckets
        ],
    )
