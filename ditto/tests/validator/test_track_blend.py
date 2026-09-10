"""Track blending math + the memory byte-identical regression guard.

The chain accepts one weight vector per validator, so independent tracks are
folded separately and blended by basis-point share. The load-bearing invariant:
a **single fully-allocated memory track reproduces today's ``compute_weights``
vector byte-for-byte**, so registering memory as a track changes no emissions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from ditto.api_models.router_ledger import (
    RouterHarness,
    RouterHarnessResult,
    RouterLedgerEntry,
)
from ditto.api_models.validator import LedgerResponse
from ditto.validator.config import BASIS_POINT_SCALE
from ditto.validator.tracks import (
    TRACK_MEMORY,
    TRACK_ROUTER,
    MemoryFoldParams,
    TrackFoldInputs,
    memory_fold,
    router_fold,
)
from ditto.validator.weights import (
    blend_track_weights,
    compute_router_weights,
    compute_weights,
    resolve_track_shares,
    track_allocated_share,
)

_T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _mem(miner: str, composite: float, *, minutes: int = 0) -> Any:
    """A duck-typed memory ledger entry (mirrors test_dethrone_band._e)."""
    return SimpleNamespace(
        miner_hotkey=miner,
        agent_id=uuid4(),
        composite=composite,
        first_seen=_T0 + timedelta(minutes=minutes),
        sha256="ab" * 32,
    )


def _router(
    miner: str,
    combined: float,
    *,
    minutes: int = 0,
    agent_id: UUID | None = None,
    weight_eligible: bool = True,
) -> RouterLedgerEntry:
    harnesses = tuple(
        RouterHarnessResult(
            harness=harness,
            operational=combined > 0.0,
            floor_pass=combined > 0.0,
            efficiency=min(max(combined, 0.0), 1.0),
            upstream_token_cost_micros=1000,
        )
        for harness in RouterHarness
    )
    return RouterLedgerEntry(
        miner_hotkey=miner,
        agent_id=agent_id or uuid4(),
        router_contract_version=1,
        weight_eligible=weight_eligible,
        combined_score=combined,
        harnesses=harnesses,
        first_seen=_T0 + timedelta(minutes=minutes),
    )


# --- memory byte-identical regression guard ------------------------------------


_MEM_PARAMS = MemoryFoldParams(
    margin=0.01, tail_size=4, rank_shares=(0.6, 0.2, 0.1, 0.06, 0.04)
)


def _compute_memory_directly(entries: list[Any]) -> dict[str, float]:
    return compute_weights(
        entries,
        margin=_MEM_PARAMS.margin,
        tail_size=_MEM_PARAMS.tail_size,
        rank_shares=_MEM_PARAMS.rank_shares,
        dethrone_z=_MEM_PARAMS.dethrone_z,
        tie_pooling=_MEM_PARAMS.tie_pooling,
        ceiling_band_clamp=_MEM_PARAMS.ceiling_band_clamp,
    )


def test_single_memory_track_is_byte_identical_to_compute_weights() -> None:
    entries = [
        _mem("alpha", 0.90, minutes=0),
        _mem("bravo", 0.70, minutes=1),
        _mem("charlie", 0.50, minutes=2),
        _mem("delta", 0.30, minutes=3),
    ]
    expected = _compute_memory_directly(entries)
    assert expected  # sanity: the fold produced a non-empty vector

    folded = memory_fold(
        TrackFoldInputs(memory_entries=entries, memory_params=_MEM_PARAMS)
    )
    blended = blend_track_weights(
        {TRACK_MEMORY: folded}, {TRACK_MEMORY: BASIS_POINT_SCALE}
    )

    assert blended.keys() == expected.keys()
    for hotkey, weight in expected.items():
        # Byte-for-byte: the single fully-allocated track is a passthrough.
        assert blended[hotkey] == weight

    # And the allocated share is exactly 1.0, so miner_share is unchanged.
    assert (
        track_allocated_share({TRACK_MEMORY: BASIS_POINT_SCALE}, {TRACK_MEMORY: folded})
        == 1.0
    )


def test_all_empty_tracks_blend_to_empty() -> None:
    assert (
        blend_track_weights({TRACK_MEMORY: {}}, {TRACK_MEMORY: BASIS_POINT_SCALE}) == {}
    )
    assert (
        track_allocated_share({TRACK_MEMORY: BASIS_POINT_SCALE}, {TRACK_MEMORY: {}})
        == 0.0
    )


# --- multi-track blend math ----------------------------------------------------


def test_two_track_blend_scales_by_bps_and_sums_shared_hotkeys() -> None:
    memory = {"alpha": 0.75, "bravo": 0.25}
    router = {"alpha": 1.0, "gamma": 3.0}  # relative allocation; normalized inside
    shares = {TRACK_MEMORY: 8000, TRACK_ROUTER: 2000}
    blended = blend_track_weights({TRACK_MEMORY: memory, TRACK_ROUTER: router}, shares)
    # memory normalizes to itself (sums to 1); router 1:3 -> 0.25:0.75.
    assert blended["alpha"] == pytest.approx(0.75 * 0.8 + 0.25 * 0.2)
    assert blended["bravo"] == pytest.approx(0.25 * 0.8)
    assert blended["gamma"] == pytest.approx(0.75 * 0.2)
    assert sum(blended.values()) == pytest.approx(1.0)


def test_empty_track_share_becomes_shortfall_to_burn() -> None:
    # Router share is configured but its vector is empty -> allocated < 1.0.
    shares = {TRACK_MEMORY: 8000, TRACK_ROUTER: 2000}
    vectors = {TRACK_MEMORY: {"alpha": 1.0}, TRACK_ROUTER: {}}
    blended = blend_track_weights(vectors, shares)
    # Not a full-pool passthrough (memory is 8000 bps, not 10000): the lone
    # contributing track scales to its share. The cap renormalizes ratios anyway,
    # so the single miner's share among miners is unaffected.
    assert blended == {"alpha": pytest.approx(0.8)}
    # The 2000 router bps did not pay a miner; it burns via allocated < 1.0.
    assert track_allocated_share(shares, vectors) == pytest.approx(0.8)


def test_zero_only_track_burns_its_share_not_leaks() -> None:
    # A {hotkey: 0.0} vector is truthy but pays nobody. It must count as
    # UNallocated (its bps burn) rather than truthy-allocated (its bps would
    # renormalize onto other tracks' miners). blend and allocated_share share the
    # one _has_positive_weight predicate, so both agree it contributes nothing.
    shares = {TRACK_MEMORY: 8000, TRACK_ROUTER: 2000}
    vectors = {TRACK_MEMORY: {"alpha": 1.0}, TRACK_ROUTER: {"zero": 0.0}}
    blended = blend_track_weights(vectors, shares)
    assert blended == {"alpha": pytest.approx(0.8)}
    assert "zero" not in blended
    # Same result as the empty-vector case: the zero-only router share burns.
    assert track_allocated_share(shares, vectors) == pytest.approx(0.8)


# --- router fold ranking -------------------------------------------------------


def test_compute_router_weights_ranks_by_combined_score() -> None:
    entries = [
        _router("low", 0.20, minutes=0),
        _router("high", 0.90, minutes=1),
        _router("mid", 0.50, minutes=2),
    ]
    shares = (0.65, 0.14, 0.10, 0.07, 0.04)
    weights = compute_router_weights(entries, rank_shares=shares)
    assert list(weights) == ["high", "mid", "low"]
    assert weights["high"] == shares[0]
    assert weights["mid"] == shares[1]
    assert weights["low"] == shares[2]


def test_compute_router_weights_drops_nonpositive_and_dedups_hotkey() -> None:
    entries = [
        _router("winner", 0.80, minutes=0),
        _router("winner", 0.90, minutes=5),  # later copy of same hotkey
        _router("zero", 0.0, minutes=1),
    ]
    weights = compute_router_weights(entries, rank_shares=(0.65, 0.14))
    # Earliest first_seen wins the hotkey; zero-scorer excluded entirely.
    assert weights == {"winner": 0.65}


def test_router_fold_matches_compute_router_weights() -> None:
    entries = [_router("a", 0.7, minutes=0), _router("b", 0.4, minutes=1)]
    shares = (0.65, 0.14, 0.10, 0.07, 0.04)
    folded = router_fold(
        TrackFoldInputs(router_entries=entries, router_rank_shares=shares)
    )
    assert folded == compute_router_weights(entries, rank_shares=shares)


def test_compute_router_weights_degrades_on_nonpositive_rank_shares() -> None:
    # On the consensus fold path a malformed share set must never raise (that
    # would kill the whole put_weights fold); it degrades to {} so the router
    # earns nothing and its bps burn. A genuine misconfig is caught at boot by
    # ValidatorConfig.__post_init__, not here.
    assert compute_router_weights([_router("a", 0.5)], rank_shares=(0.6, 0.0)) == {}
    assert (
        compute_router_weights([_router("a", 0.5)], rank_shares=(float("nan"),)) == {}
    )
    assert compute_router_weights([_router("a", 0.5)], rank_shares=()) == {}


def test_compute_router_weights_excludes_not_weight_eligible() -> None:
    # The scorer's per-entry weight_eligible echo is authoritative: an entry the
    # scorer flagged not-payable earns nothing even with a positive score.
    entries = [
        _router("payable", 0.80, minutes=0, weight_eligible=True),
        _router("flagged", 0.95, minutes=1, weight_eligible=False),
    ]
    weights = compute_router_weights(entries, rank_shares=(0.65, 0.14))
    assert weights == {"payable": 0.65}


# --- resolve_track_shares fallback ---------------------------------------------


_DEFAULT_SHARES = {TRACK_MEMORY: BASIS_POINT_SCALE, "coding": 0, TRACK_ROUTER: 0}


def test_resolve_track_shares_uses_default_when_absent() -> None:
    # An older platform omits it; pydantic defaults it to {}, which the resolver
    # reads as "no served split" and folds the compiled default.
    ledger = LedgerResponse(entries=[], count=0)
    assert resolve_track_shares(ledger, default=_DEFAULT_SHARES) == _DEFAULT_SHARES


def test_resolve_track_shares_reads_valid_platform_map() -> None:
    ledger = LedgerResponse(
        entries=[], count=0, track_shares_bps={TRACK_MEMORY: 8000, TRACK_ROUTER: 2000}
    )
    assert resolve_track_shares(ledger, default=_DEFAULT_SHARES) == {
        TRACK_MEMORY: 8000,
        TRACK_ROUTER: 2000,
    }


@pytest.mark.parametrize(
    "bad",
    [
        {TRACK_MEMORY: "8000"},  # non-int bps
        {TRACK_MEMORY: True},  # bool is not an accepted int
        {TRACK_MEMORY: -1},  # below range
        {TRACK_MEMORY: BASIS_POINT_SCALE + 1},  # above range
        {123: 5000},  # non-str id
        {TRACK_MEMORY: 7000, TRACK_ROUTER: 4000},  # sum over scale
    ],
)
def test_resolve_track_shares_falls_back_on_malformed(bad: dict[Any, Any]) -> None:
    # Built by hand rather than through the model, which would already have
    # rejected or coerced these; the resolver is the runtime defense-in-depth.
    ledger = cast("LedgerResponse", SimpleNamespace(track_shares_bps=bad))
    assert resolve_track_shares(ledger, default=_DEFAULT_SHARES) == _DEFAULT_SHARES
