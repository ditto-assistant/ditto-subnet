"""Operator model for per-validator job-poll and scoring-ledger outcomes.

Built from the Platform process's in-memory tally
(:mod:`ditto.api_server.validator_request_outcomes`). It schedules nothing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class RequestOutcomeCount(BaseModel):
    """How often one outcome ended this validator's polls on one lane."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    outcome: Annotated[
        str,
        Field(
            description=(
                "issued; served or served_stale (scoring ledger); a 204 "
                "decline gate (allocator_busy, not_accepting, "
                "slot_not_healthy, no_candidate, ...); http_<status> for a "
                "refused request (http_428 stale heartbeat, or on the ledger "
                "a validator that could not fold weights; http_426 upgrade; "
                "http_409 replay or stale claim); or error_<Exception>."
            )
        ),
    ]
    total: Annotated[int, Field(ge=0, description="Count since tracking_since.")]
    recent_15m: Annotated[int, Field(ge=0, description="Count in the last 15 min.")]
    first_at: datetime | None
    last_at: datetime | None
    last_detail: Annotated[
        str | None,
        Field(description="Last HTTP error detail for http_* outcomes, truncated."),
    ]
    last_slot_id: str | None


class ValidatorRequestOutcomes(BaseModel):
    """Every outcome one validator saw on one lane, newest first."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    validator_hotkey: str
    lane: Literal["job", "top5_confirmation", "scoring_ledger"]
    last_poll_at: datetime | None
    last_outcome: str | None
    outcomes: list[RequestOutcomeCount]


class ValidatorRequestOutcomeSummary(BaseModel):
    """Process-local poll outcome tally; resets when Platform API restarts."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    observed_at: datetime
    tracking_since: Annotated[
        datetime, Field(description="Platform API process start; counts reset then.")
    ]
    recent_window_seconds: int
    unauthenticated: Annotated[
        dict[str, int],
        Field(description="Polls that failed authentication, by lane:error."),
    ]
    validators: list[ValidatorRequestOutcomes]
