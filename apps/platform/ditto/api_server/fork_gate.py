"""Fork must beat its base (SN-302): pure detection and the paired duel.

Building on a publicly released agent is allowed on SN118. Since 2026-11-01 a
fork of **another owner's** released agent earns nothing until it beats that
base head-to-head on at least ``min_shared_seeds`` shared confirmation seeds,
through the same paired test the crown gate uses
(:func:`ditto.api_server.koth._dethrone_decision` with a
:class:`~ditto.api_server.koth.CrownGate`). The motivating case was "beast" v1
(``c0a0d917``): uploaded eight minutes after dan0l v29's release with a
one-sentence prompt delta, and payable as if it were its own work.

Detection reads two existing signals and picks the base with the most shared
distinctive content:

* the distinctive-content index (:mod:`ditto.api_server.distinctive_content`,
  #3303): every other-owner agent **publicly released before the candidate's
  upload** is scored on the items it shares with the candidate, after removing
  the packaged public corpus (pre-removed by the index), idioms common to
  :data:`~ditto.api_server.unreleased_copy.COMMON_OWNER_FLOOR` or more owners,
  and items the candidate's owner already held before that base existed. A
  pair at or above :data:`FORK_BASE_THRESHOLD` (the copy signal's calibrated
  threshold: every ruled copy scored >= 119, every independent agent <= 11.7
  against a released base) is a fork;
* the copy court's released-base clear basis (``matched_agent_public``,
  ``released_base_explains_overlap``, ``unmodified_fork_of_released_base``):
  an other-owner base the court cited is a fork base whatever its score.

Same-owner successors are exempt: owners are the unreleased-copy signal's
canonical owners (hotkey, payment coldkey, attested hotkey links).

Everything here is pure; storage is :mod:`ditto.db.queries.fork_bases`.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from ditto.api_server.koth import (
    CrownGate,
    KothEntry,
    _dethrone_decision,
    shared_seed_count,
)
from ditto.api_server.unreleased_copy import (
    COMMON_OWNER_FLOOR,
    DEFAULT_THRESHOLD,
    IndexedAgent,
    pair_score,
)

FORK_DETECTOR_VERSION = "fb1"
FORK_BASE_THRESHOLD = DEFAULT_THRESHOLD
DEFAULT_FORK_MIN_SHARED_SEEDS = 3

COURT_FORK_BASES: frozenset[str] = frozenset(
    {
        "matched_agent_public",
        "released_base_explains_overlap",
        "unmodified_fork_of_released_base",
    }
)
"""Copy-court clear bases that name a publicly released other-owner base."""

ForkBasis = Literal["distinctive", "court", "distinctive+court"]
ForkState = Literal["beats_base", "unproven"]


@dataclass(frozen=True)
class ForkBase:
    """The released other-owner agent a candidate is a fork of."""

    base_agent_id: UUID
    score: float
    shared_count: int
    basis: ForkBasis


def detect_released_base(
    candidate: IndexedAgent,
    others: Sequence[IndexedAgent],
    *,
    court_bases: Collection[UUID] = (),
    threshold: float = FORK_BASE_THRESHOLD,
    common_owner_floor: int = COMMON_OWNER_FLOOR,
) -> ForkBase | None:
    """The released base with the largest shared distinctive content, if any.

    ``others`` carry only their overlap with the candidate, exactly as
    :func:`ditto.db.queries.unreleased_copy.load_context` builds them.
    """
    t = candidate.uploaded_at

    def before(a: IndexedAgent, b: IndexedAgent) -> bool:
        return (a.uploaded_at, a.agent_id.int) < (b.uploaded_at, b.agent_id.int)

    earlier = [
        o for o in others if o.agent_id != candidate.agent_id and before(o, candidate)
    ]
    owners_by_key: dict[int, set[str]] = {}
    for other in earlier:
        if other.owner != candidate.owner:
            for key in other.keys & candidate.keys:
                owners_by_key.setdefault(key, set()).add(other.owner)
    common = {k for k, o in owners_by_key.items() if len(o) >= common_owner_floor}
    own_history = [o for o in earlier if o.owner == candidate.owner]
    cited = set(court_bases)
    found: list[tuple[float, float, str, ForkBase]] = []
    for base in earlier:
        if base.owner == candidate.owner:
            continue
        released_before = base.released_at is not None and base.released_at < t
        by_court = base.agent_id in cited
        if not released_before and not by_court:
            continue
        shared = (candidate.keys & base.keys) - common
        for own in own_history:
            if shared and before(own, base):
                shared = shared - own.keys
        score = pair_score(shared)
        by_content = released_before and score >= threshold
        if not by_content and not by_court:
            continue
        basis: ForkBasis = (
            "distinctive+court"
            if by_content and by_court
            else "distinctive"
            if by_content
            else "court"
        )
        # Most shared content wins; the earlier upload, then the UUID, break
        # ties so every read picks the same base.
        found.append(
            (
                -score,
                base.uploaded_at.timestamp(),
                str(base.agent_id),
                ForkBase(
                    base_agent_id=base.agent_id,
                    score=score,
                    shared_count=len(shared),
                    basis=basis,
                ),
            )
        )
    return min(found, key=lambda item: item[:3])[3] if found else None


@dataclass(frozen=True)
class ForkDuel:
    """A fork's head-to-head against its base on their shared seeds."""

    base_agent_id: UUID
    shared_seeds: int
    required_seeds: int
    paired_lead: float | None
    required_lead: float | None
    state: ForkState

    @property
    def lead_needed(self) -> float | None:
        if self.paired_lead is None or self.required_lead is None:
            return None
        return max(0.0, self.required_lead - self.paired_lead)


def fork_duel(
    fork: KothEntry,
    base: KothEntry | None,
    *,
    base_agent_id: UUID,
    min_shared_seeds: int = DEFAULT_FORK_MIN_SHARED_SEEDS,
    ceiling_band_clamp: bool = False,
    statistical_band_cap: bool = False,
) -> ForkDuel:
    """Whether ``fork`` beats ``base`` under the crown gate's paired test.

    A base with no confirmation evidence at this benchmark version cannot be
    beaten yet: the duel reads zero shared seeds and stays ``unproven``.
    """
    if base is None:
        return ForkDuel(
            base_agent_id=base_agent_id,
            shared_seeds=0,
            required_seeds=min_shared_seeds,
            paired_lead=None,
            required_lead=None,
            state="unproven",
        )
    decision = _dethrone_decision(
        fork,
        base,
        ceiling_band_clamp=ceiling_band_clamp,
        statistical_band_cap=statistical_band_cap,
        crown_gate=CrownGate(min_shared_seeds=min_shared_seeds, standing_rank=1),
    )
    paired = decision.method == "paired"
    return ForkDuel(
        base_agent_id=base_agent_id,
        shared_seeds=shared_seed_count(fork, base),
        required_seeds=min_shared_seeds,
        paired_lead=decision.challenger_lead if paired else None,
        required_lead=decision.required_lead if paired else None,
        state="beats_base" if decision.dethrones else "unproven",
    )


__all__ = [
    "COURT_FORK_BASES",
    "DEFAULT_FORK_MIN_SHARED_SEEDS",
    "FORK_BASE_THRESHOLD",
    "FORK_DETECTOR_VERSION",
    "ForkBase",
    "ForkDuel",
    "detect_released_base",
    "fork_duel",
]
