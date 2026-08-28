"""Audited Backroom-only inspection and removal of hotkey upload bans."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.admin_hotkey_bans import (
    AdminActiveHotkeyBan,
    AdminHotkeyBanAuditEntry,
    AdminHotkeyBanControl,
    AdminHotkeyBanList,
    AdminHotkeyUnbanRequest,
    AdminHotkeyUnbanResponse,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.models import BannedHotkey, HotkeyBanAudit
from ditto.db.queries.bans import (
    count_hotkey_bans,
    get_hotkey_ban,
    list_hotkey_ban_audit,
    list_hotkey_bans,
    unban_hotkey_with_audit,
)

router = APIRouter(prefix="/admin/hotkey-bans", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]


def _active(row: BannedHotkey) -> AdminActiveHotkeyBan:
    return AdminActiveHotkeyBan(
        hotkey=row.hotkey,
        reason=row.reason,
        banned_at=row.banned_at,
    )


def _audit(row: HotkeyBanAudit) -> AdminHotkeyBanAuditEntry:
    return AdminHotkeyBanAuditEntry(
        seq=row.seq,
        hotkey=row.hotkey,
        action="unban",
        actor=row.actor,
        reason=row.reason,
        previous_reason=row.previous_reason,
        previous_banned_at=row.previous_banned_at,
        recorded_at=row.recorded_at,
    )


@router.get("", response_model=AdminHotkeyBanList)
async def active_hotkey_bans(
    _admin: AdminDep,
    session: SessionDep,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> AdminHotkeyBanList:
    """List every active miner-wide upload ban, newest first."""
    total = await count_hotkey_bans(session)
    rows = await list_hotkey_bans(session, limit=limit, offset=offset)
    return AdminHotkeyBanList(total=total, bans=[_active(row) for row in rows])


@router.get("/{hotkey}", response_model=AdminHotkeyBanControl)
async def hotkey_ban_control(
    hotkey: str,
    _admin: AdminDep,
    session: SessionDep,
    history_limit: int = Query(default=20, ge=0, le=100),
) -> AdminHotkeyBanControl:
    """Read one exact active ban plus its durable operator action history."""
    active = await get_hotkey_ban(session, hotkey=hotkey)
    history = await list_hotkey_ban_audit(session, hotkey=hotkey, limit=history_limit)
    return AdminHotkeyBanControl(
        hotkey=hotkey,
        banned=active is not None,
        active_ban=_active(active) if active is not None else None,
        history=[_audit(row) for row in history],
    )


@router.post("/{hotkey}/unban", response_model=AdminHotkeyUnbanResponse)
async def remove_hotkey_ban(
    hotkey: str,
    body: AdminHotkeyUnbanRequest,
    _admin: AdminDep,
    session: SessionDep,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> AdminHotkeyUnbanResponse:
    """Remove one hotkey upload gate without changing any agent status."""
    actor = x_admin_actor.strip() if x_admin_actor else ""
    if not 1 <= len(actor) <= 120:
        raise HTTPException(status_code=422, detail="X-Admin-Actor is required")
    expected_confirmation = f"UNBAN HOTKEY {hotkey}"
    if body.confirmation != expected_confirmation:
        raise HTTPException(
            status_code=409,
            detail=f"confirmation must be exactly {expected_confirmation}",
        )

    async with session.begin():
        active = await get_hotkey_ban(session, hotkey=hotkey, for_update=True)
        if active is None:
            raise HTTPException(
                status_code=409,
                detail="hotkey is not currently banned; refresh before applying",
            )
        if active.banned_at != body.expected_banned_at:
            raise HTTPException(
                status_code=409,
                detail=(
                    "hotkey ban changed; refresh before applying "
                    f"(expected {body.expected_banned_at.isoformat()}, "
                    f"current {active.banned_at.isoformat()})"
                ),
            )
        action = await unban_hotkey_with_audit(
            session,
            hotkey=hotkey,
            expected_banned_at=body.expected_banned_at,
            actor=actor,
            reason=body.reason,
        )
        if action is None:  # pragma: no cover - row is locked above
            raise HTTPException(
                status_code=409, detail="hotkey ban changed concurrently"
            )

    await session.refresh(action)
    return AdminHotkeyUnbanResponse(hotkey=hotkey, banned=False, action=_audit(action))
