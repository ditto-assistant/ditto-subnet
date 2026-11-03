"""Safe public projection of wallet policy and live routing. No actors."""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.emission_allocation import EmissionSplitView
from ditto.api_models.treasury_allocation import (
    PublicEmissionShare,
    PublicServiceBucket,
    PublicTreasuryAllocation,
    PublicTreasuryRuntime,
)
from ditto.api_models.treasury_settings import TreasurySettings, public_wallet_address
from ditto.api_server.burn_settings import settings_from_row
from ditto.api_server.dependencies import get_session
from ditto.api_server.emission_allocation import PublicSplit, public_in_force_split
from ditto.api_server.koth import BLOCKS_PER_TEMPO
from ditto.api_server.ledger_pin import LedgerPin, treasury_pin_from_context
from ditto.api_server.treasury_runtime import (
    latest_runtime_row,
    runtime_revision,
    settings_match_policy,
)
from ditto.db.models import TreasurySettingsRevision
from ditto.db.queries.burn_settings import latest_burn_settings_revision
from ditto.db.queries.emission_allocation import latest_emission_allocation_number
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


RoutingStatus = Literal["not_activated", "scheduled", "active", "stale", "paused"]

# A pin covers one epoch: from its first block for BLOCKS_PER_TEMPO + 1
# blocks (~12 s each). Past that end, the current epoch's pin is missing:
# scoring serves the previous pin flagged stale and validators refuse the
# enforcing fold, so the page must not claim the split is being folded. The
# short grace only absorbs the next pin's materialization.
BLOCK_SECONDS = 12
NEXT_PIN_GRACE = timedelta(seconds=120)


def pin_epoch_ends_at(row: Any) -> datetime | None:
    pinned_at = row.pinned_at
    if pinned_at is None:
        return None
    if pinned_at.tzinfo is None:
        pinned_at = pinned_at.replace(tzinfo=UTC)
    remaining = row.last_epoch_block + BLOCKS_PER_TEMPO + 1 - row.pinned_block
    return pinned_at + timedelta(seconds=max(0, remaining) * BLOCK_SECONDS)


async def served_routing(
    session: AsyncSession,
    *,
    netuid: int,
    runtime: PublicTreasuryRuntime,
    now: datetime | None = None,
) -> tuple[RoutingStatus, int | None, float]:
    """Routing as validators were told to fold it, read from the newest pin.

    The pin is what every validator folds, so it -- not the recorded runtime --
    decides "active". An unreadable or stale pin never reports routing as
    active.
    """
    now = now or datetime.now(UTC)
    row = None
    try:
        row = await latest_pin(session, netuid=netuid)
        treasury = treasury_pin_from_context(LedgerPin.from_row(row)) if row else None
    except (TypeError, ValueError, SQLAlchemyError):
        # A malformed stored context never 500s the public page.
        treasury = None
    if isinstance(treasury, EnforcingTreasuryPin):
        ends_at = pin_epoch_ends_at(row) if row is not None else None
        if ends_at is None or now > ends_at + NEXT_PIN_GRACE:
            return "stale", treasury.epoch_index, 0.0
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
    netuid = request.app.state.config.chain.netuid
    try:
        in_force, configured = await public_in_force_split(session, netuid=netuid)
    except (ValueError, SQLAlchemyError):
        # A malformed stored policy never 500s the public page.
        in_force, configured = PublicSplit(None, None, "unavailable"), None
    allocation_revision = await latest_emission_allocation_number(session)
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
        emission_allocation_revision=allocation_revision,
        emission_split=_public_shares(in_force.view),
        emission_split_epoch=in_force.epoch,
        emission_split_source=in_force.source,
        forecast_split=_public_shares(configured),
    )


def _public_shares(view: EmissionSplitView | None) -> list[PublicEmissionShare]:
    if view is None:
        return []
    return [
        PublicEmissionShare(
            kind=share.kind,
            id=share.id if share.kind == "track" else share.bucket,
            share=share.bps / 10_000,
            bps=share.bps,
        )
        for share in view.split
    ]
