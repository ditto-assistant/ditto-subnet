"""Season title duels: the continual lane under ``retest_mode = season_duel``.

``docs/title-duels.md`` (phase A). At a dueling bench version the lane stops
walking every cohort member up to the elastic shared-seed target and runs one
duel per dueling season instead:

* **One family per season.** The season's confirmation family is anchored on
  the champion that opened it: the first reign anchor recorded for
  ``(bench_version, season)``. A crown change mid-season therefore draws no
  new seeds and re-walks nobody; the new king already holds the season's
  seeds against every challenger.
* **K seeds, all at once.** The duel is the first ``season_duel_seeds``
  replicate indices of that family. Every member owes each of them exactly
  once, and all are leasable at once (no wave growth, no tempo pacing).
* **Top five only.** The cohort is the folded emission set plus the
  public-board top five; the tie band, cohort extension and owner successors
  are not retested.

Only which seeds are leased changes. The seeds are ordinary champion-anchored
CRN seeds bound to a pinned anchor, so validators re-derive them exactly as
before, and the fold reads them exactly as before. Below the dueling floor
every helper here is inert.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

from sqlalchemy import select

from ditto.api_server.crn import champion_anchored_seeds
from ditto.db.models import ConfirmationSeedAnchor
from ditto_screening_protocol.dueling import is_dueling_bench_version

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

    from ditto.api_models.continual_retest_settings import ContinualRetestSettings


def season_duel_seed_count(
    settings: ContinualRetestSettings, *, bench_version: int
) -> int | None:
    """``season_duel_seeds`` when season duels govern ``bench_version``.

    ``None`` means the historical continual walk: the mode is ``continual``,
    or the version is below the dueling floor (no seasons to duel in).
    """
    if settings.retest_mode != "season_duel":
        return None
    if not is_dueling_bench_version(bench_version):
        return None
    return int(settings.season_duel_seeds)


async def season_duel_anchor_champion(
    session: AsyncSession, *, bench_version: int, champion_agent_id: UUID
) -> UUID:
    """The champion the current season's duel family is anchored on.

    The first anchor row recorded in the current dueling season (pinned or
    still in its finality wait), oldest anchor block first, so the family is
    the one the season opened on. With no row yet this is the current
    champion, whose first claim creates it.
    """
    from ditto.api_server.confirmation_seed_anchor import anchor_season_key

    season_key = await anchor_season_key(session, bench_version)
    first = await session.scalar(
        select(ConfirmationSeedAnchor.champion_agent_id)
        .where(
            ConfirmationSeedAnchor.bench_version == bench_version,
            ConfirmationSeedAnchor.season_key == season_key,
        )
        .order_by(
            ConfirmationSeedAnchor.anchor_block,
            ConfirmationSeedAnchor.champion_agent_id,
        )
        .limit(1)
    )
    return champion_agent_id if first is None else first


def season_duel_seeds(
    anchor_agent_id: UUID,
    *,
    version: int,
    count: int,
    block_hash: str | None,
    allow_fresh_seeds: bool,
) -> tuple[int, ...]:
    """The season's K duel seeds, or ``()`` while the anchor is unpinned.

    A binding version waits for its finalized anchor before any duel seed
    exists: the finality wait is what keeps the family unpredictable.
    """
    if not allow_fresh_seeds:
        return ()
    return tuple(
        champion_anchored_seeds(
            anchor_agent_id,
            version=version,
            max_seeds=count,
            block_hash=block_hash,
        )
    )


def season_duel_owed(
    duel_seeds: Iterable[int], member_seeds: Iterable[int]
) -> tuple[int, ...]:
    """The duel seeds a member has not yet run, in replicate order."""
    held = set(member_seeds)
    return tuple(seed for seed in duel_seeds if seed not in held)


def season_duel_unserved(
    *,
    member_ids: Iterable[UUID],
    duel_seeds: Iterable[int],
    seeds_by_agent: Mapping[UUID, Iterable[int]],
    leases: Mapping[UUID, Mapping[str, int | None]],
) -> frozenset[UUID]:
    """Members owing a duel seed that no live lease is already running."""
    seeds = tuple(duel_seeds)
    unserved: set[UUID] = set()
    for member_id in dict.fromkeys(member_ids):
        owed = season_duel_owed(seeds, seeds_by_agent.get(member_id, ()))
        if not owed:
            continue
        member_leases = leases.get(member_id, {})
        if any(seed is None for seed in member_leases.values()):
            continue
        covered = {seed for seed in member_leases.values() if seed is not None}
        if any(seed not in covered for seed in owed):
            unserved.add(member_id)
    return frozenset(unserved)
