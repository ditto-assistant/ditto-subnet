"""Track registry: lifecycle, eligible-share validation, and the shipped split."""

from __future__ import annotations

import pytest

from ditto.validator.config import BASIS_POINT_SCALE
from ditto.validator.tracks import (
    TRACK_CODING,
    TRACK_MEMORY,
    TRACK_ROUTER,
    Track,
    TrackFoldInputs,
    TrackRegistry,
    TrackState,
    build_default_registry,
    memory_fold,
    reserved_fold,
    router_fold,
)


def _track(
    track_id: str,
    state: TrackState,
    bps: int,
    *,
    eligible: bool,
) -> Track:
    return Track(
        track_id=track_id,
        name=track_id.title(),
        state=state,
        emission_share_bps=bps,
        weight_eligible=eligible,
    )


def test_shadow_and_retired_never_pay_regardless_of_configured_bps() -> None:
    shadow = _track("s", TrackState.SHADOW, BASIS_POINT_SCALE, eligible=True)
    retired = _track("r", TrackState.RETIRED, BASIS_POINT_SCALE, eligible=True)
    assert shadow.draws_emission() is False
    assert shadow.effective_bps() == 0
    assert retired.draws_emission() is False
    assert retired.effective_bps() == 0


def test_active_and_retiring_pay_when_eligible_but_not_when_ineligible() -> None:
    active = _track("a", TrackState.ACTIVE, 6000, eligible=True)
    retiring = _track("d", TrackState.RETIRING, 4000, eligible=True)
    ineligible = _track("i", TrackState.ACTIVE, 6000, eligible=False)
    assert active.draws_emission() and active.effective_bps() == 6000
    assert retiring.draws_emission() and retiring.effective_bps() == 4000
    assert ineligible.draws_emission() is False
    assert ineligible.effective_bps() == 0


def test_track_rejects_out_of_range_bps() -> None:
    with pytest.raises(ValueError, match="emission_share_bps out of range"):
        _track("x", TrackState.ACTIVE, BASIS_POINT_SCALE + 1, eligible=True)
    with pytest.raises(ValueError, match="emission_share_bps out of range"):
        _track("x", TrackState.ACTIVE, -1, eligible=True)


def test_registry_rejects_duplicate_ids() -> None:
    with pytest.raises(ValueError, match="duplicate track id"):
        TrackRegistry(
            (
                _track("dup", TrackState.ACTIVE, 5000, eligible=True),
                _track("dup", TrackState.SHADOW, 0, eligible=False),
            )
        )


def test_registry_rejects_eligible_shares_over_scale() -> None:
    with pytest.raises(ValueError, match="exceed"):
        TrackRegistry(
            (
                _track("a", TrackState.ACTIVE, 7000, eligible=True),
                _track("b", TrackState.ACTIVE, 4000, eligible=True),
            )
        )


def test_registry_ignores_shadow_share_in_the_cap() -> None:
    # A shadow track at full bps must not count toward the eligible sum.
    registry = TrackRegistry(
        (
            _track("a", TrackState.ACTIVE, BASIS_POINT_SCALE, eligible=True),
            _track("shadow", TrackState.SHADOW, BASIS_POINT_SCALE, eligible=False),
        )
    )
    assert registry.total_active_bps() == BASIS_POINT_SCALE
    assert registry.shares_bps() == {"a": BASIS_POINT_SCALE}
    assert [t.track_id for t in registry.active_eligible()] == ["a"]


def test_default_registry_is_memory_only_and_byte_share_stable() -> None:
    registry = build_default_registry(
        track_shares_bps={
            TRACK_MEMORY: BASIS_POINT_SCALE,
            TRACK_CODING: 0,
            TRACK_ROUTER: 0,
        }
    )
    memory = registry.get(TRACK_MEMORY)
    coding = registry.get(TRACK_CODING)
    router = registry.get(TRACK_ROUTER)
    assert memory is not None and coding is not None and router is not None

    # Memory is the only paying track in v1; it carries the full pool.
    assert memory.state is TrackState.ACTIVE
    assert memory.weight_eligible is True
    assert memory.fold is memory_fold
    assert registry.shares_bps() == {TRACK_MEMORY: BASIS_POINT_SCALE}

    # Coding is a reserved slot; router is shadow — neither touches emissions.
    assert coding.state is TrackState.SHADOW and coding.effective_bps() == 0
    assert coding.fold is reserved_fold
    assert router.state is TrackState.SHADOW and router.effective_bps() == 0
    assert router.fold is router_fold


def test_default_registry_router_promotion_shape() -> None:
    # Promotion flips router to ACTIVE + eligible with a real share; the split
    # must still satisfy the eligible-sum cap.
    registry = build_default_registry(
        track_shares_bps={
            TRACK_MEMORY: 8000,
            TRACK_CODING: 0,
            TRACK_ROUTER: 2000,
        },
        router_state=TrackState.ACTIVE,
        router_weight_eligible=True,
    )
    assert registry.shares_bps() == {TRACK_MEMORY: 8000, TRACK_ROUTER: 2000}


def test_default_registry_degrades_on_over_cap_partial_map() -> None:
    # A well-formed PARTIAL governance map: router is promoted eligible but memory
    # is left at the default full pool, so the eligible sum (10000 + 2000) blows
    # past the cap. build_default_registry must NOT let that raise escape onto the
    # put_weights fold path; it degrades to the memory-only safe split so the fold
    # keeps producing today's memory vector rather than crashing.
    registry = build_default_registry(
        track_shares_bps={
            TRACK_ROUTER: 2000
        },  # memory omitted -> defaults to full pool
        router_state=TrackState.ACTIVE,
        router_weight_eligible=True,
    )
    # Safe split: memory owns the whole eligible pool; router is left eligible
    # (the promotion flags are honored) but earns 0 bps, so it pays nothing.
    assert registry.total_active_bps() == BASIS_POINT_SCALE
    memory = registry.get(TRACK_MEMORY)
    assert memory is not None and memory.effective_bps() == BASIS_POINT_SCALE
    router = registry.get(TRACK_ROUTER)
    assert router is not None and router.effective_bps() == 0
    assert memory.weight_eligible is True


def test_reserved_and_folds_are_pure_on_empty_inputs() -> None:
    empty = TrackFoldInputs()
    assert reserved_fold(empty) == {}
    assert memory_fold(empty) == {}  # no memory_params -> no vector
    assert router_fold(empty) == {}  # no router_entries + no rank_shares -> {}
