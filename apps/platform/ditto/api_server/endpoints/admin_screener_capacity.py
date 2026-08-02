"""Authenticated operations view for federated screener capacity."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.screener_nodes import (
    ScreenerCapacityEventView,
    ScreenerCapacitySnapshotResponse,
    ScreenerCapacityView,
    ScreenerNodeStatus,
    ScreenerNodeView,
    ScreenerProvider,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.models import (
    ScreenerCapacityEvent,
    ScreenerCapacitySnapshot,
    ScreenerHeartbeat,
    ScreenerNode,
)

router = APIRouter(prefix="/admin", tags=["admin"])
AdminDep = Annotated[None, Depends(require_admin)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _required_aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@router.get("/screener-capacity", response_model=ScreenerCapacityView)
async def screener_capacity(
    _admin: AdminDep,
    session: SessionDep,
    environment: Annotated[str, Query(pattern=r"^[a-z][a-z0-9-]{0,31}$")] = "prod",
) -> ScreenerCapacityView:
    """Return controller state, enrolled nodes, and recent redacted audit events."""
    snapshot = await session.get(ScreenerCapacitySnapshot, environment)
    node_rows = list(
        await session.scalars(
            select(ScreenerNode)
            .where(ScreenerNode.environment == environment)
            .order_by(ScreenerNode.node_id)
        )
    )
    heartbeats: dict[tuple[str, str], ScreenerHeartbeat] = {}
    for row in await session.scalars(
        select(ScreenerHeartbeat).order_by(desc(ScreenerHeartbeat.seen_at))
    ):
        heartbeats.setdefault((row.screener_hotkey, row.instance_id), row)
    event_rows = list(
        await session.scalars(
            select(ScreenerCapacityEvent)
            .where(ScreenerCapacityEvent.environment == environment)
            .order_by(desc(ScreenerCapacityEvent.created_at))
            .limit(200)
        )
    )
    snapshot_view = None
    if snapshot is not None:
        snapshot_view = ScreenerCapacitySnapshotResponse(
            environment=snapshot.environment,
            controller_epoch=snapshot.controller_epoch,
            controller_heartbeat_at=_required_aware(snapshot.controller_heartbeat_at),
            controller_lease_expires_at=_required_aware(
                snapshot.controller_lease_expires_at
            ),
            runnable_backlog=snapshot.runnable_backlog,
            active_leases=snapshot.active_leases,
            desired_slots=snapshot.desired_slots,
            global_cap=snapshot.global_cap,
            provider_ready=snapshot.provider_ready,
            targon_capability=cast(
                Literal["go", "nogo", "unknown"], snapshot.targon_capability
            ),
            targon_available=snapshot.targon_available,
            targon_healthy=snapshot.targon_healthy,
            targon_pending=snapshot.targon_pending,
            targon_draining=snapshot.targon_draining,
            gce_target=snapshot.gce_target,
            gce_healthy=snapshot.gce_healthy,
            gce_pending=snapshot.gce_pending,
            gce_draining=snapshot.gce_draining,
            fallback_reason=snapshot.fallback_reason,
            last_provider_success_at=_aware(snapshot.last_provider_success_at),
            last_provider_error_code=snapshot.last_provider_error_code,
            last_provider_error_at=_aware(snapshot.last_provider_error_at),
            updated_at=_required_aware(snapshot.updated_at),
        )
    nodes = []
    for node in node_rows:
        heartbeat = heartbeats.get((node.screener_hotkey, node.node_id))
        progress = (
            heartbeat.system_metrics.get("screening_progress")
            if heartbeat is not None and isinstance(heartbeat.system_metrics, dict)
            else None
        )
        phase = (
            str(progress.get("stage"))
            if isinstance(progress, dict) and progress.get("stage")
            else (heartbeat.state if heartbeat is not None else None)
        )
        nodes.append(
            ScreenerNodeView(
                environment=node.environment,
                node_id=node.node_id,
                provider=cast(ScreenerProvider, node.provider),
                provider_resource_id=node.provider_resource_id,
                screener_hotkey=node.screener_hotkey,
                status=cast(ScreenerNodeStatus, node.status),
                capacity=node.capacity,
                token_expires_at=_required_aware(node.token_expires_at),
                registered_at=_required_aware(node.registered_at),
                rotated_at=_required_aware(node.rotated_at),
                revoked_at=_aware(node.revoked_at),
                status_reason=node.status_reason,
                heartbeat_seen_at=(
                    _aware(heartbeat.seen_at) if heartbeat is not None else None
                ),
                software_version=(
                    heartbeat.software_version if heartbeat is not None else None
                ),
                protocol_version=(
                    heartbeat.protocol_version if heartbeat is not None else None
                ),
                policy_version=(
                    heartbeat.policy_version if heartbeat is not None else None
                ),
                current_phase=phase,
            )
        )
    events = [
        ScreenerCapacityEventView(
            event_id=row.event_id,
            event_type=row.event_type,
            provider=cast(ScreenerProvider | None, row.provider),
            node_id=row.node_id,
            detail=row.detail,
            controller_epoch=row.controller_epoch,
            created_at=_required_aware(row.created_at),
        )
        for row in event_rows
    ]
    return ScreenerCapacityView(snapshot=snapshot_view, nodes=nodes, events=events)
