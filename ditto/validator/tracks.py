"""Scalable, retirable competition-track registry for the SN118 weight fold.

The subnet is splitting from a single competition into several independent ones
(memory, coding, router — with room for more). The chain accepts exactly **one**
weight vector per validator, so "independent tracks" is realized by folding each
track separately and blending the results by per-track emission share into one
``put_weights`` (see :func:`ditto.validator.weights.blend_track_weights`).

This module owns only the *static* description of the tracks — their id, state,
basis-point share, weight eligibility, and pure fold function — plus the
validation that keeps the split coherent (eligible shares sum to at most
``BASIS_POINT_SCALE``). Per-epoch data (the memory ledger, the published router
ledger) is threaded through :class:`TrackFoldInputs` at fold time, so a track's
``fold`` stays a pure function and adding a track is a registration, not an edit
to the fold pipeline.

Lifecycle (mirrors the ``RolloutMode`` ladder in ``internal/scoregates`` and the
``StrEnum`` style of ``coding_failure.CodingFailureStage``):

- ``SHADOW`` — folded and logged, contributes **0 bps** regardless of configured
  share (used to observe a new track before it touches emissions).
- ``ACTIVE`` — pays its configured share.
- ``RETIRING`` — still pays (draining) but takes no new work.
- ``RETIRED`` — contributes **0 bps**; kept for provenance.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from ditto.validator.config import BASIS_POINT_SCALE
from ditto.validator.weights import compute_router_weights, compute_weights

if TYPE_CHECKING:
    from ditto.api_models.router_ledger import RouterLedgerEntry
    from ditto.api_models.validator import LedgerEntry

# ``BASIS_POINT_SCALE`` (10000) lives in ``config`` as the single source shared
# with ``weights.blend_track_weights``; re-exported here for the registry API and
# to match the Go scorer's ``scoregates.BasisPointScale``. Keeping the split in
# integer bps (not floats) means every validator folds identical byte-for-byte
# shares with no rounding disagreement.

TRACK_MEMORY = "memory"
TRACK_CODING = "coding"
TRACK_ROUTER = "router"


class TrackState(StrEnum):
    SHADOW = "shadow"
    ACTIVE = "active"
    RETIRING = "retiring"
    RETIRED = "retired"


# States that draw emissions when also ``weight_eligible``. RETIRING still pays
# while it drains; SHADOW/RETIRED never do.
_PAYING_STATES = frozenset({TrackState.ACTIVE, TrackState.RETIRING})


@dataclass(frozen=True)
class MemoryFoldParams:
    """The exact ``compute_weights`` arguments the memory track folds with.

    Carried per epoch (resolved from the ledger + config) so the memory fold
    stays a pure function of :class:`TrackFoldInputs`.
    """

    margin: float
    tail_size: int
    rank_shares: tuple[float, ...]
    dethrone_z: float = 0.0
    tie_pooling: bool = False
    ceiling_band_clamp: bool = False


@dataclass(frozen=True)
class TrackFoldInputs:
    """Per-epoch data threaded to every track's fold.

    A track reads only the fields it needs; empty inputs fold to ``{}`` (which
    contributes zero emissions), which is exactly the shadow / no-data case.
    """

    memory_entries: Sequence[LedgerEntry] = ()
    memory_params: MemoryFoldParams | None = None
    router_entries: Sequence[RouterLedgerEntry] = ()
    router_rank_shares: tuple[float, ...] = ()


TrackFold = Callable[[TrackFoldInputs], dict[str, float]]


def memory_fold(inputs: TrackFoldInputs) -> dict[str, float]:
    """Fold the memory track via the **current** ``compute_weights`` unchanged.

    A single-track memory registry must reproduce today's weight vector
    byte-for-byte — this closure is the regression guard.
    """
    params = inputs.memory_params
    if params is None:
        return {}
    return compute_weights(
        inputs.memory_entries,
        margin=params.margin,
        tail_size=params.tail_size,
        rank_shares=params.rank_shares,
        dethrone_z=params.dethrone_z,
        tie_pooling=params.tie_pooling,
        ceiling_band_clamp=params.ceiling_band_clamp,
    )


def router_fold(inputs: TrackFoldInputs) -> dict[str, float]:
    """Fold the router track: rank floor-clearing miners by combined score."""
    return compute_router_weights(
        inputs.router_entries, rank_shares=inputs.router_rank_shares
    )


def reserved_fold(_inputs: TrackFoldInputs) -> dict[str, float]:
    """A reserved slot that another owner fills later (e.g. the coding track)."""
    return {}


@dataclass(frozen=True)
class Track:
    """Static description of one competition track."""

    track_id: str
    name: str
    state: TrackState
    emission_share_bps: int
    weight_eligible: bool
    fold: TrackFold = field(repr=False, default=reserved_fold)

    def __post_init__(self) -> None:
        if not self.track_id:
            raise ValueError("track_id must be non-empty")
        if not isinstance(self.state, TrackState):
            raise ValueError("track state must be a TrackState")
        if not 0 <= self.emission_share_bps <= BASIS_POINT_SCALE:
            raise ValueError(
                f"track {self.track_id!r} emission_share_bps out of range: "
                f"{self.emission_share_bps}"
            )

    def draws_emission(self) -> bool:
        """Whether this track pays this epoch (paying state and eligible)."""
        return self.weight_eligible and self.state in _PAYING_STATES

    def effective_bps(self) -> int:
        """Configured bps if paying, else 0 (shadow/retired never pay)."""
        return self.emission_share_bps if self.draws_emission() else 0


@dataclass(frozen=True)
class TrackRegistry:
    """Ordered set of tracks with a coherent, capped emission split."""

    tracks: tuple[Track, ...]

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for track in self.tracks:
            if track.track_id in seen:
                raise ValueError(f"duplicate track id: {track.track_id!r}")
            seen.add(track.track_id)
        total = sum(track.effective_bps() for track in self.tracks)
        if total > BASIS_POINT_SCALE:
            raise ValueError(
                f"eligible track emission shares exceed {BASIS_POINT_SCALE} bps: "
                f"{total}"
            )

    def get(self, track_id: str) -> Track | None:
        for track in self.tracks:
            if track.track_id == track_id:
                return track
        return None

    def active_eligible(self) -> tuple[Track, ...]:
        """Tracks that pay this epoch, in registration order."""
        return tuple(track for track in self.tracks if track.draws_emission())

    def total_active_bps(self) -> int:
        return sum(track.effective_bps() for track in self.tracks)

    def shares_bps(self) -> dict[str, int]:
        """``{track_id: effective_bps}`` for the paying tracks."""
        return {
            track.track_id: track.effective_bps() for track in self.active_eligible()
        }


def build_default_registry(
    *,
    track_shares_bps: Mapping[str, int],
    router_state: TrackState = TrackState.SHADOW,
    router_weight_eligible: bool = False,
    coding_state: TrackState = TrackState.SHADOW,
    coding_weight_eligible: bool = False,
) -> TrackRegistry:
    """The shipped three-track registry.

    Memory is ``ACTIVE`` and eligible (the existing competition); coding and
    router default to ``SHADOW`` / not-eligible so v1 touches no emissions. The
    per-track bps come from the resolved ``track_shares_bps`` (compiled fallback
    or, later, the platform governance field).
    """
    return TrackRegistry(
        (
            Track(
                track_id=TRACK_MEMORY,
                name="Memory",
                state=TrackState.ACTIVE,
                emission_share_bps=track_shares_bps.get(
                    TRACK_MEMORY, BASIS_POINT_SCALE
                ),
                weight_eligible=True,
                fold=memory_fold,
            ),
            Track(
                track_id=TRACK_CODING,
                name="Coding",
                state=coding_state,
                emission_share_bps=track_shares_bps.get(TRACK_CODING, 0),
                weight_eligible=coding_weight_eligible,
                fold=reserved_fold,
            ),
            Track(
                track_id=TRACK_ROUTER,
                name="Router",
                state=router_state,
                emission_share_bps=track_shares_bps.get(TRACK_ROUTER, 0),
                weight_eligible=router_weight_eligible,
                fold=router_fold,
            ),
        )
    )


__all__ = [
    "BASIS_POINT_SCALE",
    "TRACK_CODING",
    "TRACK_MEMORY",
    "TRACK_ROUTER",
    "MemoryFoldParams",
    "Track",
    "TrackFoldInputs",
    "TrackRegistry",
    "TrackState",
    "build_default_registry",
    "memory_fold",
    "reserved_fold",
    "router_fold",
]
