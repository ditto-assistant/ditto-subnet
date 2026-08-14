"""Privacy-safe confirmation progress carried by signed validator heartbeats."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

ConfirmationProgressStage = Literal[
    "preparing",
    "running_confirmation",
    "finalizing",
    "submitting_result",
    "failed_retrying",
]

MAX_CONFIRMATION_SLOTS = 4
MAX_CONFIRMATION_UNITS = 10_000
_TERMINAL_WORK_STAGES = {"finalizing", "submitting_result"}


class ConfirmationProgress(BaseModel):
    """One independent LongMem/ablation slot reported by its validator.

    Scores, provider details, prompts, response text, and errors are deliberately
    absent. Platform joins this opaque ticket identity to its own ledger before
    publishing it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    bundle_id: UUID
    ticket_id: UUID
    agent_id: UUID
    slot_id: Annotated[str, Field(pattern=r"^longmem-[0-3]$")]
    stage: ConfirmationProgressStage
    completed: Annotated[
        StrictInt | None, Field(default=None, ge=0, le=MAX_CONFIRMATION_UNITS)
    ] = None
    total: Annotated[
        StrictInt | None, Field(default=None, ge=1, le=MAX_CONFIRMATION_UNITS)
    ] = None
    ticket_deadline: datetime

    @field_validator("ticket_deadline")
    @classmethod
    def deadline_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("ticket_deadline must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def counts_are_consistent(self) -> ConfirmationProgress:
        if (self.completed is None) != (self.total is None):
            raise ValueError("completed and total must be reported together")
        if (
            self.completed is not None
            and self.total is not None
            and self.completed > self.total
        ):
            raise ValueError("completed cannot exceed total")
        if self.stage in _TERMINAL_WORK_STAGES and (
            self.completed is None or self.completed != self.total
        ):
            raise ValueError(f"{self.stage} requires completed to equal total")
        return self


def confirmation_progress_signing_token(
    progresses: Sequence[ConfirmationProgress] | None,
) -> str:
    """Return a deterministic bounded token for heartbeat protocol v22."""
    if progresses is None:
        return "-"
    ordered = sorted(progresses, key=lambda progress: progress.slot_id)
    if len(ordered) > MAX_CONFIRMATION_SLOTS:
        raise ValueError("confirmation progress exceeds the slot bound")
    if len({progress.slot_id for progress in ordered}) != len(ordered):
        raise ValueError("confirmation progress contains duplicate slots")
    terms: list[str] = []
    for progress in ordered:
        completed = "-" if progress.completed is None else str(progress.completed)
        total = "-" if progress.total is None else str(progress.total)
        deadline = progress.ticket_deadline.astimezone(UTC).isoformat(
            timespec="microseconds"
        )
        terms.append(
            ",".join(
                (
                    progress.slot_id,
                    str(progress.bundle_id),
                    str(progress.ticket_id),
                    str(progress.agent_id),
                    progress.stage,
                    completed,
                    total,
                    deadline,
                )
            )
        )
    return ";".join(terms)
