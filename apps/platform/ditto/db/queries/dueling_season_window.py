"""Which dueling seasons' scores count, at bench v16+.

Scores from different generator sets are not comparable, so from
``DUELING_MIN_BENCH_VERSION`` the season is part of a score's identity
(``research/dittobench-datagen/docs/dueling-bench.md`` section 3, "Churn and
comparability"):

* the **current** season of a bench version is its open season, or (between a
  close and the next open) the most recently opened one;
* the **previous** season is the one opened before it. Its scores stay visible
  as *provisional* for one season;
* every older season has **expired**: its scores leave the authoritative
  ledger, the queue floors and rollout completion. They stay in the database
  for audit and Backroom.

The two-season window is what ranking, the validator ledger, queue order and
rollout completion read. Seed *planning* reads the current season alone, so a
rotation starts a fresh confirmation family on its own reign anchor instead of
catching up on the last season's seeds.

Every helper is inert below the dueling floor: it returns ``None`` (no
filter), so v2-v15 reads are byte-identical.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import and_, false, or_, select, true

from ditto.db.models import DuelingSeason
from ditto_screening_protocol.dueling import (
    DUELING_MIN_BENCH_VERSION,
    is_dueling_bench_version,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement


@dataclass(frozen=True)
class DuelingSeasonWindow:
    """The seasons whose scores count for one dueling bench version."""

    bench_version: int
    current: UUID | None
    previous: UUID | None

    @property
    def comparable(self) -> frozenset[UUID]:
        """Current plus provisional: what ranking and completion read."""
        return frozenset(s for s in (self.current, self.previous) if s is not None)

    @property
    def planning(self) -> frozenset[UUID]:
        """The current season only: what seed planning reads."""
        return frozenset(() if self.current is None else (self.current,))


async def dueling_season_window(
    session: AsyncSession, bench_version: int | None
) -> DuelingSeasonWindow | None:
    """The window for ``bench_version``, or ``None`` below the floor.

    Shadow and authoritative seasons of one version form one sequence: a
    rollout's last shadow season is the provisional season of the first
    authoritative one after activation. A dueling version that has never
    opened a season has an empty window, so none of its rows count.
    """
    if bench_version is None or not is_dueling_bench_version(bench_version):
        return None
    rows = list(
        await session.scalars(
            select(DuelingSeason.id)
            .where(
                DuelingSeason.bench_version == bench_version,
                # Only an open season closes, so these are exactly the
                # seasons that ever opened.
                DuelingSeason.status.in_(("open", "closed", "revealed")),
            )
            .order_by(DuelingSeason.season_index.desc())
            .limit(2)
        )
    )
    return DuelingSeasonWindow(
        bench_version=int(bench_version),
        current=rows[0] if rows else None,
        previous=rows[1] if len(rows) > 1 else None,
    )


async def dueling_season_windows(
    session: AsyncSession, bench_versions: Iterable[int | None]
) -> dict[int, DuelingSeasonWindow]:
    """One window per dueling version in ``bench_versions`` (others skipped)."""
    out: dict[int, DuelingSeasonWindow] = {}
    for version in sorted({v for v in bench_versions if v is not None}):
        window = await dueling_season_window(session, version)
        if window is not None:
            out[version] = window
    return out


def season_scoped(
    version_column: ColumnElement[Any] | Any,
    season_column: ColumnElement[Any] | Any,
    windows: Mapping[int, DuelingSeasonWindow],
    *,
    planning: bool = False,
) -> ColumnElement[bool]:
    """SQL: a row below the floor, or a dueling row inside its version's window.

    ``windows`` must name every dueling version the query can return; a
    dueling row of a version missing from it is excluded (fail closed), never
    admitted unscoped.
    """
    clauses: list[ColumnElement[bool]] = [version_column < DUELING_MIN_BENCH_VERSION]
    for version, window in sorted(windows.items()):
        ids = window.planning if planning else window.comparable
        clauses.append(
            and_(version_column == version, season_column.in_(sorted(ids)))
            if ids
            else false()
        )
    return or_(*clauses) if len(clauses) > 1 else clauses[0]


async def season_scope_predicate(
    session: AsyncSession,
    *,
    version_column: ColumnElement[Any] | Any,
    season_column: ColumnElement[Any] | Any,
    bench_versions: Iterable[int | None],
    planning: bool = False,
) -> ColumnElement[bool]:
    """:func:`season_scoped` for ``bench_versions``; ``true()`` below the floor."""
    versions = [v for v in bench_versions if v is not None]
    if not any(is_dueling_bench_version(v) for v in versions):
        return true()
    windows = await dueling_season_windows(session, versions)
    return season_scoped(version_column, season_column, windows, planning=planning)
