"""Audited fleet-wide interruption and managed validator update control."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.ticket_status import TicketStatus
from ditto.api_models.validator_fleet_update import (
    CONFIRMATION,
    AdminValidatorFleetUpdateRequest,
    AdminValidatorFleetUpdateResponse,
    ValidatorFleetUpdateOperationView,
    ValidatorFleetUpdatePreview,
    ValidatorFleetUpdateTarget,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.models import (
    ValidatorFleetUpdateOperation,
    ValidatorHeartbeat,
    ValidatorTicket,
)
from ditto.db.queries.lease_liveness import LeaseLiveness, force_expire_lease
from ditto.db.queries.validator_fleet_updates import latest_fleet_update_operation

router = APIRouter(prefix="/admin/validator-fleet-update", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]

_FLEET_FRESHNESS = timedelta(minutes=5)
_ACTION = "operator_forced_update"
_CONTEXT = "admin_fleet_update"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _managed(heartbeat: ValidatorHeartbeat) -> bool:
    stack = heartbeat.stack if isinstance(heartbeat.stack, dict) else {}
    capabilities = (
        heartbeat.capabilities if isinstance(heartbeat.capabilities, dict) else {}
    )
    return (
        heartbeat.protocol_version >= 19
        and stack.get("mode") == "managed"
        and capabilities.get("stack_updater") is True
    )


def _stack_revision(heartbeat: ValidatorHeartbeat) -> str | None:
    stack = heartbeat.stack if isinstance(heartbeat.stack, dict) else {}
    components = stack.get("components")
    subnet = components.get("ditto_subnet") if isinstance(components, dict) else None
    value = subnet.get("source_revision") if isinstance(subnet, dict) else None
    return value if isinstance(value, str) else None


async def _targets(session: AsyncSession, *, now: datetime) -> list[ValidatorHeartbeat]:
    rows = list(
        await session.scalars(
            select(ValidatorHeartbeat)
            .where(ValidatorHeartbeat.seen_at >= now - _FLEET_FRESHNESS)
            .order_by(ValidatorHeartbeat.validator_hotkey)
        )
    )
    return [row for row in rows if _managed(row)]


async def _live_tickets(
    session: AsyncSession,
    *,
    hotkeys: list[str],
    now: datetime,
    for_update: bool = False,
) -> list[ValidatorTicket]:
    if not hotkeys:
        return []
    stmt = (
        select(ValidatorTicket)
        .where(
            ValidatorTicket.validator_hotkey.in_(hotkeys),
            ValidatorTicket.status == TicketStatus.ISSUED,
            ValidatorTicket.deadline > now,
        )
        .order_by(
            ValidatorTicket.validator_hotkey,
            ValidatorTicket.slot_id,
            ValidatorTicket.agent_id,
        )
    )
    if for_update:
        stmt = stmt.with_for_update()
    return list(await session.scalars(stmt))


def _snapshot(targets: list[ValidatorHeartbeat], tickets: list[ValidatorTicket]) -> str:
    payload = {
        "targets": [
            {
                "hotkey": row.validator_hotkey,
                "software_version": row.software_version,
                "stack_revision": _stack_revision(row),
                "last_operation": (
                    str(row.last_fleet_update_operation_id)
                    if row.last_fleet_update_operation_id is not None
                    else None
                ),
            }
            for row in targets
        ],
        "leases": [
            {
                "agent_id": str(ticket.agent_id),
                "validator_hotkey": ticket.validator_hotkey,
                "slot_id": ticket.slot_id,
                "bench_version": ticket.bench_version,
                "deadline": _aware(ticket.deadline).isoformat(),
            }
            for ticket in tickets
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _target_views(
    targets: list[ValidatorHeartbeat],
    tickets: list[ValidatorTicket],
    *,
    operation_id: UUID | None = None,
) -> list[ValidatorFleetUpdateTarget]:
    lease_counts: dict[str, int] = {}
    for ticket in tickets:
        lease_counts[ticket.validator_hotkey] = (
            lease_counts.get(ticket.validator_hotkey, 0) + 1
        )
    return [
        ValidatorFleetUpdateTarget(
            validator_hotkey=row.validator_hotkey,
            software_version=row.software_version,
            stack_revision=_stack_revision(row),
            active_lease_count=lease_counts.get(row.validator_hotkey, 0),
            acknowledged=(
                operation_id is not None
                and row.last_fleet_update_operation_id == operation_id
            ),
        )
        for row in targets
    ]


async def _latest(
    session: AsyncSession,
) -> ValidatorFleetUpdateOperation | None:
    return await latest_fleet_update_operation(session)


async def _operation_view(
    session: AsyncSession, row: ValidatorFleetUpdateOperation
) -> ValidatorFleetUpdateOperationView:
    hotkeys = [str(value) for value in row.target_validator_hotkeys]
    heartbeats = list(
        await session.scalars(
            select(ValidatorHeartbeat)
            .where(ValidatorHeartbeat.validator_hotkey.in_(hotkeys))
            .order_by(ValidatorHeartbeat.validator_hotkey)
        )
    )
    by_hotkey = {item.validator_hotkey: item for item in heartbeats}
    targets = [
        ValidatorFleetUpdateTarget(
            validator_hotkey=hotkey,
            software_version=(
                by_hotkey[hotkey].software_version if hotkey in by_hotkey else "unknown"
            ),
            stack_revision=(
                _stack_revision(by_hotkey[hotkey])
                if hotkey in by_hotkey
                else row.target_stack_revisions.get(hotkey)
            ),
            active_lease_count=0,
            acknowledged=(
                hotkey in by_hotkey
                and by_hotkey[hotkey].last_fleet_update_operation_id == row.operation_id
            ),
        )
        for hotkey in hotkeys
    ]
    return ValidatorFleetUpdateOperationView(
        operation_id=row.operation_id,
        expected_snapshot=row.expected_snapshot,
        targets=targets,
        revoked_lease_count=row.revoked_lease_count,
        acknowledged_count=sum(item.acknowledged for item in targets),
        actor=row.actor,
        reason=row.reason,
        created_at=_aware(row.created_at),
    )


@router.get("", response_model=ValidatorFleetUpdatePreview)
async def preview_validator_fleet_update(
    _admin: AdminDep,
    session: SessionDep,
) -> ValidatorFleetUpdatePreview:
    """Preview the exact managed fleet and leases a forced update would touch."""
    now = datetime.now(UTC)
    targets = await _targets(session, now=now)
    tickets = await _live_tickets(
        session, hotkeys=[row.validator_hotkey for row in targets], now=now
    )
    latest = await _latest(session)
    return ValidatorFleetUpdatePreview(
        generated_at=now,
        snapshot=_snapshot(targets, tickets),
        target_count=len(targets),
        active_lease_count=len(tickets),
        targets=_target_views(targets, tickets),
        latest_operation=(
            await _operation_view(session, latest) if latest is not None else None
        ),
    )


@router.post("", response_model=AdminValidatorFleetUpdateResponse)
async def force_validator_fleet_update(
    payload: AdminValidatorFleetUpdateRequest,
    _admin: AdminDep,
    session: SessionDep,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> AdminValidatorFleetUpdateResponse:
    """Revoke live benchmark leases and command every managed validator to update."""
    actor = (x_admin_actor or payload.actor).strip()
    if not actor:
        raise HTTPException(status_code=422, detail="admin actor is required")
    if payload.confirmation != CONFIRMATION:
        raise HTTPException(
            status_code=409,
            detail=f"confirmation must be exactly {CONFIRMATION}",
        )
    now = datetime.now(UTC)
    async with session.begin():
        existing = await session.get(ValidatorFleetUpdateOperation, payload.request_id)
        if existing is not None:
            if (
                existing.actor != actor
                or existing.reason != payload.reason.strip()
                or existing.expected_snapshot != payload.expected_snapshot
            ):
                raise HTTPException(status_code=409, detail="request id already used")
            return AdminValidatorFleetUpdateResponse(
                operation=await _operation_view(session, existing), idempotent=True
            )

        targets = await _targets(session, now=now)
        hotkeys = [row.validator_hotkey for row in targets]
        tickets = await _live_tickets(
            session, hotkeys=hotkeys, now=now, for_update=True
        )
        current_snapshot = _snapshot(targets, tickets)
        if current_snapshot != payload.expected_snapshot:
            raise HTTPException(
                status_code=409,
                detail="validator fleet changed; refresh before forcing an update",
            )
        if not targets:
            raise HTTPException(
                status_code=409,
                detail="no online managed validators with stack updater support",
            )

        for ticket in tickets:
            await force_expire_lease(
                session,
                ticket=ticket,
                now=now,
                liveness=LeaseLiveness(
                    idle=True,
                    reason=_ACTION,
                    evidence={
                        "operator_actor": actor,
                        "operator_reason": payload.reason.strip(),
                        "operator_request_id": str(payload.request_id),
                    },
                ),
                context=_CONTEXT,
                action=_ACTION,
                compensate=True,
            )
        row = ValidatorFleetUpdateOperation(
            operation_id=payload.request_id,
            expected_snapshot=current_snapshot,
            target_validator_hotkeys=hotkeys,
            target_stack_revisions={
                target.validator_hotkey: _stack_revision(target) for target in targets
            },
            revoked_lease_count=len(tickets),
            actor=actor,
            reason=payload.reason.strip(),
            created_at=now,
        )
        session.add(row)
        await session.flush()
        return AdminValidatorFleetUpdateResponse(
            operation=await _operation_view(session, row), idempotent=False
        )
