"""Authenticated operations view for federated screener capacity."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.screener_node_settings import (
    ScreenerNodeAdminStatusWriteRequest,
    ScreenerNodeChannelSettings,
    ScreenerNodeChannelSettingsControl,
    ScreenerNodeChannelSettingsRevision,
    ScreenerNodeChannelSettingsWriteRequest,
    ScreenerNodeChannelUsage,
    node_channel_settings_confirmation,
    node_status_confirmation,
)
from ditto.api_models.screener_nodes import (
    ScreenerCapacityEventView,
    ScreenerCapacitySnapshotResponse,
    ScreenerCapacityView,
    ScreenerNodeStatus,
    ScreenerNodeView,
    ScreenerProvider,
    ScreenerProviderJobView,
    TrustedImageBuildCreateRequest,
    TrustedImageBuildRetryRequest,
    TrustedImageBuildStatus,
    TrustedImageBuildView,
)
from ditto.api_models.screener_provider_settings import (
    ScreenerProviderSettings,
    ScreenerProviderSettingsControl,
    ScreenerProviderSettingsWriteRequest,
    provider_settings_confirmation,
)
from ditto.api_models.screener_provider_settings import (
    ScreenerProviderSettingsRevision as ProviderSettingsRevisionModel,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.models import (
    ScreenerCapacityEvent,
    ScreenerCapacitySnapshot,
    ScreenerHeartbeat,
    ScreenerNode,
    ScreenerProviderSettingsRevision,
    SubmissionImageBuild,
    SubmissionSourceReview,
    TrustedImageBuild,
)
from ditto.db.models import (
    ScreenerNodeChannelSettingsRevision as ScreenerNodeChannelSettingsRevisionRow,
)
from ditto.db.queries.screener_node_settings import (
    DEFAULT_SCREENER_NODE_CHANNEL_SETTINGS,
    latest_screener_node_channel_settings,
)
from ditto.db.queries.screener_provider_settings import (
    DEFAULT_SCREENER_PROVIDER_SETTINGS,
    latest_screener_provider_settings,
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
        provider=cast(Literal["targon", "gcp", "hetzner"] | None, row.provider),
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


def _provider_revision(
    row: ScreenerProviderSettingsRevision,
) -> ProviderSettingsRevisionModel:
    return ProviderSettingsRevisionModel(
        environment=row.environment,
        revision=row.revision,
        parent_revision=row.parent_revision,
        settings=ScreenerProviderSettings.model_validate(row.settings),
        reason=row.reason,
        actor=row.actor,
        created_at=_required_aware(row.created_at),
    )


def _node_channel_revision(
    row: ScreenerNodeChannelSettingsRevisionRow,
) -> ScreenerNodeChannelSettingsRevision:
    return ScreenerNodeChannelSettingsRevision(
        environment=row.environment,
        node_id=row.node_id,
        revision=row.revision,
        parent_revision=row.parent_revision,
        settings=ScreenerNodeChannelSettings.model_validate(row.settings),
        reason=row.reason,
        actor=row.actor,
        created_at=_required_aware(row.created_at),
    )


def _default_node_channel_revision(
    *, environment: str, node_id: str
) -> ScreenerNodeChannelSettingsRevision:
    return ScreenerNodeChannelSettingsRevision(
        environment=environment,
        node_id=node_id,
        revision=0,
        parent_revision=0,
        settings=DEFAULT_SCREENER_NODE_CHANNEL_SETTINGS,
        reason="New nodes are disabled until an operator assigns capacity",
        actor="platform",
        created_at=None,
    )


async def _node_channel_control(
    session: AsyncSession, *, environment: str, node_id: str
) -> ScreenerNodeChannelSettingsControl:
    rows = list(
        await session.scalars(
            select(ScreenerNodeChannelSettingsRevisionRow)
            .where(
                ScreenerNodeChannelSettingsRevisionRow.environment == environment,
                ScreenerNodeChannelSettingsRevisionRow.node_id == node_id,
            )
            .order_by(desc(ScreenerNodeChannelSettingsRevisionRow.revision))
            .limit(100)
        )
    )
    build_active = int(
        await session.scalar(
            select(func.count())
            .select_from(SubmissionImageBuild)
            .where(
                SubmissionImageBuild.node_id == node_id,
                SubmissionImageBuild.status.in_(("leased", "running")),
            )
        )
        or 0
    )
    runtime_active = int(
        await session.scalar(
            select(func.count())
            .select_from(SubmissionImageBuild)
            .where(
                SubmissionImageBuild.runtime_node_id == node_id,
                SubmissionImageBuild.runtime_status == "running",
            )
        )
        or 0
    )
    source_review_active = int(
        await session.scalar(
            select(func.count())
            .select_from(SubmissionSourceReview)
            .where(
                SubmissionSourceReview.node_id == node_id,
                SubmissionSourceReview.status.in_(("leased", "running")),
            )
        )
        or 0
    )
    return ScreenerNodeChannelSettingsControl(
        current=(
            _node_channel_revision(rows[0])
            if rows
            else _default_node_channel_revision(
                environment=environment, node_id=node_id
            )
        ),
        history=[_node_channel_revision(row) for row in rows],
        usage=ScreenerNodeChannelUsage(
            sandbox_active=build_active + runtime_active,
            build_active=build_active,
            runtime_active=runtime_active,
            source_review_active=source_review_active,
        ),
    )


def _default_provider_revision(environment: str) -> ProviderSettingsRevisionModel:
    return ProviderSettingsRevisionModel(
        environment=environment,
        revision=0,
        parent_revision=0,
        settings=DEFAULT_SCREENER_PROVIDER_SETTINGS,
        reason="Built-in single-shot Targon settings",
        actor="platform",
        created_at=None,
    )


async def _provider_control(
    session: AsyncSession, *, environment: str
) -> ScreenerProviderSettingsControl:
    rows = list(
        await session.scalars(
            select(ScreenerProviderSettingsRevision)
            .where(ScreenerProviderSettingsRevision.environment == environment)
            .order_by(desc(ScreenerProviderSettingsRevision.revision))
            .limit(100)
        )
    )
    return ScreenerProviderSettingsControl(
        current=(
            _provider_revision(rows[0])
            if rows
            else _default_provider_revision(environment)
        ),
        history=[_provider_revision(row) for row in rows],
    )


@router.get(
    "/screener-provider-settings", response_model=ScreenerProviderSettingsControl
)
async def get_screener_provider_settings(
    _admin: AdminDep,
    session: SessionDep,
    environment: Annotated[str, Query(pattern=r"^[a-z][a-z0-9-]{0,31}$")] = "prod",
) -> ScreenerProviderSettingsControl:
    return await _provider_control(session, environment=environment)


@router.post(
    "/screener-provider-settings", response_model=ProviderSettingsRevisionModel
)
async def set_screener_provider_settings(
    payload: ScreenerProviderSettingsWriteRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> ProviderSettingsRevisionModel:
    expected_confirmation = provider_settings_confirmation(payload.settings)
    if payload.confirmation != expected_confirmation:
        raise HTTPException(
            status_code=409,
            detail=f"confirmation must be exactly {expected_confirmation}",
        )
    latest = await latest_screener_provider_settings(
        session, environment=payload.environment
    )
    actual_revision = latest.revision if latest is not None else 0
    if payload.expected_revision != actual_revision:
        raise HTTPException(
            status_code=409,
            detail=(
                "screener provider settings changed; refresh before applying "
                f"(expected {payload.expected_revision}, current {actual_revision})"
            ),
        )
    row = ScreenerProviderSettingsRevision(
        environment=payload.environment,
        parent_revision=actual_revision,
        settings=payload.settings.model_dump(mode="json"),
        reason=payload.reason.strip(),
        actor=payload.actor.strip(),
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                "screener provider settings changed concurrently; "
                "refresh before applying"
            ),
        ) from error
    await session.refresh(row)
    return _provider_revision(row)


@router.get(
    "/screener-nodes/{node_id}/channel-settings",
    response_model=ScreenerNodeChannelSettingsControl,
)
async def get_screener_node_channel_settings(
    node_id: str,
    _admin: AdminDep,
    session: SessionDep,
    environment: Annotated[str, Query(pattern=r"^[a-z][a-z0-9-]{0,31}$")] = "prod",
) -> ScreenerNodeChannelSettingsControl:
    node = await session.get(ScreenerNode, node_id)
    if node is None or node.environment != environment:
        raise HTTPException(status_code=404, detail="screener node not found")
    return await _node_channel_control(
        session, environment=environment, node_id=node_id
    )


@router.post(
    "/screener-nodes/{node_id}/channel-settings",
    response_model=ScreenerNodeChannelSettingsRevision,
)
async def set_screener_node_channel_settings(
    node_id: str,
    payload: ScreenerNodeChannelSettingsWriteRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> ScreenerNodeChannelSettingsRevision:
    expected_confirmation = node_channel_settings_confirmation(
        node_id, payload.settings
    )
    if payload.confirmation != expected_confirmation:
        raise HTTPException(
            status_code=409,
            detail=f"confirmation must be exactly {expected_confirmation}",
        )
    node = await session.get(ScreenerNode, node_id)
    if node is None or node.environment != payload.environment:
        raise HTTPException(status_code=404, detail="screener node not found")
    latest = await latest_screener_node_channel_settings(session, node_id=node_id)
    actual_revision = latest.revision if latest is not None else 0
    if payload.expected_revision != actual_revision:
        raise HTTPException(
            status_code=409,
            detail=(
                "screener node channel settings changed; refresh before applying "
                f"(expected {payload.expected_revision}, current {actual_revision})"
            ),
        )
    row = ScreenerNodeChannelSettingsRevisionRow(
        environment=payload.environment,
        node_id=node_id,
        parent_revision=actual_revision,
        settings=payload.settings.model_dump(mode="json"),
        reason=payload.reason.strip(),
        actor=payload.actor.strip(),
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                "screener node channel settings changed concurrently; "
                "refresh before applying"
            ),
        ) from error
    await session.refresh(row)
    return _node_channel_revision(row)


@router.post(
    "/screener-nodes/{node_id}/status",
    response_model=None,
    status_code=204,
)
async def set_screener_node_status(
    node_id: str,
    payload: ScreenerNodeAdminStatusWriteRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> None:
    expected_confirmation = node_status_confirmation(node_id, payload.status)
    if payload.confirmation != expected_confirmation:
        raise HTTPException(
            status_code=409,
            detail=f"confirmation must be exactly {expected_confirmation}",
        )
    now = datetime.now(UTC)
    async with session.begin():
        node = await session.scalar(
            select(ScreenerNode)
            .where(ScreenerNode.node_id == node_id)
            .with_for_update()
        )
        if node is None or node.environment != payload.environment:
            raise HTTPException(status_code=404, detail="screener node not found")
        if node.status != payload.expected_status:
            raise HTTPException(
                status_code=409,
                detail=(
                    "screener node status changed; refresh before applying "
                    f"(expected {payload.expected_status}, current {node.status})"
                ),
            )
        node.status = payload.status
        node.status_reason = payload.reason.strip()
        session.add(
            ScreenerCapacityEvent(
                event_id=uuid4(),
                environment=payload.environment,
                event_type="node_status_changed",
                provider=cast(ScreenerProvider, node.provider),
                node_id=node.node_id,
                detail=(
                    f"status={payload.status} actor={payload.actor.strip()} "
                    f"reason={payload.reason.strip()}"
                )[:500],
                controller_epoch="backroom-node-control",
                created_at=now,
            )
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


@router.post(
    "/trusted-image-builds/{build_id}/retry",
    response_model=TrustedImageBuildView,
)
async def retry_trusted_image_build(
    build_id: UUID,
    payload: TrustedImageBuildRetryRequest,
    _admin: AdminDep,
    session: SessionDep,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> TrustedImageBuildView:
    """Manually requeue one exact terminal trusted build without erasing attempts."""
    actor = x_admin_actor.strip() if x_admin_actor else ""
    if not 1 <= len(actor) <= 120:
        raise HTTPException(status_code=400, detail="X-Admin-Actor is required")
    now = datetime.now(UTC)
    async with session.begin():
        row = await session.scalar(
            select(TrustedImageBuild)
            .where(TrustedImageBuild.build_id == build_id)
            .with_for_update()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="trusted image build not found")
        if row.status != payload.expected_status:
            raise HTTPException(
                status_code=409, detail="trusted image build status changed"
            )
        if row.attempt_count != payload.expected_attempt_count:
            raise HTTPException(
                status_code=409, detail="trusted image build attempts changed"
            )
        row.status = "queued"
        row.provider = None
        row.provider_resource_id = None
        row.image_digest = None
        row.error_code = None
        row.controller_epoch = None
        row.lease_expires_at = None
        row.started_at = None
        row.completed_at = None
        row.updated_at = now
        session.add(
            ScreenerCapacityEvent(
                event_id=uuid4(),
                environment=row.environment,
                event_type="trusted_build_manual_retry",
                provider=None,
                node_id=None,
                detail=(
                    f"build_id={build_id} attempts={row.attempt_count} "
                    f"actor={actor} reason={payload.reason.strip()}"
                )[:500],
                controller_epoch="backroom-manual-retry",
                created_at=now,
            )
        )
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
    submission_build_rows = list(
        await session.scalars(
            select(SubmissionImageBuild)
            .where(SubmissionImageBuild.environment == environment)
            .order_by(desc(SubmissionImageBuild.created_at))
            .limit(50)
        )
    )
    source_review_rows = list(
        await session.scalars(
            select(SubmissionSourceReview)
            .where(SubmissionSourceReview.environment == environment)
            .order_by(desc(SubmissionSourceReview.created_at))
            .limit(50)
        )
    )
    snapshot_view = None
    if snapshot is not None:
        snapshot_view = ScreenerCapacitySnapshotResponse(
            environment=snapshot.environment,
            controller_epoch=snapshot.controller_epoch,
            controller_source_sha=snapshot.controller_source_sha,
            provider_settings_revision=snapshot.provider_settings_revision,
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
        provider_jobs=sorted(
            [
                *[
                    ScreenerProviderJobView(
                        job_id=row.build_id,
                        lane="build",
                        status=row.status,
                        provider=cast(
                            Literal["targon", "gcp", "hetzner"] | None,
                            row.provider,
                        ),
                        node_id=row.node_id,
                        provider_resource_id=row.provider_resource_id,
                        error_code=row.error_code,
                        created_at=_required_aware(row.created_at),
                        updated_at=_required_aware(row.updated_at),
                    )
                    for row in submission_build_rows
                ],
                *[
                    ScreenerProviderJobView(
                        job_id=row.build_id,
                        lane="runtime",
                        status=row.runtime_status,
                        provider=(
                            cast(Literal["targon", "gcp", "hetzner"], row.provider)
                            if row.runtime_status != "skipped"
                            else None
                        ),
                        node_id=row.runtime_node_id,
                        provider_resource_id=row.runtime_provider_resource_id,
                        image_reference=row.runtime_image_reference,
                        error_code=row.runtime_error_code,
                        created_at=_required_aware(row.created_at),
                        updated_at=_required_aware(row.updated_at),
                    )
                    for row in submission_build_rows
                ],
                *[
                    ScreenerProviderJobView(
                        job_id=row.review_id,
                        lane="source_review",
                        status=row.status,
                        provider=cast(
                            Literal["targon", "gcp", "hetzner"] | None,
                            row.provider,
                        ),
                        node_id=row.node_id,
                        provider_resource_id=row.provider_resource_id,
                        error_code=row.error_code,
                        created_at=_required_aware(row.created_at),
                        updated_at=_required_aware(row.updated_at),
                    )
                    for row in source_review_rows
                ],
            ],
            key=lambda row: row.updated_at,
            reverse=True,
        )[:100],
        provider_control=await _provider_control(session, environment=environment),
        node_controls=[
            await _node_channel_control(
                session, environment=environment, node_id=node.node_id
            )
            for node in node_rows
        ],
    )
