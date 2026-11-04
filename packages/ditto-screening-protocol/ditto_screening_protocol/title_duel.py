"""Sequential paired title duel: the shared arithmetic of the protocol-35 fold.

``docs/title-duels.md`` (phase B). Under a served title duel the KOTH dethrone
test stops being a single fixed-z look at one season's shared seeds. It
accumulates paired evidence across dueling seasons instead:

* a **pair** is one ``(season, seed)`` both agents scored inside that season,
  each side's composite recomputed with both owners masked. Season isolation
  keeps score *levels* apart, because generator sets differ; it does not stop
  accumulating within-season *differences*, which are comparable;
* the **window** is the newest :data:`TITLE_DUEL_WINDOW_PAIRS` pairs, ordered
  ``(season_index desc, season_id, seed)``: five 3-seed seasons, equal to the
  validator's compiled confirmation ceiling;
* the challenger dethrones only when ``mean_d`` strictly exceeds
  ``max(margin_floor, c(n) * se_d)``, where ``c(n)`` is the frozen
  :data:`TITLE_DUEL_BOUNDARY`. ``c(n)`` is the Student-t quantile at a constant
  per-look level of 0.01 (Pocock-style), which covers a fold that looks after
  every new pair and a variance estimated from a handful of seeds. The
  statistical term is deliberately neither capped nor decayed.

Both folds (``ditto/validator/weights.py`` and Platform's ``koth.py``) build
their per-pair seed scores their own way and then call these functions, so the
consensus arithmetic exists once.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

TITLE_DUEL_METHOD = "sequential_paired_v1"
TITLE_DUEL_WINDOW_PAIRS = 15

TITLE_DUEL_BOUNDARY: Mapping[int, float] = MappingProxyType(
    {
        3: 6.965,
        4: 4.541,
        5: 3.747,
        6: 3.365,
        7: 3.143,
        8: 2.998,
        9: 2.896,
        10: 2.821,
        11: 2.764,
        12: 2.718,
        13: 2.681,
        14: 2.650,
        15: 2.624,
    }
)
"""``c(n)`` for ``n`` paired differences: ``t_{n-1}^{-1}(0.99)`` to three
decimals. No entry below three pairs: two pairs cannot dethrone."""


@dataclass(frozen=True)
class DuelPair:
    """One shared ``(season, seed)`` with both sides' seed scores."""

    season_index: int
    season_id: str
    seed: int
    challenger: float
    champion: float

    @property
    def difference(self) -> float:
        return self.challenger - self.champion


@dataclass(frozen=True)
class DuelStatistic:
    """The windowed paired statistic the dethrone test reads."""

    pairs: tuple[DuelPair, ...]
    mean_difference: float
    champion_reference: float
    standard_error: float

    @property
    def count(self) -> int:
        return len(self.pairs)


def season_seed_key(
    season_id: str, season_index: int, seed: int
) -> tuple[str, int, int]:
    """The grouping key of one dueling confirmation record."""
    return (season_id, int(season_index), int(seed))


def collapse_records(
    values: Iterable[tuple[tuple[str, int, int], float]],
) -> dict[tuple[str, int], tuple[int, float]]:
    """Group ``((season_id, season_index, seed), value)`` to one median per pair.

    Keyed ``(season_id, seed)`` -> ``(season_index, median)``. A pair whose
    records disagree on the season index is dropped (consensus-safe: never a
    guess between two orderings).
    """
    grouped: dict[tuple[str, int], list[float]] = {}
    indices: dict[tuple[str, int], set[int]] = {}
    for (season_id, season_index, seed), value in values:
        key = (season_id, seed)
        grouped.setdefault(key, []).append(float(value))
        indices.setdefault(key, set()).add(season_index)
    out: dict[tuple[str, int], tuple[int, float]] = {}
    for key, samples in grouped.items():
        if len(indices[key]) != 1:
            continue
        ordered = sorted(samples)
        middle = len(ordered) // 2
        median = (
            ordered[middle]
            if len(ordered) % 2
            else (ordered[middle - 1] + ordered[middle]) / 2.0
        )
        out[key] = (next(iter(indices[key])), median)
    return out


def duel_pairs(
    challenger: Mapping[tuple[str, int], tuple[int, float]],
    champion: Mapping[tuple[str, int], tuple[int, float]],
    *,
    window: int = TITLE_DUEL_WINDOW_PAIRS,
) -> tuple[DuelPair, ...]:
    """The newest ``window`` shared pairs, ``(season_index desc, id, seed)``."""
    pairs = [
        DuelPair(
            season_index=challenger[key][0],
            season_id=key[0],
            seed=key[1],
            challenger=challenger[key][1],
            champion=champion[key][1],
        )
        for key in challenger.keys() & champion.keys()
        if challenger[key][0] == champion[key][0]
    ]
    pairs.sort(key=lambda pair: (-pair.season_index, pair.season_id, pair.seed))
    return tuple(pairs[: max(0, int(window))])


def duel_statistic(pairs: Sequence[DuelPair]) -> DuelStatistic | None:
    """Mean, champion reference and standard error over at least two pairs."""
    if len(pairs) < 2:
        return None
    n = len(pairs)
    differences = [pair.difference for pair in pairs]
    mean = sum(differences) / n
    reference = sum(pair.champion for pair in pairs) / n
    variance = sum((d - mean) ** 2 for d in differences) / (n - 1)
    return DuelStatistic(
        pairs=tuple(pairs),
        mean_difference=mean,
        champion_reference=reference,
        standard_error=math.sqrt(variance / n),
    )


def duel_required_lead(
    statistic: DuelStatistic, *, margin_floor: float
) -> float | None:
    """``max(margin_floor, c(n) * se)``, or ``None`` when ``n`` has no boundary."""
    critical = TITLE_DUEL_BOUNDARY.get(statistic.count)
    if critical is None:
        return None
    return max(margin_floor, critical * statistic.standard_error)
