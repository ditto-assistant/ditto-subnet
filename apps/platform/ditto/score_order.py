"""The single comparator that puts submissions in order, in both languages.

This is the leaf of the ranking stack: the public leaderboard's ``rank``, the
validator ledger, the KOTH champion/tail fold, the queue's score floors and the
efficiency lineage dedupe all order rows through :func:`score_order_key`, and
the ledger read produces the same order in SQL through
:func:`score_order_terms`. Those two forms are the only two copies of the rule
that exist, they live next to each other here, and
``ditto/tests/db/queries/test_score_ranking.py`` asserts they agree row for row.

It carries no ditto imports on purpose. :mod:`ditto.api_server.koth` mirrors the
subnet's frozen consensus fold and must be able to read the comparator without
pulling the database layer in behind it; :mod:`ditto.db.queries.score_ranking`
re-exports everything here beside the score the comparator ranks on.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol, TypeVar
from uuid import UUID

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy import ColumnElement


class RankableRow(Protocol):
    """The minimum a row needs to take a place in the canonical order."""

    @property
    def agent_id(self) -> UUID: ...

    @property
    def first_seen(self) -> datetime: ...

    @property
    def composite(self) -> float: ...


class FinalizedRow(RankableRow, Protocol):
    """A rankable row that can also be projected through the KOTH fold."""

    @property
    def miner_hotkey(self) -> str: ...

    @property
    def bench_version(self) -> int: ...


R = TypeVar("R", bound=RankableRow)
F = TypeVar("F", bound=FinalizedRow)

ScoreOrderKey = tuple[bool, float, float, datetime, str]


def score_order_key(
    row: RankableRow,
    *,
    score: float | None = None,
    secondary_score: float | None = None,
) -> ScoreOrderKey:
    """The canonical comparator: quality first, optional tiebreak score second.

    ``score`` overrides the row's stored ``composite`` -- pass the agent's
    ``official_composite`` to rank on the continual mean. Omitted, the row ranks
    on the raw quorum median, which is the correct key only for surfaces that
    are deliberately reporting the *raw* pool (the fold's ``raw_rank``
    provenance field) rather than a standing.

    ``secondary_score`` is considered only after exact equality on ``score``.
    Curve-v3 uses it for the efficiency-adjusted projection, so a lower-quality
    agent can never outrank a higher-quality one merely by spending fewer
    tokens. Omitted, it equals the primary score and preserves the historical
    comparator byte-for-byte in outcome.

    A row with no ``eligible`` attribute is treated as ranked: pools that carry
    the flag use it to sink smoke runs and zero-scoring full runs below every
    real result, and pools that do not carry it have already filtered them out.
    """
    primary = row.composite if score is None else score
    secondary = primary if secondary_score is None else secondary_score
    return (
        not bool(getattr(row, "eligible", True)),
        -primary,
        -secondary,
        row.first_seen,
        str(row.agent_id),
    )


def score_order_terms(
    *,
    eligible: ColumnElement[Any],
    composite: ColumnElement[Any],
    first_seen: ColumnElement[Any],
    agent_id: ColumnElement[Any],
) -> tuple[ColumnElement[Any], ...]:
    """SQL twin of :func:`score_order_key`, for ``ORDER BY`` and window frames.

    The ledger read has to produce this order in the database, so the comparator
    exists twice in two languages -- but only twice, and only here, where the
    two forms sit next to each other and a test can assert they agree.
    """
    return (eligible.desc(), composite.desc(), first_seen.asc(), agent_id.asc())


def rank_submissions(
    rows: Iterable[R],
    *,
    scores: Mapping[UUID, float] | None = None,
    secondary_scores: Mapping[UUID, float] | None = None,
) -> list[R]:
    """Return rows in canonical quality-first order.

    ``secondary_scores`` can reorder only exact primary-score ties.
    """
    values = scores or {}
    secondary = (
        secondary_scores
        if secondary_scores is not None
        else getattr(scores, "secondary_scores", {})
    )
    return sorted(
        rows,
        key=lambda row: score_order_key(
            row,
            score=values.get(row.agent_id),
            secondary_score=secondary.get(row.agent_id),
        ),
    )
