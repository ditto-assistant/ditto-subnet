"""Operator model for the subnet liveness read (ditto-subnet#2600, invariant 1).

See :mod:`ditto.api_server.subnet_liveness` for how each signal is derived
from durable Platform state and why each default threshold was chosen.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

LivenessStatus = Literal["ok", "warn", "breach"]

LivenessSignalName = Literal[
    "screening_admission",
    "oldest_claimable_upload",
    "scoring_throughput",
    "v13_scorer_cohort_pin",
    "oldest_actionable_hold",
    "lease_overrun",
    "source_emission_collector",
]

LivenessUnit = Literal["seconds", "members"]


class SubnetLivenessSignal(BaseModel):
    """One liveness signal: a measured value compared against its thresholds.

    Higher ``value`` is always worse. ``status`` is ``breach`` when ``value``
    reaches ``threshold``, ``warn`` when it reaches ``warn_threshold``, and
    ``ok`` otherwise. A null ``value`` means the signal does not apply right
    now (for example no scorer pin is active); it is always ``ok``.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    name: LivenessSignalName
    status: LivenessStatus
    value: Annotated[float | None, Field(default=None, ge=0)]
    unit: LivenessUnit
    warn_threshold: Annotated[float | None, Field(default=None, gt=0)]
    threshold: Annotated[
        float, Field(gt=0, description="Breach threshold, in the same unit as value.")
    ]
    since: Annotated[
        datetime | None,
        Field(
            default=None,
            description=(
                "Start of the measured clock when it is derivable from durable "
                "state; null when the value is zero, not applicable, or the "
                "start is not recorded."
            ),
        ),
    ]
    hint: Annotated[str, Field(max_length=400)]
    detail: dict[str, int | float | str | bool | None] = Field(default_factory=dict)


class SubnetLivenessUnavailableSignal(BaseModel):
    """A #2600 liveness signal this read cannot derive from durable state."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    name: str
    reason: str


class SubnetLiveness(BaseModel):
    """Read-only liveness rollup; it pages nobody and changes nothing."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    generated_at: datetime
    environment: str
    status: LivenessStatus
    signals: list[SubnetLivenessSignal]
    unavailable: list[SubnetLivenessUnavailableSignal]
