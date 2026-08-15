"""Unit tests for the queue-policy wire model's invariants.

The endpoint contract lives in
``ditto.tests.api_server.endpoints.test_admin_queue_policy_settings``; this file
covers the model in isolation, including the two properties the rest of the
system relies on being true: that the shipped defaults reproduce the previously
hard-coded queue exactly, and that the lane decision has exactly one
implementation.
"""

from __future__ import annotations

import pytest

from ditto.api_models.queue_policy_settings import (
    DEFAULT_FRESH_SUBMISSION_SLOTS,
    DEFAULT_LANE_CYCLE_SIZE,
    DEFAULT_OWNER_CONCURRENT_SUBMISSIONS,
    DEFAULT_PREV_GEN_CARRYOVER_AGENTS,
    MAX_COHORT_SIZE,
    MAX_OWNER_CONCURRENT_SUBMISSIONS,
    MAX_PREV_GEN_CARRYOVER_AGENTS,
    MIN_COHORT_SIZE,
    MIN_OWNER_CONCURRENT_SUBMISSIONS,
    PrevGenCarryoverSettings,
    QueuePolicySettings,
    rollout_locked_change,
)
from ditto.db.queries.benchmark_rollout import (
    DEFAULT_RESCORE_COHORT_SIZE,
    MAX_PERSISTED_RESCORE_COHORT_SIZE,
    PRIORITY_COHORT_SIZE,
)
from ditto.db.queries.queue_order import (
    MAX_OWNER_CONCURRENT_SUBMISSION_LIMIT,
    MIN_OWNER_CONCURRENT_SUBMISSION_LIMIT,
    OWNER_CONCURRENT_SUBMISSION_LIMIT_DEFAULT,
)


class TestQueuePolicyBoundsMatchQueueConstants:
    """The wire model spells its bounds out to avoid an import cycle; these are
    the guards against the two copies drifting apart."""

    def test_queue_policy_bounds_match_queue_constants(self) -> None:
        assert MIN_COHORT_SIZE == PRIORITY_COHORT_SIZE
        assert MAX_COHORT_SIZE == MAX_PERSISTED_RESCORE_COHORT_SIZE
        assert MIN_OWNER_CONCURRENT_SUBMISSIONS == (
            MIN_OWNER_CONCURRENT_SUBMISSION_LIMIT
        )
        assert MAX_OWNER_CONCURRENT_SUBMISSIONS == (
            MAX_OWNER_CONCURRENT_SUBMISSION_LIMIT
        )
        assert DEFAULT_OWNER_CONCURRENT_SUBMISSIONS == (
            OWNER_CONCURRENT_SUBMISSION_LIMIT_DEFAULT
        )

    def test_owner_ceiling_ships_relaxed_but_conservative(self) -> None:
        """The relaxation is ON by default; only its size is tunable.

        A knob defaulting to the old behaviour would leave the idle slots idle,
        which is the whole defect. ``1`` is the identity value an operator can
        return to without a deploy, so it must remain *reachable* but must not
        be the default.
        """
        assert QueuePolicySettings().owner_concurrent_submission_limit == 2
        assert MIN_OWNER_CONCURRENT_SUBMISSIONS == 1

    @pytest.mark.parametrize("limit", [0, -1, MAX_OWNER_CONCURRENT_SUBMISSIONS + 1])
    def test_owner_ceiling_rejects_out_of_range(self, limit: int) -> None:
        with pytest.raises(ValueError):
            QueuePolicySettings(owner_concurrent_submission_limit=limit)

    def test_defaults_reproduce_the_previously_hard_coded_queue(self) -> None:
        """If any of these drift, merging the board retunes the live queue."""
        settings = QueuePolicySettings()
        assert settings.rescore_cohort_size == DEFAULT_RESCORE_COHORT_SIZE
        assert settings.priority_cohort_size == PRIORITY_COHORT_SIZE
        # The shipped values of validator._LANE_CYCLE_SIZE / _FRESH_SUBMISSION_SLOTS.
        assert settings.lane_cycle_size == 4
        assert settings.fresh_submission_slots == (0, 1, 3)
        assert settings.lane_cycle_size == DEFAULT_LANE_CYCLE_SIZE
        assert settings.fresh_submission_slots == DEFAULT_FRESH_SUBMISSION_SLOTS

    def test_carryover_ships_disabled(self) -> None:
        """Change 1 must be a no-op until an operator turns it on."""
        assert QueuePolicySettings().prev_gen_carryover.enabled is False
        assert PrevGenCarryoverSettings().enabled is False

    def test_carryover_defaults_are_the_conservative_ones(self) -> None:
        carryover = PrevGenCarryoverSettings()
        # Only submissions already at 2 of 3, which have demonstrated they run.
        assert carryover.min_score_count == 2
        assert carryover.include_exhausted is False
        assert carryover.dedupe_scope == "coldkey"
        assert carryover.require_cohort_complete is True

    def test_the_top_twenty_five_qualify_to_enter_the_new_era(self) -> None:
        """Carryover depth matches the cohort depth it exists to populate.

        Adopting fewer than the retest lane will later re-benchmark hands the
        new era a leaderboard too short for its own cohort, so these two move
        together. ``MAX_COHORT_SIZE`` is the shared ceiling.
        """
        assert PrevGenCarryoverSettings().max_agents == 25
        assert DEFAULT_PREV_GEN_CARRYOVER_AGENTS == MAX_COHORT_SIZE
        # The size dial moved; every gate that decides *whether* anyone is
        # adopted is untouched, so this remains a no-op on a live fleet.
        assert PrevGenCarryoverSettings().enabled is False

    def test_the_carryover_ceiling_still_admits_the_new_default(self) -> None:
        """The board's own bound never became the thing blocking the default.

        This is the #473 failure mode in miniature: a default that a validator
        would reject is a delayed boot failure rather than a rejected write.
        ``max_agents`` has no env-var twin and no CHECK constraint, so this
        field-level bound is the only one there is.
        """
        assert DEFAULT_PREV_GEN_CARRYOVER_AGENTS <= MAX_PREV_GEN_CARRYOVER_AGENTS
        assert (
            PrevGenCarryoverSettings(
                max_agents=MAX_PREV_GEN_CARRYOVER_AGENTS
            ).max_agents
            == MAX_PREV_GEN_CARRYOVER_AGENTS
        )
        with pytest.raises(ValueError):
            PrevGenCarryoverSettings(max_agents=MAX_PREV_GEN_CARRYOVER_AGENTS + 1)

    def test_a_retired_era_has_no_authoritative_knob_left_to_turn(self) -> None:
        """``allow_retired_era_backfill`` is gone, and gone is stronger than off.

        It shipped ``False``, but it was an MCP-exposed runtime setting whose
        own docstring advertised that flipping it restored retired-era
        admission "without a deploy". One Backroom write re-opened v6. A
        default is not a floor.

        A stale writer may still send the retired key during a rolling upgrade,
        but it is ignored and never becomes an authoritative model field.
        """
        assert "allow_retired_era_backfill" not in PrevGenCarryoverSettings.model_fields
        carryover = PrevGenCarryoverSettings(
            allow_retired_era_backfill=True  # type: ignore[call-arg]
        )
        settings = QueuePolicySettings(
            prev_gen_carryover={"allow_retired_era_backfill": True}  # type: ignore[arg-type]
        )
        assert "allow_retired_era_backfill" not in carryover.model_dump()
        assert "allow_retired_era_backfill" not in (
            settings.prev_gen_carryover.model_dump()
        )


class TestLaneDecision:
    """One implementation of the lane rotation, so no projection can drift."""

    def test_default_split_is_three_fresh_per_one_cohort(self) -> None:
        settings = QueuePolicySettings()
        due = [settings.fresh_submission_lane_due(n) for n in range(8)]
        # Slots 0, 1, 3 are fresh; slot 2 is the cohort slot, in both cycles.
        assert due == [True, True, False, True, True, True, False, True]

    def test_the_cohort_slot_sits_mid_cycle_not_at_the_end(self) -> None:
        """Guards the exact shipped interleave: deriving the slot set from a
        count would give (0, 1, 2) and move which poll takes cohort work."""
        settings = QueuePolicySettings()
        assert settings.fresh_submission_lane_due(2) is False
        assert settings.fresh_submission_lane_due(3) is True

    def test_a_retuned_split_rotates_on_the_new_modulus(self) -> None:
        settings = QueuePolicySettings(
            lane_cycle_size=6, fresh_submission_slots=(0, 1, 2, 4)
        )
        due = [settings.fresh_submission_lane_due(n) for n in range(6)]
        assert due == [True, True, True, False, True, False]

    def test_slots_are_reported_sorted(self) -> None:
        settings = QueuePolicySettings(
            lane_cycle_size=4, fresh_submission_slots=(3, 0, 1)
        )
        assert settings.sorted_fresh_submission_slots == (0, 1, 3)

    def test_a_json_array_is_accepted_for_the_slot_set(self) -> None:
        """JSON has no tuple type, so the wire form must be an array."""
        settings = QueuePolicySettings.model_validate(
            {"lane_cycle_size": 4, "fresh_submission_slots": [0, 1, 3]}
        )
        assert settings.fresh_submission_slots == (0, 1, 3)


class TestDeadlockGuards:
    """Each of these individually validates but jointly wedges the queue."""

    def test_empty_fresh_lane_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="starvation"):
            QueuePolicySettings(fresh_submission_slots=())

    def test_fresh_lane_filling_the_cycle_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="never reach quorum"):
            QueuePolicySettings(lane_cycle_size=3, fresh_submission_slots=(0, 1, 2))

    def test_slots_outside_the_cycle_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="outside the cycle"):
            QueuePolicySettings(lane_cycle_size=4, fresh_submission_slots=(0, 9))

    def test_duplicate_slots_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="unique"):
            QueuePolicySettings(lane_cycle_size=4, fresh_submission_slots=(1, 1))

    def test_priority_cohort_cannot_exceed_the_rescore_cohort(self) -> None:
        with pytest.raises(ValueError, match="could never\n?\\s*activate"):
            QueuePolicySettings(rescore_cohort_size=10, priority_cohort_size=25)

    def test_priority_equal_to_the_rescore_cohort_is_allowed(self) -> None:
        assert QueuePolicySettings(rescore_cohort_size=10, priority_cohort_size=10)


class TestRolloutLockedChange:
    def test_no_lane_change_is_reported_when_only_cohorts_move(self) -> None:
        current = QueuePolicySettings()
        proposed = QueuePolicySettings(rescore_cohort_size=25)
        assert rollout_locked_change(current, proposed) == ()

    def test_cycle_size_change_is_reported(self) -> None:
        current = QueuePolicySettings()
        proposed = QueuePolicySettings(
            lane_cycle_size=6, fresh_submission_slots=(0, 1, 3)
        )
        assert rollout_locked_change(current, proposed) == ("lane_cycle_size",)

    def test_slot_set_change_is_reported(self) -> None:
        current = QueuePolicySettings()
        proposed = QueuePolicySettings(fresh_submission_slots=(0, 1, 2))
        assert rollout_locked_change(current, proposed) == ("fresh_submission_slots",)

    def test_reordered_slots_are_not_a_change(self) -> None:
        """Order is not semantic, so re-sending the same set must be writable
        during a rollout."""
        current = QueuePolicySettings(fresh_submission_slots=(0, 1, 3))
        proposed = QueuePolicySettings(fresh_submission_slots=(3, 1, 0))
        assert rollout_locked_change(current, proposed) == ()

    def test_carryover_is_never_rollout_locked(self) -> None:
        """Carryover must be togglable mid-rollout: that is when it matters."""
        current = QueuePolicySettings()
        proposed = QueuePolicySettings(
            prev_gen_carryover=PrevGenCarryoverSettings(enabled=True)
        )
        assert rollout_locked_change(current, proposed) == ()
