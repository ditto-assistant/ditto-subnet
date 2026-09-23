"""Report-only judgments for bounded v13 runtime probes.

These judgments never settle a source-integrity hold. They use high-entropy
ephemeral values that are absent from the /run question, and expose only coded
outcomes to logs; the current Platform receipts remain recorded_unverified.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class SemanticOutcome:
    status: Literal["pass", "fail", "inconclusive"]
    reason: str


def _asserted_text(response: Mapping[str, object] | None) -> str | None:
    if response is None:
        return None
    answer = response.get("answer")
    if isinstance(answer, str) and answer:
        return answer
    final_text = response.get("final_text")
    return final_text if isinstance(final_text, str) else None


def judge_ordinary_run(
    response: Mapping[str, object] | None,
    *,
    gateway_tokens: tuple[str, str],
    model_calls: int,
) -> SemanticOutcome:
    answer = _asserted_text(response)
    if answer is None:
        return SemanticOutcome("inconclusive", "run_response_unavailable")
    if model_calls == 0:
        return SemanticOutcome("fail", "no_model_call")
    if not any(token in answer for token in gateway_tokens):
        return SemanticOutcome("fail", "model_answer_not_used")
    return SemanticOutcome("pass", "gateway_answer_used")


def judge_tool_run(
    response: Mapping[str, object] | None,
    *,
    expected_result: str,
    model_calls: int,
    events: list[str],
) -> SemanticOutcome:
    answer = _asserted_text(response)
    if answer is None:
        return SemanticOutcome("inconclusive", "run_response_unavailable")
    if model_calls == 0:
        return SemanticOutcome("fail", "no_model_call")
    if "emitted" not in events:
        return SemanticOutcome("fail", "no_model_tool_selection")
    if events.count("executed") != 1:
        return SemanticOutcome("fail", "no_unique_observed_execution")
    if expected_result not in answer:
        return SemanticOutcome("fail", "tool_result_not_used")
    return SemanticOutcome("pass", "observed_model_tool_result_used")


def judge_memory_run(
    response: Mapping[str, object] | None,
    *,
    expected: str,
    forbidden: str | None,
    model_calls: int,
) -> SemanticOutcome:
    answer = _asserted_text(response)
    if answer is None:
        return SemanticOutcome("inconclusive", "run_response_unavailable")
    if model_calls == 0:
        return SemanticOutcome("fail", "no_model_call")
    final_text = response.get("final_text") if response is not None else None
    if forbidden is not None and (
        forbidden in answer or (isinstance(final_text, str) and forbidden in final_text)
    ):
        return SemanticOutcome("fail", "cross_user_value_disclosed")
    if expected not in answer:
        return SemanticOutcome("fail", "seeded_value_not_recalled")
    return SemanticOutcome("pass", "seeded_value_recalled")


def judge_isolation(
    first: Mapping[str, object] | None,
    second: Mapping[str, object] | None,
    *,
    first_value: str,
    second_value: str,
    first_model_calls: int,
    second_model_calls: int,
) -> SemanticOutcome:
    left = judge_memory_run(
        first,
        expected=first_value,
        forbidden=second_value,
        model_calls=first_model_calls,
    )
    right = judge_memory_run(
        second,
        expected=second_value,
        forbidden=first_value,
        model_calls=second_model_calls,
    )
    if "inconclusive" in {left.status, right.status}:
        return SemanticOutcome("inconclusive", "isolation_response_unavailable")
    if left.status == "fail":
        return SemanticOutcome("fail", f"first_user_{left.reason}")
    if right.status == "fail":
        return SemanticOutcome("fail", f"second_user_{right.reason}")
    return SemanticOutcome("pass", "both_users_isolated")
