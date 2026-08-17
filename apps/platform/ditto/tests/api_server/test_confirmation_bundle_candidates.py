from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ditto.api_server.confirmation_bundles import (
    ConfirmationCandidate,
    ConfirmationEligibilityMode,
    ConfirmationMode,
    ConfirmationPolicyError,
    ConfirmationState,
    full_confirmation_eligible,
    plausibly_crosses_base_cutoff,
    reward_eligible,
    select_confirmation_candidates,
)

_T0 = datetime(2026, 8, 8, 12, tzinfo=UTC)


def candidate(
    index: int,
    score: float,
    *,
    owner: str | None = None,
    stderr: float | None = 0.01,
    state: ConfirmationState = ConfirmationState.BASE_ONLY,
    full: float | None = None,
    version: int = 9,
    digest: str | None = None,
    minute: int | None = None,
) -> ConfirmationCandidate:
    return ConfirmationCandidate(
        agent_id=f"agent-{index:02d}",
        owner_id=owner or f"owner-{index:02d}",
        artifact_sha256=(f"{index + 1:064x}" if digest is None else digest),
        bench_version=version,
        base_composite=score,
        base_stderr=stderr,
        first_seen=_T0 + timedelta(minutes=index if minute is None else minute),
        confirmation_state=state,
        full_composite=full,
    )


def ids(rows: tuple[ConfirmationCandidate, ...]) -> list[str]:
    return [row.agent_id for row in rows]


class TestCandidateValidation:
    @pytest.mark.parametrize("agent_id", ["", None])
    def test_requires_agent_id(self, agent_id: str | None) -> None:
        with pytest.raises(ConfirmationPolicyError, match="agent and owner ids"):
            ConfirmationCandidate(
                agent_id=agent_id,  # type: ignore[arg-type]
                owner_id="owner",
                artifact_sha256="a" * 64,
                bench_version=9,
                base_composite=0.8,
                base_stderr=0.01,
                first_seen=_T0,
            )

    @pytest.mark.parametrize("owner_id", ["", None])
    def test_requires_owner_id(self, owner_id: str | None) -> None:
        with pytest.raises(ConfirmationPolicyError, match="agent and owner ids"):
            ConfirmationCandidate(
                agent_id="agent",
                owner_id=owner_id,  # type: ignore[arg-type]
                artifact_sha256="a" * 64,
                bench_version=9,
                base_composite=0.8,
                base_stderr=0.01,
                first_seen=_T0,
            )

    @pytest.mark.parametrize(
        "digest",
        [
            "",
            "a" * 63,
            "a" * 65,
            "A" * 64,
            "g" * 64,
            "a" * 63 + " ",
        ],
    )
    def test_requires_canonical_artifact_digest(self, digest: str) -> None:
        with pytest.raises(ConfirmationPolicyError, match="artifact sha256"):
            candidate(1, 0.8, digest=digest)

    @pytest.mark.parametrize("version", [0, -1, True, 1_000_001])
    def test_rejects_invalid_bench_version(self, version: int) -> None:
        with pytest.raises(ConfirmationPolicyError, match="bench_version"):
            candidate(1, 0.8, version=version)

    @pytest.mark.parametrize("score", [-0.001, 1.001, float("nan"), float("inf")])
    def test_rejects_invalid_base_composite(self, score: float) -> None:
        with pytest.raises(ConfirmationPolicyError, match="base_composite"):
            candidate(1, score)

    @pytest.mark.parametrize(
        "stderr", [-0.001, 1.001, float("nan"), float("inf"), True]
    )
    def test_rejects_invalid_base_stderr(self, stderr: float) -> None:
        with pytest.raises(ConfirmationPolicyError, match="base_stderr"):
            candidate(1, 0.8, stderr=stderr)

    def test_accepts_missing_base_stderr_for_visible_fail_closed_handling(self) -> None:
        row = candidate(1, 0.8, stderr=None)
        assert row.base_stderr is None

    @pytest.mark.parametrize("full", [-0.001, 1.001, float("nan"), float("inf")])
    def test_rejects_invalid_full_composite(self, full: float) -> None:
        with pytest.raises(ConfirmationPolicyError, match="full_composite"):
            candidate(1, 0.8, full=full)

    def test_requires_timezone_aware_first_seen(self) -> None:
        with pytest.raises(ConfirmationPolicyError, match="timezone-aware"):
            ConfirmationCandidate(
                agent_id="agent",
                owner_id="owner",
                artifact_sha256="a" * 64,
                bench_version=9,
                base_composite=0.8,
                base_stderr=0.01,
                first_seen=datetime(2026, 8, 8, 12),
            )

    def test_requires_enum_state(self) -> None:
        with pytest.raises(ConfirmationPolicyError, match="ConfirmationState"):
            candidate(1, 0.8, state="completed")  # type: ignore[arg-type]


class TestRewardEligibility:
    @pytest.mark.parametrize("version", range(1, 9))
    @pytest.mark.parametrize("mode", list(ConfirmationMode))
    def test_pre_v9_remains_authoritatively_eligible_without_bundle(
        self, version: int, mode: ConfirmationMode
    ) -> None:
        assert reward_eligible(candidate(1, 0.8, version=version), mode=mode)

    @pytest.mark.parametrize("mode", [ConfirmationMode.OFF, ConfirmationMode.SHADOW])
    def test_nonactivation_modes_preserve_existing_v9_base_authority(
        self, mode: ConfirmationMode
    ) -> None:
        assert reward_eligible(candidate(1, 0.8), mode=mode)
        assert not full_confirmation_eligible(candidate(1, 0.8))

    @pytest.mark.parametrize(
        "state",
        [
            ConfirmationState.BASE_ONLY,
            ConfirmationState.BLOCKED_BUDGET,
            ConfirmationState.PENDING,
            ConfirmationState.LEASED,
            ConfirmationState.FAILED,
            ConfirmationState.SUPERSEDED,
        ],
    )
    def test_v9_noncompleted_state_is_never_reward_eligible(
        self, state: ConfirmationState
    ) -> None:
        row = candidate(1, 0.99, state=state, full=0.99)
        assert not full_confirmation_eligible(row)
        assert not reward_eligible(row, mode=ConfirmationMode.ENFORCE)

    def test_completed_without_full_composite_fails_closed(self) -> None:
        row = candidate(1, 0.99, state=ConfirmationState.COMPLETED, full=None)
        assert not full_confirmation_eligible(row)
        assert not reward_eligible(row, mode=ConfirmationMode.ENFORCE)

    @pytest.mark.parametrize("full", [0.0, 0.5, 1.0])
    def test_completed_valid_full_composite_is_eligible(self, full: float) -> None:
        row = candidate(1, 0.7, state=ConfirmationState.COMPLETED, full=full)
        assert full_confirmation_eligible(row)
        assert reward_eligible(row, mode=ConfirmationMode.ENFORCE)


class TestBaseCutoffComparison:
    def test_score_above_cutoff_crosses_even_without_uncertainty(self) -> None:
        challenger = candidate(2, 0.81, stderr=None)
        cutoff = candidate(1, 0.80, stderr=None)
        assert plausibly_crosses_base_cutoff(challenger, cutoff, challenger_z=1.64) == (
            True,
            False,
        )

    def test_exact_tie_crosses_even_without_uncertainty(self) -> None:
        challenger = candidate(2, 0.80, stderr=None)
        cutoff = candidate(1, 0.80, stderr=None)
        assert plausibly_crosses_base_cutoff(challenger, cutoff, challenger_z=1.64) == (
            True,
            False,
        )

    def test_lower_score_missing_candidate_uncertainty_fails_closed(self) -> None:
        challenger = candidate(2, 0.79, stderr=None)
        cutoff = candidate(1, 0.80, stderr=0.01)
        assert plausibly_crosses_base_cutoff(challenger, cutoff, challenger_z=1.64) == (
            False,
            True,
        )

    def test_lower_score_missing_cutoff_uncertainty_fails_closed(self) -> None:
        challenger = candidate(2, 0.79, stderr=0.01)
        cutoff = candidate(1, 0.80, stderr=None)
        assert plausibly_crosses_base_cutoff(challenger, cutoff, challenger_z=1.64) == (
            False,
            True,
        )

    def test_boundary_is_inclusive(self) -> None:
        stderr = 0.01
        tolerance = 1.64 * (2 * stderr**2) ** 0.5
        cutoff = candidate(1, 0.80, stderr=stderr)
        challenger = candidate(2, 0.80 - tolerance, stderr=stderr)
        crosses, missing = plausibly_crosses_base_cutoff(
            challenger, cutoff, challenger_z=1.64
        )
        assert crosses
        assert not missing

    def test_just_outside_boundary_is_rejected(self) -> None:
        stderr = 0.01
        tolerance = 1.64 * (2 * stderr**2) ** 0.5
        cutoff = candidate(1, 0.80, stderr=stderr)
        challenger = candidate(2, 0.80 - tolerance - 1e-12, stderr=stderr)
        assert plausibly_crosses_base_cutoff(challenger, cutoff, challenger_z=1.64) == (
            False,
            False,
        )

    def test_zero_z_admits_only_equal_or_higher(self) -> None:
        cutoff = candidate(1, 0.80, stderr=0.5)
        lower = candidate(2, 0.799999, stderr=0.5)
        tied = candidate(3, 0.80, stderr=0.0)
        assert plausibly_crosses_base_cutoff(lower, cutoff, challenger_z=0.0) == (
            False,
            False,
        )
        assert plausibly_crosses_base_cutoff(tied, cutoff, challenger_z=0.0) == (
            True,
            False,
        )

    def test_rejects_mixed_versions(self) -> None:
        with pytest.raises(ConfirmationPolicyError, match="mix bench versions"):
            plausibly_crosses_base_cutoff(
                candidate(1, 0.8, version=9),
                candidate(2, 0.8, version=10),
                challenger_z=1.64,
            )

    @pytest.mark.parametrize("z", [-0.1, 3.1, float("nan"), float("inf"), True])
    def test_rejects_invalid_z(self, z: float) -> None:
        with pytest.raises(ConfirmationPolicyError, match="challenger_z"):
            plausibly_crosses_base_cutoff(
                candidate(1, 0.8), candidate(2, 0.8), challenger_z=z
            )


class TestOwnerGrouping:
    def test_one_base_representative_per_owner(self) -> None:
        rows = [
            candidate(1, 0.90, owner="same"),
            candidate(2, 0.80, owner="same"),
            candidate(3, 0.70, owner="other"),
        ]
        selection = select_confirmation_candidates(rows, top_n=5)
        assert ids(selection.base_board) == ["agent-01", "agent-03"]

    def test_highest_base_score_wins_owner_slot(self) -> None:
        rows = [
            candidate(1, 0.70, owner="same", minute=0),
            candidate(2, 0.90, owner="same", minute=1),
        ]
        assert ids(select_confirmation_candidates(rows).base_board) == ["agent-02"]

    def test_earliest_first_seen_breaks_equal_base_tie(self) -> None:
        rows = [
            candidate(1, 0.90, owner="same", minute=5),
            candidate(2, 0.90, owner="same", minute=1),
        ]
        assert ids(select_confirmation_candidates(rows).base_board) == ["agent-02"]

    def test_agent_id_breaks_equal_score_and_time_tie(self) -> None:
        rows = [
            candidate(2, 0.90, owner="same", minute=1),
            candidate(1, 0.90, owner="same", minute=1),
        ]
        assert ids(select_confirmation_candidates(rows).base_board) == ["agent-01"]

    def test_confirmed_owner_representative_uses_full_axis(self) -> None:
        rows = [
            candidate(
                1,
                0.95,
                owner="same",
                state=ConfirmationState.COMPLETED,
                full=0.70,
            ),
            candidate(
                2,
                0.80,
                owner="same",
                state=ConfirmationState.COMPLETED,
                full=0.90,
            ),
        ]
        selection = select_confirmation_candidates(rows)
        assert ids(selection.base_board) == ["agent-01"]
        assert ids(selection.confirmed_board) == ["agent-02"]

    def test_ineligible_higher_full_score_cannot_take_confirmed_owner_slot(
        self,
    ) -> None:
        rows = [
            candidate(
                1,
                0.95,
                owner="same",
                state=ConfirmationState.BASE_ONLY,
                full=0.99,
            ),
            candidate(
                2,
                0.80,
                owner="same",
                state=ConfirmationState.COMPLETED,
                full=0.70,
            ),
        ]
        selection = select_confirmation_candidates(rows)
        assert ids(selection.confirmed_board) == ["agent-02"]

    def test_input_order_does_not_change_grouping(self) -> None:
        rows = [
            candidate(1, 0.9, owner="a"),
            candidate(2, 0.8, owner="b"),
            candidate(3, 0.7, owner="c"),
        ]
        forward = select_confirmation_candidates(rows)
        reverse = select_confirmation_candidates(reversed(rows))
        assert forward == reverse


class TestCandidateSelection:
    def test_score_threshold_selects_every_owner_at_or_above_fixed_score(self) -> None:
        rows = [
            candidate(1, 0.99),
            candidate(2, 0.95),
            candidate(3, 0.949999),
            candidate(4, 0.20),
        ]
        selection = select_confirmation_candidates(
            rows,
            eligibility_mode=ConfirmationEligibilityMode.SCORE_THRESHOLD,
            min_base_score_micros=950_000,
        )
        assert ids(selection.selected) == ["agent-01", "agent-02"]
        assert selection.threshold_agent_ids == frozenset({"agent-01", "agent-02"})
        assert selection.top_n_agent_ids == frozenset()
        assert selection.challenger_agent_ids == frozenset()
        assert selection.cutoff_source == "score_threshold"

    def test_score_threshold_applies_after_owner_grouping(self) -> None:
        rows = [
            candidate(1, 0.96, owner="same"),
            candidate(2, 0.99, owner="same"),
            candidate(3, 0.951, owner="other"),
        ]
        selection = select_confirmation_candidates(
            rows,
            eligibility_mode=ConfirmationEligibilityMode.SCORE_THRESHOLD,
            min_base_score_micros=950_000,
        )
        assert ids(selection.selected) == ["agent-02", "agent-03"]

    def test_score_threshold_can_select_more_than_legacy_top_n_cap(self) -> None:
        rows = [candidate(i, 0.99 - (i / 10_000)) for i in range(1, 20)]
        selection = select_confirmation_candidates(
            rows,
            top_n=1,
            eligibility_mode=ConfirmationEligibilityMode.SCORE_THRESHOLD,
            min_base_score_micros=950_000,
        )
        assert len(selection.selected) == 19

    @pytest.mark.parametrize("threshold", [-1, 1_000_001, True, 0.95])
    def test_rejects_invalid_score_threshold(self, threshold: object) -> None:
        with pytest.raises(ConfirmationPolicyError, match="min_base_score_micros"):
            select_confirmation_candidates(
                [candidate(1, 0.99)],
                eligibility_mode=ConfirmationEligibilityMode.SCORE_THRESHOLD,
                min_base_score_micros=threshold,  # type: ignore[arg-type]
            )

    @pytest.mark.parametrize("mode", ["rank", "score_threshold", None, True])
    def test_rejects_untyped_eligibility_mode(self, mode: object) -> None:
        with pytest.raises(
            ConfirmationPolicyError, match="ConfirmationEligibilityMode"
        ):
            select_confirmation_candidates(
                [candidate(1, 0.99)],
                eligibility_mode=mode,  # type: ignore[arg-type]
            )

    def test_empty_pool_has_no_frontier(self) -> None:
        selection = select_confirmation_candidates([], top_n=5)
        assert selection.base_board == ()
        assert selection.confirmed_board == ()
        assert selection.selected == ()
        assert selection.cutoff is None
        assert selection.cutoff_source == "none"

    def test_fewer_than_n_selects_every_owner_without_fake_cutoff(self) -> None:
        rows = [candidate(1, 0.9), candidate(2, 0.8), candidate(3, 0.7)]
        selection = select_confirmation_candidates(rows, top_n=5)
        assert ids(selection.selected) == ["agent-01", "agent-02", "agent-03"]
        assert selection.cutoff is None
        assert selection.cutoff_source == "none"
        assert selection.challenger_agent_ids == frozenset()

    def test_exactly_n_uses_base_frontier_until_n_are_confirmed(self) -> None:
        rows = [candidate(i, 1 - i / 100) for i in range(1, 6)]
        selection = select_confirmation_candidates(rows, top_n=5)
        assert selection.cutoff == rows[-1]
        assert selection.cutoff_source == "base_frontier"
        assert ids(selection.selected) == [f"agent-{i:02d}" for i in range(1, 6)]

    def test_top_n_selected_regardless_of_missing_uncertainty(self) -> None:
        rows = [candidate(i, 1 - i / 100, stderr=None) for i in range(1, 7)]
        selection = select_confirmation_candidates(rows, top_n=5)
        assert ids(selection.selected) == [f"agent-{i:02d}" for i in range(1, 6)]
        assert selection.missing_uncertainty_agent_ids == frozenset({"agent-06"})

    def test_challenger_crossing_base_frontier_is_added(self) -> None:
        rows = [
            candidate(1, 0.90, stderr=0.001),
            candidate(2, 0.80, stderr=0.001),
            candidate(3, 0.70, stderr=0.001),
            candidate(4, 0.60, stderr=0.001),
            candidate(5, 0.50, stderr=0.02),
            candidate(6, 0.49, stderr=0.02),
        ]
        selection = select_confirmation_candidates(rows, top_n=5)
        assert selection.challenger_agent_ids == frozenset({"agent-06"})
        assert ids(selection.selected)[-1] == "agent-06"

    def test_challenger_outside_base_frontier_is_not_added(self) -> None:
        rows = [
            candidate(1, 0.90, stderr=0.001),
            candidate(2, 0.80, stderr=0.001),
            candidate(3, 0.70, stderr=0.001),
            candidate(4, 0.60, stderr=0.001),
            candidate(5, 0.50, stderr=0.001),
            candidate(6, 0.49, stderr=0.001),
        ]
        selection = select_confirmation_candidates(rows, top_n=5)
        assert selection.challenger_agent_ids == frozenset()
        assert "agent-06" not in ids(selection.selected)

    def test_tied_sixth_is_not_split_by_age_tiebreak(self) -> None:
        rows = [candidate(i, 1 - i / 100) for i in range(1, 5)]
        rows.extend([candidate(5, 0.50, stderr=None), candidate(6, 0.50, stderr=None)])
        selection = select_confirmation_candidates(rows, top_n=5)
        assert ids(selection.selected) == [
            "agent-01",
            "agent-02",
            "agent-03",
            "agent-04",
            "agent-05",
            "agent-06",
        ]
        assert selection.challenger_agent_ids == frozenset({"agent-06"})

    def test_n_confirmed_switches_to_confirmed_frontier(self) -> None:
        rows = [
            candidate(
                i,
                1 - i / 100,
                state=ConfirmationState.COMPLETED,
                full=0.5 + i / 100,
            )
            for i in range(1, 6)
        ]
        rows.append(candidate(6, 0.40, stderr=0.001))
        selection = select_confirmation_candidates(rows, top_n=5)
        assert selection.cutoff_source == "confirmed_frontier"
        # Full scores rank agent 5 first and agent 1 fifth. The cutoff comparison
        # therefore uses agent 1's base score, never its 0.51 full composite.
        assert selection.cutoff is not None
        assert selection.cutoff.agent_id == "agent-01"
        assert selection.cutoff.base_composite == pytest.approx(0.99)

    def test_confirmed_frontier_comparison_stays_on_base_axis(self) -> None:
        confirmed = [
            candidate(
                i,
                0.90 - i / 100,
                stderr=0.001,
                state=ConfirmationState.COMPLETED,
                full=0.90 - i / 100,
            )
            for i in range(1, 6)
        ]
        # Nth full-confirmed is agent 5 with base 0.85. A challenger at base
        # 0.84 cannot cross at narrow uncertainty. Comparing 0.84 to some full
        # composite scale other than the cutoff's base would be a contract bug.
        challenger = candidate(6, 0.84, stderr=0.001)
        selection = select_confirmation_candidates([*confirmed, challenger], top_n=5)
        assert selection.cutoff is not None
        assert selection.cutoff.base_composite == pytest.approx(0.85)
        assert "agent-06" not in selection.challenger_agent_ids

    def test_confirmed_frontier_can_promote_challenger_outside_base_top_n(self) -> None:
        confirmed = [
            candidate(
                i,
                0.90 - i / 100,
                stderr=0.02,
                state=ConfirmationState.COMPLETED,
                full=0.90 - i / 100,
            )
            for i in range(1, 6)
        ]
        challenger = candidate(6, 0.84, stderr=0.02)
        selection = select_confirmation_candidates([*confirmed, challenger], top_n=5)
        assert selection.challenger_agent_ids == frozenset({"agent-06"})

    def test_pending_contains_only_not_reward_eligible_selected_rows(self) -> None:
        rows = [
            candidate(
                1,
                0.90,
                state=ConfirmationState.COMPLETED,
                full=0.90,
            ),
            candidate(2, 0.80),
        ]
        selection = select_confirmation_candidates(rows, top_n=2)
        assert ids(selection.pending) == ["agent-02"]

    def test_same_digest_does_not_collapse_different_owners_at_selection_time(
        self,
    ) -> None:
        digest = "d" * 64
        rows = [
            candidate(1, 0.90, owner="a", digest=digest),
            candidate(2, 0.80, owner="b", digest=digest),
        ]
        selection = select_confirmation_candidates(rows, top_n=5)
        assert ids(selection.selected) == ["agent-01", "agent-02"]
        # Bundle-key dedupe, tested separately, reuses the paid evidence. Owner
        # grouping must not erase either public submission from candidate state.

    def test_different_digest_same_owner_uses_only_best_base_submission(self) -> None:
        rows = [
            candidate(1, 0.90, owner="a", digest="a" * 64),
            candidate(2, 0.91, owner="a", digest="b" * 64),
        ]
        selection = select_confirmation_candidates(rows)
        assert ids(selection.selected) == ["agent-02"]

    def test_all_plausible_challengers_are_selected_deterministically(self) -> None:
        rows = [candidate(i, 0.9 - i / 100, stderr=0.05) for i in range(1, 15)]
        selection = select_confirmation_candidates(rows, top_n=5)
        assert ids(selection.selected) == [f"agent-{i:02d}" for i in range(1, 15)]

    def test_challenger_order_remains_base_rank_order(self) -> None:
        rows = [candidate(i, 0.9 - i / 100, stderr=0.05) for i in range(1, 9)]
        selection = select_confirmation_candidates(rows, top_n=5)
        assert ids(selection.selected) == [f"agent-{i:02d}" for i in range(1, 9)]

    def test_rejects_mixed_benchmark_versions(self) -> None:
        with pytest.raises(ConfirmationPolicyError, match="mix bench versions"):
            select_confirmation_candidates(
                [candidate(1, 0.9, version=9), candidate(2, 0.8, version=10)]
            )

    @pytest.mark.parametrize("version", [1, 8])
    def test_rejects_pre_contract_candidate_axis(self, version: int) -> None:
        with pytest.raises(ConfirmationPolicyError, match="carrying base evidence"):
            select_confirmation_candidates([candidate(1, 0.9, version=version)])

    @pytest.mark.parametrize("version", [9, 10, 11])
    def test_accepts_any_confirmation_capable_axis(self, version: int) -> None:
        """Selection follows the live benchmark instead of one frozen epoch."""
        selection = select_confirmation_candidates([candidate(1, 0.9, version=version)])
        assert len(selection.selected) == 1
        assert selection.selected[0].bench_version == version

    @pytest.mark.parametrize("top_n", [0, -1, 11, True])
    def test_rejects_invalid_top_n(self, top_n: int) -> None:
        with pytest.raises(ConfirmationPolicyError, match="top_n"):
            select_confirmation_candidates([candidate(1, 0.9)], top_n=top_n)

    def test_selection_is_repeatable(self) -> None:
        rows = [candidate(i, 0.9 - i / 100, stderr=0.02) for i in range(1, 10)]
        first = select_confirmation_candidates(rows, top_n=5)
        second = select_confirmation_candidates(rows, top_n=5)
        assert first == second

    def test_completed_missing_full_never_enters_confirmed_frontier(self) -> None:
        rows = [
            candidate(i, 0.9 - i / 100, state=ConfirmationState.COMPLETED)
            for i in range(1, 7)
        ]
        selection = select_confirmation_candidates(rows, top_n=5)
        assert selection.confirmed_board == ()
        assert selection.cutoff_source == "base_frontier"
