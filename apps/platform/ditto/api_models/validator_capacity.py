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
    inference_chat_requests: Annotated[
        int | None,
        Field(
            ge=0,
            description=(
                "Chat requests this lease's live hosted-inference grant has "
                "served so far. Null when the lease has no live grant."
            ),
        ),
    ] = None
    inference_embedding_requests: Annotated[
        int | None,
        Field(
            ge=0,
            description=(
                "Embedding requests this lease's live grant has served so far. "
                "Null when the lease has no live grant."
            ),
        ),
    ] = None
    inference_last_activity_at: Annotated[
        datetime | None,
        Field(
            description=(
                "When this lease's grant last began or finished a request. A "
                "recent time means the harness is executing whatever `stage` "
                "says; `stalled` is false while it is inside the 15-minute "
                "grace. Null before the first request or without a live grant."
            ),
        ),
    ] = None
    inference_idle_seconds: Annotated[
        int | None,
        Field(
            ge=0,
            description=(
                "Seconds since `inference_last_activity_at`, as of this read."
            ),
        ),
    ] = None


SlotUnusableReason = Literal[
    "admission_draining",
    "admission_paused",
    "admission_resource_constrained",
    "host_memory_admission",
    "validator_unhealthy",
    "bench_scorer_unverified",
    "bench_software_obsolete",
    "issuance_paused",
    "platform_slot_cap",
]
"""Why one configured slot cannot take a ticket, in the order it is judged.

The first three are the validator's own admission: a host that is not
``accepting`` offers no healthy slot. ``host_memory_admission`` is a slot past
``(host memory - reserve) // harness cap``, derived here from the host's
announced memory and the live sandbox memory cap -- the validator withholds it
because that many harnesses at the cap would overcommit the host.
``validator_unhealthy`` is any other slot the validator did not offer (a
sibling infrastructure failure, a resource-blocked slot, scorer capacity
below the configured count). The last three are Platform's: the validator
cannot serve the active benchmark, Backroom paused its issuance, or the
operator slot cap (or a tripped resource breaker) funds fewer slots than the
validator offers.
"""


class ValidatorCapacitySlot(BaseModel):
    """One configured slot and what it is doing, or why it cannot work."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    slot_id: str
    state: Annotated[
        Literal["claimed", "free", "reserved_round", "unusable"],
        Field(
            description=(
                "`claimed` holds a lease or signed occupancy; `free` can take "
                "canonical or retest work now; `reserved_round` is held back for "
                "generator-round runs; `unusable` cannot take any ticket, and "
                "`reason` says why."
            )
        ),
    ]
    reason: SlotUnusableReason | None = None


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
    reserved_round_slots: Annotated[
        int,
        Field(
            ge=0,
            description=(
                "Of the allowed slots, how many are held back for generator-"
                "round runs: canonical and retest work cannot take them. Zero "
                "while validator rounds are disabled or the validator does not "
                "advertise round support."
            ),
        ),
    ] = 0
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
    memory_admitted_slots: Annotated[
        int | None,
        Field(
            default=None,
            ge=0,
            description=(
                "Derived: harnesses at the live sandbox memory cap this host's "
                "announced memory admits after the published host reserve. Null "
                "when the host announced no memory. A validator with a custom "
                "reserve may admit slightly fewer or more."
            ),
        ),
    ]
    memory_required_mib: Annotated[
        int | None,
        Field(
            default=None,
            ge=0,
            description=(
                "Derived: the least host memory that admits every configured "
                "slot at the live sandbox memory cap. Null when unknown."
            ),
        ),
    ]
    memory_note: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "When memory admits fewer slots than configured: how many, and "
                "the host memory needed for one slot (if none) and for all."
            ),
        ),
    ]
    unusable_slots: Annotated[
        int,
        Field(ge=0, description="Configured slots whose `state` is `unusable`."),
    ] = 0
    slots: Annotated[
        list[ValidatorCapacitySlot],
        Field(
            default_factory=list,
            description="Every configured slot with its state or unusable reason.",
        ),
    ]


class RelayLaneSaturation(BaseModel):
    """Live hosted-inference load on one lane against its global ceiling."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    request_kind: Literal["chat", "embedding", "decision"]
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
    configured_slots: Annotated[
        int,
        Field(ge=0, description="Sum of every live validator's configured slots."),
    ] = 0
    memory_admitted_slots: Annotated[
        int,
        Field(
            ge=0,
            description=(
                "Sum over live validators that announced memory of "
                "`min(memory_admitted_slots, configured_slots)`: the most "
                "slots this fleet's hosts can run at the live cap, whatever "
                "`configured_slots` says."
            ),
        ),
    ] = 0
    serviceable_slots: Annotated[int, Field(ge=0)]
    unusable_slots_by_reason: Annotated[
        dict[str, int],
        Field(
            default_factory=dict,
            description=(
                "Configured slots that cannot take a ticket, by reason, across "
                "every live validator. Claimed slots are never counted here."
            ),
        ),
    ]
    sandbox_memory_limit_mib: Annotated[
        int | None,
        Field(
            default=None,
            description=(
                "The live per-harness memory cap `memory_admitted_slots` uses."
            ),
        ),
    ]
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
