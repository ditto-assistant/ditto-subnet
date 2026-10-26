"""Protocol 33: the generator track ships active, and stays inert until governed.

The release promotes the generator track (``active`` + weight-eligible + ledger
read on). That is safe only because two Platform-owned switches still gate
every rao: the ledger must serve a nonzero ``track_shares_bps`` generator share,
and Platform must mark ledger entries ``weight_eligible`` (generator settings
``mode=enforce``). These tests pin the weight vector the worker submits:

- no served share: byte-identical to the shadow (protocol 32) fold, whatever
  the generator ledger holds;
- a served share but no eligible entry: the share falls back to memory, so the
  vector is again byte-identical (nothing burns, nobody is shorted);
- a served share and eligible entries: generators split exactly their share.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from ditto.api_models.generator_ledger import (
    GeneratorLedgerEntry,
    GeneratorLedgerResponse,
)
from ditto.api_models.validator import LedgerResponse
from ditto.tests.validator.test_worker import _config, _platform_with_ledger
from ditto.tests.validator.test_worker import _entry as _memory_entry
from ditto.validator.config import ValidatorConfig
from ditto.validator.tracks import (
    TRACK_GENERATOR,
    TRACK_MEMORY,
    TrackState,
    build_default_registry,
)
from ditto.validator.weights import reassign_unclaimed_shares, track_allocated_share
from ditto.validator.worker import ValidatorWorker

_T0 = datetime(2026, 10, 1, tzinfo=UTC)
_CHAMPION = "5Champion" + "x" * 39
_SECOND = "5Second" + "x" * 41
_THIRD = "5Third" + "x" * 42
_GEN_A = "5GenA" + "x" * 43
_GEN_B = "5GenB" + "x" * 43
_ACTIVATION_SHARES = {TRACK_MEMORY: 9000, TRACK_GENERATOR: 1000}


def _shipped(name: str) -> Any:
    return next(
        f for f in dataclasses.fields(ValidatorConfig) if f.name == name
    ).default


def _generator(hotkey: str, score: float, *, eligible: bool, seen: int = 0) -> Any:
    return GeneratorLedgerEntry(
        miner_hotkey=hotkey,
        submission_id=uuid4(),
        round_index=9,
        weight_eligible=eligible,
        score=score,
        first_seen=_T0 + timedelta(hours=seen),
        scored_at=_T0 + timedelta(hours=seen + 1),
    )


async def _submitted_weights(
    *,
    protocol_33: bool,
    served_shares: dict[str, int] | None = None,
    generator_entries: list[Any] | None = None,
    generator_mode: str = "shadow",
    generator_ledger_unavailable: bool = False,
) -> dict[str, float] | None:
    platform = _platform_with_ledger(
        jobs=[],
        ledger=[
            _memory_entry(_CHAMPION, 0.91),
            _memory_entry(_SECOND, 0.80),
            _memory_entry(_THIRD, 0.70),
        ],
    )
    ledger: LedgerResponse = platform.get_ledger.return_value
    # A served 20% burn, so the tests also pin that a generator share never
    # changes how much burns.
    update: dict[str, Any] = {"burn_share": 0.2}
    if served_shares is not None:
        update["track_shares_bps"] = served_shares
    platform.get_ledger.return_value = ledger.model_copy(update=update)
    chain = MagicMock()
    chain.get_recent_neurons = AsyncMock(
        return_value=[
            SimpleNamespace(hotkey=hotkey)
            for hotkey in (_CHAMPION, _SECOND, _THIRD, _GEN_A, _GEN_B)
        ]
    )
    chain.put_weights = AsyncMock()
    config = _config()
    source: MagicMock | None = None
    if protocol_33:
        config.generator_track_state = _shipped("generator_track_state")
        config.generator_weight_eligible = _shipped("generator_weight_eligible")
        config.generator_ledger_read_enabled = _shipped("generator_ledger_read_enabled")
        source = MagicMock()
        source.fetch = AsyncMock(
            return_value=None
            if generator_ledger_unavailable
            else GeneratorLedgerResponse(
                entries=generator_entries or [],
                count=len(generator_entries or []),
                mode=generator_mode,
            )
        )
    worker = ValidatorWorker(
        config=config,
        platform=platform,
        dittobench=MagicMock(),
        chain=chain,
        keypair=MagicMock(),
        generator_ledger_source=source,
    )
    await worker.run_once()
    if not chain.put_weights.await_count:
        return None
    weights: dict[str, float] = chain.put_weights.await_args.args[0]
    return dict(weights)


def test_protocol_33_compiles_the_generator_track_active_but_unfunded() -> None:
    assert _shipped("generator_track_state") == TrackState.ACTIVE.value
    assert _shipped("generator_weight_eligible") is True
    assert _shipped("generator_ledger_read_enabled") is True
    registry = build_default_registry(
        track_shares_bps=ValidatorConfig.__dataclass_fields__[
            "track_shares_bps"
        ].default_factory(),  # type: ignore[misc]
        generator_state=TrackState.ACTIVE,
        generator_weight_eligible=True,
    )
    assert registry.shares_bps().get(TRACK_GENERATOR, 0) == 0


async def test_no_served_share_is_byte_identical_even_with_eligible_entries() -> None:
    baseline = await _submitted_weights(protocol_33=False)
    assert baseline is not None
    promoted = await _submitted_weights(
        protocol_33=True,
        generator_entries=[
            _generator(_GEN_A, 0.6, eligible=True),
            _generator(_GEN_B, 0.4, eligible=True, seen=1),
        ],
        generator_mode="enforce",
    )
    assert promoted == baseline
    assert list(promoted) == list(baseline)


async def test_served_share_without_eligible_entries_falls_back_to_memory() -> None:
    baseline = await _submitted_weights(protocol_33=False)
    for entries in (
        [],
        [
            _generator(_GEN_A, 0.6, eligible=False),
            _generator(_GEN_B, 0.4, eligible=False, seen=1),
        ],
    ):
        promoted = await _submitted_weights(
            protocol_33=True,
            served_shares=_ACTIVATION_SHARES,
            generator_entries=entries,
        )
        assert promoted == baseline
        assert _GEN_A not in (promoted or {}) and _GEN_B not in (promoted or {})


async def test_served_share_with_eligible_entries_pays_exactly_that_share() -> None:
    baseline = await _submitted_weights(protocol_33=False)
    assert baseline is not None
    burn = 1.0 - sum(
        w for k, w in baseline.items() if k in (_CHAMPION, _SECOND, _THIRD)
    )
    paid = await _submitted_weights(
        protocol_33=True,
        served_shares=_ACTIVATION_SHARES,
        generator_entries=[
            _generator(_GEN_A, 0.6, eligible=True),
            _generator(_GEN_B, 0.4, eligible=True, seen=1),
            _generator("5Ineligible" + "x" * 37, 0.99, eligible=False),
        ],
        generator_mode="enforce",
    )
    assert paid is not None
    assert burn == pytest.approx(0.2)
    total = sum(paid.values())
    generator_share = (paid[_GEN_A] + paid[_GEN_B]) / total
    memory_share = sum(paid[k] for k in (_CHAMPION, _SECOND, _THIRD)) / total
    miner_share = 1.0 - burn
    assert generator_share == pytest.approx(miner_share * 0.1)
    assert memory_share == pytest.approx(miner_share * 0.9)
    assert paid[_GEN_A] / paid[_GEN_B] == pytest.approx(0.40 / 0.25)
    assert "5Ineligible" + "x" * 37 not in paid
    # The burn fraction is unchanged by the generator share.
    assert 1.0 - memory_share - generator_share == pytest.approx(burn)


async def test_unreadable_ledger_without_a_share_still_sets_weights() -> None:
    baseline = await _submitted_weights(protocol_33=False)
    promoted = await _submitted_weights(
        protocol_33=True, generator_ledger_unavailable=True
    )
    assert promoted == baseline


async def test_unreadable_ledger_with_a_share_keeps_last_vector() -> None:
    assert (
        await _submitted_weights(
            protocol_33=True,
            served_shares=_ACTIVATION_SHARES,
            generator_ledger_unavailable=True,
        )
        is None
    )


async def test_an_over_cap_served_map_folds_the_memory_only_split() -> None:
    # Memory absent reads as the full pool, so a lone generator share is over
    # the cap and the registry degrades to the compiled safe split.
    baseline = await _submitted_weights(protocol_33=False)
    promoted = await _submitted_weights(
        protocol_33=True,
        served_shares={TRACK_GENERATOR: 1000},
        generator_entries=[_generator(_GEN_A, 0.6, eligible=True)],
        generator_mode="enforce",
    )
    assert promoted == baseline


def test_reassign_moves_only_a_declared_empty_track() -> None:
    vectors = {TRACK_MEMORY: {"m": 1.0}, TRACK_GENERATOR: {}, "router": {}}
    shares = {TRACK_MEMORY: 8000, TRACK_GENERATOR: 1000, "router": 500}
    reassigned = reassign_unclaimed_shares(
        shares, vectors, fallbacks={TRACK_GENERATOR: TRACK_MEMORY}
    )
    assert reassigned == {TRACK_MEMORY: 9000, "router": 500}
    # The router share has no fallback, so it still burns.
    assert track_allocated_share(reassigned, vectors) == pytest.approx(0.9)


def test_reassign_keeps_a_paying_track_and_zero_only_vectors_count_as_empty() -> None:
    shares = {TRACK_MEMORY: 9000, TRACK_GENERATOR: 1000}
    fallbacks = {TRACK_GENERATOR: TRACK_MEMORY}
    assert (
        reassign_unclaimed_shares(
            shares,
            {TRACK_MEMORY: {"m": 1.0}, TRACK_GENERATOR: {"g": 1.0}},
            fallbacks=fallbacks,
        )
        == shares
    )
    assert reassign_unclaimed_shares(
        shares,
        {TRACK_MEMORY: {"m": 1.0}, TRACK_GENERATOR: {"g": 0.0}},
        fallbacks=fallbacks,
    ) == {TRACK_MEMORY: 10000}


def test_reassign_is_a_single_hop() -> None:
    shares = {"a": 1000, "b": 2000, "c": 7000}
    reassigned = reassign_unclaimed_shares(
        shares, {"c": {"x": 1.0}}, fallbacks={"a": "b", "b": "c"}
    )
    # a's 1000 lands on b and stays there unclaimed (b is empty); only b's own
    # 2000 moves on to c.
    assert reassigned == {"b": 1000, "c": 9000}


def test_empty_memory_still_burns_the_moved_share() -> None:
    shares = {TRACK_MEMORY: 9000, TRACK_GENERATOR: 1000}
    vectors: dict[str, dict[str, float]] = {TRACK_MEMORY: {}, TRACK_GENERATOR: {}}
    reassigned = reassign_unclaimed_shares(
        shares, vectors, fallbacks={TRACK_GENERATOR: TRACK_MEMORY}
    )
    assert reassigned == {TRACK_MEMORY: 10000}
    assert track_allocated_share(reassigned, vectors) == 0.0


def test_registry_claimed_shares_use_the_generator_fallback() -> None:
    registry = build_default_registry(
        track_shares_bps=_ACTIVATION_SHARES,
        generator_state=TrackState.ACTIVE,
        generator_weight_eligible=True,
    )
    assert registry.claimed_shares_bps(
        {TRACK_MEMORY: {"m": 1.0}, TRACK_GENERATOR: {}}
    ) == {TRACK_MEMORY: 10000}
    assert (
        registry.claimed_shares_bps(
            {TRACK_MEMORY: {"m": 1.0}, TRACK_GENERATOR: {"g": 1.0}}
        )
        == _ACTIVATION_SHARES
    )
