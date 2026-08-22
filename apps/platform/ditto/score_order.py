"""The single comparator that puts submissions in order, in both languages.

This is the leaf of the ranking stack: the public leaderboard's ``rank``, the
validator ledger, the KOTH champion/tail fold, the queue's score floors and the
efficiency lineage dedupe all order rows through :func:`score_order_key`, and
the ledger read produces the same order in SQL through
:func:`score_order_terms`. Those two forms are the only two copies of the rule
that exist, they live next to each other here, and
``ditto/tests/db/queries/test_score_ranking.py`` asserts they agree row for row.

:func:`owner_family_order_terms` / :func:`owner_family_key` are the one
deliberate variation, and they are not a third copy of the rule: they order
one owner's own generations against each other, and they differ from the
canonical comparator in exactly one term (newest upload, not earliest lineage
arrival). The between-owner order still uses the lineage clock so a later
tarball that only matches or improves the owner's best score by less than the
dethrone margin does not lose standing.

It carries no ditto imports on purpose. :mod:`ditto.api_server.koth` mirrors the
subnet's frozen consensus fold and must be able to read the comparator without
pulling the database layer in behind it; :mod:`ditto.db.queries.score_ranking`
re-exports everything here beside the score the comparator ranks on.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
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
OwnerFamilyKey = tuple[bool, float, float, timedelta, str]

# Horizon for inverting upload time inside :func:`owner_family_key`. A later
# tarball must sort first without a second comparator; this is far beyond any
# submission timestamp the board will ever see.
_RANK_HORIZON = datetime(9999, 12, 31, 23, 59, 59)


def ranking_first_seen(row: RankableRow) -> datetime:
    """The timestamp the canonical rank comparator orders on.

    A row that carries a lineage crown (``fold_first_seen``) ranks by that
    clock so a miner keeps seniority across a marginal resubmission. Rows that
    do not -- ``KothEntry`` after the fold has already substituted the crown,
    fixtures, anti-copy reads -- rank by the tarball's upload time.
    """
    fold = getattr(row, "fold_first_seen", None)
    return fold if isinstance(fold, datetime) else row.first_seen


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

    Remaining ties break on the lineage arrival (:func:`ranking_first_seen`),
    not the winning tarball's upload time, then ``agent_id``. A miner who
    iterates at a plateau or improves by less than the dethrone margin keeps
    the earlier clock; a jump larger than that margin resets the clock and
    stands behind anyone who already held the new score.

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
        ranking_first_seen(row),
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


def owner_family_order_terms(
    *,
    eligible: ColumnElement[Any],
    composite: ColumnElement[Any],
    first_seen: ColumnElement[Any],
    agent_id: ColumnElement[Any],
) -> tuple[ColumnElement[Any], ...]:
    """:func:`score_order_terms`, but newest-first on an exact score tie.

    Used only to choose which of ONE owner's generations represents it. Between
    owners, the earliest *lineage* arrival still wins a tie
    (:func:`score_order_terms` on ``crown_first_seen``) -- that is the seniority
    rule the whole fold rests on, and flipping it there would reorder the board
    for every miner sitting at the same score.

    Inside a family there is no seniority to protect, because
    ``LedgerRow.crown_first_seen`` already carries the lineage's arrival time
    across whichever generation is chosen. So the tie can be spent on something
    useful instead: a saturated benchmark reports a miner's improvements as
    *equal* composites, and picking the earliest one left the owner represented
    by an older agent it had already moved past, with no way to be seen. Newest
    wins the tie; a strictly better score still wins outright, so submitting
    something worse can never cost an owner its standing.
    """
    return (eligible.desc(), composite.desc(), first_seen.desc(), agent_id.asc())


def owner_family_key(
    row: RankableRow,
    *,
    score: float | None = None,
    secondary_score: float | None = None,
) -> OwnerFamilyKey:
    """Python twin of :func:`owner_family_order_terms`: newest upload on a tie.

    Used only to choose which of one owner's generations is *shown*. Between
    owners, :func:`score_order_key` still breaks ties on the lineage clock.
    """
    primary = row.composite if score is None else score
    secondary = primary if secondary_score is None else secondary_score
    return (
        not bool(getattr(row, "eligible", True)),
        -primary,
        -secondary,
        _RANK_HORIZON.replace(tzinfo=row.first_seen.tzinfo) - row.first_seen,
        str(row.agent_id),
    )


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


def select_owner_representative(
    rows: Iterable[R],
    *,
    scores: Mapping[UUID, float] | None = None,
    secondary_scores: Mapping[UUID, float] | None = None,
) -> list[R]:
    """Return one owner's generations, best score first, newest upload on a tie.

    A strictly worse score never wins, so experimenting cannot cost standing.
    An exact quality (and optional efficiency) tie shows the newest generation;
    :func:`rank_submissions` then orders that winner against other owners on
    the lineage clock.
    """
    values = scores or {}
    secondary = (
        secondary_scores
        if secondary_scores is not None
        else getattr(scores, "secondary_scores", {})
    )
    return sorted(
        rows,
        key=lambda row: owner_family_key(
            row,
            score=values.get(row.agent_id),
            secondary_score=secondary.get(row.agent_id),
        ),
    )
