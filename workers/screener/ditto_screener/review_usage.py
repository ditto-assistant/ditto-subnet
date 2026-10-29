"""Attempt-scoped model usage accounting for the source review stages.

The worker opens one collector around a screening attempt; L1 and L2/L3 model
turns record the provider-reported usage of each successful response into
whichever collector is current. A ``ContextVar`` carries it, so it follows the
attempt into the tasks and threads the review spawns without threading a new
argument through every gate decision, and it is a no-op outside an attempt.

Only counts, model ids and costs are kept: never prompts, source, model text
or provider messages.
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from ditto_screening_protocol import ReviewBudgetStop, ReviewUsageStageEntry

_L2_ROLE_STAGES = {
    "analyst": "l2_analyst",
    "critic": "l2_critic",
    "adjudicator": "l3_adjudicator",
    "violation_adjudicator": "l3_adjudicator",
    "violation_tiebreaker": "l2_tiebreak",
}
_MODEL_FALLBACK = "unknown"


def _model_id(model: object) -> str:
    if isinstance(model, str):
        text = model.strip()
        if (
            text
            and len(text) <= 128
            and all(ch.isalnum() or ch in "/._:-" for ch in text)
            and text[0].isalnum()
        ):
            return text
    return _MODEL_FALLBACK


def l2_stage_for_role(role: str) -> str:
    """Map an L2/L3 trajectory role onto its telemetry stage."""
    return _L2_ROLE_STAGES.get(role, "l2_total")


@dataclass
class _StageTotals:
    requests: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    estimated_cost_usd: float = 0.0
    reported_cost_usd: float = 0.0
    reported_requests: int = 0
    first_started: float | None = None
    last_finished: float | None = None


class ReviewUsageCollector:
    """Per-(stage, model) totals for one attempt."""

    def __init__(self) -> None:
        self._stages: dict[tuple[str, str], _StageTotals] = {}
        self.budget_stop: ReviewBudgetStop | None = None

    def record(
        self,
        stage: str,
        *,
        model: object,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int = 0,
        reasoning_tokens: int = 0,
        estimated_cost_usd: float = 0.0,
        reported_cost_usd: float | None = None,
        started_at: float | None = None,
    ) -> None:
        totals = self._stages.setdefault((stage, _model_id(model)), _StageTotals())
        finished = time.monotonic()
        started = finished if started_at is None else min(started_at, finished)
        if totals.first_started is None or started < totals.first_started:
            totals.first_started = started
        totals.last_finished = finished
        totals.requests += 1
        totals.input_tokens += max(0, int(input_tokens))
        totals.cached_input_tokens += max(
            0, min(int(cached_input_tokens), int(input_tokens))
        )
        totals.output_tokens += max(0, int(output_tokens))
        totals.reasoning_tokens += max(0, int(reasoning_tokens))
        if math.isfinite(estimated_cost_usd) and estimated_cost_usd > 0:
            totals.estimated_cost_usd += estimated_cost_usd
        if (
            reported_cost_usd is not None
            and math.isfinite(reported_cost_usd)
            and reported_cost_usd >= 0
        ):
            totals.reported_cost_usd += reported_cost_usd
            totals.reported_requests += 1

    @property
    def empty(self) -> bool:
        return not self._stages

    def entries(self) -> list[ReviewUsageStageEntry]:
        out: list[ReviewUsageStageEntry] = []
        for (stage, model), totals in sorted(self._stages.items()):
            out.append(
                ReviewUsageStageEntry(
                    stage=stage,  # type: ignore[arg-type]
                    model=model,
                    requests=totals.requests,
                    input_tokens=totals.input_tokens,
                    cached_input_tokens=totals.cached_input_tokens,
                    output_tokens=totals.output_tokens,
                    reasoning_tokens=totals.reasoning_tokens,
                    estimated_cost_usd=round(totals.estimated_cost_usd, 6),
                    # A partial report would understate the stage; only a
                    # stage where every request reported a cost carries one.
                    reported_cost_usd=(
                        round(totals.reported_cost_usd, 6)
                        if totals.reported_requests == totals.requests
                        else None
                    ),
                    wall_ms=(
                        int((totals.last_finished - totals.first_started) * 1000)
                        if totals.first_started is not None
                        and totals.last_finished is not None
                        else None
                    ),
                )
            )
        return out


_CURRENT: ContextVar[ReviewUsageCollector | None] = ContextVar(
    "ditto_screener_review_usage", default=None
)


@contextmanager
def collect_review_usage() -> Iterator[ReviewUsageCollector]:
    """Make a fresh collector current for the enclosed attempt."""
    collector = ReviewUsageCollector()
    token = _CURRENT.set(collector)
    try:
        yield collector
    finally:
        _CURRENT.reset(token)


def record_usage(stage: str, **values: Any) -> None:
    """Record one successful model response into the current collector."""
    collector = _CURRENT.get()
    if collector is not None:
        collector.record(stage, **values)


def record_budget_stop(stop: ReviewBudgetStop) -> None:
    """Keep the first model budget that stopped this attempt's review."""
    collector = _CURRENT.get()
    if collector is not None and collector.budget_stop is None:
        collector.budget_stop = stop


def record_chat_completion_usage(
    stage: str,
    payload: Mapping[str, object],
    *,
    default_model: str,
    started_at: float | None = None,
) -> None:
    """Record an OpenAI-compatible chat-completions ``usage`` block.

    Malformed or missing usage is skipped: telemetry never fails a review.
    """
    if _CURRENT.get() is None:
        return
    usage = payload.get("usage")
    if not isinstance(usage, Mapping):
        return

    def count(value: object) -> int:
        return (
            value
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0
            else 0
        )

    prompt = count(usage.get("prompt_tokens"))
    completion = count(usage.get("completion_tokens"))
    prompt_details = usage.get("prompt_tokens_details")
    completion_details = usage.get("completion_tokens_details")
    cached = count(
        prompt_details.get("cached_tokens")
        if isinstance(prompt_details, Mapping)
        else 0
    )
    reasoning = count(
        completion_details.get("reasoning_tokens")
        if isinstance(completion_details, Mapping)
        else 0
    )
    cost = usage.get("cost")
    reported = (
        float(cost)
        if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0
        else None
    )
    model = payload.get("model")
    record_usage(
        stage,
        model=model if isinstance(model, str) else default_model,
        input_tokens=prompt,
        output_tokens=completion,
        cached_input_tokens=cached,
        reasoning_tokens=reasoning,
        reported_cost_usd=reported,
        started_at=started_at,
    )


__all__ = [
    "ReviewUsageCollector",
    "collect_review_usage",
    "l2_stage_for_role",
    "record_budget_stop",
    "record_chat_completion_usage",
    "record_usage",
]
