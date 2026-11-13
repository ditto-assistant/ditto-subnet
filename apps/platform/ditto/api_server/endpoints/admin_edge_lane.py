"""Operator reads and test-data writes for the Ditto Edge shadow lane (SN-481).

Reads cover the lane's state, nodes, objects, and the hourly shadow ledger.
The ledger read first scores any closed window that has none yet; scoring is
insert-once and deterministic, so the read is safe to repeat.

The only writes add public test objects and revoke them. Neither touches
weights, emissions, or users: lane objects are served only to enrolled shadow
nodes, never to a viewer. Revoking an object puts it on every holder's next
poll and starts revocation checks against those holders.
"""

from __future__ import annotations

import base64
import binascii
import logging
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import ColumnElement, and_, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.edge_lane import (
    EdgeLaneOverview,
    EdgeLedgerResponse,
    EdgeLedgerRow,
    EdgeNodeRow,
    EdgeNodesResponse,
    EdgeObjectIngestRequest,
    EdgeObjectIngestResponse,
    EdgeObjectRevokeRequest,
    EdgeObjectRevokeResponse,
    EdgeObjectRow,
    EdgeObjectsResponse,
    EdgeWindowRow,
)
from ditto.api_server import edge_lane
from ditto.api_server.dependencies import get_session, get_storage_client
from ditto.api_server.edge_lane import edge_now
from ditto.api_server.edge_lane_settings import (
    EdgeLaneSettingsResolver,
    ResolvedEdgeLaneSettings,
)
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.storage import S3StorageClient
from ditto.db.models import (
    EdgeAssignment,
    EdgeLedgerWindow,
    EdgeNode,
    EdgeObject,
    EdgeWindowClosure,
)
from ditto_edge_protocol.limits import MEDIA_TYPES
from ditto_edge_protocol.proofs import digest_of

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/edge", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]
StorageDep = Annotated[S3StorageClient, Depends(get_storage_client)]
LimitQuery = Annotated[int, Query(ge=1, le=1000)]


def _ledger_row(row: EdgeLedgerWindow) -> EdgeLedgerRow:
    return EdgeLedgerRow(
        window_index=row.window_index,
        node_id=row.node_id,
        operator=row.operator,
        scheduled=row.scheduled,
        passed=row.passed,
        failed=row.failed,
        missed=row.missed,
        refilled=row.refilled,
        refused=row.refused,
        revocation_checks=row.revocation_checks,
        revocation_violations=row.revocation_violations,
        held_gib_hours=row.held_gib_hours,
        availability=row.availability,
        eligible=row.eligible,
        verified_units=row.verified_units,
        hypothetical_share=row.hypothetical_share,
        reasons=[str(reason) for reason in row.reasons],
        settings_revision=row.settings_revision,
        weight_eligible=False,
        computed_at=row.computed_at,
    )


async def _resolve(request: Request) -> ResolvedEdgeLaneSettings:
    resolver: EdgeLaneSettingsResolver = request.app.state.edge_lane_settings
    return await resolver.resolve(request.app.state.session_maker)


def _held_spans(now: datetime) -> ColumnElement[bool]:
    """Spans a node holds now: acknowledged, open, under a live lease."""
    return and_(
        EdgeAssignment.acked_at.is_not(None),
        EdgeAssignment.released_at.is_(None),
        EdgeAssignment.lease_expires_at > now,
    )


@router.get("/overview", response_model=EdgeLaneOverview)
async def overview(
    request: Request, _admin: AdminDep, session: SessionDep
) -> EdgeLaneOverview:
    resolved = await _resolve(request)
    settings = resolved.settings
    now = edge_now(request)
    lease = timedelta(seconds=settings.lease_seconds)
    statuses = dict(
        (
            await session.execute(
                select(EdgeNode.status, func.count()).group_by(EdgeNode.status)
            )
        )
        .tuples()
        .all()
    )
    live = await session.scalar(
        select(func.count())
        .select_from(EdgeNode)
        .where(EdgeNode.status == "active", EdgeNode.last_poll_at >= now - lease)
    )
    active_objects, active_bytes = (
        await session.execute(
            select(
                func.count(), func.coalesce(func.sum(EdgeObject.size_bytes), 0)
            ).where(EdgeObject.revoked_at.is_(None))
        )
    ).one()
    revoked_objects = await session.scalar(
        select(func.count())
        .select_from(EdgeObject)
        .where(EdgeObject.revoked_at.is_not(None))
    )
    open_spans = await session.scalar(
        select(func.count())
        .select_from(EdgeAssignment)
        .where(EdgeAssignment.released_at.is_(None))
    )
    held_spans = await session.scalar(
        select(func.count()).select_from(EdgeAssignment).where(_held_spans(now))
    )
    windows = (
        await session.execute(
            select(
                EdgeWindowClosure,
                func.count(EdgeLedgerWindow.node_id).filter(EdgeLedgerWindow.eligible),
                func.coalesce(func.sum(EdgeLedgerWindow.verified_units), 0.0),
            )
            .outerjoin(
                EdgeLedgerWindow,
                EdgeLedgerWindow.window_index == EdgeWindowClosure.window_index,
            )
            .group_by(EdgeWindowClosure.window_index)
            .order_by(EdgeWindowClosure.window_index.desc())
            .limit(24)
        )
    ).all()
    return EdgeLaneOverview(
        mode=settings.mode,
        settings_revision=resolved.revision,
        nodes_active=statuses.get("active", 0),
        nodes_live=live or 0,
        nodes_suspended=statuses.get("suspended", 0),
        nodes_retired=statuses.get("retired", 0),
        objects_active=active_objects,
        objects_revoked=revoked_objects or 0,
        active_object_bytes=int(active_bytes),
        open_assignments=open_spans or 0,
        held_assignments=held_spans or 0,
        current_window_index=int(now.timestamp() // settings.window_seconds),
        recent_windows=[
            EdgeWindowRow(
                window_index=closure.window_index,
                settings_revision=closure.settings_revision,
                node_count=closure.node_count,
                eligible_nodes=eligible,
                verified_units=round(float(units), 9),
                closed_at=closure.closed_at,
            )
            for closure, eligible, units in windows
        ],
    )


@router.get("/nodes", response_model=EdgeNodesResponse)
async def nodes(
    request: Request, _admin: AdminDep, session: SessionDep, limit: LimitQuery = 200
) -> EdgeNodesResponse:
    now = edge_now(request)
    rows = list(
        await session.scalars(
            select(EdgeNode)
            .order_by(EdgeNode.last_poll_at.desc().nulls_last(), EdgeNode.node_id)
            .limit(limit)
        )
    )
    node_ids = [node.node_id for node in rows]
    held = dict(
        (
            await session.execute(
                select(
                    EdgeAssignment.node_id,
                    func.count(),
                )
                .where(EdgeAssignment.node_id.in_(node_ids), _held_spans(now))
                .group_by(EdgeAssignment.node_id)
            )
        )
        .tuples()
        .all()
    )
    held_bytes = dict(
        (
            await session.execute(
                select(EdgeAssignment.node_id, func.sum(EdgeObject.size_bytes))
                .join(EdgeObject, EdgeObject.digest == EdgeAssignment.digest)
                .where(EdgeAssignment.node_id.in_(node_ids), _held_spans(now))
                .group_by(EdgeAssignment.node_id)
            )
        )
        .tuples()
        .all()
    )
    latest = {
        row.node_id: row
        for row in await session.scalars(
            select(EdgeLedgerWindow)
            .where(EdgeLedgerWindow.node_id.in_(node_ids))
            .distinct(EdgeLedgerWindow.node_id)
            .order_by(EdgeLedgerWindow.node_id, EdgeLedgerWindow.window_index.desc())
        )
    }
    return EdgeNodesResponse(
        nodes=[
            EdgeNodeRow(
                node_id=node.node_id,
                operator=node.operator,
                hotkey_ss58=node.hotkey_ss58,
                client_name=node.client_name,
                client_version=node.client_version,
                platform=dict(node.platform),
                status=node.status,
                quota_bytes=node.quota_bytes,
                enrolled_at=node.enrolled_at,
                last_poll_at=node.last_poll_at,
                held_objects=held.get(node.node_id, 0),
                held_bytes=int(held_bytes.get(node.node_id) or 0),
                latest_window=(
                    _ledger_row(latest[node.node_id])
                    if node.node_id in latest
                    else None
                ),
            )
            for node in rows
        ]
    )


@router.get("/objects", response_model=EdgeObjectsResponse)
async def objects(
    request: Request, _admin: AdminDep, session: SessionDep, limit: LimitQuery = 200
) -> EdgeObjectsResponse:
    now = edge_now(request)
    holders = (
        select(EdgeAssignment.digest, func.count().label("holders"))
        .where(_held_spans(now))
        .group_by(EdgeAssignment.digest)
        .subquery()
    )
    rows = (
        await session.execute(
            select(EdgeObject, func.coalesce(holders.c.holders, 0))
            .outerjoin(holders, holders.c.digest == EdgeObject.digest)
            .order_by(EdgeObject.created_at.desc(), EdgeObject.digest)
            .limit(limit)
        )
    ).all()
    return EdgeObjectsResponse(
        objects=[
            EdgeObjectRow(
                digest=obj.digest,
                size_bytes=obj.size_bytes,
                media_type=obj.media_type,
                source=obj.source,
                created_by=obj.created_by,
                created_at=obj.created_at,
                revoked_at=obj.revoked_at,
                revoke_reason=obj.revoke_reason,
                holders=count,
            )
            for obj, count in rows
        ]
    )


@router.get("/ledger", response_model=EdgeLedgerResponse)
async def ledger(
    request: Request,
    _admin: AdminDep,
    session: SessionDep,
    window_index: Annotated[int | None, Query(ge=0)] = None,
    node_id: Annotated[str | None, Query(max_length=40)] = None,
    limit: LimitQuery = 200,
) -> EdgeLedgerResponse:
    resolved = await _resolve(request)
    now = edge_now(request)
    async with session.begin():
        closed = await edge_lane.close_windows(
            session,
            settings=resolved.settings,
            settings_revision=resolved.revision,
            now=now,
        )
    statement = select(EdgeLedgerWindow)
    if window_index is not None:
        statement = statement.where(EdgeLedgerWindow.window_index == window_index)
    if node_id is not None:
        statement = statement.where(EdgeLedgerWindow.node_id == node_id)
    rows = await session.scalars(
        statement.order_by(
            EdgeLedgerWindow.window_index.desc(), EdgeLedgerWindow.node_id
        ).limit(limit)
    )
    return EdgeLedgerResponse(
        rows=[_ledger_row(row) for row in rows], closed_now=closed
    )


@router.post("/objects", response_model=EdgeObjectIngestResponse)
async def ingest(
    request: Request,
    payload: EdgeObjectIngestRequest,
    _admin: AdminDep,
    session: SessionDep,
    storage: StorageDep,
) -> EdgeObjectIngestResponse:
    settings = (await _resolve(request)).settings
    if payload.media_type not in MEDIA_TYPES:
        raise HTTPException(status_code=422, detail="media type is not allowed")
    try:
        data = base64.b64decode(payload.data_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=422, detail="data_b64 is not base64") from exc
    if not 0 < len(data) <= settings.max_object_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"objects are 1..{settings.max_object_bytes} bytes in this lane",
        )
    digest = digest_of(data)
    async with session.begin():
        existing = await session.get(EdgeObject, digest)
        if existing is not None:
            if existing.revoked_at is not None:
                raise HTTPException(
                    status_code=409, detail="a revoked digest stays revoked"
                )
            return EdgeObjectIngestResponse(
                digest=digest,
                size_bytes=existing.size_bytes,
                media_type=existing.media_type,
                created=False,
            )
        active_bytes = await session.scalar(
            select(func.coalesce(func.sum(EdgeObject.size_bytes), 0)).where(
                EdgeObject.revoked_at.is_(None)
            )
        )
    if int(active_bytes or 0) + len(data) > settings.max_total_object_bytes:
        raise HTTPException(
            status_code=409, detail="the lane's total object budget is full"
        )
    # Store first, then record: an orphan object is cheap, a row without its
    # bytes would fail every fill.
    key = edge_lane.object_key(digest)
    await storage.put_object(key=key, body=data, content_type=payload.media_type)
    async with session.begin():
        created = await session.scalar(
            insert(EdgeObject)
            .values(
                digest=digest,
                size_bytes=len(data),
                media_type=payload.media_type,
                source=payload.source.strip(),
                storage_key=key,
                created_by=payload.actor.strip(),
            )
            .on_conflict_do_nothing()
            .returning(EdgeObject.digest)
        )
    logger.info("edge lane object %s ingested by %s", digest, payload.actor)
    return EdgeObjectIngestResponse(
        digest=digest,
        size_bytes=len(data),
        media_type=payload.media_type,
        created=created is not None,
    )


@router.post("/objects/{digest}/revoke", response_model=EdgeObjectRevokeResponse)
async def revoke(
    request: Request,
    digest: str,
    payload: EdgeObjectRevokeRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> EdgeObjectRevokeResponse:
    now = edge_now(request)
    async with session.begin():
        obj = await session.get(EdgeObject, digest, with_for_update=True)
        if obj is None:
            raise HTTPException(status_code=404, detail="unknown object")
        newly = obj.revoked_at is None
        revoked_at = obj.revoked_at or now
        reason = obj.revoke_reason or (
            f"{payload.reason.strip()} ({payload.actor.strip()})"
        )
        obj.revoked_at = revoked_at
        obj.revoke_reason = reason
    logger.warning("edge lane object %s revoked by %s", digest, payload.actor)
    return EdgeObjectRevokeResponse(
        digest=digest,
        revoked_at=revoked_at,
        revoke_reason=reason,
        newly_revoked=newly,
    )
