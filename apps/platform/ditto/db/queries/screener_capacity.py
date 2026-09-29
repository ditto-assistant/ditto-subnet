"""Shared controller fallback and operator admission gates for GCE."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.screener_node_settings import ScreenerNodeChannelSettings
from ditto.api_models.screener_provider_settings import ScreenerProviderSettings
from ditto.db.models import ScreenerCapacitySnapshot, ScreenerNode
from ditto.db.queries.screener_node_settings import (
    latest_screener_node_channel_settings,
)

ScreenerFallbackReason = Literal[
    "controller_missing", "controller_stale", "provider_not_ready", "controller_fresh"
]


def screener_fallback_active(
    snapshot: ScreenerCapacitySnapshot | None, now: datetime
) -> tuple[bool, ScreenerFallbackReason]:
    """Use the same expiry boundary for the metric and legacy GCP claims."""
    if snapshot is None:
        return True, "controller_missing"
    expiry = snapshot.controller_lease_expires_at
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    if now >= expiry:
        return True, "controller_stale"
    if not snapshot.provider_ready:
        return True, "provider_not_ready"
    return False, "controller_fresh"


async def screener_gcp_fallback_allowed(
    session: AsyncSession, *, environment: str, settings: ScreenerProviderSettings
) -> bool:
    """Never let a controller outage bypass the current operator stop.

    Explicit GCP-first routing is the operator's outage override. Hetzner
    overflow and retired-provider routing require a known, open primary;
    readiness and heartbeat loss are host failures, not admission closures.
    Read current settings rather than a potentially stale controller snapshot.
    """
    lanes = (
        settings.build_provider_priority,
        settings.runtime_provider_priority,
        settings.source_review_provider_priority,
    )
    if all(lane[0] != "hetzner" for lane in lanes) and any(
        lane[0] == "gcp" for lane in lanes
    ):
        return True
    if any(lane[0] == "hetzner" for lane in lanes) and (
        not settings.gce_overflow_enabled or settings.gce_overflow_max_instances == 0
    ):
        return False
    if settings.primary_node_id is None:
        return False
    primary = await session.get(ScreenerNode, settings.primary_node_id)
    if (
        primary is None
        or primary.environment != environment
        or primary.provider != "hetzner"
    ):
        return False
    revision = await latest_screener_node_channel_settings(
        session, node_id=primary.node_id
    )
    if revision is None or revision.environment != environment:
        return False
    channels = ScreenerNodeChannelSettings.model_validate(revision.settings)
    return channels.screening_concurrency > 0
