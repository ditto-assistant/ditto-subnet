"""The model-use rule, calibrated against the production distribution.

Every honest fixture below is a real measured run: the score's ``n`` joined to
the inference grant bound to that score's lease, over all 436 scored
``bench_version=7`` runs in production on 2026-07-27.
"""

from __future__ import annotations

import pytest

from ditto.api_models.model_use import ModelUseVerdict
from ditto.api_server.model_use import (
    MIN_CALLS_PER_CASE,
    MIN_PROMPT_TOKENS_PER_CALL,
    MIN_PROMPT_TOKENS_PER_CASE,
    ModelUseMode,
    ModelUsePolicy,
    evaluate_model_use,
    model_use_factor,
)
from ditto.db.queries.inference import LeaseModelUsage

SHADOW = ModelUsePolicy(mode=ModelUseMode.SHADOW)
ENFORCE = ModelUsePolicy(mode=ModelUseMode.ENFORCE)


def _usage(calls: int, prompt: int, completion: int = 0) -> LeaseModelUsage:
    return LeaseModelUsage(
        chat_calls=calls,
        prompt_tokens=prompt,
        completion_tokens=completion,
        accounting_version=2,
    )


# (name, calls, prompt_tokens, cases) -- measured production runs.
HONEST_RUNS = [
    ("kabaw-v49 (champion, honest minimum)", 416, 687_293, 281),
    ("kabaw-v49", 445, 761_280, 280),
    ("kabaw-v31", 398, 791_254, 279),
    ("kabaw-v24c", 389, 964_044, 279),
    ("lihai", 453, 1_115_518, 280),
    ("OracleMind", 432, 1_231_525, 280),
    ("cliM@X-v22", 421, 2_080_075, 280),
    ("dominator-4", 590, 1_237_559, 280),
    ("isop", 705, 3_102_922, 280),
    ("maybe-v0", 412, 2_035_635, 280),
    ("banblackycat", 444, 1_167_404, 277),
    ("aiforge", 408, 1_099_295, 279),
    ("Jupiter_v7_2", 261, 4_019_317, 280),
    ("maia-sn118-v1", 523, 8_780_215, 280),
]


@pytest.mark.parametrize(("name", "calls", "prompt", "cases"), HONEST_RUNS)
def test_no_honest_miner_in_the_measured_distribution_is_flagged(
    name: str, calls: int, prompt: int, cases: int
) -> None:
    """The rule must never fire on a real miner. This is the whole safety case.

    A rule that zeroes an honest miner is far worse than one that misses an
    abuser, so this parametrization is the test that matters most.
    """
    result = evaluate_model_use(_usage(calls, prompt), cases=cases, policy=ENFORCE)
    assert result.verdict is ModelUseVerdict.USED, f"{name} was wrongly flagged"
    assert result.reason is None


def test_the_honest_minimum_clears_both_gates_with_an_order_of_magnitude_to_spare() -> (
    None
):
    """Margin, stated numerically, so a future threshold edit has to face it."""
    champion = evaluate_model_use(_usage(416, 687_293), cases=281, policy=ENFORCE)
    assert champion.prompt_tokens_per_case is not None
    assert champion.calls_per_case is not None
    assert champion.prompt_tokens_per_case / MIN_PROMPT_TOKENS_PER_CASE > 10
    assert champion.calls_per_case / MIN_CALLS_PER_CASE > 5


def test_the_retrieval_only_shape_is_flagged() -> None:
    """One 74-token probe across a 280-case run: the observed abuse."""
    result = evaluate_model_use(_usage(1, 74, 1), cases=280, policy=ENFORCE)
    assert result.verdict is ModelUseVerdict.NOT_USED
    assert result.reason is not None
    assert "prompt tokens/case" in result.reason
    assert "model calls/case" in result.reason
    assert model_use_factor(result.verdict, mode=ModelUseMode.ENFORCE) == 0.0


def test_padding_with_empty_calls_does_not_clear_the_bar() -> None:
    """The defeat of the naive rule, pinned.

    280 calls is one per case and would satisfy any call-count floor. At the
    observed 74-token size they carry no case, and the token gate holds.
    """
    padded = evaluate_model_use(_usage(280, 280 * 74), cases=280, policy=ENFORCE)
    assert padded.calls_per_case == 1.0
    assert padded.verdict is ModelUseVerdict.NOT_USED
    assert padded.reason is not None
    assert "prompt tokens/case" in padded.reason
    assert "model calls/case" not in padded.reason


def test_bulk_padding_that_defeats_a_per_case_only_gate_is_still_caught() -> None:
    """The regression that forced the third gate into existence.

    2800 probe-sized calls across 280 cases clears *both* the per-case token
    floor (740/case) and the calls floor (10/case) for about a third of what
    the honest champion spends. Only the per-call floor catches it.
    """
    result = evaluate_model_use(_usage(2800, 2800 * 74), cases=280, policy=ENFORCE)
    assert result.prompt_tokens_per_case == 740.0
    assert result.calls_per_case == 10.0
    assert result.verdict is ModelUseVerdict.NOT_USED
    assert result.reason is not None
    assert "prompt tokens/call" in result.reason
    assert "prompt tokens/case" not in result.reason


def test_padding_raises_the_bar_it_must_clear() -> None:
    """The economics, pinned: each dummy call makes passing more expensive.

    With a per-call floor, a run making N calls must carry at least
    ``300 x N`` prompt tokens. So the 2800-call shape needs 840k -- more than
    the honest champion's 687k. Padding is never the cheap option.
    """
    calls = 2800
    required = calls * MIN_PROMPT_TOKENS_PER_CALL
    assert required > 687_293  # the honest champion's actual prompt spend

    just_under = evaluate_model_use(
        _usage(calls, int(required) - 1), cases=280, policy=ENFORCE
    )
    assert just_under.verdict is ModelUseVerdict.NOT_USED

    at_the_bar = evaluate_model_use(
        _usage(calls, int(required)), cases=280, policy=ENFORCE
    )
    assert at_the_bar.verdict is ModelUseVerdict.USED


def test_one_stuffed_megacall_does_not_clear_the_bar() -> None:
    """The mirror evasion: enough tokens on average, but not case-by-case."""
    result = evaluate_model_use(_usage(1, 280 * 5000), cases=280, policy=ENFORCE)
    assert result.prompt_tokens_per_case == 5000.0
    assert result.verdict is ModelUseVerdict.NOT_USED
    assert result.reason is not None
    assert "model calls/case" in result.reason


def test_clearing_the_bar_by_padding_requires_paying_for_real_prompts() -> None:
    """States the price of evasion: it stops being an evasion.

    The cheapest padding that passes both gates must send ~200 real tokens per
    case across ~70 calls -- i.e. actually put the cases to the model.
    """
    cases = 280
    result = evaluate_model_use(
        _usage(
            int(cases * MIN_CALLS_PER_CASE), int(cases * MIN_PROMPT_TOKENS_PER_CASE)
        ),
        cases=cases,
        policy=ENFORCE,
    )
    assert result.verdict is ModelUseVerdict.USED
    assert result.prompt_tokens is not None
    assert result.prompt_tokens >= 56_000


def test_zero_usage_is_flagged_but_a_missing_grant_is_not() -> None:
    """Measured-and-zero and never-measured are different findings."""
    measured_zero = evaluate_model_use(_usage(0, 0), cases=280, policy=ENFORCE)
    assert measured_zero.verdict is ModelUseVerdict.NOT_USED

    unmeasured = evaluate_model_use(None, cases=280, policy=ENFORCE)
    assert unmeasured.verdict is ModelUseVerdict.UNMEASURED
    assert model_use_factor(unmeasured.verdict, mode=ModelUseMode.ENFORCE) == 1.0


def test_a_run_that_graded_no_cases_is_never_penalised() -> None:
    """No cases means no denominator, not an accusation."""
    result = evaluate_model_use(_usage(0, 0), cases=0, policy=ENFORCE)
    assert result.verdict is ModelUseVerdict.UNMEASURED


def test_shadow_mode_publishes_the_verdict_but_changes_no_score() -> None:
    """ditto-platform#506 invariant 5, and the fairness constraint.

    The finding is computed and visible; the score is untouched until an
    operator flips to enforce, after the threshold has been public.
    """
    result = evaluate_model_use(_usage(1, 74, 1), cases=280, policy=SHADOW)
    assert result.verdict is ModelUseVerdict.NOT_USED
    assert model_use_factor(result.verdict, mode=ModelUseMode.SHADOW) == 1.0


def test_off_mode_makes_no_finding_at_all() -> None:
    result = evaluate_model_use(
        _usage(1, 74, 1), cases=280, policy=ModelUsePolicy(mode=ModelUseMode.OFF)
    )
    assert result.verdict is ModelUseVerdict.UNMEASURED


def test_the_public_projection_leaks_no_source_or_case_content() -> None:
    """Observability that is code-privacy preserving.

    The projection is an allowlist by construction; this pins that it stays
    one, so a later field addition has to be deliberate.
    """
    result = evaluate_model_use(_usage(416, 687_293), cases=281, policy=ENFORCE)
    published = result.as_public_dict()
    assert set(published) == {
        "verdict",
        "calls",
        "prompt_tokens",
        "completion_tokens",
        "cases",
        "prompt_tokens_per_case",
        "calls_per_case",
        "prompt_tokens_per_call",
        "reason",
        "min_prompt_tokens_per_case",
        "min_calls_per_case",
        "min_prompt_tokens_per_call",
    }


def test_the_published_thresholds_are_the_enforced_thresholds() -> None:
    """A miner cannot be held to a bar different from the one on the page."""
    published = evaluate_model_use(
        _usage(1, 74, 1), cases=280, policy=ENFORCE
    ).as_public_dict()
    assert published["min_prompt_tokens_per_case"] == MIN_PROMPT_TOKENS_PER_CASE
    assert published["min_calls_per_case"] == MIN_CALLS_PER_CASE


def test_the_public_projection_round_trips_through_the_wire_model() -> None:
    """What the score stores is exactly what the leaderboard can publish.

    Guards the seam between `as_public_dict` (written at submit time) and
    `PublicModelUse` (read at render time). A field added on one side without
    the other silently disappears from the miner's view.
    """
    from ditto.api_models.public import PublicModelUse

    stored = evaluate_model_use(
        _usage(1, 74, 1), cases=280, policy=ENFORCE
    ).as_public_dict()
    published = PublicModelUse.model_validate(stored)

    assert published.verdict == "not_used"
    assert published.reason is not None
    assert published.min_prompt_tokens_per_case == MIN_PROMPT_TOKENS_PER_CASE
    assert published.min_calls_per_case == MIN_CALLS_PER_CASE
    assert published.min_prompt_tokens_per_call == MIN_PROMPT_TOKENS_PER_CALL
    assert published.model_dump(exclude_none=True).keys() <= set(stored)


def test_the_wire_model_carries_no_source_or_case_content_field() -> None:
    """Code-privacy preserving by construction, asserted rather than assumed."""
    from ditto.api_models.public import PublicModelUse

    forbidden = {
        "source",
        "prompt",
        "prompts",
        "completion",
        "completions",
        "case",
        "cases_detail",
        "answers",
        "transcript",
        "code",
    }
    assert set(PublicModelUse.model_fields) & forbidden == set()
