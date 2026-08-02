"""Contract tests for the public projection of ditto-subnet's KOTH fold."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from ditto.api_models.continual_retest_settings import (
    EMISSION_SET_SIZE,
    MAX_RETEST_COHORT_SIZE,
)
from ditto.api_server.koth import (
    BLOCKS_PER_TEMPO,
    KothEntry,
    effective_composite,
    emission_set,
    project_koth,
    retest_cohort,
    tempo_index,
    top5_round_is_due,
)

_T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _entry(
    marker: int,
    composite: float,
    *,
    minutes: int,
    stderr: float | None = None,
    confirmations: tuple[float, ...] | None = None,
    seeds: tuple[int, ...] | None = None,
    bench_version: int = 1,
    quorum: tuple[float, ...] | None = None,
    waves: tuple[float, ...] | None = None,
    efficiency_bonus: float | None = None,
) -> KothEntry:
    return KothEntry(
        miner_hotkey="5" + str(marker) * 47,
        agent_id=UUID(int=marker),
        composite=composite,
        first_seen=_T0 + timedelta(minutes=minutes),
        raw_rank=marker,
        bench_version=bench_version,
        composite_stderr=stderr,
        quorum_composites=quorum,
        completed_wave_composites=waves,
        confirmation_composites=confirmations,
        confirmation_seeds=seeds,
        efficiency_bonus=efficiency_bonus,
    )


def test_completed_waves_switch_from_quorum_median_to_rolling_mean() -> None:
    entry = _entry(
        1,
        0.8,
        minutes=0,
        quorum=(0.7, 0.8, 0.9),
        waves=(0.5, 1.0),
    )

    assert effective_composite(entry) == pytest.approx(0.78)


def test_partial_or_missing_wave_keeps_canonical_median() -> None:
    entry = _entry(1, 0.8, minutes=0, quorum=(0.7, 0.8, 0.9))

    assert effective_composite(entry) == 0.8


def test_efficiency_bonus_multiplies_the_continual_score() -> None:
    entry = _entry(
        1,
        0.8,
        minutes=0,
        quorum=(0.7, 0.8, 0.9),
        waves=(0.5, 1.0),
        efficiency_bonus=0.1,
    )

    assert effective_composite(entry) == pytest.approx(0.78 * 1.1)


@pytest.mark.parametrize("bench_version", [6, 7, 8])
def test_high_score_band_decay_applies_from_v6_forward(bench_version: int) -> None:
    incumbent = _entry(2, 0.95, minutes=0, bench_version=bench_version)
    challenger = _entry(1, 0.954, minutes=1, bench_version=bench_version)

    projection = project_koth([challenger, incumbent])

    assert projection is not None
    assert projection.champion == challenger


def test_pre_v6_and_mixed_version_comparisons_keep_legacy_band() -> None:
    incumbent_v5 = _entry(2, 0.95, minutes=0, bench_version=5)
    challenger_v5 = _entry(1, 0.954, minutes=1, bench_version=5)
    challenger_v7 = _entry(3, 0.954, minutes=1, bench_version=7)

    pre_v6 = project_koth([challenger_v5, incumbent_v5])
    mixed = project_koth([challenger_v7, incumbent_v5])

    assert pre_v6 is not None and pre_v6.champion == incumbent_v5
    assert mixed is not None and mixed.champion == incumbent_v5


def test_v6_decay_scales_the_paired_uncertainty_band() -> None:
    incumbent = _entry(
        2,
        0.95,
        minutes=0,
        bench_version=6,
        confirmations=(0.94, 0.95, 0.96),
        seeds=(10, 20, 30),
    )
    challenger = _entry(
        1,
        0.96,
        minutes=1,
        bench_version=6,
        confirmations=(0.93, 0.96, 0.985),
        seeds=(10, 20, 30),
    )

    projection = project_koth([challenger, incumbent])

    assert projection is not None
    assert projection.champion == challenger


def test_older_incumbent_survives_a_sub_margin_raw_leader() -> None:
    incumbent = _entry(2, 0.800, minutes=0)
    raw_leader = _entry(1, 0.804, minutes=1)

    projection = project_koth([raw_leader, incumbent])

    assert projection is not None
    assert projection.champion == incumbent
    assert projection.raw_leader == raw_leader
    assert projection.raw_leader_decision is not None
    assert projection.raw_leader_decision.challenger_lead == pytest.approx(0.004)
    assert projection.raw_leader_decision.required_lead == pytest.approx(0.007)
    assert projection.raw_leader_decision.method == "flat"
    assert projection.raw_leader_decision.dethrones is False


def test_statistical_band_matches_validator_unpaired_rule() -> None:
    incumbent = _entry(2, 0.80, minutes=0, stderr=0.03)
    raw_leader = _entry(1, 0.85, minutes=1, stderr=0.03)

    projection = project_koth([raw_leader, incumbent])

    assert projection is not None
    decision = projection.raw_leader_decision
    assert decision is not None
    assert projection.champion == incumbent
    assert decision.margin_lead == pytest.approx(0.007)
    assert decision.statistical_lead == pytest.approx(1.64 * (0.03**2 + 0.03**2) ** 0.5)
    assert decision.required_lead == decision.statistical_lead
    assert decision.method == "unpaired"


def test_clear_challenger_dethrones_and_tail_uses_raw_composite_order() -> None:
    incumbent = _entry(3, 0.80, minutes=0, stderr=0.01)
    challenger = _entry(1, 0.90, minutes=2, stderr=0.01)
    runner_up = _entry(2, 0.85, minutes=1)

    projection = project_koth([runner_up, challenger, incumbent])

    assert projection is not None
    assert projection.champion == challenger
    assert projection.raw_leader_decision is None
    assert projection.tail == (runner_up, incumbent)


def test_fixed_margin_does_not_grow_into_a_ceiling_lock() -> None:
    incumbent = _entry(2, 0.930, minutes=0)
    challenger = _entry(1, 0.938, minutes=1)

    projection = project_koth([challenger, incumbent])

    assert projection is not None
    assert projection.champion == challenger

    exact_boundary = project_koth([_entry(3, 0.937, minutes=1), incumbent])
    assert exact_boundary is not None
    assert exact_boundary.champion == incumbent


def test_confirmation_median_and_paired_seed_band_match_validator_fold() -> None:
    incumbent = _entry(
        2,
        0.80,
        minutes=0,
        confirmations=(0.80, 0.82, 0.78),
        seeds=(10, 20, 30),
    )
    lucky_raw_leader = _entry(
        1,
        0.90,
        minutes=1,
        confirmations=(0.804, 0.824, 0.784),
        seeds=(10, 20, 30),
    )

    projection = project_koth([lucky_raw_leader, incumbent])

    assert projection is not None
    assert projection.champion == incumbent
    decision = projection.raw_leader_decision
    assert decision is not None
    assert decision.method == "paired"
    assert decision.challenger_lead == pytest.approx(0.004)
    assert decision.margin_lead == pytest.approx(0.007)
    assert decision.dethrones is False


def test_efficiency_bonus_applies_inside_paired_seed_comparison() -> None:
    incumbent = _entry(
        2,
        0.80,
        minutes=0,
        confirmations=(0.80, 0.80, 0.80),
        seeds=(10, 20, 30),
    )
    challenger = _entry(
        1,
        0.78,
        minutes=1,
        confirmations=(0.78, 0.78, 0.78),
        seeds=(10, 20, 30),
        efficiency_bonus=0.1,
    )

    projection = project_koth([challenger, incumbent])

    assert projection is not None
    assert projection.champion == challenger


def test_empty_or_non_positive_pool_has_no_projection() -> None:
    assert project_koth([]) is None
    assert project_koth([_entry(1, 0.0, minutes=0)]) is None


def test_emission_set_is_champion_plus_four_distinct_miner_tail() -> None:
    # Oldest + highest composite is the champion; five others trail it.
    champion = _entry(1, 0.90, minutes=0)
    tail = [
        _entry(2, 0.88, minutes=1),
        _entry(3, 0.86, minutes=2),
        _entry(4, 0.84, minutes=3),
        _entry(5, 0.82, minutes=4),
    ]
    sixth = _entry(6, 0.80, minutes=5)

    members = emission_set(project_koth([champion, *tail, sixth]))

    assert len(members) == 5
    assert members[0].agent_id == champion.agent_id
    assert {m.agent_id for m in members} == {UUID(int=i) for i in range(1, 6)}
    # The set follows the top five: the sixth-place agent is not in the lane.
    assert sixth.agent_id not in {m.agent_id for m in members}


def test_emission_set_admits_a_new_top_five_entrant() -> None:
    champion = _entry(1, 0.90, minutes=0)
    incumbents = [
        _entry(2, 0.88, minutes=1),
        _entry(3, 0.86, minutes=2),
        _entry(4, 0.84, minutes=3),
        _entry(5, 0.82, minutes=4),
    ]
    before = emission_set(project_koth([champion, *incumbents]))
    assert {m.agent_id for m in before} == {UUID(int=i) for i in range(1, 6)}

    # A fresh entrant scoring above the weakest tail member joins automatically
    # and evicts agent 5 (0.82); membership follows the set with no manual list.
    newcomer = _entry(6, 0.85, minutes=5)
    after = emission_set(project_koth([champion, *incumbents, newcomer]))

    assert newcomer.agent_id in {m.agent_id for m in after}
    assert UUID(int=5) not in {m.agent_id for m in after}
    assert len(after) == 5


def test_emission_set_empty_pool_is_empty() -> None:
    assert emission_set(None) == ()
    assert emission_set(project_koth([])) == ()


def _ranked_pool(count: int) -> list[KothEntry]:
    return [
        _entry(rank, 0.90 - rank / 100, minutes=rank) for rank in range(1, count + 1)
    ]


def test_retest_cohort_of_five_is_exactly_the_emission_set() -> None:
    """The dial's floor must be byte-identical to the historical lane."""
    pool = _ranked_pool(12)
    projection = project_koth(pool)

    assert retest_cohort(pool, projection, size=EMISSION_SET_SIZE) == emission_set(
        projection
    )


def test_retest_cohort_extends_down_the_ranking_in_fold_order() -> None:
    pool = _ranked_pool(30)
    projection = project_koth(pool)

    top10 = retest_cohort(pool, projection, size=10)
    assert [member.agent_id for member in top10] == [UUID(int=i) for i in range(1, 11)]
    # The champion stays the anchor at every width; only the depth changes.
    assert top10[0].agent_id == emission_set(projection)[0].agent_id
    assert [member.agent_id for member in top10[:5]] == [
        member.agent_id for member in emission_set(projection)
    ]

    top25 = retest_cohort(pool, projection, size=MAX_RETEST_COHORT_SIZE)
    assert len(top25) == 25
    assert [member.agent_id for member in top25[:10]] == [
        member.agent_id for member in top10
    ]


def test_retest_cohort_is_capped_by_the_field_not_the_dial() -> None:
    """Asking for 25 in a field of three yields three, not an error."""
    pool = _ranked_pool(3)
    projection = project_koth(pool)

    cohort = retest_cohort(pool, projection, size=25)

    assert len(cohort) == 3
    assert retest_cohort([], None, size=25) == ()


class TestTieTolerantEligibility:
    """The band exists so a rank cutoff cannot split an identical score."""

    def test_the_band_is_off_unless_a_ceiling_is_given(self) -> None:
        """What ships must be byte-identical to the fixed rank cutoff."""
        pool = _ranked_pool(30)
        projection = project_koth(pool)

        fixed = retest_cohort(pool, projection, size=10)

        assert retest_cohort(pool, projection, size=10, tolerance_z=1.64) == fixed
        assert retest_cohort(pool, projection, size=10, max_size=None) == fixed
        # A ceiling at or below the size is not a band either.
        assert retest_cohort(pool, projection, size=10, max_size=10) == fixed

    def test_an_exact_tie_at_the_boundary_is_never_split(self) -> None:
        """Peyton's case: number 11 holds the same score as number 10.

        No stderr anywhere and a tolerance of zero -- the narrowest possible
        band. An exact tie still has to come in, because the only thing that
        separated the two was ``first_seen``.
        """
        pool = [_entry(rank, 0.90 - rank / 100, minutes=rank) for rank in range(1, 10)]
        tenth = _entry(10, 0.50, minutes=10)
        eleventh = _entry(11, 0.50, minutes=11)
        twelfth = _entry(12, 0.40, minutes=12)
        pool += [tenth, eleventh, twelfth]
        projection = project_koth(pool)

        assert len(retest_cohort(pool, projection, size=10)) == 10
        cohort = retest_cohort(pool, projection, size=10, max_size=25, tolerance_z=0.0)

        ids = [member.agent_id for member in cohort]
        assert ids[:10] == [UUID(int=i) for i in range(1, 11)]
        assert UUID(int=11) in ids
        # 12 is genuinely behind, so the band stops there rather than running on.
        assert UUID(int=12) not in ids

    def test_the_band_admits_what_the_dethrone_test_would_call_undecided(
        self,
    ) -> None:
        """Same z, same arithmetic as the fold's own unpaired comparison."""
        pool = [
            _entry(rank, 0.90 - rank / 100, minutes=rank, stderr=0.01)
            for rank in range(1, 6)
        ]
        # Cutoff sits at 0.85. sqrt(0.01^2 + 0.01^2) * 1.64 ~= 0.0232, so 0.84 is
        # inside the band and 0.80 is well outside it.
        near = _entry(6, 0.84, minutes=6, stderr=0.01)
        far = _entry(7, 0.80, minutes=7, stderr=0.01)
        pool += [near, far]
        projection = project_koth(pool)

        cohort = retest_cohort(pool, projection, size=5, max_size=25, tolerance_z=1.64)

        ids = [member.agent_id for member in cohort]
        assert UUID(int=6) in ids
        assert UUID(int=7) not in ids

    def test_a_tighter_measurement_narrows_the_band_on_its_own(self) -> None:
        """The self-adjusting property: more evidence, a smaller cohort.

        This is why the band is stated in standard errors rather than composite
        points. Nothing is retuned; the same z admits fewer agents once the
        measurements get good.
        """
        base = [
            _entry(rank, 0.90 - rank / 100, minutes=rank, stderr=0.01)
            for rank in range(1, 6)
        ]
        candidate_composite = 0.84

        noisy = [*base, _entry(6, candidate_composite, minutes=6, stderr=0.01)]
        precise_base = [
            _entry(rank, 0.90 - rank / 100, minutes=rank, stderr=0.001)
            for rank in range(1, 6)
        ]
        precise = [
            *precise_base,
            _entry(6, candidate_composite, minutes=6, stderr=0.001),
        ]

        wide = retest_cohort(
            noisy, project_koth(noisy), size=5, max_size=25, tolerance_z=1.64
        )
        narrow = retest_cohort(
            precise, project_koth(precise), size=5, max_size=25, tolerance_z=1.64
        )

        assert len(wide) == 6
        assert len(narrow) == 5

    def test_the_ceiling_bounds_a_pathologically_flat_field(self) -> None:
        """An unbounded band would sweep the whole leaderboard into retests."""
        pool = [_entry(rank, 0.80, minutes=rank) for rank in range(1, 41)]
        projection = project_koth(pool)

        cohort = retest_cohort(
            pool,
            projection,
            size=EMISSION_SET_SIZE,
            max_size=MAX_RETEST_COHORT_SIZE,
            tolerance_z=3.0,
        )

        assert len(cohort) == MAX_RETEST_COHORT_SIZE

    def test_the_band_does_not_chain_down_the_leaderboard(self) -> None:
        """Every extension is measured against the cutoff, not its predecessor.

        A transitive walk would let a smooth gradient admit everyone: each agent
        indistinguishable from the one above it, all the way down.
        """
        pool = [_entry(rank, 0.90 - rank / 100, minutes=rank) for rank in range(1, 5)]
        # A staircase of exact 0.01 steps below the cutoff. Each step is tied
        # with its neighbour under a 0.01-wide band but not with the cutoff.
        pool += [
            _entry(5, 0.50, minutes=5, stderr=0.0),
            _entry(6, 0.49, minutes=6, stderr=0.0),
            _entry(7, 0.48, minutes=7, stderr=0.0),
            _entry(8, 0.47, minutes=8, stderr=0.0),
        ]
        projection = project_koth(pool)

        cohort = retest_cohort(pool, projection, size=5, max_size=25, tolerance_z=0.0)

        # Only the fixed five: 6 is 0.01 behind the cutoff with zero tolerance.
        assert len(cohort) == 5

    def test_the_floor_still_holds_under_the_band(self) -> None:
        """The band only ever adds; it can never cut below the emission set."""
        pool = _ranked_pool(12)
        projection = project_koth(pool)

        cohort = retest_cohort(
            pool,
            projection,
            size=EMISSION_SET_SIZE,
            max_size=MAX_RETEST_COHORT_SIZE,
            tolerance_z=1.64,
        )

        assert len(cohort) >= EMISSION_SET_SIZE
        assert [member.agent_id for member in cohort[:EMISSION_SET_SIZE]] == [
            member.agent_id for member in emission_set(projection)
        ]


def test_retest_cohort_ranks_on_the_same_effective_composite_as_the_fold() -> None:
    """Completed waves move cohort membership exactly as they move the tail."""
    champion = _entry(1, 0.90, minutes=0)
    # Raw composite would rank 3 above 2; the wave evidence reverses that, and
    # the cohort must follow the estimator the crown itself is decided on.
    # mean(0.70, 0.70, 0.70, 0.95) = 0.7625, above agent 3's raw 0.75.
    second = _entry(2, 0.70, minutes=1, quorum=(0.70, 0.70, 0.70), waves=(0.95,))
    third = _entry(3, 0.75, minutes=2)
    pool = [champion, second, third]

    cohort = retest_cohort(pool, project_koth(pool), size=3)

    assert [member.agent_id for member in cohort] == [
        UUID(int=1),
        UUID(int=2),
        UUID(int=3),
    ]


def _due_reign_tempos(
    max_tempo: int, *, base: int, doubling_k: int, cap: int, crown_block: int = 0
) -> list[int]:
    """Reign-tempos (from the crown) at which a round is due, for assertions."""
    return [
        t
        for t in range(max_tempo + 1)
        if top5_round_is_due(
            crown_block + t * BLOCKS_PER_TEMPO,
            crown_block,
            base=base,
            doubling_k=doubling_k,
            cap=cap,
        )
    ]


def test_backoff_is_deterministic_across_validators() -> None:
    # Two independent "validators" reading the same chain height + crown block
    # get byte-identical due decisions -- pure function, no clock, no RNG.
    for block in (0, 720, 5000, 100_000):
        a = top5_round_is_due(block, 0, base=2, doubling_k=20, cap=8)
        b = top5_round_is_due(block, 0, base=2, doubling_k=20, cap=8)
        assert a == b


def test_backoff_is_dense_early_and_sparse_late() -> None:
    # base=2, K=20, cap=8: interval holds at 2 for the first 20 reign-tempos
    # (front-loading the ~24h reveal window), then doubles to 4, then caps at 8.
    due = _due_reign_tempos(80, base=2, doubling_k=20, cap=8)
    # Dense early: every 2 tempos through the first ~20.
    assert due[:11] == [0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20]
    # After 20 reign-tempos the interval doubles to 4.
    assert 24 in due and 22 not in due
    # Gaps only grow, and never exceed the cap of 8 tempos (never zero-rate).
    gaps = [b - a for a, b in zip(due, due[1:], strict=False)]
    assert gaps == sorted(gaps)
    assert max(gaps) == 8
    assert min(gaps) == 2


def test_backoff_caps_the_interval() -> None:
    # Far into a long reign the interval flatlines at the cap; rounds keep firing.
    due = _due_reign_tempos(400, base=2, doubling_k=20, cap=8)
    tail_gaps = [b - a for a, b in zip(due, due[1:], strict=False)][-10:]
    assert set(tail_gaps) == {8}


def test_backoff_resets_on_king_change() -> None:
    # A new champion (new crown block) re-enters the dense regime: block that was
    # sparse-late under the old crown is due-at-offset-0 under the new one.
    old_crown = 0
    new_crown = 900 * BLOCKS_PER_TEMPO  # a fresh coronation far later
    # At exactly the new crown block, reign-tempo 0 -> due.
    assert top5_round_is_due(new_crown, new_crown, base=2, doubling_k=20, cap=8)
    # The same height under the old, long crown is on the sparse cap schedule and
    # is (generally) not a scheduled point -- the reset changes the answer.
    old_due = top5_round_is_due(new_crown, old_crown, base=2, doubling_k=20, cap=8)
    new_due = top5_round_is_due(new_crown, new_crown, base=2, doubling_k=20, cap=8)
    assert new_due is True
    assert old_due is False


def test_backoff_disabled_when_base_non_positive() -> None:
    assert top5_round_is_due(0, 0, base=0, doubling_k=20, cap=8) is False
    assert top5_round_is_due(1440, 0, base=-1, doubling_k=20, cap=8) is False


def test_tempo_index_counts_360_block_windows() -> None:
    assert tempo_index(0) == 0
    assert tempo_index(359) == 0
    assert tempo_index(360) == 1
    assert tempo_index(1440) == 4
