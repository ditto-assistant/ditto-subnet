"""Statistically-principled (indifference-band) KOTH dethroning.

A challenger must beat the incumbent by more than the **indifference band** =
max(fixed composite-point margin, z·√(se_c² + se_champ²)). With no per-entry
``composite_stderr`` the band is exactly the fixed composite-point margin.
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
    _entry_seed_composites,
    _entry_stderr,
    _paired_dethrone,
    compute_weights,
    contested_confirmation_set,
    top5_confirmation_set,
)

_T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _e(
    miner: str,
    composite: float,
    *,
    stderr: float | None = None,
    confirmations: list[float] | None = None,
    seeds: list[int] | None = None,
    bench_version: int | None = None,
    minutes: int = 0,
) -> Any:
    """A duck-typed ledger entry. ``stderr=None`` models a platform that does not
    surface ``composite_stderr`` (the field is simply absent); likewise
    ``confirmations=None`` / ``seeds=None`` model an absent
    ``confirmation_composites`` / ``confirmation_seeds``."""
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
    if seeds is not None:
        ns.confirmation_seeds = seeds
    if bench_version is not None:
        ns.bench_version = bench_version
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


class TestTop5ConfirmationSet:
    def test_bootstraps_champion_and_catches_up_tail(self) -> None:
        champion = _e("champ", 0.9, bench_version=6)
        tail = _e("tail", 0.8, bench_version=6, minutes=1)
        plan = top5_confirmation_set(
            [tail, champion],
            current_version=6,
            margin=0.02,
            dethrone_z=1.64,
            tail_size=4,
            baseline_seeds=3,
            max_seeds=16,
            catch_up_rate=2,
        )
        assert plan is not None
        by_miner = {member.entry.miner_hotkey: member for member in plan.members}
        assert len(by_miner["champ"].seeds_to_score) == 3
        assert len(by_miner["tail"].seeds_to_score) == 2

    def test_ignores_stale_benchmark_entries(self) -> None:
        stale = _e("stale", 0.99, bench_version=5)
        assert (
            top5_confirmation_set(
                [stale],
                current_version=6,
                margin=0.02,
                dethrone_z=1.64,
                tail_size=4,
                baseline_seeds=3,
                max_seeds=16,
            )
            is None
        )


class TestEntryConfirmations:
    def test_absent_field_is_none(self) -> None:
        assert _entry_confirmations(_e("a", 0.5)) is None

    def test_fewer_than_two_values_is_none(self) -> None:
        # A single-seed sweep (K=1) must stay byte-identical to the raw composite.
        assert _entry_confirmations(_e("a", 0.5, confirmations=[0.5])) is None
        assert _entry_confirmations(_e("a", 0.5, confirmations=[])) is None

    def test_valid_list(self) -> None:
        assert _entry_confirmations(_e("a", 0.5, confirmations=[0.4, 0.6])) == [
            0.4,
            0.6,
        ]

    def test_out_of_range_or_non_finite_treated_as_absent(self) -> None:
        # A malformed list must degrade to the raw composite, never poison the
        # deterministic fold with a value one validator accepts and another does not.
        assert _entry_confirmations(_e("a", 0.5, confirmations=[0.4, 1.5])) is None
        assert _entry_confirmations(_e("a", 0.5, confirmations=[-0.1, 0.6])) is None
        assert (
            _entry_confirmations(_e("a", 0.5, confirmations=[float("nan"), 0.6]))
            is None
        )

    def test_v5_waste_adjusted_confirmations_remain_bounded(self) -> None:
        assert _entry_confirmations(
            _e("a", 0.855, confirmations=[0.855, 0.88], bench_version=5)
        ) == [0.855, 0.88]
        assert (
            _entry_confirmations(
                _e("a", 0.9, confirmations=[0.88, 1.001], bench_version=5)
            )
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
        # median(chal)=0.90 vs median(champ)=0.80, lead 0.10 > flat 0.05 → dethrone.
        assert _beats(chal, champ, 0.05, 0.0)


class TestBeats:
    def test_no_stderr_is_fixed_composite_point_margin(self) -> None:
        champ = _e("champ", 0.80, minutes=0)
        # margin 0.04 → threshold 0.80 + 0.04 = 0.84.
        assert not _beats(_e("c", 0.84, minutes=1), champ, 0.04, 1.64)  # exactly at
        assert not _beats(_e("c", 0.839, minutes=1), champ, 0.04, 1.64)
        assert _beats(_e("c", 0.841, minutes=1), champ, 0.04, 1.64)

    def test_fixed_margin_does_not_grow_into_a_ceiling_lock(self) -> None:
        champ = _e("champ", 0.930, minutes=0)
        # The production 0.007-point hysteresis lets a real 0.008 improvement
        # contend near the ceiling. The old 2% relative rule required 0.0186.
        assert not _beats(_e("tie", 0.937, minutes=1), champ, 0.007, 0.0)
        assert _beats(_e("better", 0.938, minutes=1), champ, 0.007, 0.0)

    def test_statistical_band_blocks_a_sub_uncertainty_lead(self) -> None:
        champ = _champ()  # stderr 0.03
        # fixed margin = 0.04; stat band = 1.64*sqrt(0.03²+0.03²) ≈ 0.0696.
        # A 0.05 lead clears the flat margin but NOT the statistical band.
        challenger = _e("c", 0.85, stderr=0.03, minutes=1)
        assert 0.85 - 0.80 > 0.04  # would dethrone under the old flat rule
        assert not _beats(challenger, champ, 0.04, 1.64)

    def test_clear_lead_beyond_uncertainty_dethrones(self) -> None:
        champ = _champ()
        assert _beats(_e("c", 0.88, stderr=0.03, minutes=1), champ, 0.04, 1.64)

    def test_z_zero_disables_statistical_band(self) -> None:
        champ = _champ()
        # With z=0 the band is the flat margin (0.04); a 0.05 lead dethrones.
        assert _beats(_e("c", 0.85, stderr=0.03, minutes=1), champ, 0.04, 0.0)

    def test_both_entries_need_stderr(self) -> None:
        champ = _champ()  # has stderr
        # Challenger lacks stderr → statistical band inapplicable → flat margin.
        assert _beats(_e("c", 0.85, minutes=1), champ, 0.04, 1.64)

    def test_manual_band_matches_formula(self) -> None:
        champ = _champ()
        band = 1.64 * math.sqrt(0.03**2 + 0.03**2)
        # A lead just under the band does not dethrone; just over does.
        assert not _beats(
            _e("c", 0.80 + band - 0.001, stderr=0.03, minutes=1), champ, 0.04, 1.64
        )
        assert _beats(
            _e("c", 0.80 + band + 0.001, stderr=0.03, minutes=1), champ, 0.04, 1.64
        )


class TestComputeWeightsWithBand:
    def test_default_z_zero_is_backward_compatible(self) -> None:
        # No dethrone_z passed → fixed composite-point margin, even with stderr present.
        entries = [
            _e("champ", 0.80, stderr=0.03, minutes=0),
            _e("chal", 0.85, stderr=0.03, minutes=1),
        ]
        w = compute_weights(entries, margin=0.04, tail_size=0, rank_shares=(1.0,))
        assert w == {"chal": pytest.approx(1.0)}  # 0.05 lead > flat 0.04

    def test_band_keeps_incumbent_under_uncertainty(self) -> None:
        entries = [
            _e("champ", 0.80, stderr=0.03, minutes=0),
            _e("chal", 0.85, stderr=0.03, minutes=1),
        ]
        # Same entries, now with the statistical band active: the 0.05 lead is
        # inside the ~0.0696 band, so the incumbent keeps the crown.
        w = compute_weights(
            entries, margin=0.04, tail_size=0, rank_shares=(1.0,), dethrone_z=1.64
        )
        assert w == {"champ": pytest.approx(1.0)}

    def test_band_allows_a_clear_dethrone(self) -> None:
        entries = [
            _e("champ", 0.80, stderr=0.03, minutes=0),
            _e("chal", 0.90, stderr=0.03, minutes=1),
        ]
        w = compute_weights(
            entries, margin=0.04, tail_size=0, rank_shares=(1.0,), dethrone_z=1.64
        )
        assert w == {"chal": pytest.approx(1.0)}

    def test_no_stderr_ledger_identical_with_or_without_z(self) -> None:
        # A ledger with no stderr must fold identically whether or not z is set —
        # the zero-regression guarantee.
        entries = [
            _e("champ", 0.80, minutes=0),
            _e("chal", 0.85, minutes=1),
        ]
        base = compute_weights(entries, margin=0.04, tail_size=0, rank_shares=(1.0,))
        withz = compute_weights(
            entries, margin=0.04, tail_size=0, rank_shares=(1.0,), dethrone_z=1.64
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
        w = compute_weights(entries, margin=0.05, tail_size=0, rank_shares=(1.0,))
        assert w == {"champ": pytest.approx(1.0)}

    def test_median_over_seeds_allows_a_replicated_dethrone(self) -> None:
        entries = [
            _e("champ", 0.80, confirmations=[0.80, 0.79, 0.81], minutes=0),
            _e("chal", 0.90, confirmations=[0.90, 0.88, 0.92], minutes=1),
        ]
        w = compute_weights(entries, margin=0.05, tail_size=0, rank_shares=(1.0,))
        assert w == {"chal": pytest.approx(1.0)}


class TestEntrySeedComposites:
    def test_none_without_seeds(self) -> None:
        # confirmations present but no aligned seeds -> not pairable.
        assert _entry_seed_composites(_e("a", 0.8, confirmations=[0.8, 0.82])) is None

    def test_maps_seed_to_composite(self) -> None:
        e = _e("a", 0.8, confirmations=[0.90, 0.70, 0.80], seeds=[10, 20, 30])
        assert _entry_seed_composites(e) == {10: 0.90, 20: 0.70, 30: 0.80}

    def test_length_mismatch_is_absent(self) -> None:
        e = _e("a", 0.8, confirmations=[0.90, 0.70, 0.80], seeds=[10, 20])
        assert _entry_seed_composites(e) is None

    def test_duplicate_or_negative_seed_is_absent(self) -> None:
        dup = _e("a", 0.8, confirmations=[0.9, 0.7, 0.8], seeds=[10, 10, 30])
        neg = _e("a", 0.8, confirmations=[0.9, 0.7, 0.8], seeds=[10, -1, 30])
        assert _entry_seed_composites(dup) is None
        assert _entry_seed_composites(neg) is None


class TestPairedDethrone:
    def test_none_when_no_shared_seeds(self) -> None:
        chal = _e("chal", 0.9, confirmations=[0.9, 0.92], seeds=[1, 2])
        champ = _e("champ", 0.8, confirmations=[0.8, 0.81], seeds=[3, 4])
        assert _paired_dethrone(chal, champ, 1.64) is None

    def test_none_when_z_not_positive(self) -> None:
        chal = _e("chal", 0.9, confirmations=[0.9, 0.92], seeds=[1, 2])
        champ = _e("champ", 0.8, confirmations=[0.8, 0.81], seeds=[1, 2])
        assert _paired_dethrone(chal, champ, 0.0) is None

    def test_pairs_over_common_seeds_only(self) -> None:
        # Seed 9 is challenger-only, seed 8 champion-only; pairing uses {1, 2}.
        chal = _e("chal", 0.9, confirmations=[0.90, 0.94, 0.50], seeds=[1, 2, 9])
        champ = _e("champ", 0.8, confirmations=[0.80, 0.82, 0.50], seeds=[1, 2, 8])
        out = _paired_dethrone(chal, champ, 1.64)
        assert out is not None
        mean_diff, champ_ref, se_diff = out
        # diffs over {1,2} = [0.10, 0.12]; mean 0.11; champ_ref mean(0.80,0.82)=0.81
        assert mean_diff == pytest.approx(0.11)
        assert champ_ref == pytest.approx(0.81)
        # SEM of [0.10, 0.12]: sample var 0.0002, se = sqrt(0.0002 / 2) = 0.01.
        assert se_diff == pytest.approx(0.01)


class TestBeatsPaired:
    def test_tight_paired_lead_dethrones_where_unpaired_holds(self) -> None:
        # Both carry stderr 0.03 AND aligned seeds, with a steady +0.05 per-seed
        # lead. PAIRED: se_diff ~ 0, so the fixed 0.02-point test margin wins and
        # the 0.05 lead clears it. UNPAIRED (seeds stripped): the independent-sum
        # band 1.64*sqrt(0.03^2 + 0.03^2) = 0.070 holds the same 0.05 lead. Same
        # data, opposite verdict -- that is exactly the CRN pairing win.
        champ = _e(
            "champ",
            0.80,
            stderr=0.03,
            confirmations=[0.80, 0.79, 0.81],
            seeds=[1, 2, 3],
            minutes=0,
        )
        chal = _e(
            "chal",
            0.85,
            stderr=0.03,
            confirmations=[0.85, 0.84, 0.86],
            seeds=[1, 2, 3],
            minutes=1,
        )
        assert _beats(chal, champ, margin=0.02, dethrone_z=1.64) is True
        chal_unpaired = _e(
            "chal",
            0.85,
            stderr=0.03,
            confirmations=[0.85, 0.84, 0.86],
            minutes=1,
        )
        assert _beats(chal_unpaired, champ, margin=0.02, dethrone_z=1.64) is False

    def test_noisy_paired_lead_is_held_by_the_band(self) -> None:
        # Same ~0.05 mean lead but the per-seed differences swing (se_diff large),
        # so the paired z-band holds the incumbent.
        champ = _e(
            "champ", 0.80, confirmations=[0.80, 0.79, 0.81], seeds=[1, 2, 3], minutes=0
        )
        chal = _e(
            "chal", 0.85, confirmations=[0.95, 0.70, 0.90], seeds=[1, 2, 3], minutes=1
        )
        assert _beats(chal, champ, margin=0.02, dethrone_z=1.64) is False

    def test_falls_back_to_unpaired_without_seeds(self) -> None:
        # No seeds on either side -> unpaired median/independent-sum path, unchanged.
        champ = _e("champ", 0.80, stderr=0.03, minutes=0)
        chal = _e("chal", 0.90, stderr=0.03, minutes=1)
        # independent band 1.64*sqrt(2)*0.03 = 0.070 < lead 0.10 -> dethrones.
        assert _beats(chal, champ, margin=0.02, dethrone_z=1.64) is True


class TestContestedConfirmationSet:
    """Near-band contested-dethrone selection: the champion + every
    current-version challenger inside the unpaired indifference band, skipped
    once the paired statistic can already decide every contested pair."""

    _KW: dict[str, Any] = {"current_version": 1, "margin": 0.02, "dethrone_z": 0.0}

    def test_in_band_challenger_selects_champion_and_challenger(self) -> None:
        champ = _e("5A" + "a" * 44, 0.80)
        chall = _e("5B" + "b" * 44, 0.79, minutes=1)  # deficit 0.01 <= band 0.02
        got = contested_confirmation_set([champ, chall], **self._KW)
        assert [e.agent_id for e in got] == [champ.agent_id, chall.agent_id]

    def test_clear_loss_is_not_contested(self) -> None:
        champ = _e("5A" + "a" * 44, 0.80)
        chall = _e("5B" + "b" * 44, 0.70, minutes=1)  # deficit 0.10 > band
        assert contested_confirmation_set([champ, chall], **self._KW) == []

    def test_clear_win_flips_crown_without_confirmation(self) -> None:
        old = _e("5A" + "a" * 44, 0.80)
        new = _e("5B" + "b" * 44, 0.90, minutes=1)  # dethrones; old 0.10 behind
        assert contested_confirmation_set([old, new], **self._KW) == []

    def test_z_band_widens_the_contested_zone(self) -> None:
        # Deficit 0.05 clears the fixed 0.02-point margin but sits inside the z band
        # (1.64 * sqrt(0.03^2 + 0.03^2) ~= 0.0696), so the pair is contested.
        champ = _e("5A" + "a" * 44, 0.80, stderr=0.03)
        chall = _e("5B" + "b" * 44, 0.75, stderr=0.03, minutes=1)
        got = contested_confirmation_set(
            [champ, chall], current_version=1, margin=0.02, dethrone_z=1.64
        )
        assert [e.agent_id for e in got] == [champ.agent_id, chall.agent_id]

    def test_settled_pair_never_retriggers(self) -> None:
        champ = _e(
            "5A" + "a" * 44, 0.80, confirmations=[0.79, 0.80, 0.81], seeds=[7, 8, 9]
        )
        chall = _e(
            "5B" + "b" * 44,
            0.79,
            confirmations=[0.78, 0.79, 0.80],
            seeds=[7, 8, 9],
            minutes=1,
        )
        assert contested_confirmation_set([champ, chall], **self._KW) == []

    def test_new_entrant_does_not_reopen_a_settled_pair(self) -> None:
        # A settled challenger already shares the champion's seeds; a fresh
        # in-band entrant must be confirmed WITHOUT re-scoring the settled pair
        # (champion-anchored seeds do not move when the cohort grows). This is
        # the O(1)-per-entrant property that bounds confirmation cost.
        champ = _e(
            "5A" + "a" * 44, 0.80, confirmations=[0.79, 0.80, 0.81], seeds=[7, 8, 9]
        )
        settled = _e(
            "5B" + "b" * 44,
            0.79,
            confirmations=[0.78, 0.79, 0.80],
            seeds=[7, 8, 9],
            minutes=1,
        )
        entrant = _e("5C" + "c" * 44, 0.795, minutes=2)  # in band, no shared seeds
        got = contested_confirmation_set([champ, settled, entrant], **self._KW)
        # Only the champion (anchor) and the unsettled entrant; the settled
        # challenger is excluded, so it is never re-scored.
        assert [e.agent_id for e in got] == [champ.agent_id, entrant.agent_id]

    def test_many_near_band_entrants_stay_linear(self) -> None:
        # Griefing guard: N in-band challengers, none sharing seeds yet, select
        # the champion once plus the N challengers — never an O(N^2) re-scoring
        # cascade of already-processed members.
        champ = _e("5A" + "a" * 44, 0.80)
        challengers = [
            _e(f"5{chr(66 + i)}" + "b" * 44, 0.795, minutes=i + 1) for i in range(6)
        ]
        got = contested_confirmation_set([champ, *challengers], **self._KW)
        assert got[0].agent_id == champ.agent_id
        assert len(got) == 1 + len(challengers)

    def test_stale_challenger_is_the_version_sweeps_job(self) -> None:
        champ = _e("5A" + "a" * 44, 0.80)
        chall = _e("5B" + "b" * 44, 0.79, minutes=1)
        champ.bench_version = 2
        # challenger carries no bench_version -> DEFAULT_BENCH_VERSION (1) < 2.
        got = contested_confirmation_set(
            [champ, chall], current_version=2, margin=0.02, dethrone_z=0.0
        )
        assert got == []

    def test_stale_champion_defers_to_version_sweep(self) -> None:
        champ = _e("5A" + "a" * 44, 0.80)  # no bench_version -> stale at v2
        chall = _e("5B" + "b" * 44, 0.79, minutes=1)
        chall.bench_version = 2
        got = contested_confirmation_set(
            [champ, chall], current_version=2, margin=0.02, dethrone_z=0.0
        )
        assert got == []

    def test_future_champion_is_not_confirmed_by_older_worker(self) -> None:
        champ = _e("5A" + "a" * 44, 0.80)
        chall = _e("5B" + "b" * 44, 0.79, minutes=1)
        champ.bench_version = 3
        chall.bench_version = 2
        assert (
            contested_confirmation_set(
                [champ, chall], current_version=2, margin=0.02, dethrone_z=0.0
            )
            == []
        )

    def test_future_challenger_is_not_confirmed_by_older_worker(self) -> None:
        champ = _e("5A" + "a" * 44, 0.80)
        chall = _e("5B" + "b" * 44, 0.79, minutes=1)
        champ.bench_version = 2
        chall.bench_version = 3
        assert (
            contested_confirmation_set(
                [champ, chall], current_version=2, margin=0.02, dethrone_z=0.0
            )
            == []
        )

    def test_contest_uses_effective_composites(self) -> None:
        # The champion's raw composite (0.90, a lucky representative run) would
        # put the challenger far outside the band; its confirmation MEDIAN
        # (0.80) is what the fold compares, so the contest must too.
        champ = _e(
            "5A" + "a" * 44, 0.90, confirmations=[0.79, 0.80, 0.81], seeds=[7, 8, 9]
        )
        chall = _e("5B" + "b" * 44, 0.79, minutes=1)
        got = contested_confirmation_set([champ, chall], **self._KW)
        assert [e.agent_id for e in got] == [champ.agent_id, chall.agent_id]

    def test_single_entry_ledger_has_no_contest(self) -> None:
        assert contested_confirmation_set([_e("5A" + "a" * 44, 0.8)], **self._KW) == []
