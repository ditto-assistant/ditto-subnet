"""Statistically-principled (indifference-band) KOTH dethroning.

A challenger must beat the incumbent by more than the **indifference band** =
max(flat relative margin, z·√(se_c² + se_champ²)). With no per-entry
``composite_stderr`` the band is exactly the flat relative margin, so the fold is
byte-identical to the pre-band rule.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from ditto.validator.weights import (
    _beats,
    _effective_composite,
    _entry_confirmations,
    _entry_stderr,
    compute_weights,
)

_T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _e(
    miner: str,
    composite: float,
    *,
    stderr: float | None = None,
    confirmations: list[float] | None = None,
    minutes: int = 0,
) -> Any:
    """A duck-typed ledger entry. ``stderr=None`` models a platform that does not
    surface ``composite_stderr`` (the field is simply absent); likewise
    ``confirmations=None`` models an absent ``confirmation_composites``."""
    ns = SimpleNamespace(
        miner_hotkey=miner,
        agent_id=uuid4(),
        composite=composite,
        first_seen=_T0 + timedelta(minutes=minutes),
        sha256="ab" * 32,
    )
    if stderr is not None:
        ns.composite_stderr = stderr
    if confirmations is not None:
        ns.confirmation_composites = confirmations
    return ns


# champ is first-seen (minutes=0); challenger comes later (minutes=1).
def _champ() -> Any:
    return _e("champ", 0.80, stderr=0.03, minutes=0)


class TestEntryStderr:
    def test_absent_field_is_none(self) -> None:
        assert _entry_stderr(_e("a", 0.5)) is None

    def test_present_value(self) -> None:
        assert _entry_stderr(_e("a", 0.5, stderr=0.02)) == pytest.approx(0.02)

    def test_non_finite_and_negative_treated_as_absent(self) -> None:
        assert _entry_stderr(_e("a", 0.5, stderr=float("nan"))) is None
        assert _entry_stderr(_e("a", 0.5, stderr=float("inf"))) is None
        assert _entry_stderr(_e("a", 0.5, stderr=-0.01)) is None


class TestEntryConfirmations:
    def test_absent_field_is_none(self) -> None:
        assert _entry_confirmations(_e("a", 0.5)) is None

    def test_fewer_than_two_values_is_none(self) -> None:
        # A single-seed sweep (K=1) must stay byte-identical to the raw composite.
        assert _entry_confirmations(_e("a", 0.5, confirmations=[0.5])) is None
        assert _entry_confirmations(_e("a", 0.5, confirmations=[])) is None

    def test_valid_list(self) -> None:
        assert _entry_confirmations(_e("a", 0.5, confirmations=[0.4, 0.6])) == [0.4, 0.6]

    def test_out_of_range_or_non_finite_treated_as_absent(self) -> None:
        # A malformed list must degrade to the raw composite, never poison the
        # deterministic fold with a value one validator accepts and another does not.
        assert _entry_confirmations(_e("a", 0.5, confirmations=[0.4, 1.5])) is None
        assert _entry_confirmations(_e("a", 0.5, confirmations=[-0.1, 0.6])) is None
        assert (
            _entry_confirmations(_e("a", 0.5, confirmations=[float("nan"), 0.6]))
            is None
        )


class TestEffectiveComposite:
    def test_no_confirmations_is_raw_composite(self) -> None:
        assert _effective_composite(_e("a", 0.73)) == 0.73

    def test_odd_count_is_the_middle_value(self) -> None:
        assert _effective_composite(_e("a", 0.9, confirmations=[0.9, 0.5, 0.7])) == 0.7

    def test_even_count_is_mean_of_two_middle(self) -> None:
        assert _effective_composite(
            _e("a", 0.9, confirmations=[0.9, 0.5, 0.7, 0.6])
        ) == pytest.approx((0.6 + 0.7) / 2)


class TestBeatsWithConfirmations:
    def test_a_lucky_single_seed_lead_does_not_dethrone_on_the_median(self) -> None:
        # Champion is steady; the challenger's raw composite beats it, but its
        # per-seed MEDIAN (the effective composite) does not — so no crown flip.
        champ = _e("champ", 0.80, confirmations=[0.80, 0.79, 0.81], minutes=0)
        chal = _e("chal", 0.90, confirmations=[0.90, 0.70, 0.72], minutes=1)
        assert chal.composite - champ.composite > 0.80 * 0.05  # raw lead clears margin
        # median(chal)=0.72 < median(champ)=0.80 → does not beat.
        assert not _beats(chal, champ, 0.05, 0.0)

    def test_a_median_lead_beyond_margin_dethrones(self) -> None:
        champ = _e("champ", 0.80, confirmations=[0.80, 0.79, 0.81], minutes=0)
        chal = _e("chal", 0.90, confirmations=[0.90, 0.88, 0.92], minutes=1)
        # median(chal)=0.90 vs median(champ)=0.80, lead 0.10 > flat 0.04 → dethrone.
        assert _beats(chal, champ, 0.05, 0.0)


class TestBeats:
    def test_no_stderr_is_flat_relative_margin(self) -> None:
        champ = _e("champ", 0.80, minutes=0)
        # margin 0.05 → threshold 0.80*1.05 = 0.84.
        assert not _beats(_e("c", 0.84, minutes=1), champ, 0.05, 1.64)  # exactly at
        assert not _beats(_e("c", 0.839, minutes=1), champ, 0.05, 1.64)
        assert _beats(_e("c", 0.841, minutes=1), champ, 0.05, 1.64)

    def test_statistical_band_blocks_a_sub_uncertainty_lead(self) -> None:
        champ = _champ()  # stderr 0.03
        # margin band = 0.05*0.80 = 0.04; stat band = 1.64*sqrt(0.03²+0.03²) ≈ 0.0696.
        # A 0.05 lead clears the flat margin but NOT the statistical band.
        challenger = _e("c", 0.85, stderr=0.03, minutes=1)
        assert 0.85 - 0.80 > 0.04  # would dethrone under the old flat rule
        assert not _beats(challenger, champ, 0.05, 1.64)

    def test_clear_lead_beyond_uncertainty_dethrones(self) -> None:
        champ = _champ()
        assert _beats(_e("c", 0.88, stderr=0.03, minutes=1), champ, 0.05, 1.64)

    def test_z_zero_disables_statistical_band(self) -> None:
        champ = _champ()
        # With z=0 the band is the flat margin (0.04); a 0.05 lead dethrones.
        assert _beats(_e("c", 0.85, stderr=0.03, minutes=1), champ, 0.05, 0.0)

    def test_both_entries_need_stderr(self) -> None:
        champ = _champ()  # has stderr
        # Challenger lacks stderr → statistical band inapplicable → flat margin.
        assert _beats(_e("c", 0.85, minutes=1), champ, 0.05, 1.64)

    def test_manual_band_matches_formula(self) -> None:
        champ = _champ()
        band = 1.64 * math.sqrt(0.03**2 + 0.03**2)
        # A lead just under the band does not dethrone; just over does.
        assert not _beats(
            _e("c", 0.80 + band - 0.001, stderr=0.03, minutes=1), champ, 0.05, 1.64
        )
        assert _beats(
            _e("c", 0.80 + band + 0.001, stderr=0.03, minutes=1), champ, 0.05, 1.64
        )


class TestComputeWeightsWithBand:
    def test_default_z_zero_is_backward_compatible(self) -> None:
        # No dethrone_z passed → flat relative margin, even with stderr present.
        entries = [
            _e("champ", 0.80, stderr=0.03, minutes=0),
            _e("chal", 0.85, stderr=0.03, minutes=1),
        ]
        w = compute_weights(entries, margin=0.05, tail_size=0, champion_share=1.0)
        assert w == {"chal": pytest.approx(1.0)}  # 0.05 lead > flat 0.04

    def test_band_keeps_incumbent_under_uncertainty(self) -> None:
        entries = [
            _e("champ", 0.80, stderr=0.03, minutes=0),
            _e("chal", 0.85, stderr=0.03, minutes=1),
        ]
        # Same entries, now with the statistical band active: the 0.05 lead is
        # inside the ~0.0696 band, so the incumbent keeps the crown.
        w = compute_weights(
            entries, margin=0.05, tail_size=0, champion_share=1.0, dethrone_z=1.64
        )
        assert w == {"champ": pytest.approx(1.0)}

    def test_band_allows_a_clear_dethrone(self) -> None:
        entries = [
            _e("champ", 0.80, stderr=0.03, minutes=0),
            _e("chal", 0.90, stderr=0.03, minutes=1),
        ]
        w = compute_weights(
            entries, margin=0.05, tail_size=0, champion_share=1.0, dethrone_z=1.64
        )
        assert w == {"chal": pytest.approx(1.0)}

    def test_no_stderr_ledger_identical_with_or_without_z(self) -> None:
        # A ledger with no stderr must fold identically whether or not z is set —
        # the zero-regression guarantee.
        entries = [
            _e("champ", 0.80, minutes=0),
            _e("chal", 0.85, minutes=1),
        ]
        base = compute_weights(entries, margin=0.05, tail_size=0, champion_share=1.0)
        withz = compute_weights(
            entries, margin=0.05, tail_size=0, champion_share=1.0, dethrone_z=1.64
        )
        assert base == withz == {"chal": pytest.approx(1.0)}


class TestComputeWeightsWithConfirmations:
    def test_median_over_seeds_keeps_incumbent(self) -> None:
        # The challenger's raw composite would dethrone, but its per-seed median
        # does not, so the incumbent keeps the crown (P4).
        entries = [
            _e("champ", 0.80, confirmations=[0.80, 0.79, 0.81], minutes=0),
            _e("chal", 0.90, confirmations=[0.90, 0.70, 0.72], minutes=1),
        ]
        w = compute_weights(entries, margin=0.05, tail_size=0, champion_share=1.0)
        assert w == {"champ": pytest.approx(1.0)}

    def test_median_over_seeds_allows_a_replicated_dethrone(self) -> None:
        entries = [
            _e("champ", 0.80, confirmations=[0.80, 0.79, 0.81], minutes=0),
            _e("chal", 0.90, confirmations=[0.90, 0.88, 0.92], minutes=1),
        ]
        w = compute_weights(entries, margin=0.05, tail_size=0, champion_share=1.0)
        assert w == {"chal": pytest.approx(1.0)}
