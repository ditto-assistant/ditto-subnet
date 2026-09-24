"""Exact, immutable V13 scorer routing and signed packet checks."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.benchmark_capacity import BenchmarkCapacity
from ditto.api_models.ticket_status import TicketStatus
from ditto.api_models.validator_capabilities import ValidatorStackIdentity
from ditto.api_models.validator_slot_settings import ValidatorSlotSettings
from ditto.db.models import V13ScorerCohortPin, ValidatorHeartbeat, ValidatorTicket
from ditto.db.queries.benchmark_rollout import (
    heartbeat_supports_version,
    verified_scorer_for_version,
)
from ditto.db.queries.validator_slot_settings import (
    latest_validator_slot_settings_revision,
)


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


async def current_pin(session: AsyncSession) -> V13ScorerCohortPin | None:
    return await session.get(V13ScorerCohortPin, 13)


async def pinned_validator_allowed(
    session: AsyncSession, *, hotkey: str, now: datetime
) -> bool:
    pin = await current_pin(session)
    if pin is None:
        return True  # Before activation, existing V13 routing remains in force.
    if hotkey not in pin.hotkeys:
        return False
    heartbeat = await session.get(ValidatorHeartbeat, hotkey)
    packet = packet_for_heartbeat(heartbeat, now=now)
    return packet is not None and packet.model_dump(mode="json") == pin.packet


async def pinned_cohort_packet(
    session: AsyncSession, *, now: datetime
) -> tuple[V13ScorerPacket, int] | None:
    pin = await current_pin(session)
    if pin is None:
        return None
    try:
        expected = V13ScorerPacket.model_validate(pin.packet)
    except ValidationError:
        return None
    if len(pin.hotkeys) != 3 or len(set(pin.hotkeys)) != 3:
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
    for hotkey in pin.hotkeys:
        heartbeat = await session.get(ValidatorHeartbeat, hotkey)
        if packet_for_heartbeat(heartbeat, now=now) != expected:
            return None
        assert heartbeat is not None
        if hotkey in paused:
            return None
        try:
            capacity = BenchmarkCapacity.model_validate(heartbeat.benchmark_capacity)
        except ValidationError:
            return None
        if capacity.admission != "accepting" or not capacity.healthy_slots:
            return None
        scorer = verified_scorer_for_version(heartbeat, version=13)
        if scorer is None or scorer.observed_at is None:
            return None
        observations.append(scorer.observed_at)
    # A pin only certifies future work once all nonmembers have drained. Check
    # on each lease too, so a stray writer cannot silently widen authority.
    unpinned_live = await session.scalar(
        select(ValidatorTicket.agent_id)
        .where(
            ValidatorTicket.bench_version == 13,
            ValidatorTicket.status == TicketStatus.ISSUED,
            ValidatorTicket.deadline > now,
            ValidatorTicket.validator_hotkey.not_in(pin.hotkeys),
        )
        .limit(1)
    )
    if unpinned_live is not None:
        return None
    return expected, min(observations)
