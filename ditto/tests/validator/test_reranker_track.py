"""Season 2 reranker track (SN-209): hard shadow, read-and-log only.

The reranker track is registered SHADOW and not weight-eligible with no
configuration, environment variable or served share able to change that. These
tests prove the weight vector is byte-identical whether the reranker ledger is
absent, present, or (defensively) served with weight-bearing values, and that
a served non-zero ``reranker`` share can never shrink the memory track.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from ditto.api_models.reranker_ledger import (
    RerankerLedgerChampion,
    RerankerLedgerEntry,
    RerankerLedgerResponse,
)
from ditto.api_models.validator import LedgerResponse
from ditto.tests.validator.test_config import _base_env
from ditto.tests.validator.test_provisional_incumbent import _worker
from ditto.tests.validator.test_worker import _BURN_HOTKEY, _T0, _config, _entry
from ditto.validator.config import (
    BASIS_POINT_SCALE,
    TRACK_SHARES_BPS,
    parse_validator_config_from_env,
)
from ditto.validator.tracks import (
    TRACK_MEMORY,
    TRACK_RERANKER,
    TrackFoldInputs,
    TrackState,
    build_default_registry,
    reranker_fold,
)
from ditto.validator.weights import resolve_track_shares
from ditto.validator.worker import (
    EmptyRerankerLedgerSource,
    PlatformRerankerLedgerSource,
    RerankerLedgerSource,
)

_CHAMP = "5Champ" + "x" * 42


def _weight_bearing_ledger() -> RerankerLedgerResponse:
    """A ledger shaped as if it could pay, which Platform never serves."""
    submission_id = uuid4()
    champion = RerankerLedgerChampion(
        kind="submission",
        submission_id=submission_id,
        miner_hotkey=_CHAMP,
        artifact_sha256="a" * 64,
        crowned_at=_T0,
        weight_eligible=True,
        combined_score=1.0,
    )
    entry = RerankerLedgerEntry(
        submission_id=submission_id,
        miner_hotkey=_CHAMP,
        artifact_sha256="a" * 64,
        status="scored",
        shadow_score=0.9,
        champion_score=0.8,
        champion_kind="incumbent",
        mean_diff=0.1,
        threshold=0.005,
        dethrones=True,
        latency_passed=True,
        transfer_gate="passed",
        crowned=True,
        pools_sha256="d" * 64,
        scored_at=_T0,
        weight_eligible=True,
        combined_score=1.0,
    )
    return RerankerLedgerResponse(champion=champion, entries=[entry], count=1)


class _Stub:
    def __init__(self, ledger: RerankerLedgerResponse) -> None:
        self.ledger = ledger
        self.calls = 0

    async def fetch(self) -> RerankerLedgerResponse:
        self.calls += 1
        return self.ledger


def _memory_ledger(**overrides: Any) -> LedgerResponse:
    entries = [
        _entry("5Alpha" + "x" * 42, 0.91, first_seen=_T0),
        _entry("5Beta" + "x" * 43, 0.85, first_seen=_T0 + timedelta(days=1)),
        _entry("5Gamma" + "x" * 42, 0.80, first_seen=_T0 + timedelta(days=2)),
    ]
    return LedgerResponse(entries=entries, count=len(entries), **overrides)


async def _weights(
    ledger: LedgerResponse, reranker: RerankerLedgerResponse | None
) -> dict[str, float]:
    worker = _worker(ledger, _config())
    stub = _Stub(reranker) if reranker is not None else None
    if stub is not None:
        worker._reranker_ledger_source = stub
    outcome = await worker._update_weights()
    assert outcome.submitted
    if stub is not None:
        assert stub.calls == 1
    return outcome.weights


# --- Registry ---------------------------------------------------------------


def test_compiled_split_records_the_reranker_at_zero() -> None:
    assert TRACK_SHARES_BPS[TRACK_RERANKER] == 0
    assert TRACK_SHARES_BPS[TRACK_MEMORY] == BASIS_POINT_SCALE


@pytest.mark.parametrize("served", [0, 1, 2_500, BASIS_POINT_SCALE])
@pytest.mark.parametrize(
    ("router_state", "router_eligible"),
    [(TrackState.SHADOW, False), (TrackState.ACTIVE, True)],
)
def test_reranker_is_hard_shadow_whatever_is_served(
    served: int, router_state: TrackState, router_eligible: bool
) -> None:
    registry = build_default_registry(
        # Memory unset defaults to the whole pool; the reranker is served.
        track_shares_bps={TRACK_RERANKER: served},
        router_state=router_state,
        router_weight_eligible=router_eligible,
    )
    track = registry.get(TRACK_RERANKER)
    assert track is not None
    assert track.state is TrackState.SHADOW
    assert track.weight_eligible is False
    assert track.effective_bps() == 0
    assert TRACK_RERANKER not in registry.shares_bps()
    assert track not in registry.active_eligible()
    # Memory keeps the whole pool: the reranker bps never come out of it.
    assert registry.shares_bps()[TRACK_MEMORY] == BASIS_POINT_SCALE


@pytest.mark.parametrize(
    ("with_reranker", "without_reranker"),
    [
        ({TRACK_RERANKER: 4_000}, {}),
        ({TRACK_RERANKER: BASIS_POINT_SCALE}, {}),
        (
            {TRACK_MEMORY: BASIS_POINT_SCALE, TRACK_RERANKER: 1},
            {TRACK_MEMORY: BASIS_POINT_SCALE},
        ),
        (
            {TRACK_MEMORY: 9_000, TRACK_RERANKER: 1_000},
            {TRACK_MEMORY: 9_000},
        ),
    ],
)
def test_served_reranker_share_cannot_shrink_memory(
    with_reranker: dict[str, int], without_reranker: dict[str, int]
) -> None:
    """``resolve_track_shares`` accepts unknown ids; the registry neutralizes it."""
    shares = [
        resolve_track_shares(
            LedgerResponse(entries=[], count=0, track_shares_bps=served),
            default=TRACK_SHARES_BPS,
        )
        for served in (with_reranker, without_reranker)
    ]
    registries = [build_default_registry(track_shares_bps=s) for s in shares]
    assert registries[0].shares_bps() == registries[1].shares_bps()
    assert registries[0].total_active_bps() == registries[1].total_active_bps()


def test_fold_pays_only_a_weight_bearing_champion() -> None:
    clamped = _weight_bearing_ledger().champion
    assert clamped is not None
    shadow = clamped.model_copy(update={"weight_eligible": False, "combined_score": 0})
    assert reranker_fold(TrackFoldInputs(reranker_champion=None)) == {}
    assert reranker_fold(TrackFoldInputs(reranker_champion=shadow)) == {}
    assert (
        reranker_fold(
            TrackFoldInputs(reranker_champion=RerankerLedgerChampion(kind="incumbent"))
        )
        == {}
    )
    # The fold itself works; the track's hard shadow state is what blocks it.
    assert reranker_fold(TrackFoldInputs(reranker_champion=clamped)) == {_CHAMP: 1.0}


# --- Worker: the weight vector ---------------------------------------------


async def test_weights_are_byte_identical_with_and_without_the_ledger() -> None:
    ledger = _memory_ledger()
    absent = await _weights(ledger, None)
    empty = await _weights(ledger, RerankerLedgerResponse())
    weight_bearing = await _weights(ledger, _weight_bearing_ledger())
    assert absent == empty == weight_bearing
    assert _CHAMP not in weight_bearing
    assert sum(absent.values()) == pytest.approx(1.0)
    assert _BURN_HOTKEY not in absent


@pytest.mark.parametrize(
    "served",
    [
        {TRACK_RERANKER: 5_000},
        {TRACK_MEMORY: BASIS_POINT_SCALE, TRACK_RERANKER: BASIS_POINT_SCALE},
        {TRACK_MEMORY: BASIS_POINT_SCALE, "coding": 0, TRACK_RERANKER: 3},
    ],
)
async def test_served_reranker_share_leaves_weights_byte_identical(
    served: dict[str, int],
) -> None:
    baseline = await _weights(_memory_ledger(), None)
    with_share = await _weights(
        _memory_ledger(track_shares_bps=served), _weight_bearing_ledger()
    )
    assert with_share == baseline
    assert _BURN_HOTKEY not in with_share


# --- Sources and config ----------------------------------------------------


async def test_sources_default_empty_and_fail_closed() -> None:
    empty = EmptyRerankerLedgerSource()
    assert isinstance(empty, RerankerLedgerSource)
    assert (await empty.fetch()).entries == []

    published = _weight_bearing_ledger()

    async def read() -> RerankerLedgerResponse:
        return published

    async def boom() -> RerankerLedgerResponse:
        raise RuntimeError("platform unreachable")

    assert await PlatformRerankerLedgerSource(read).fetch() is published
    failed = await PlatformRerankerLedgerSource(boom).fetch()
    assert failed.entries == [] and failed.champion is None


def test_worker_defaults_to_the_empty_source() -> None:
    worker = _worker(_memory_ledger(), _config())
    assert isinstance(worker._reranker_ledger_source, EmptyRerankerLedgerSource)


@pytest.mark.parametrize(
    ("value", "enabled"), [(None, False), ("true", True), ("0", False)]
)
def test_read_flag_is_default_off(
    monkeypatch: pytest.MonkeyPatch, value: str | None, enabled: bool
) -> None:
    _base_env(monkeypatch)
    if value is None:
        monkeypatch.delenv("VALIDATOR_RERANKER_LEDGER_READ_ENABLED", raising=False)
    else:
        monkeypatch.setenv("VALIDATOR_RERANKER_LEDGER_READ_ENABLED", value)
    assert parse_validator_config_from_env().reranker_ledger_read_enabled is enabled


# --- Wire model ------------------------------------------------------------


def test_champion_identity_matches_its_kind() -> None:
    with pytest.raises(ValidationError):
        RerankerLedgerChampion(kind="incumbent", miner_hotkey=_CHAMP)
    with pytest.raises(ValidationError):
        RerankerLedgerChampion(kind="submission")


def test_timestamps_must_be_aware_and_unknown_fields_are_ignored() -> None:
    with pytest.raises(ValidationError):
        RerankerLedgerResponse(generated_at=datetime(2026, 10, 13))
    parsed = RerankerLedgerResponse.model_validate(
        {"generated_at": datetime(2026, 10, 13, tzinfo=UTC).isoformat(), "new": 1}
    )
    assert parsed.count == 0 and parsed.champion is None
