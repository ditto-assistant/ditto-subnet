"""Exact, append-only V13 scorer cohort routing and signed packet checks.

The pinned hotkeys (three to sixteen of them) are the only validators that may
be issued or submit V13 work. Each agent is still scored by exactly
``SCORING_QUORUM`` distinct validators; a larger cohort only widens who may
claim one of those seats.

The scorer packet is not stored authority. It follows the *authority packet*:
the verified managed release signed by a strict majority of the live members,
where that majority is itself at least ``SCORING_QUORUM`` members. Routine
fleet auto-updates therefore never need a manual rotation, one lagging or
offline member no longer freezes the others, and with no such majority nobody
is issued V13 work (fail closed). For a three-member cohort the rule reduces to
"all three live and identical", which is the original unanimity rule.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.benchmark_capacity import BenchmarkCapacity
from ditto.api_models.ticket_status import TicketStatus
from ditto.api_models.validator_capabilities import ValidatorStackIdentity
from ditto.api_models.validator_slot_settings import ValidatorSlotSettings
from ditto.db.models import (
    V13ScorerCohortPin,
    V13ScorerCohortRotation,
    ValidatorHeartbeat,
    ValidatorTicket,
)
from ditto.db.queries.benchmark_rollout import (
    SCORING_QUORUM,
    heartbeat_supports_version,
    verified_scorer_for_version,
)
from ditto.db.queries.validator_slot_settings import (
    latest_validator_slot_settings_revision,
)

MIN_COHORT_MEMBERS = SCORING_QUORUM
"""A cohort smaller than the quorum could never finish an agent."""
MAX_COHORT_MEMBERS = 16
"""Storage ceiling, mirrored by the pin and rotation CHECK constraints."""


class V13ScorerPacket(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    source_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    release_descriptor_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    scorer_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    scorer_env_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    injected_keys: Annotated[
        tuple[str, ...],
        BeforeValidator(
            lambda value: tuple(value) if isinstance(value, list) else value
        ),
    ]


def packet_for_heartbeat(
    heartbeat: ValidatorHeartbeat | None, *, now: datetime
) -> V13ScorerPacket | None:
    if heartbeat is None or not heartbeat_supports_version(
        heartbeat, now=now, version=13
    ):
        return None
    try:
        stack = ValidatorStackIdentity.model_validate_json(json.dumps(heartbeat.stack))
    except ValidationError:
        return None
    if stack.mode != "managed" or stack.release_descriptor_digest is None:
        return None
    image = stack.components.dittobench_api.image_digest
    scorer = verified_scorer_for_version(heartbeat, version=13)
    env = scorer.scored_runtime_env if scorer is not None else None
    if image is None or env is None:
        return None
    try:
        return V13ScorerPacket(
            source_revision=env.source_revision,
            release_descriptor_digest=stack.release_descriptor_digest,
            scorer_image_digest=image,
            scorer_env_sha256=env.sha256,
            injected_keys=env.injected_keys,
        )
    except ValidationError:
        return None


async def current_pin(
    session: AsyncSession,
) -> V13ScorerCohortPin | V13ScorerCohortRotation | None:
    rotation = await session.scalar(
        select(V13ScorerCohortRotation)
        .where(V13ScorerCohortRotation.bench_version == 13)
        .order_by(V13ScorerCohortRotation.rotation_id.desc())
        .limit(1)
    )
    return (
        rotation if rotation is not None else await session.get(V13ScorerCohortPin, 13)
    )


@dataclass(frozen=True)
class CohortAuthority:
    """The packet a live majority of the pinned members signs, and who signs it."""

    packet: V13ScorerPacket
    supporters: tuple[str, ...]
    """Pinned members whose fresh heartbeat signs ``packet``, sorted."""
    live: int
    """Pinned members with any fresh verified managed packet at all."""


@dataclass(frozen=True)
class CohortPacket:
    """The authority packet plus the members that can route work on it now."""

    packet: V13ScorerPacket
    oldest_scorer_observed_at: int
    routable: tuple[str, ...]
    """Supporters that are unpaused, accepting and expose a scorer probe."""


def valid_membership(hotkeys: object) -> bool:
    """Three to sixteen distinct hotkeys; anything else fails closed."""
    return (
        isinstance(hotkeys, list | tuple)
        and MIN_COHORT_MEMBERS <= len(hotkeys) <= MAX_COHORT_MEMBERS
        and all(isinstance(hotkey, str) for hotkey in hotkeys)
        and len(set(hotkeys)) == len(hotkeys)
    )


def authority_from_packets(
    packets: Mapping[str, V13ScorerPacket | None],
) -> CohortAuthority | None:
    """Pick the packet a strict majority of live members signs, if any.

    A member is live when its fresh, verified, managed heartbeat yields a
    packet. The winning packet needs a strict majority of the live members
    *and* at least ``SCORING_QUORUM`` signers, so a quorum can always be built
    from members that agree. A tie, a plurality, or fewer than three signers is
    no authority at all.

    Issuance-paused members still vote: they run a verified release, and
    excluding them would freeze a three-member cohort whenever one member is
    paused, where today the other two keep leasing. They are declined
    individually by the pause gate in ``/validator/job``.
    """
    live = {hotkey: packet for hotkey, packet in packets.items() if packet is not None}
    if not live:
        return None
    packet, support = Counter(live.values()).most_common(1)[0]
    if support < SCORING_QUORUM or support * 2 <= len(live):
        return None
    return CohortAuthority(
        packet=packet,
        supporters=tuple(
            sorted(hotkey for hotkey, signed in live.items() if signed == packet)
        ),
        live=len(live),
    )


async def member_packets(
    session: AsyncSession, hotkeys: Sequence[str], *, now: datetime
) -> dict[str, V13ScorerPacket | None]:
    """Each named member's current verified packet, ``None`` when not live."""
    rows = (
        await session.scalars(
            select(ValidatorHeartbeat).where(
                ValidatorHeartbeat.validator_hotkey.in_(list(hotkeys))
            )
        )
    ).all()
    by_hotkey = {row.validator_hotkey: row for row in rows}
    return {
        hotkey: packet_for_heartbeat(by_hotkey.get(hotkey), now=now)
        for hotkey in hotkeys
    }


async def cohort_authority(
    session: AsyncSession,
    pin: V13ScorerCohortPin | V13ScorerCohortRotation,
    *,
    now: datetime,
) -> CohortAuthority | None:
    """The authority packet for the stored pin, or ``None`` (fail closed)."""
    if not valid_membership(pin.hotkeys):
        return None
    return authority_from_packets(await member_packets(session, pin.hotkeys, now=now))


async def pinned_validator_allowed(
    session: AsyncSession, *, hotkey: str, now: datetime
) -> bool:
    """Admit new V13 work only for a member signing the authority packet.

    Nonmembers are always refused. A member on another packet (still
    updating, or on a release the majority has left) is refused on its own,
    without freezing the members that agree. With no authority packet nobody
    is admitted, which is the fail-closed answer for a split cohort.
    """
    pin = await current_pin(session)
    if pin is None:
        return True  # Before activation, existing V13 routing remains in force.
    if hotkey not in pin.hotkeys:
        return False
    authority = await cohort_authority(session, pin, now=now)
    return authority is not None and hotkey in authority.supporters


async def pinned_validator_may_submit(
    session: AsyncSession, *, hotkey: str, now: datetime
) -> bool:
    """Accept a V13 score from a member still running a verified release.

    Issuance already required the authority packet, so a lease that outlives a
    fleet update is not rejected merely because its validator updated first.
    """
    pin = await current_pin(session)
    if pin is None:
        return True
    if hotkey not in pin.hotkeys:
        return False
    heartbeat = await session.get(ValidatorHeartbeat, hotkey)
    return packet_for_heartbeat(heartbeat, now=now) is not None


async def pinned_cohort_packet(
    session: AsyncSession, *, now: datetime
) -> CohortPacket | None:
    return await _cohort_packet(session, now=now, report_only=False)


async def report_only_current_cohort_packet(
    session: AsyncSession, *, now: datetime
) -> CohortPacket | None:
    """Observe the current pinned members without changing primary authority."""
    return await _cohort_packet(session, now=now, report_only=True)


async def _cohort_packet(
    session: AsyncSession, *, now: datetime, report_only: bool
) -> CohortPacket | None:
    """The authority packet, while at least a quorum of its signers can route.

    This is the scorer environment a screening lease binds to: only members
    signing the authority packet are issued V13 work, so any future scorer of
    the claimed submission runs exactly this packet. It is available only
    while ``SCORING_QUORUM`` of those signers are unpaused, accepting, and
    expose a verified scorer probe. For a three-member cohort that is all
    three, as before.
    """
    pin = await current_pin(session)
    if pin is None:
        return None
    if not valid_membership(pin.hotkeys):
        return None
    try:
        V13ScorerPacket.model_validate(pin.packet)
    except ValidationError:
        return None
    # Authority follows the release a live majority signs; the stored packet
    # records the last operator rotation and no longer gates leases.
    # ``report_only`` keeps its historical reader name for the same answer.
    del report_only
    return await routable_cohort_packet(session, hotkeys=pin.hotkeys, now=now)


async def routable_cohort_packet(
    session: AsyncSession, *, hotkeys: Sequence[str], now: datetime
) -> CohortPacket | None:
    """:func:`_cohort_packet` for an explicit membership, stored or proposed.

    Activation and rotation evaluate the proposed membership with this before
    they write, so a refused write never consumes a rotation ID.
    """
    if not valid_membership(list(hotkeys)):
        return None
    authority = authority_from_packets(await member_packets(session, hotkeys, now=now))
    if authority is None:
        return None
    settings = await latest_validator_slot_settings_revision(session)
    if settings is None:
        return None
    try:
        paused = set(
            ValidatorSlotSettings.model_validate(
                settings.settings
            ).paused_validator_hotkeys
        )
    except ValidationError:
        return None
    observations: list[int] = []
    routable: list[str] = []
    for hotkey in authority.supporters:
        if hotkey in paused:
            continue
        heartbeat = await session.get(ValidatorHeartbeat, hotkey)
        if heartbeat is None:
            continue
        try:
            capacity = BenchmarkCapacity.model_validate(heartbeat.benchmark_capacity)
        except ValidationError:
            continue
        if capacity.admission != "accepting" or not capacity.healthy_slots:
            continue
        scorer = verified_scorer_for_version(heartbeat, version=13)
        if scorer is None or scorer.observed_at is None:
            continue
        observations.append(scorer.observed_at)
        routable.append(hotkey)
    if len(routable) < SCORING_QUORUM:
        return None
    # A pin only certifies future work once all nonmembers have drained. Check
    # on each lease too, so a stray writer cannot silently widen authority.
    unpinned_live = await session.scalar(
        select(ValidatorTicket.agent_id)
        .where(
            ValidatorTicket.bench_version == 13,
            ValidatorTicket.status == TicketStatus.ISSUED,
            ValidatorTicket.deadline > now,
            ValidatorTicket.validator_hotkey.not_in(list(hotkeys)),
        )
        .limit(1)
    )
    if unpinned_live is not None:
        return None
    return CohortPacket(
        packet=authority.packet,
        oldest_scorer_observed_at=min(observations),
        routable=tuple(routable),
    )
