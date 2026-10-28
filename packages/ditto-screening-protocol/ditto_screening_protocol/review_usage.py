"""Attempt-owned model usage telemetry for one screening review.

The worker reports, once per attempt and before its verdict, how many model
requests and tokens each review stage spent and what they cost. It is
telemetry only: it is not signed into the verdict, never changes an outcome,
and carries no prompt, source, model text or provider message.
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

ReviewUsageStage = Literal[
    "l1",
    "l2_analyst",
    "l2_critic",
    "l3_adjudicator",
    "l2_tiebreak",
    "l2_total",
]
MAX_REVIEW_USAGE_STAGES = 24
# One attempt is bounded by the review settings (at most $25 and 5M billable
# input tokens per L2 run today); these ceilings only reject nonsense.
MAX_STAGE_REQUESTS = 10_000
MAX_STAGE_TOKENS = 200_000_000
MAX_STAGE_COST_USD = 1_000.0
MAX_REVIEW_ELAPSED_MS = 24 * 3_600_000


class ReviewUsageStageEntry(BaseModel):
    """Usage one review stage spent on one model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: ReviewUsageStage
    model: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9/._:-]{0,127}$")] = (
        "unknown"
    )
    requests: Annotated[int, Field(ge=0, le=MAX_STAGE_REQUESTS)] = 0
    input_tokens: Annotated[int, Field(ge=0, le=MAX_STAGE_TOKENS)] = 0
    cached_input_tokens: Annotated[int, Field(ge=0, le=MAX_STAGE_TOKENS)] = 0
    output_tokens: Annotated[int, Field(ge=0, le=MAX_STAGE_TOKENS)] = 0
    reasoning_tokens: Annotated[int, Field(ge=0, le=MAX_STAGE_TOKENS)] = 0
    estimated_cost_usd: Annotated[float, Field(ge=0, le=MAX_STAGE_COST_USD)] = 0.0
    # Gateway-reported cost when every request in the stage reported one.
    reported_cost_usd: Annotated[float, Field(ge=0, le=MAX_STAGE_COST_USD)] | None = (
        None
    )

    @model_validator(mode="after")
    def _cached_within_input(self) -> ReviewUsageStageEntry:
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached input tokens cannot exceed input tokens")
        return self

    @property
    def cost_usd(self) -> float:
        """Reported cost when present, otherwise the worker's estimate."""
        return (
            self.reported_cost_usd
            if self.reported_cost_usd is not None
            else self.estimated_cost_usd
        )


class ScreeningReviewUsageRequest(BaseModel):
    """One attempt's review usage, posted by the screener that holds it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt_id: UUID
    artifact_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    stages: Annotated[
        list[ReviewUsageStageEntry],
        Field(min_length=1, max_length=MAX_REVIEW_USAGE_STAGES),
    ]
    elapsed_ms: Annotated[int, Field(ge=0, le=MAX_REVIEW_ELAPSED_MS)] | None = None
    # Echo of the lineage packet scope the review ran with, when it had one.
    lineage_scope: Literal["diff", "full"] | None = None

    @model_validator(mode="after")
    def _unique_stage_models(self) -> ScreeningReviewUsageRequest:
        keys = [(entry.stage, entry.model) for entry in self.stages]
        if len(keys) != len(set(keys)):
            raise ValueError("each (stage, model) pair may appear once")
        return self

    @property
    def total_input_tokens(self) -> int:
        return sum(entry.input_tokens for entry in self.stages)

    @property
    def total_output_tokens(self) -> int:
        return sum(entry.output_tokens for entry in self.stages)

    @property
    def total_cost_usd(self) -> float:
        return sum(entry.cost_usd for entry in self.stages)


class ScreeningReviewUsageResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    accepted: bool


__all__ = [
    "MAX_REVIEW_USAGE_STAGES",
    "ReviewUsageStage",
    "ReviewUsageStageEntry",
    "ScreeningReviewUsageRequest",
    "ScreeningReviewUsageResponse",
]
