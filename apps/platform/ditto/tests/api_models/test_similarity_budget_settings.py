"""The similarity budget board an operator actually turns.

Three things have to hold for this knob to be safe to hand over: the bounds on
the wire are the bounds the queue enforces (or a revision could be accepted here
and refused at the next restart), the kill switch is total, and a whole-object
write cannot silently reset the board an operator did not mention.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ditto.api_models.queue_policy_settings import (
    DEFAULT_SIMILARITY_CONCURRENT_SUBMISSIONS,
    DEFAULT_SIMILARITY_CONTAINMENT_THRESHOLD,
    DEFAULT_SIMILARITY_JACCARD_THRESHOLD,
    MAX_SIMILARITY_CONCURRENT_SUBMISSIONS,
    MAX_SIMILARITY_THRESHOLD_SETTING,
    MIN_SIMILARITY_CONCURRENT_SUBMISSIONS,
    MIN_SIMILARITY_THRESHOLD_SETTING,
    AdminQueuePolicySettingsRequest,
    QueuePolicySettings,
    SimilarityBudgetSettings,
)
from ditto.db.queries.queue_order import (
    MAX_SIMILARITY_CONCURRENT_SUBMISSION_LIMIT,
    MIN_SIMILARITY_CONCURRENT_SUBMISSION_LIMIT,
    SIMILARITY_CONCURRENT_SUBMISSION_LIMIT_DEFAULT,
)
from ditto.db.queries.similarity_grouping import (
    DEFAULT_CONTAINMENT_THRESHOLD,
    DEFAULT_JACCARD_THRESHOLD,
    MAX_SIMILARITY_THRESHOLD,
    MIN_SIMILARITY_THRESHOLD,
    SimilarityBudgetPolicy,
    policy_from_settings,
)


def test_board_bounds_match_the_queue_constants() -> None:
    """A revision this board accepts must be one the queue can enforce.

    The two live in different packages only because importing back would be a
    cycle. Drifting them apart would let an operator store a policy the next
    platform restart refuses to construct.
    """
    assert MIN_SIMILARITY_CONCURRENT_SUBMISSIONS == (
        MIN_SIMILARITY_CONCURRENT_SUBMISSION_LIMIT
    )
    assert MAX_SIMILARITY_CONCURRENT_SUBMISSIONS == (
        MAX_SIMILARITY_CONCURRENT_SUBMISSION_LIMIT
    )
    assert DEFAULT_SIMILARITY_CONCURRENT_SUBMISSIONS == (
        SIMILARITY_CONCURRENT_SUBMISSION_LIMIT_DEFAULT
    )
    assert MIN_SIMILARITY_THRESHOLD_SETTING == MIN_SIMILARITY_THRESHOLD
    assert MAX_SIMILARITY_THRESHOLD_SETTING == MAX_SIMILARITY_THRESHOLD
    assert DEFAULT_SIMILARITY_JACCARD_THRESHOLD == DEFAULT_JACCARD_THRESHOLD
    assert DEFAULT_SIMILARITY_CONTAINMENT_THRESHOLD == DEFAULT_CONTAINMENT_THRESHOLD


def test_every_board_value_is_constructible_by_the_gate() -> None:
    """The corners, not just the defaults.

    A bound that agrees numerically but disagrees about inclusivity would only
    show up at an extreme, which is exactly where an operator reaches during an
    incident.
    """
    for jaccard in (MIN_SIMILARITY_THRESHOLD_SETTING, MAX_SIMILARITY_THRESHOLD_SETTING):
        for containment in (
            MIN_SIMILARITY_THRESHOLD_SETTING,
            MAX_SIMILARITY_THRESHOLD_SETTING,
        ):
            settings = SimilarityBudgetSettings(
                jaccard_threshold=jaccard, containment_threshold=containment
            )
            policy = policy_from_settings(settings)
            assert policy == SimilarityBudgetPolicy(
                jaccard_threshold=jaccard, containment_threshold=containment
            )


def test_the_rail_ships_on_with_the_measured_thresholds() -> None:
    """Shipping it off would leave the queue in the state that motivated it."""
    settings = QueuePolicySettings().similarity_budget

    assert settings.enabled is True
    assert settings.jaccard_threshold == 0.90
    assert settings.containment_threshold == 0.95
    assert settings.concurrent_submission_limit == 1


def test_disabling_the_board_is_a_total_kill_switch() -> None:
    """Off means no policy at all, which is how the whole path spells "off"."""
    assert policy_from_settings(SimilarityBudgetSettings(enabled=False)) is None
    assert policy_from_settings(SimilarityBudgetSettings(enabled=True)) is not None


@pytest.mark.parametrize("threshold", [0.0, 0.5, 0.69, 1.01, -1.0])
def test_a_threshold_beneath_the_floor_is_refused(threshold: float) -> None:
    """Independent production miners reach 0.625; a lower floor invites them in."""
    with pytest.raises(ValidationError):
        SimilarityBudgetSettings(jaccard_threshold=threshold)
    with pytest.raises(ValidationError):
        SimilarityBudgetSettings(containment_threshold=threshold)


@pytest.mark.parametrize("limit", [0, -1, MAX_SIMILARITY_CONCURRENT_SUBMISSIONS + 1])
def test_the_concurrency_limit_rejects_out_of_range(limit: int) -> None:
    with pytest.raises(ValidationError):
        SimilarityBudgetSettings(concurrent_submission_limit=limit)


def test_a_partial_write_names_the_missing_similarity_fields() -> None:
    """A revision stores the whole policy, so silence must not mean "default".

    Without this, an operator sending the rest of the policy would quietly
    re-enable a rail they had turned off, or reset a threshold they had tuned.
    """
    complete = QueuePolicySettings().model_dump()
    complete["similarity_budget"] = {"enabled": False}

    with pytest.raises(ValidationError) as excinfo:
        AdminQueuePolicySettingsRequest(
            expected_revision=0,
            settings=QueuePolicySettings.model_validate(complete),
            reason="turning the similarity rail off for an experiment",
            confirmation="APPLY QUEUE POLICY SETTINGS",
        )

    message = str(excinfo.value)
    assert "similarity_budget is stored whole too" in message
    assert "jaccard_threshold" in message


def test_a_complete_write_is_accepted() -> None:
    complete = QueuePolicySettings().model_dump()

    request = AdminQueuePolicySettingsRequest(
        expected_revision=0,
        settings=QueuePolicySettings.model_validate(complete),
        reason="pinning the shipped policy explicitly",
        confirmation="APPLY QUEUE POLICY SETTINGS",
    )

    assert request.settings.similarity_budget.enabled is True


def test_the_board_is_frozen_and_ignores_unknown_fields() -> None:
    settings = SimilarityBudgetSettings()

    with_typo = SimilarityBudgetSettings(
        jaccard_treshold=0.9  # type: ignore[call-arg]
    )
    assert "jaccard_treshold" not in with_typo.model_dump()
    with pytest.raises(ValidationError):
        settings.enabled = False  # type: ignore[misc]
