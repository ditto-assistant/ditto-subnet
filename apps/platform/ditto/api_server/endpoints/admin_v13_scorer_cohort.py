"""One-way, exact V13 scorer cohort activation after nonmember drain."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.ticket_status import TicketStatus
from ditto.api_models.upload import _SS58_PATTERN
from ditto.api_models.validator_slot_settings import ValidatorSlotSettings
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.v13_scorer_cohort import (
    V13ScorerPacket,
    current_pin,
    packet_for_heartbeat,
    pinned_cohort_packet,
)
from ditto.db.models import V13ScorerCohortPin, ValidatorHeartbeat, ValidatorTicket
from ditto.db.queries.rollout_dispatch import try_lock_rollout_dispatch
from ditto.db.queries.validator_slot_settings import (
    latest_validator_slot_settings_revision,
)

router = APIRouter(prefix="/admin/v13-scorer-cohort", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]


class ActivateV13ScorerCohortRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    hotkeys: Annotated[
        tuple[str, str, str],
        BeforeValidator(
            lambda value: tuple(value) if isinstance(value, list) else value
        ),
    ]
    packet: V13ScorerPacket
    expected_slot_settings_revision: int = Field(ge=1)
    expected_slot_settings_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(min_length=8, max_length=2000)
    actor: str = Field(min_length=1, max_length=200)
    confirmation: str

    @field_validator("hotkeys")
    @classmethod
    def exact_sorted_hotkeys(cls, value: tuple[str, str, str]) -> tuple[str, str, str]:
        import re

        if tuple(sorted(set(value))) != value or any(
            re.fullmatch(_SS58_PATTERN, item) is None for item in value
        ):
            raise ValueError("hotkeys must be three distinct sorted SS58 identities")
        return value


class V13ScorerCohortView(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="ignore")

    bench_version: int
    hotkeys: list[str]
    packet: V13ScorerPacket
    slot_settings_revision: int
    slot_settings_checksum: str
    reason: str
    actor: str
    created_at: datetime


@router.get("", response_model=V13ScorerCohortView | None)
async def get_pin(_admin: AdminDep, session: SessionDep) -> V13ScorerCohortPin | None:
    return await current_pin(session)


@router.post("", response_model=V13ScorerCohortView)
async def activate_pin(
    payload: ActivateV13ScorerCohortRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> V13ScorerCohortPin:
    if payload.confirmation != "PIN V13 SCORER COHORT":
        raise HTTPException(409, "confirmation must be PIN V13 SCORER COHORT")
    async with session.begin():
        if not await try_lock_rollout_dispatch(session):
            raise HTTPException(409, "ticket dispatch is busy; retry after it settles")
        if await current_pin(session) is not None:
            raise HTTPException(409, "V13 scorer cohort is already pinned")
        settings = await latest_validator_slot_settings_revision(session)
        if settings is None or (
            settings.revision != payload.expected_slot_settings_revision
            or settings.checksum != payload.expected_slot_settings_checksum
        ):
            raise HTTPException(409, "validator slot settings changed; refresh first")
        paused = set(
            ValidatorSlotSettings.model_validate(settings.settings).paused_validator_hotkeys
        )
        now = datetime.now(UTC)
        heartbeats = (await session.scalars(select(ValidatorHeartbeat))).all()
        for heartbeat in heartbeats:
            if heartbeat.validator_hotkey in payload.hotkeys:
                continue
            # Every other fresh V13-capable scorer must already be paused.
            from ditto.db.queries.benchmark_rollout import heartbeat_supports_version

            if heartbeat_supports_version(heartbeat, now=now, version=13) and (
                heartbeat.validator_hotkey not in paused
            ):
                raise HTTPException(409, "a nonmember V13 validator is not paused")
        for hotkey in payload.hotkeys:
            heartbeat = await session.get(ValidatorHeartbeat, hotkey)
            packet = packet_for_heartbeat(heartbeat, now=now)
            if packet is None or packet != payload.packet or hotkey in paused:
                raise HTTPException(409, "pinned validator packet or admission changed")
        unpinned_live = await session.scalar(
            select(ValidatorTicket.agent_id).where(
                ValidatorTicket.bench_version == 13,
                ValidatorTicket.status == TicketStatus.ISSUED,
                ValidatorTicket.deadline > now,
                ValidatorTicket.validator_hotkey.not_in(payload.hotkeys),
            ).limit(1)
        )
        if unpinned_live is not None:
            raise HTTPException(409, "nonmember V13 tickets must drain before pinning")
        row = V13ScorerCohortPin(
            bench_version=13,
            hotkeys=list(payload.hotkeys),
            packet=payload.packet.model_dump(mode="json"),
            slot_settings_revision=settings.revision,
            slot_settings_checksum=settings.checksum,
            reason=payload.reason.strip(),
            actor=payload.actor.strip(),
        )
        session.add(row)
        await session.flush()
        if await pinned_cohort_packet(session, now=now) is None:
            raise HTTPException(409, "pinned validators are not all routable")
    await session.refresh(row)
    return row
