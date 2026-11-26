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
    recent_15m_by_slot: Annotated[
        dict[str, int],
        Field(
            default_factory=dict,
            description=(
                "The last-15-minute count split by the slot the poll offered "
                "(`unknown` when it named none)."
            ),
        ),
    ]
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


class DispatchFenceHolder(BaseModel):
    """The job transaction holding the allocator fence at read time."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    validator_hotkey: str
    slot_id: str
    acquired_at: datetime
    held_seconds: Annotated[float, Field(ge=0)]


class DispatchFenceValidatorHolds(BaseModel):
    """One validator's fence holds in the recent window."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    validator_hotkey: str
    holds: Annotated[int, Field(ge=0)]
    held_seconds: Annotated[float, Field(ge=0)]
    max_hold_seconds: Annotated[float, Field(ge=0)]


class DispatchFenceBusyPair(BaseModel):
    """Polls from one validator that bounced off a fence another poll held."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    validator_hotkey: Annotated[str, Field(description="The validator declined.")]
    holder_hotkey: Annotated[
        str | None,
        Field(
            description=(
                "Whose job transaction held the fence. Null when the holder was "
                "not a tracked job poll (an operator cohort write)."
            )
        ),
    ]
    busy_declines: Annotated[int, Field(ge=0)]
    same_validator: Annotated[
        bool,
        Field(
            description=(
                "The holder was this validator's own sibling slot: the host "
                "polls in synchronized bursts and collides with itself."
            )
        ),
    ]
    by_slot: Annotated[
        dict[str, int],
        Field(description="The declined validator's busy count per slot."),
    ]


class DispatchFenceSnapshot(BaseModel):
    """What the job allocator's dispatch fence is busy with.

    A high `recent_utilization` means the allocator transaction itself is slow.
    A low one with many `recent_busy_same_validator` means a host's slots poll
    in bursts and collide with each other, not with the allocator.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    observed_at: datetime
    tracking_since: datetime
    recent_window_seconds: int
    holder: DispatchFenceHolder | None
    recent_holds: Annotated[int, Field(ge=0)]
    recent_held_seconds: Annotated[float, Field(ge=0)]
    recent_utilization: Annotated[
        float,
        Field(
            ge=0,
            le=1,
            description=(
                "Share of the window (or of the time since tracking started, "
                "if shorter) the fence was held."
            ),
        ),
    ]
    hold_p50_seconds: float | None
    hold_p95_seconds: float | None
    hold_max_seconds: float | None
    hold_outcomes: Annotated[
        dict[str, int],
        Field(
            description=(
                "How the recent holds ended: issued, no_candidate, a decline "
                "gate, or error_<Exception>."
            )
        ),
    ]
    recent_busy_declines: Annotated[int, Field(ge=0)]
    recent_busy_same_validator: Annotated[int, Field(ge=0)]
    recent_busy_untracked_holder: Annotated[int, Field(ge=0)]
    holds_by_validator: list[DispatchFenceValidatorHolds]
    busy_by_validator: list[DispatchFenceBusyPair]


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
    allocator: Annotated[
        DispatchFenceSnapshot | None,
        Field(
            default=None,
            description=(
                "The job allocator's dispatch fence: who holds it, hold times, "
                "and whose allocator_busy polls bounced off whose."
            ),
        ),
    ]
