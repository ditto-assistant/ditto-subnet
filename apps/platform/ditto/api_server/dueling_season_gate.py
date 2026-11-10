"""Whether queued scoring is held for a dueling season, for public reads.

From bench v16 every scored run carries a generator slice from the open
season of its bench version
(:func:`ditto.api_server.dueling_seasons.resolve_dueling_slice`). While none
is open, validators are refused with ``retry_later`` and a waiting
submission is not waiting for validators at all: it waits for the next
season, which opens at a generator-round boundary. This module answers that
once per request, read-only, for the dashboard.

The opening time is an estimate: the round driver's last observed chain
position, the generator-round schedule, and the nominal block time. It is
``None`` whenever any of those is missing, stale, or no season can open.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import select

from ditto.api_server.dueling_seasons import dueling_target, open_season
from ditto.api_server.generator_competition import load_settings
from ditto.api_server.generator_rounds import CHAIN_BLOCK_SECONDS
from ditto.db.models import DuelingSeason
from ditto.db.queries.benchmark_carryover import carryover_agent_ids
from ditto.db.queries.benchmark_rollout import active_bench_version, open_rollout
from ditto_screening_protocol.dueling import is_dueling_bench_version

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from ditto.api_models.generator_rounds import GeneratorRoundSchedule


@dataclass(frozen=True)
class DuelingSeasonGate:
    """The dueling-season state of the bench version the queue scores at."""

    bench_version: int
    open: bool
    next_round_epoch: int | None
    """Subnet epoch of the round boundary the next season opens at."""
    next_opens_at: datetime | None
    """Approximate UTC instant of ``next_round_epoch``."""
    scheduled: bool = True
    """Whether a season for this version opens at a round boundary at all.

    False while the dueling target is another version (the source era of an
    open rollout) or no season can open: the row waits for a season of its
    version, not for the next boundary."""


@dataclass(frozen=True)
class QueueSeasonGates:
    """The season gates of the versions waiting rows are leased at.

    Mirrors the lease path in ``endpoints/validator.py``: during an open
    rollout, rows submitted at or after the rollout started and adopted
    carryover rows are leased at the desired version, while earlier rows still
    owed source-era scores are finished at ``from_version`` (source backfill).
    """

    desired: DuelingSeasonGate | None
    source: DuelingSeasonGate | None = None
    rollout_started_at: datetime | None = None
    adopted: frozenset[UUID] = frozenset()

    def for_agent(
        self, agent_id: UUID, created_at: datetime
    ) -> DuelingSeasonGate | None:
        """The gate on the version this agent's next lease would use."""
        if (
            self.rollout_started_at is not None
            and created_at < self.rollout_started_at
            and agent_id not in self.adopted
        ):
            return self.source
        return self.desired


def round_schedule(app_state: Any) -> GeneratorRoundSchedule | None:
    """The round driver's last observed schedule, if it has ticked."""
    loop = getattr(app_state, "generator_round_loop", None)
    return getattr(loop, "schedule", None)


def season_opening_epoch(
    schedule: GeneratorRoundSchedule | None, opening_round: int | None
) -> int | None:
    """First epoch of ``opening_round`` (default: the next round).

    Rounds after the current one follow back to back at the current
    ``epochs_per_round`` (:func:`ditto.api_server.generator_rounds.derive_round_clock`).
    None when the schedule is unknown or the boundary has already passed.
    """
    if (
        schedule is None
        or schedule.mode == "off"
        or schedule.current_round is None
        or schedule.next_round_epoch is None
    ):
        return None
    target = schedule.current_round + 1 if opening_round is None else opening_round
    ahead = target - schedule.current_round - 1
    if ahead < 0:
        return None
    return schedule.next_round_epoch + ahead * schedule.epochs_per_round


def epoch_starts_at(
    schedule: GeneratorRoundSchedule | None, epoch: int | None, *, now: datetime
) -> datetime | None:
    """Approximate instant the chain reaches subnet epoch ``epoch``.

    Mirrors :func:`ditto.api_server.generator_rounds.round_seconds_left`: the
    next epoch fires at ``next_epoch_block``, each later one ``tempo`` blocks
    on, measured from the block the driver observed. None when an input is
    missing or the estimate has already passed (a stale observation).
    """
    if (
        schedule is None
        or epoch is None
        or schedule.subnet_epoch_index is None
        or schedule.next_epoch_block is None
        or schedule.tempo is None
        or schedule.block is None
        or schedule.observed_at is None
    ):
        return None
    epochs_after_next = epoch - schedule.subnet_epoch_index - 1
    if epochs_after_next < 0:
        return None
    target_block = schedule.next_epoch_block + epochs_after_next * schedule.tempo
    at = schedule.observed_at + timedelta(
        seconds=(target_block - schedule.block) * CHAIN_BLOCK_SECONDS
    )
    return at if at > now else None


async def dueling_season_gate(
    session: AsyncSession,
    *,
    schedule: GeneratorRoundSchedule | None,
    now: datetime,
    active_version: int | None = None,
) -> DuelingSeasonGate | None:
    """The season gate on queued scoring, or None below the dueling floor.

    The queue scores fresh submissions at the rollout's desired version while
    a rollout is open, else at the active version (the lease path in
    ``endpoints/validator.py``); that version's lease needs an open season.
    ``active_version`` reuses the caller's already-resolved authority.
    """
    rollout = await open_rollout(session)
    if rollout is not None:
        version = rollout.desired_version
    elif active_version is not None:
        version = active_version
    else:
        version = await active_bench_version(session, open_transition=None)
    return await _gate_for_version(session, version, schedule=schedule, now=now)


async def queue_season_gates(
    session: AsyncSession,
    *,
    schedule: GeneratorRoundSchedule | None,
    now: datetime,
    active_version: int | None = None,
) -> QueueSeasonGates:
    """Season gates for every version a waiting row can be leased at."""
    rollout = await open_rollout(session)
    if rollout is None:
        version = (
            active_version
            if active_version is not None
            else await active_bench_version(session, open_transition=None)
        )
        return QueueSeasonGates(
            desired=await _gate_for_version(
                session, version, schedule=schedule, now=now
            )
        )
    return QueueSeasonGates(
        desired=await _gate_for_version(
            session, rollout.desired_version, schedule=schedule, now=now
        ),
        source=await _gate_for_version(
            session, rollout.from_version, schedule=schedule, now=now
        ),
        rollout_started_at=rollout.created_at,
        adopted=frozenset(await carryover_agent_ids(session, rollout=rollout)),
    )


async def _gate_for_version(
    session: AsyncSession,
    version: int,
    *,
    schedule: GeneratorRoundSchedule | None,
    now: datetime,
) -> DuelingSeasonGate | None:
    """The season gate on leases at ``version``, or None below the dueling floor."""
    if not is_dueling_bench_version(version):
        return None
    season = await open_season(session, version)
    if season is not None and season.season_seed is not None:
        return DuelingSeasonGate(
            bench_version=version,
            open=True,
            next_round_epoch=None,
            next_opens_at=None,
        )
    target = await dueling_target(session, (await load_settings(session)).settings)
    if target.blocked is not None or target.bench_version != version:
        # No season will open for this version at any boundary we can name.
        return DuelingSeasonGate(
            bench_version=version,
            open=False,
            next_round_epoch=None,
            next_opens_at=None,
            scheduled=False,
        )
    committed = await session.scalar(
        select(DuelingSeason.season_index).where(
            DuelingSeason.bench_version == version,
            DuelingSeason.status == "committed",
        )
    )
    epoch = season_opening_epoch(schedule, committed)
    return DuelingSeasonGate(
        bench_version=version,
        open=False,
        next_round_epoch=epoch,
        next_opens_at=epoch_starts_at(schedule, epoch, now=now),
    )
