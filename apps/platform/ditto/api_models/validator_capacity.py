"""Operator model for the validator capacity summary (ditto-subnet#2036).

Telemetry slice only: it describes serviceable and claimed ordinary benchmark
slots, live assignment progress, queue age, and relay saturation so an operator
can tell a legitimately slow run from queue starvation. It schedules nothing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ditto.api_models.benchmark_capacity import BenchmarkAdmission
from ditto.api_models.benchmark_progress import BenchmarkProgressStage
from ditto.api_models.public import BenchServiceability
from ditto.api_models.ticket_status import TicketPurpose


class ValidatorCapacityAssignment(BaseModel):
    """One live ordinary lease with the progress its validator last signed."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    agent_id: UUID
    agent_name: str
    slot_id: str
    bench_version: int
    purpose: TicketPurpose
    stage: BenchmarkProgressStage | None
    started_at: Annotated[
        datetime, Field(description="When the validator ticket was issued (UTC).")
    ]
    age_seconds: Annotated[int, Field(ge=0)]
    completed_checks: int | None
    total_checks: int | None
    stalled: bool
    checks_per_minute: Annotated[
        float | None,
        Field(
            description=(
                "Estimate: completed checks divided by the minutes from ticket "
                "issue to the validator's latest heartbeat. Pre-run stages count "
                "against it, so it understates a run's steady rate. Null until "
                "at least one check has completed."
            ),
        ),
    ]
    estimated_remaining_slot_minutes: Annotated[
        float | None,
        Field(
            description=(
                "Estimate: remaining checks of the current run at "
                "`checks_per_minute`, as of the latest heartbeat. Null whenever "
                "the rate is unknown; never a guess."
            ),
        ),
    ]


class ValidatorCapacityEntry(BaseModel):
    """One live validator's ordinary slot capacity and live assignments."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    validator_hotkey: str
    seen_at: datetime
    bench_serviceability: BenchServiceability
    admission: BenchmarkAdmission
    issuance_paused: bool
    configured_slots: int
    serviceable_slots: Annotated[
        int,
        Field(
            ge=0,
            description=(
                "Healthy slots dispatch will fund right now: the fleet view's "
                "`allowed_slots` narrowed to `healthy_slots`, and zero unless the "
                "validator can serve the active benchmark."
            ),
        ),
    ]
    claimed_slots: Annotated[
        int,
        Field(
            ge=0,
            description=(
                "Distinct ordinary slots that are not free: a live lease, signed "
                "heartbeat occupancy, or an evicted lease whose container may "
                "still be running. Confirmation (longmem) slots are excluded."
            ),
        ),
    ]
    assignments: list[ValidatorCapacityAssignment]


class RelayLaneSaturation(BaseModel):
    """Live hosted-inference load on one lane against its global ceiling."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    request_kind: Literal["chat", "embedding"]
    active_requests: Annotated[int, Field(ge=0)]
    global_limit: Annotated[int, Field(ge=1)]
    saturation: Annotated[
        float, Field(ge=0, description="`active_requests / global_limit`.")
    ]


class ValidatorCapacitySummary(BaseModel):
    """Bounded fleet capacity summary built from existing heartbeat truth.

    Only validators whose heartbeat is inside the fleet view's online window
    are counted. Totals cover every live validator; ``validators`` is capped
    and ``validators_truncated`` says when rows were dropped from it.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    generated_at: datetime
    active_bench_version: int
    online_window_seconds: int
    live_validator_count: Annotated[int, Field(ge=0)]
    serviceable_validator_count: Annotated[
        int,
        Field(
            ge=0,
            description="Live validators with at least one serviceable slot.",
        ),
    ]
    serviceable_slots: Annotated[int, Field(ge=0)]
    claimed_slots: Annotated[int, Field(ge=0)]
    active_assignment_count: Annotated[int, Field(ge=0)]
    estimated_remaining_slot_minutes: Annotated[
        float,
        Field(
            ge=0,
            description=(
                "Estimate: sum of every assignment's known "
                "`estimated_remaining_slot_minutes`. Assignments without a rate "
                "are counted in `unestimated_assignment_count`, not here."
            ),
        ),
    ]
    unestimated_assignment_count: Annotated[int, Field(ge=0)]
    eligible_unleased_count: Annotated[
        int,
        Field(
            ge=0,
            description=(
                "Active-era submissions passing the fleet-wide queue filter with "
                "quorum slots left and no live lease. Owner serialization and "
                "per-validator exclusions are not applied, so this is an upper "
                "bound on leasable work."
            ),
        ),
    ]
    oldest_eligible_unleased_age_seconds: Annotated[
        int | None,
        Field(
            default=None,
            ge=0,
            description=(
                "Queue age of the oldest of those submissions on the allocator's "
                "FIFO clock (arrival clamped to the era start). Null when none "
                "wait."
            ),
        ),
    ]
    relay: list[RelayLaneSaturation]
    validators: list[ValidatorCapacityEntry]
    validators_truncated: bool
