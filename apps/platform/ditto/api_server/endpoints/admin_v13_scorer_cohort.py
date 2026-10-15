"""Guarded, append-only V13 scorer cohort activation and rotation.

A cohort names three to sixteen validators. Each agent is still scored by
exactly three distinct validators; membership only decides who may claim one
of those seats. Rotation appends a new row that may change the scorer packet,
the membership, or both, and the full history stays readable.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.benchmark_capacity import BenchmarkCapacity
from ditto.api_models.ticket_status import TicketStatus
from ditto.api_models.upload import _SS58_PATTERN
from ditto.api_models.validator_slot_settings import ValidatorSlotSettings
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.v13_scorer_cohort import (
    MAX_COHORT_MEMBERS,
    MIN_COHORT_MEMBERS,
    CohortPacket,
    V13ScorerPacket,
    authority_from_packets,
    cohort_authority,
    current_pin,
    member_packets,
    packet_for_heartbeat,
    pinned_cohort_packet,
    report_only_current_cohort_packet,
    routable_cohort_packet,
)
from ditto.db.models import (
    V13ScorerCohortPin,
    V13ScorerCohortRotation,
    ValidatorHeartbeat,
    ValidatorSlotSettingsRevision,
    ValidatorTicket,
)
from ditto.db.queries.benchmark_rollout import heartbeat_supports_version
from ditto.db.queries.rollout_dispatch import try_lock_rollout_dispatch
from ditto.db.queries.validator_slot_settings import (
    latest_validator_slot_settings_revision,
)

router = APIRouter(prefix="/admin/v13-scorer-cohort", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]

CohortHotkeys = Annotated[
    tuple[str, ...],
    BeforeValidator(lambda value: tuple(value) if isinstance(value, list) else value),
    Field(min_length=MIN_COHORT_MEMBERS, max_length=MAX_COHORT_MEMBERS),
]


def _exact_sorted_hotkeys(value: tuple[str, ...]) -> tuple[str, ...]:
    if tuple(sorted(set(value))) != value or any(
        re.fullmatch(_SS58_PATTERN, item) is None for item in value
    ):
        raise ValueError(
            f"hotkeys must be {MIN_COHORT_MEMBERS} to {MAX_COHORT_MEMBERS} "
            "distinct sorted SS58 identities"
        )
    return value


class ActivateV13ScorerCohortRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    hotkeys: CohortHotkeys
    packet: V13ScorerPacket
    expected_slot_settings_revision: int = Field(ge=1)
    expected_slot_settings_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(min_length=8)
    actor: str = Field(min_length=1, max_length=200)
    confirmation: str

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value: str) -> str:
        if len(value.strip()) < 8:
            raise ValueError("reason must contain at least eight nonblank characters")
        return value.strip()

    @field_validator("hotkeys")
    @classmethod
    def exact_sorted_hotkeys(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _exact_sorted_hotkeys(value)


class RotateV13ScorerCohortRequest(ActivateV13ScorerCohortRequest):
    expected_current_packet: V13ScorerPacket
    expected_current_rotation_id: int | None = Field(default=None, ge=1)
    expected_current_hotkeys: CohortHotkeys | None = Field(
        default=None,
        description=(
            "The current pinned membership, required to change it. When "
            "omitted, hotkeys must equal the current membership."
        ),
    )

    @field_validator("expected_current_hotkeys")
    @classmethod
    def exact_sorted_current_hotkeys(
        cls, value: tuple[str, ...] | None
    ) -> tuple[str, ...] | None:
        return None if value is None else _exact_sorted_hotkeys(value)


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
    rotation_id: int | None = None
    previous_packet: V13ScorerPacket | None = None


class V13ScorerPreflightValidator(BaseModel):
    hotkey: str
    capable: bool
    packet: V13ScorerPacket | None
    accepting: bool
    paused: bool
    live_v13_tickets: int
    member: bool = Field(
        default=False, description="Whether the current pin names this validator."
    )


class V13ScorerPreflight(BaseModel):
    observed_at: datetime
    slot_settings_revision: int | None
    slot_settings_checksum: str | None
    validators: list[V13ScorerPreflightValidator]
    pinned_hotkeys: list[str] | None = Field(
        default=None, description="Current pinned membership; null before a pin."
    )
    authority_packet: V13ScorerPacket | None = Field(
        default=None,
        description=(
            "Packet a strict majority (and at least three) of the live pinned "
            "members sign. Only its signers are issued V13 work; null means "
            "no V13 issuance."
        ),
    )
    authority_hotkeys: list[str] = Field(
        default_factory=list,
        description="Pinned members signing the authority packet.",
    )


class V13ReportOnlyCurrentPacket(BaseModel):
    hotkeys: list[str]
    packet: V13ScorerPacket
    oldest_scorer_observed_at: int
    matches_effective_pin: bool
    routable_hotkeys: list[str] = Field(
        default_factory=list,
        description=(
            "Unpaused, accepting pinned members signing the authority packet; "
            "always at least three."
        ),
    )


@router.get("", response_model=V13ScorerCohortView | None)
async def get_pin(
    _admin: AdminDep, session: SessionDep
) -> V13ScorerCohortPin | V13ScorerCohortRotation | None:
    return await current_pin(session)


@router.get("/history", response_model=list[V13ScorerCohortView])
async def get_history(
    _admin: AdminDep, session: SessionDep
) -> list[V13ScorerCohortView]:
    original = await session.get(V13ScorerCohortPin, 13)
    rotations = (
        await session.scalars(
            select(V13ScorerCohortRotation)
            .where(V13ScorerCohortRotation.bench_version == 13)
            .order_by(V13ScorerCohortRotation.rotation_id)
        )
    ).all()
    rows = ([original] if original is not None else []) + list(rotations)
    return [V13ScorerCohortView.model_validate(row) for row in rows]


@router.get(
    "/report-only-current-packet", response_model=V13ReportOnlyCurrentPacket | None
)
async def get_report_only_current_packet(
    _admin: AdminDep, session: SessionDep
) -> V13ReportOnlyCurrentPacket | None:
    """Read the live-majority member packet, without changing authority."""
    pin = await current_pin(session)
    if pin is None:
        return None
    observed = await report_only_current_cohort_packet(session, now=datetime.now(UTC))
    if observed is None:
        return None
    return V13ReportOnlyCurrentPacket(
        hotkeys=pin.hotkeys,
        packet=observed.packet,
        oldest_scorer_observed_at=observed.oldest_scorer_observed_at,
        matches_effective_pin=observed.packet.model_dump(mode="json") == pin.packet,
        routable_hotkeys=list(observed.routable),
    )


@router.get("/preflight", response_model=V13ScorerPreflight)
async def get_preflight(_admin: AdminDep, session: SessionDep) -> V13ScorerPreflight:
    """Read the exact current packet and drain state for activation planning."""
    now = datetime.now(UTC)
    settings = await latest_validator_slot_settings_revision(session)
    paused = set()
    if settings is not None:
        paused = set(
            ValidatorSlotSettings.model_validate(
                settings.settings
            ).paused_validator_hotkeys
        )
    live_rows = (
        await session.execute(
            select(ValidatorTicket.validator_hotkey, func.count())
            .where(
                ValidatorTicket.bench_version == 13,
                ValidatorTicket.status == TicketStatus.ISSUED,
                ValidatorTicket.deadline > now,
            )
            .group_by(ValidatorTicket.validator_hotkey)
        )
    ).all()
    live_counts: dict[str, int] = {row[0]: int(row[1]) for row in live_rows}
    pin = await current_pin(session)
    members = set(pin.hotkeys) if pin is not None else set()
    authority = (
        await cohort_authority(session, pin, now=now) if pin is not None else None
    )
    heartbeats = (await session.scalars(select(ValidatorHeartbeat))).all()
    validators: list[V13ScorerPreflightValidator] = []
    for heartbeat in heartbeats:
        capable = heartbeat_supports_version(heartbeat, now=now, version=13)
        packet = packet_for_heartbeat(heartbeat, now=now) if capable else None
        validators.append(
            V13ScorerPreflightValidator(
                hotkey=heartbeat.validator_hotkey,
                capable=capable,
                packet=packet,
                accepting=capable and _accepting(heartbeat),
                paused=heartbeat.validator_hotkey in paused,
                live_v13_tickets=live_counts.get(heartbeat.validator_hotkey, 0),
                member=heartbeat.validator_hotkey in members,
            )
        )
    return V13ScorerPreflight(
        observed_at=now,
        slot_settings_revision=settings.revision if settings is not None else None,
        slot_settings_checksum=settings.checksum if settings is not None else None,
        validators=sorted(validators, key=lambda item: item.hotkey),
        pinned_hotkeys=list(pin.hotkeys) if pin is not None else None,
        authority_packet=authority.packet if authority is not None else None,
        authority_hotkeys=list(authority.supporters) if authority is not None else [],
    )


def _accepting(heartbeat: ValidatorHeartbeat) -> bool:
    try:
        capacity = BenchmarkCapacity.model_validate(heartbeat.benchmark_capacity)
    except ValidationError:
        return False
    return capacity.admission == "accepting" and bool(capacity.healthy_slots)


async def _require_nonmembers_paused(
    session: AsyncSession,
    *,
    members: tuple[str, ...],
    paused: set[str],
    now: datetime,
) -> None:
    """Every fresh V13-capable validator outside ``members`` must be paused.

    Membership already declines nonmembers (here and in the ticket trigger).
    The pause makes the operator name every capable validator the cohort
    leaves out, and keeps it out of the quorum-capable fleet.
    """
    heartbeats = (await session.scalars(select(ValidatorHeartbeat))).all()
    for heartbeat in heartbeats:
        if heartbeat.validator_hotkey in members:
            continue
        if heartbeat_supports_version(heartbeat, now=now, version=13) and (
            heartbeat.validator_hotkey not in paused
        ):
            raise HTTPException(409, "a nonmember V13 validator is not paused")


async def _require_admitted_signers(
    session: AsyncSession,
    *,
    hotkeys: list[str],
    packet: V13ScorerPacket,
    paused: set[str],
    now: datetime,
) -> None:
    """Each named validator signs ``packet``, is unpaused and is accepting."""
    for hotkey in hotkeys:
        heartbeat = await session.get(ValidatorHeartbeat, hotkey)
        if packet_for_heartbeat(heartbeat, now=now) != packet or hotkey in paused:
            raise HTTPException(409, "member signed packet or pause state changed")
        assert heartbeat is not None
        try:
            capacity = BenchmarkCapacity.model_validate(heartbeat.benchmark_capacity)
        except ValidationError as exc:
            raise HTTPException(409, "member V13 scorer capacity is invalid") from exc
        if capacity.admission != "accepting" or not capacity.healthy_slots:
            raise HTTPException(409, "member V13 scorer is not accepting")


_NOT_ROUTABLE = "fewer than three proposed members are routable on the target packet"


def _require_routed_packet(
    routed: CohortPacket | None, *, packet: V13ScorerPacket
) -> None:
    """Refuse unless the cohort routes exactly ``packet``, naming which failed.

    No routable packet and a routable but different packet need different
    fixes: the first is member admission (pause, capacity, probe), the second
    is a stale target, so re-read the preflight ``authority_packet``.
    """
    if routed is None:
        raise HTTPException(409, _NOT_ROUTABLE)
    if routed.packet != packet:
        raise HTTPException(
            409,
            "the cohort routes signed packet source_revision "
            f"{routed.packet.source_revision}, not the target "
            f"{packet.source_revision}; re-read the preflight authority_packet",
        )


async def _require_routable(
    session: AsyncSession,
    *,
    hotkeys: tuple[str, ...],
    packet: V13ScorerPacket,
    now: datetime,
) -> None:
    """At least a quorum of the proposed members can lease on ``packet`` now.

    Checked before the write so a refusal never consumes a rotation ID, and
    again after it against the stored row.
    """
    _require_routed_packet(
        await routable_cohort_packet(session, hotkeys=hotkeys, now=now),
        packet=packet,
    )


def _settings_paused(
    settings: ValidatorSlotSettingsRevision | None, *, revision: int, checksum: str
) -> tuple[int, str, set[str]]:
    """Bind the write to the exact slot policy the operator read."""
    if settings is None or (
        settings.revision != revision or settings.checksum != checksum
    ):
        raise HTTPException(409, "validator slot settings changed; refresh first")
    paused = set(
        ValidatorSlotSettings.model_validate(settings.settings).paused_validator_hotkeys
    )
    return settings.revision, settings.checksum, paused


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
        revision, checksum, paused = _settings_paused(
            await latest_validator_slot_settings_revision(session),
            revision=payload.expected_slot_settings_revision,
            checksum=payload.expected_slot_settings_checksum,
        )
        now = datetime.now(UTC)
        await _require_nonmembers_paused(
            session, members=payload.hotkeys, paused=paused, now=now
        )
        # A first pin has no history to vote with: every member is new and
        # must sign the exact packet.
        for hotkey in payload.hotkeys:
            member_heartbeat = await session.get(ValidatorHeartbeat, hotkey)
            packet = packet_for_heartbeat(member_heartbeat, now=now)
            if packet is None or packet != payload.packet or hotkey in paused:
                raise HTTPException(409, "pinned validator packet or admission changed")
        unpinned_live = await session.scalar(
            select(ValidatorTicket.agent_id)
            .where(
                ValidatorTicket.bench_version == 13,
                ValidatorTicket.status == TicketStatus.ISSUED,
                ValidatorTicket.deadline > now,
                ValidatorTicket.validator_hotkey.not_in(payload.hotkeys),
            )
            .limit(1)
        )
        if unpinned_live is not None:
            raise HTTPException(409, "nonmember V13 tickets must drain before pinning")
        await _require_routable(
            session, hotkeys=payload.hotkeys, packet=payload.packet, now=now
        )
        row = V13ScorerCohortPin(
            bench_version=13,
            hotkeys=list(payload.hotkeys),
            packet=payload.packet.model_dump(mode="json"),
            slot_settings_revision=revision,
            slot_settings_checksum=checksum,
            reason=payload.reason.strip(),
            actor=payload.actor.strip(),
        )
        session.add(row)
        await session.flush()
        _require_routed_packet(
            await pinned_cohort_packet(session, now=now), packet=payload.packet
        )
    await session.refresh(row)
    return row


@router.post("/rotate", response_model=V13ScorerCohortView)
async def rotate_pin(
    payload: RotateV13ScorerCohortRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> V13ScorerCohortRotation:
    """Append a rotation that changes the packet, the membership, or both.

    * Optimistic concurrency: the current rotation ID, packet and membership
      (``expected_current_hotkeys``, defaulting to ``hotkeys``) must match.
    * Every *new* member must sign the target packet, be unpaused and accept.
    * The target packet must be the authority packet of the proposed cohort: a
      strict majority of its live members, and at least three, sign it.
    * At least three proposed members must be routable on it (unpaused,
      accepting, with a scorer probe), checked before and after the write.
    * Every fresh V13-capable nonmember must be paused.
    * A packet change drains every live V13 ticket first, as before. A
      membership-only change drains just the removed members' live tickets;
      retained members keep their leases, which were issued on this packet.
    """
    if payload.confirmation != "ROTATE V13 SCORER PACKET":
        raise HTTPException(409, "confirmation must be ROTATE V13 SCORER PACKET")
    async with session.begin():
        if not await try_lock_rollout_dispatch(session):
            raise HTTPException(409, "ticket dispatch is busy; retry after it settles")
        current = await current_pin(session)
        if current is None:
            raise HTTPException(409, "activate the V13 cohort before rotating")
        current_rotation_id = (
            current.rotation_id
            if isinstance(current, V13ScorerCohortRotation)
            else None
        )
        expected_hotkeys = (
            payload.expected_current_hotkeys
            if payload.expected_current_hotkeys is not None
            else payload.hotkeys
        )
        if (
            current_rotation_id != payload.expected_current_rotation_id
            or current.hotkeys != list(expected_hotkeys)
            or current.packet != payload.expected_current_packet.model_dump(mode="json")
        ):
            raise HTTPException(409, "current V13 cohort changed; refresh first")
        packet_changed = payload.packet != payload.expected_current_packet
        membership_changed = list(payload.hotkeys) != current.hotkeys
        if not packet_changed and not membership_changed:
            raise HTTPException(
                409, "rotation must change the scorer packet or the membership"
            )
        revision, checksum, paused = _settings_paused(
            await latest_validator_slot_settings_revision(session),
            revision=payload.expected_slot_settings_revision,
            checksum=payload.expected_slot_settings_checksum,
        )
        now = datetime.now(UTC)
        await _require_nonmembers_paused(
            session, members=payload.hotkeys, paused=paused, now=now
        )
        new_members = sorted(set(payload.hotkeys) - set(current.hotkeys))
        await _require_admitted_signers(
            session,
            hotkeys=new_members,
            packet=payload.packet,
            paused=paused,
            now=now,
        )
        authority = authority_from_packets(
            await member_packets(session, payload.hotkeys, now=now)
        )
        if authority is None or authority.packet != payload.packet:
            raise HTTPException(
                409,
                "target packet is not signed by a live majority of at least "
                "three proposed members",
            )
        drain = select(ValidatorTicket.agent_id).where(
            ValidatorTicket.bench_version == 13,
            ValidatorTicket.status == TicketStatus.ISSUED,
            ValidatorTicket.deadline > now,
        )
        if not packet_changed:
            drain = drain.where(
                ValidatorTicket.validator_hotkey.not_in(payload.hotkeys)
            )
        if await session.scalar(drain.limit(1)) is not None:
            raise HTTPException(
                409,
                "all live V13 tickets must drain before rotation"
                if packet_changed
                else "removed members' live V13 tickets must drain before rotation",
            )
        await _require_routable(
            session, hotkeys=payload.hotkeys, packet=payload.packet, now=now
        )
        row = V13ScorerCohortRotation(
            bench_version=13,
            hotkeys=list(payload.hotkeys),
            packet=payload.packet.model_dump(mode="json"),
            previous_packet=current.packet,
            slot_settings_revision=revision,
            slot_settings_checksum=checksum,
            reason=payload.reason,
            actor=payload.actor.strip(),
        )
        session.add(row)
        await session.flush()
        _require_routed_packet(
            await pinned_cohort_packet(session, now=now), packet=payload.packet
        )
    await session.refresh(row)
    return row
