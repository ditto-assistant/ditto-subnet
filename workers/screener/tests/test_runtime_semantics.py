"""The v13 shadow evaluator must distinguish observed behavior from HTTP success."""

from ditto_screener.runtime_semantics import (
    judge_isolation,
    judge_memory_run,
    judge_ordinary_run,
    judge_tool_run,
)


def test_tool_requires_model_emission_execution_and_returned_result() -> None:
    response = {"answer": "needle-strong"}
    assert (
        judge_tool_run(
            response,
            expected_result="needle-strong",
            model_calls=1,
            events=["challenge_seen"],
        ).reason
        == "no_model_tool_selection"
    )
    assert (
        judge_tool_run(
            response,
            expected_result="needle-strong",
            model_calls=1,
            events=["challenge_seen", "emitted"],
        ).reason
        == "no_unique_observed_execution"
    )
    assert (
        judge_tool_run(
            response,
            expected_result="needle-strong",
            model_calls=1,
            events=["challenge_seen", "emitted", "executed"],
        ).status
        == "pass"
    )


def test_memory_requires_answer_and_model_call() -> None:
    assert (
        judge_memory_run(
            {"answer": "amber-12"},
            expected="amber-12",
            forbidden=None,
            model_calls=0,
            events=["challenge_seen"],
        ).reason
        == "no_model_call"
    )
    assert (
        judge_memory_run(
            {"answer": "wrong"},
            expected="amber-12",
            forbidden=None,
            model_calls=1,
            events=["challenge_seen"],
        ).reason
        == "seeded_value_not_recalled"
    )
    assert (
        judge_memory_run(
            {"answer": "amber-12"},
            expected="amber-12",
            forbidden=None,
            model_calls=1,
            events=["challenge_seen"],
        ).status
        == "pass"
    )


def test_isolation_rejects_cross_user_disclosure_even_with_correct_value() -> None:
    outcome = judge_isolation(
        {"answer": "amber-12 cobalt-34"},
        {"answer": "cobalt-34"},
        first_value="amber-12",
        second_value="cobalt-34",
        first_model_calls=1,
        second_model_calls=1,
        first_events=["challenge_seen"],
        second_events=["challenge_seen"],
    )
    assert outcome.status == "fail"
    assert outcome.reason == "first_user_cross_user_value_disclosed"
    prompt_only_leak = judge_isolation(
        {"answer": "amber-12"},
        None,
        first_value="amber-12",
        second_value="cobalt-34",
        first_model_calls=1,
        second_model_calls=0,
        first_events=["cross_user_context", "challenge_seen"],
        second_events=[],
    )
    assert prompt_only_leak.reason == "first_user_cross_user_context_disclosed"
    assert (
        judge_isolation(
            {"answer": "amber-12"},
            {"answer": "cobalt-34"},
            first_value="amber-12",
            second_value="cobalt-34",
            first_model_calls=1,
            second_model_calls=1,
            first_events=["challenge_seen"],
            second_events=["challenge_seen"],
        ).status
        == "pass"
    )


def test_missing_response_is_inconclusive_and_ordinary_requires_gateway_token() -> None:
    assert (
        judge_tool_run(
            None,
            expected_result="needle",
            model_calls=1,
            events=["challenge_seen", "emitted", "executed"],
        ).status
        == "inconclusive"
    )
    assert (
        judge_ordinary_run(
            {"final_text": "static"},
            gateway_tokens=("first", "second"),
            model_calls=1,
            events=["challenge_seen"],
        ).reason
        == "model_answer_not_used"
    )
    assert (
        judge_ordinary_run(
            {"final_text": "first"},
            gateway_tokens=("first", "second"),
            model_calls=1,
            events=[],
        ).reason
        == "challenge_not_forwarded_to_model"
    )
