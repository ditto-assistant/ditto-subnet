"""Pin, read, and apply the finalized-block binding of a confirmation seed family.

The champion-anchored CRN family (:mod:`ditto.api_server.crn`) used to be a pure
function of the champion's public agent id, so a challenger could name the
champion's confirmation datasets before they were ever leased. From
:data:`~ditto.api_server.crn.CRN_BLOCK_BINDING_MIN_BENCH_VERSION` every fresh
seed of a reign also hashes the finalized chain block ``B_ready + Δ``:

* ``B_ready`` is the latest block when the reign first asks for a seed. The
  anchor row is created then, **unpinned**.
* ``Δ`` (:data:`~ditto.api_server.crn.CRN_ANCHOR_BLOCK_DELTA`) puts the anchor
  height in the future for everyone, Platform included, so "when to read" is
  not a choice anyone can grind.
* The hash is read only through ``ChainClient.get_finalized_block_hash``, which
  refuses a not-yet-finalized height. Until it lands the lane may still issue
  **catch-up** seeds (recorded coverage needs no fresh draw) but introduces no
  unbound fresh seed: the finality wait is the only thing that opens the wave.
* The chain read never runs inside a write transaction. A claim first
  :func:`prefetch_finalized_anchor_hashes` (a short read of the waiting rows,
  then bounded ``asyncio.wait_for`` RPCs) and only then opens its transaction
  and :func:`resolve_reign_seed_anchor` pins from what was prefetched. A slow
  Substrate endpoint therefore costs one claim a few seconds, never a held
  Platform row lock.

Everything here is version-gated by :func:`crn_block_binding_active`; legacy
versions resolve to the unbound derivation and never create a row.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ditto.api_server.crn import (
    CRN_ANCHOR_BLOCK_DELTA,
    champion_anchored_seeds,
    crn_block_binding_active,
)
from ditto.api_server.koth import TOP5_MAX_CONFIRMATION_SEEDS
from ditto.chain import ChainError
from ditto.db.queries.confirmation_seed_anchors import (
    ensure_confirmation_seed_anchor,
    get_confirmation_seed_anchor,
    list_pinned_confirmation_seed_anchors,
    list_unpinned_confirmation_seed_anchors,
    pin_confirmation_seed_anchor,
)

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

    from ditto.chain.client import ChainClient
    from ditto.db.models import ConfirmationSeedAnchor

logger = logging.getLogger(__name__)

CRN_ANCHOR_PIN_TIMEOUT_SECONDS = 3.0
"""Ceiling on one finalized-hash read during a claim. A read that outlasts it
is "not pinned yet": the row stays unpinned and the next claim retries."""

CRN_ANCHOR_PINS_PER_CLAIM = 4
"""Most waiting anchors one claim tries to pin, bounding its chain RPC budget."""

AnchorKey = tuple["UUID", int]
"""``(champion_agent_id, bench_version)``: the reign an anchor row belongs to."""


@dataclass(frozen=True)
class ReignSeedAnchor:
    """One reign's finalized-block binding, pinned or still waiting."""

    champion_agent_id: UUID
    bench_version: int
    ready_block: int
    anchor_block: int
    block_hash: str | None

    @property
    def pinned(self) -> bool:
        return self.block_hash is not None

    @property
    def planning(self) -> tuple[str | None, bool]:
        """``(block_hash, allow_fresh_seeds)`` for the seed planners.

        A pinned anchor plans fresh bound seeds; an unpinned one plans only
        recorded coverage (``allow_fresh_seeds=False``).
        """
        return (self.block_hash, self.block_hash is not None)


@dataclass(frozen=True)
class SeedBinding:
    """How one issued seed re-derives: ``crn_seed([anchor], version, k, hash)``."""

    anchor_agent_id: UUID
    seed_index: int
    seed_block: int
    seed_block_hash: str


LEGACY_PLANNING: tuple[str | None, bool] = (None, True)
"""Unbound derivation, fresh seeds allowed: what every version below the floor plans."""


def _anchor_from_row(row: ConfirmationSeedAnchor) -> ReignSeedAnchor:
    return ReignSeedAnchor(
        champion_agent_id=row.champion_agent_id,
        bench_version=row.bench_version,
        ready_block=row.ready_block,
        anchor_block=row.anchor_block,
        block_hash=row.anchor_block_hash,
    )


async def prefetch_finalized_anchor_hashes(
    session: AsyncSession,
    chain: ChainClient,
    *,
    latest_block: int,
    timeout: float = CRN_ANCHOR_PIN_TIMEOUT_SECONDS,
    limit: int = CRN_ANCHOR_PINS_PER_CLAIM,
) -> dict[AnchorKey, str]:
    """Read the finalized hash of every waiting anchor the head has reached.

    Runs **before** a claim's write transaction: the waiting rows are read in
    their own short transaction on ``session`` and every chain RPC happens with
    no transaction open at all, each bounded by ``timeout``. Chain trouble or a
    timeout is logged and simply leaves that reign out of the result -- the
    claim still proceeds, the row stays unpinned, and the next claim retries.
    The result is what :func:`resolve_reign_seed_anchor` pins from.
    """
    async with session.begin():
        waiting = await list_unpinned_confirmation_seed_anchors(
            session, up_to_block=latest_block, limit=limit
        )
        pending = [
            (row.champion_agent_id, row.bench_version, row.anchor_block)
            for row in waiting
        ]
    finalized: dict[AnchorKey, str] = {}
    for champion_agent_id, bench_version, anchor_block in pending:
        try:
            block_hash = await asyncio.wait_for(
                chain.get_finalized_block_hash(anchor_block), timeout=timeout
            )
        except (ChainError, TimeoutError) as exc:
            logger.info(
                "confirmation seed anchor champion=%s version=%d block=%d not "
                "pinned yet: %s",
                champion_agent_id,
                bench_version,
                anchor_block,
                exc,
            )
            continue
        finalized[(champion_agent_id, bench_version)] = block_hash
    return finalized


async def resolve_reign_seed_anchor(
    session: AsyncSession,
    *,
    champion_agent_id: UUID,
    bench_version: int,
    ready_block: int,
    now: datetime,
    finalized_hashes: Mapping[AnchorKey, str] | None = None,
    delta: int = CRN_ANCHOR_BLOCK_DELTA,
) -> ReignSeedAnchor | None:
    """Create-or-read the reign's anchor and pin it from a prefetched hash.

    ``None`` below the binding floor. ``finalized_hashes`` is what
    :func:`prefetch_finalized_anchor_hashes` read before the caller's
    transaction opened; a reign absent from it (never waiting, not finalized,
    chain trouble) is left unpinned and keeps serving catch-up. No chain call
    happens here, so the caller's transaction never waits on Substrate, and a
    pin is never a non-finalized hash: only the finalized read produces one.
    """
    if not crn_block_binding_active(bench_version):
        return None
    row = await ensure_confirmation_seed_anchor(
        session,
        champion_agent_id=champion_agent_id,
        bench_version=bench_version,
        ready_block=ready_block,
        delta=delta,
    )
    if row.anchor_block_hash is None and finalized_hashes:
        block_hash = finalized_hashes.get((champion_agent_id, bench_version))
        if block_hash is not None:
            row = await pin_confirmation_seed_anchor(
                session, row, block_hash=block_hash, now=now
            )
            logger.info(
                "confirmation seed anchor pinned champion=%s version=%d block=%d",
                champion_agent_id,
                bench_version,
                row.anchor_block,
            )
    return _anchor_from_row(row)


async def read_reign_seed_anchor(
    session: AsyncSession, *, champion_agent_id: UUID, bench_version: int
) -> ReignSeedAnchor | None:
    """The reign's anchor as recorded, never creating or pinning. ``None`` below
    the floor or when the reign has never claimed."""
    if not crn_block_binding_active(bench_version):
        return None
    row = await get_confirmation_seed_anchor(
        session, champion_agent_id=champion_agent_id, bench_version=bench_version
    )
    return None if row is None else _anchor_from_row(row)


async def reign_seed_planning(
    session: AsyncSession, *, champion_agent_id: UUID, bench_version: int
) -> tuple[str | None, bool]:
    """``(block_hash, allow_fresh_seeds)`` for planners that only read.

    Below the floor: the legacy unbound derivation with fresh seeds allowed. At
    or above it: the pinned hash, or -- with no pin yet -- **no fresh seeds**,
    so a reign that has not finished its finality wait can only catch up.
    """
    if not crn_block_binding_active(bench_version):
        return LEGACY_PLANNING
    anchor = await read_reign_seed_anchor(
        session, champion_agent_id=champion_agent_id, bench_version=bench_version
    )
    if anchor is None:
        return (None, False)
    return anchor.planning


async def list_reign_seed_anchors(
    session: AsyncSession, *, bench_version: int
) -> list[ReignSeedAnchor]:
    """Every pinned anchor of one version, oldest first; empty below the floor."""
    if not crn_block_binding_active(bench_version):
        return []
    rows = await list_pinned_confirmation_seed_anchors(
        session, bench_version=bench_version
    )
    return [_anchor_from_row(row) for row in rows]


def bind_confirmation_seed(
    anchors: Sequence[ReignSeedAnchor],
    *,
    seed: int,
    bench_version: int,
    max_seeds: int = TOP5_MAX_CONFIRMATION_SEEDS,
) -> SeedBinding | None:
    """Name the ``(anchor, k, block)`` a seed re-derives from, or None.

    Searched across every pinned anchor of the version, oldest first: a
    catch-up seed introduced under a previous reign still binds to *that*
    reign's block, so a validator can re-derive it without knowing reign
    history. ``None`` means no pinned anchor of this version derives the seed
    -- a legacy seed, or a version below the floor.
    """
    if not crn_block_binding_active(bench_version):
        return None
    for anchor in anchors:
        if anchor.block_hash is None or anchor.bench_version != bench_version:
            continue
        family = champion_anchored_seeds(
            anchor.champion_agent_id,
            version=bench_version,
            max_seeds=max_seeds,
            block_hash=anchor.block_hash,
        )
        if seed in family:
            return SeedBinding(
                anchor_agent_id=anchor.champion_agent_id,
                seed_index=family.index(seed),
                seed_block=anchor.anchor_block,
                seed_block_hash=anchor.block_hash,
            )
    return None
