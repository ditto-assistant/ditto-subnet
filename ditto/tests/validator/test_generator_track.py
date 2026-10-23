"""Generator-competition track: shadow registration, fold, and ledger read."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from ditto.api_models.generator_ledger import (
    GeneratorLedgerEntry,
    GeneratorLedgerResponse,
)
from ditto.validator.config import BASIS_POINT_SCALE, GENERATOR_RANK_SHARES
from ditto.validator.tracks import (
    TRACK_GENERATOR,
    TRACK_MEMORY,
    TrackFoldInputs,
    TrackState,
    build_default_registry,
    generator_fold,
)
from ditto.validator.weights import compute_generator_weights
from ditto.validator.worker import PlatformGeneratorLedgerSource

_T0 = datetime(2026, 10, 1, tzinfo=UTC)


def _entry(
    hotkey: str,
    score: float,
    *,
    eligible: bool = True,
    seen: int = 0,
    run_id: UUID | None = None,
) -> GeneratorLedgerEntry:
    return GeneratorLedgerEntry(
        miner_hotkey=hotkey,
        submission_id=uuid4(),
        run_id=run_id or uuid4(),
        weight_eligible=eligible,
        score=score,
        first_seen=_T0 + timedelta(hours=seen),
        scored_at=_T0 + timedelta(hours=seen + 1),
    )


def test_generator_track_ships_shadow_and_pays_nothing() -> None:
    registry = build_default_registry(
        track_shares_bps={TRACK_MEMORY: BASIS_POINT_SCALE, TRACK_GENERATOR: 2000}
    )
    track = registry.get(TRACK_GENERATOR)
    assert track is not None
    assert track.state is TrackState.SHADOW and not track.weight_eligible
    assert track.effective_bps() == 0
    assert registry.shares_bps() == {TRACK_MEMORY: BASIS_POINT_SCALE}


def test_generator_promotion_pays_its_share() -> None:
    registry = build_default_registry(
        track_shares_bps={TRACK_MEMORY: 8000, TRACK_GENERATOR: 2000},
        generator_state=TrackState.ACTIVE,
        generator_weight_eligible=True,
    )
    assert registry.shares_bps() == {TRACK_MEMORY: 8000, TRACK_GENERATOR: 2000}


def test_fold_ranks_by_score_with_deterministic_ties() -> None:
    early, late = uuid4(), uuid4()
    entries = [
        _entry("hk-c", 0.10),
        _entry("hk-a", 0.30, seen=5, run_id=late),
        _entry("hk-b", 0.30, seen=1, run_id=early),
        _entry("hk-ineligible", 0.99, eligible=False),
        _entry("hk-zero", 0.0),
    ]
    weights = compute_generator_weights(entries, rank_shares=(3.0, 2.0, 1.0))
    assert weights == {"hk-b": 3.0, "hk-a": 2.0, "hk-c": 1.0}
    assert (
        generator_fold(
            TrackFoldInputs(
                generator_entries=entries, generator_rank_shares=(3.0, 2.0, 1.0)
            )
        )
        == weights
    )


def test_fold_keeps_one_slot_per_hotkey() -> None:
    entries = [_entry("hk-a", 0.5), _entry("hk-a", 0.4), _entry("hk-b", 0.3)]
    assert compute_generator_weights(entries, rank_shares=(2.0, 1.0)) == {
        "hk-a": 2.0,
        "hk-b": 1.0,
    }


@pytest.mark.parametrize("shares", [(), (1.0, 0.0), (float("nan"),), (-1.0,)])
def test_malformed_rank_shares_degrade_to_empty(shares: tuple[float, ...]) -> None:
    assert compute_generator_weights([_entry("hk", 0.5)], rank_shares=shares) == {}


def test_shipped_rank_shares_are_positive_and_flatter_than_the_crown() -> None:
    assert all(share > 0 for share in GENERATOR_RANK_SHARES)
    assert GENERATOR_RANK_SHARES[0] < 0.5


async def test_ledger_source_fails_closed() -> None:
    async def _boom() -> GeneratorLedgerResponse:
        raise RuntimeError("platform down")

    ledger = await PlatformGeneratorLedgerSource(read=_boom).fetch()
    assert ledger.entries == []


def test_ledger_ignores_unknown_fields() -> None:
    ledger = GeneratorLedgerResponse.model_validate(
        {"entries": [], "count": 0, "future_field": {"x": 1}}
    )
    assert ledger.count == 0
