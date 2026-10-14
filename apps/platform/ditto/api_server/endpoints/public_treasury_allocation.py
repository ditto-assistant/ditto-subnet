"""Safe public projection of wallet policy and live routing. No actors."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.treasury_allocation import (
    PublicServiceBucket,
    PublicTreasuryAllocation,
    PublicTreasuryRuntime,
)
from ditto.api_models.treasury_settings import TreasurySettings, public_wallet_address
from ditto.api_server.burn_settings import settings_from_row
from ditto.api_server.dependencies import get_session
from ditto.api_server.ledger_pin import LedgerPin, treasury_pin_from_context
from ditto.api_server.treasury_runtime import (
    latest_runtime_row,
    runtime_revision,
    settings_match_policy,
)
from ditto.db.models import TreasurySettingsRevision
from ditto.db.queries.burn_settings import latest_burn_settings_revision
from ditto.db.queries.ledger_epochs import latest_pin
from ditto_screening_protocol.treasury_enforcement import EnforcingTreasuryPin

router = APIRouter(prefix="/public/treasury-allocation", tags=["public"])


async def public_runtime(
    session: AsyncSession, allocation: TreasurySettings
) -> PublicTreasuryRuntime:
    """Expose only verified control mode, never approval, actor or failure text."""
    row = None
    try:
        row = await latest_runtime_row(session)
        if row is None:
            return PublicTreasuryRuntime()
        settings = runtime_revision(row).settings
        policy = settings.approval.policy
        matches = settings_match_policy(allocation, policy)
        return PublicTreasuryRuntime(
            revision=row.revision,
            mode=settings.mode,
            activation_epoch=settings.activation_epoch,
            allocation_matches=matches,
        )
    except (ValueError, SQLAlchemyError):
        return PublicTreasuryRuntime(
            revision=row.revision if row else 0, mode="unavailable"
        )


RoutingStatus = Literal["not_activated", "scheduled", "active", "paused"]


async def served_routing(
    session: AsyncSession, *, netuid: int, runtime: PublicTreasuryRuntime
) -> tuple[RoutingStatus, int | None, float]:
    """Routing as validators were told to fold it, read from the newest pin.

    The pin is what every validator folds, so it -- not the recorded runtime --
    decides "active". An unreadable pin never reports routing as active.
    """
    try:
        row = await latest_pin(session, netuid=netuid)
        treasury = treasury_pin_from_context(LedgerPin.from_row(row)) if row else None
    except (ValueError, SQLAlchemyError):
        treasury = None
    if isinstance(treasury, EnforcingTreasuryPin):
        return "active", treasury.epoch_index, treasury.service_bps / 10_000
    if runtime.mode == "enforce":
        return "scheduled", None, 0.0
    if runtime.mode == "pause":
        return "paused", None, 0.0
    return "not_activated", None, 0.0


@router.get("", response_model=PublicTreasuryAllocation)
async def get_public_treasury_allocation(
    request: Request,
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
    runtime = await public_runtime(session, policy)
    routing_status, routing_epoch, effective = await served_routing(
        session, netuid=request.app.state.config.chain.netuid, runtime=runtime
    )
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
        mode=runtime.mode
        if runtime.mode in ("observe", "enforce", "pause")
        else "shadow",
        routing_status=routing_status,
        routing_epoch=routing_epoch,
        effective_service_share=effective,
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
        runtime=runtime,
    )
