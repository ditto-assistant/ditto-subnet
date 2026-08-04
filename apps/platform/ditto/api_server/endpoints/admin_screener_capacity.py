"""Authenticated operations view for federated screener capacity."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal, cast
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.screener_nodes import (
    ScreenerCapacityEventView,
    ScreenerCapacitySnapshotResponse,
    ScreenerCapacityView,
    ScreenerNodeStatus,
    ScreenerNodeView,
    ScreenerProvider,
    TrustedImageBuildCreateRequest,
    TrustedImageBuildStatus,
    TrustedImageBuildView,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.models import (
    ScreenerCapacityEvent,
    ScreenerCapacitySnapshot,
    ScreenerHeartbeat,
    ScreenerNode,
    TrustedImageBuild,
)

router = APIRouter(prefix="/admin", tags=["admin"])
AdminDep = Annotated[None, Depends(require_admin)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
_SOURCE_REPOSITORY = "https://github.com/ditto-assistant/ditto-subnet.git"
_RUNTIME_REGISTRY = "us-central1-docker.pkg.dev/ditto-app-dev/ditto-public-runtime"


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _required_aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _build_view(row: TrustedImageBuild) -> TrustedImageBuildView:
    return TrustedImageBuildView(
        build_id=row.build_id,
        environment=row.environment,
        component=cast(Literal["screener"], row.component),
        source_repository=row.source_repository,
        source_sha=row.source_sha,
        context_path=row.context_path,
        dockerfile_path=row.dockerfile_path,
        destination=row.destination,
        status=cast(TrustedImageBuildStatus, row.status),
        provider=cast(Literal["targon", "gcp"] | None, row.provider),
        provider_resource_id=row.provider_resource_id,
        image_digest=row.image_digest,
        error_code=row.error_code,
        attempt_count=row.attempt_count,
        controller_epoch=row.controller_epoch,
        lease_expires_at=_aware(row.lease_expires_at),
        created_by=row.created_by,
        reason=row.reason,
        created_at=_required_aware(row.created_at),
        started_at=_aware(row.started_at),
        completed_at=_aware(row.completed_at),
        updated_at=_required_aware(row.updated_at),
    )


@router.post("/trusted-image-builds", response_model=TrustedImageBuildView)
async def create_trusted_image_build(
    payload: TrustedImageBuildCreateRequest,
    _admin: AdminDep,
    session: SessionDep,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> TrustedImageBuildView:
    """Queue one allowlisted monorepo image build; never accept arbitrary paths."""
    actor = x_admin_actor.strip() if x_admin_actor else ""
    if not 1 <= len(actor) <= 120:
        raise HTTPException(status_code=400, detail="X-Admin-Actor is required")
    async with session.begin():
        existing = await session.scalar(
            select(TrustedImageBuild).where(
                TrustedImageBuild.environment == "prod",
                TrustedImageBuild.component == payload.component,
                TrustedImageBuild.source_sha == payload.source_sha,
            )
        )
        if existing is not None:
            return _build_view(existing)
        row = TrustedImageBuild(
            build_id=uuid4(),
            environment="prod",
            component="screener",
            source_repository=_SOURCE_REPOSITORY,
            source_sha=payload.source_sha,
            context_path=".",
            dockerfile_path="workers/screener/Dockerfile",
            destination=f"{_RUNTIME_REGISTRY}/screener:sha-{payload.source_sha}",
            status="queued",
            created_by=actor,
            reason=payload.reason,
        )
        session.add(row)
    await session.refresh(row)
    return _build_view(row)


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
    build_rows = list(
        await session.scalars(
            select(TrustedImageBuild)
            .where(TrustedImageBuild.environment == environment)
            .order_by(desc(TrustedImageBuild.created_at))
            .limit(100)
        )
    )
    snapshot_view = None
    if snapshot is not None:
        snapshot_view = ScreenerCapacitySnapshotResponse(
            environment=snapshot.environment,
            controller_epoch=snapshot.controller_epoch,
            controller_source_sha=snapshot.controller_source_sha,
            provider_ready=snapshot.provider_ready,
            controller_heartbeat_at=_required_aware(snapshot.controller_heartbeat_at),
            controller_lease_expires_at=_required_aware(
                snapshot.controller_lease_expires_at
            ),
            runnable_backlog=snapshot.runnable_backlog,
            active_leases=snapshot.active_leases,
            desired_slots=snapshot.desired_slots,
            global_cap=snapshot.global_cap,
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
                image_reference=node.image_reference,
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
    return ScreenerCapacityView(
        snapshot=snapshot_view,
        nodes=nodes,
        events=events,
        builds=[_build_view(row) for row in build_rows],
    )
