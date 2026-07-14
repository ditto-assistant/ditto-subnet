"""Privacy-safe benchmark lifecycle carried by signed validator heartbeats."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

BenchmarkProgressStage = Literal[
    "preparing",
    "building_harness",
    "starting_harness",
    "running_benchmark",
    "finalizing",
    "submitting_result",
    "failed_retrying",
]

MAX_BENCHMARK_CHECKS = 10_000
_EARLY_STAGES = {"preparing", "building_harness", "starting_harness"}
_TERMINAL_WORK_STAGES = {"finalizing", "submitting_result"}


class BenchmarkProgress(BaseModel):
    """One ticket-bound stage with an optional paired aggregate check count.

    The model intentionally has no display text, error detail, run identifier,
    score, floating-point percentage, or arbitrary metadata. Percent and public
    counts are derived and coarsened by the platform.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: BenchmarkProgressStage
    completed: Annotated[
        StrictInt | None, Field(default=None, ge=0, le=MAX_BENCHMARK_CHECKS)
    ] = None
    total: Annotated[
        StrictInt | None, Field(default=None, ge=1, le=MAX_BENCHMARK_CHECKS)
    ] = None
    ticket_deadline: datetime

    @field_validator("ticket_deadline")
    @classmethod
    def ticket_deadline_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("ticket_deadline must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def counts_are_safe_for_stage(self) -> BenchmarkProgress:
        if (self.completed is None) != (self.total is None):
            raise ValueError("completed and total must be reported together")
        if (
            self.completed is not None
            and self.total is not None
            and self.completed > self.total
        ):
            raise ValueError("completed cannot exceed total")
        if self.stage in _EARLY_STAGES and self.completed is not None:
            raise ValueError(f"{self.stage} must omit completed and total")
        if self.stage in _TERMINAL_WORK_STAGES and (
            self.completed is None or self.completed != self.total
        ):
            raise ValueError(f"{self.stage} requires completed to equal total")
        return self


def benchmark_progress_signing_token(progress: BenchmarkProgress | None) -> str:
    """Return an unambiguous bounded token for heartbeat protocol v4."""
    if progress is None:
        return "-"
    completed = "-" if progress.completed is None else str(progress.completed)
    total = "-" if progress.total is None else str(progress.total)
    deadline = progress.ticket_deadline.astimezone(UTC).isoformat(
        timespec="microseconds"
    )
    return f"{progress.stage},{completed},{total},{deadline}"
