"""Focused validation for safe public score-progress models."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ditto.api_models.public import (
    PublicLeaderboardFamilyMember,
    PublicProvisionalScore,
    PublicSubmissionFamilyMember,
)


def test_provisional_score_accepts_reproducible_safe_fields() -> None:
    score = PublicProvisionalScore(
        composite=0.625,
        seed="5585512758338063316",
        run_size="full",
        bench_version=2,
        datagen_version="v0.7.0",
        seed_source="on_chain",
        dataset_sha256="ab" * 32,
        accepted_at=datetime(2026, 7, 14, tzinfo=UTC),
        reproduction_command="generate -seed 123456789 -run-size full",
        verification_command="generate -seed 123456789 -run-size full -sha",
        case_results=None,
    )

    assert score.composite == pytest.approx(0.625)
    assert score.seed == "5585512758338063316"
    assert score.seed_source == "on_chain"


def test_provisional_score_keeps_v5_composite_bounded() -> None:
    score = PublicProvisionalScore(
        composite=0.855,
        seed="5585512758338063316",
        run_size="full",
        bench_version=5,
        datagen_version="v0.10.0",
        seed_source="on_chain",
        dataset_sha256="ab" * 32,
        accepted_at=datetime(2026, 7, 20, tzinfo=UTC),
        reproduction_command="generate -seed 123456789 -run-size full",
        verification_command="generate -seed 123456789 -run-size full -sha",
        case_results=None,
    )
    assert score.composite == pytest.approx(0.855)

    with pytest.raises(ValidationError):
        PublicProvisionalScore.model_validate(
            {**score.model_dump(), "composite": 1.001}
        )


@pytest.mark.parametrize(
    "seed_source", ["on_chain", "random_fallback", "validator_local"]
)
def test_provisional_score_accepts_each_seed_source(seed_source: str) -> None:
    score = PublicProvisionalScore(
        composite=0.5,
        seed="123456789",
        run_size=None,
        bench_version=None,
        datagen_version=None,
        seed_source=seed_source,
        dataset_sha256=None,
        accepted_at=datetime(2026, 7, 14, tzinfo=UTC),
        reproduction_command=None,
        verification_command=None,
        case_results=None,
    )

    assert score.seed_source == seed_source


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_size", "custom; rm -rf /"),
        ("datagen_version", "latest"),
        ("seed_source", "miner_supplied"),
        ("dataset_sha256", "not-a-hash"),
        ("seed", "1e9"),
    ],
)
def test_provisional_score_rejects_untrusted_command_inputs(
    field: str, value: str
) -> None:
    payload = {
        "composite": 0.625,
        "seed": "123456789",
        "run_size": "full",
        "bench_version": 2,
        "datagen_version": "v0.7.0",
        "seed_source": "on_chain",
        "dataset_sha256": "ab" * 32,
        "accepted_at": datetime(2026, 7, 14, tzinfo=UTC),
    }
    payload[field] = value

    with pytest.raises(ValidationError):
        PublicProvisionalScore.model_validate(payload)


@pytest.mark.parametrize(
    "model, extra",
    [
        (PublicLeaderboardFamilyMember, {}),
        (
            PublicSubmissionFamilyMember,
            {
                "miner_hotkey": "5" + "T" * 47,
                "submitted_at": datetime(2026, 6, 8, tzinfo=UTC),
            },
        ),
    ],
    ids=["compact", "full"],
)
def test_family_members_render_a_zero_canonical_composite(
    model: type, extra: dict[str, object]
) -> None:
    """A legacy child that genuinely scored 0.0 is history, not a bad value.

    Rejecting it here did not keep a zero score off the leaderboard -- it made
    the whole response unserializable, so every caller of
    `/api/v1/public/leaderboard?bench_version=6` got a 500.
    """
    member = model.model_validate(
        {
            "agent_id": "11111111-1111-4111-8111-111111111111",
            "agent_name": "legacy",
            "canonical_composite": 0.0,
            **extra,
        }
    )

    assert member.canonical_composite == 0.0


@pytest.mark.parametrize(
    "model, extra",
    [
        (PublicLeaderboardFamilyMember, {}),
        (
            PublicSubmissionFamilyMember,
            {
                "miner_hotkey": "5" + "T" * 47,
                "submitted_at": datetime(2026, 6, 8, tzinfo=UTC),
            },
        ),
    ],
    ids=["compact", "full"],
)
@pytest.mark.parametrize(
    "composite",
    [-0.1, -0.0001, 1.0001, 2.0, float("nan"), float("inf"), float("-inf")],
    ids=["negative", "tiny-negative", "just-over-one", "two", "nan", "inf", "-inf"],
)
def test_family_members_still_reject_impossible_composites(
    model: type, extra: dict[str, object], composite: float
) -> None:
    """Widening the floor to zero must not admit anything else.

    NaN and +inf fail the `le` bound, -inf fails `ge`; none of them are a score.
    """
    with pytest.raises(ValidationError):
        model.model_validate(
            {
                "agent_id": "11111111-1111-4111-8111-111111111111",
                "agent_name": "legacy",
                "canonical_composite": composite,
                **extra,
            }
        )
