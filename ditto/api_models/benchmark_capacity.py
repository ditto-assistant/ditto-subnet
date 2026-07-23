"""Signed bounded benchmark-slot capacity for heartbeat protocol v10+."""

from __future__ import annotations

import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ditto.api_models.benchmark_progress import BenchmarkProgress

_SLOT_PATTERN = r"^slot-[0-7]$"

BenchmarkAdmission = Literal["accepting", "draining", "paused"]


class ActiveBenchmarkSlot(BaseModel):
    """One active, ticket-bound benchmark execution slot."""

    model_config = ConfigDict(extra="forbid")

    slot_id: Annotated[str, Field(pattern=_SLOT_PATTERN)]
    agent_id: UUID
    bench_version: Annotated[int, Field(ge=1)]
    progress: BenchmarkProgress
    healthy: bool = True


class BenchmarkCapacity(BaseModel):
    """Authoritative admission and per-slot progress advertised by a validator.

    ``configured_slots`` is deliberately bounded to eight. ``healthy_slots``
    lists slots that may accept work; an active slot may be absent while it is
    draining or unhealthy, but the platform will never place new work there.
    """

    model_config = ConfigDict(extra="forbid")

    configured_slots: Annotated[int, Field(ge=1, le=8)] = 1
    healthy_slots: Annotated[list[str], Field(max_length=8)] = Field(
        default_factory=lambda: ["slot-0"]
    )
    admission: BenchmarkAdmission = "accepting"
    active: Annotated[list[ActiveBenchmarkSlot], Field(max_length=8)] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def validate_slots(self) -> BenchmarkCapacity:
        configured = {f"slot-{index}" for index in range(self.configured_slots)}
        healthy = set(self.healthy_slots)
        active_ids = [slot.slot_id for slot in self.active]
        if len(healthy) != len(self.healthy_slots):
            raise ValueError("healthy benchmark slot ids must be unique")
        if len(set(active_ids)) != len(active_ids):
            raise ValueError("active benchmark slot ids must be unique")
        if not healthy.issubset(configured):
            raise ValueError("healthy benchmark slots exceed configured capacity")
        if not set(active_ids).issubset(configured):
            raise ValueError("active benchmark slots exceed configured capacity")
        if self.admission != "accepting" and healthy:
            raise ValueError(
                "draining or paused capacity cannot advertise healthy slots"
            )
        return self

    @property
    def free_healthy_slots(self) -> tuple[str, ...]:
        active = {slot.slot_id for slot in self.active}
        if self.admission != "accepting":
            return ()
        return tuple(slot for slot in self.healthy_slots if slot not in active)


def benchmark_capacity_signing_token(capacity: BenchmarkCapacity | None) -> str:
    """Return one length-prefixed canonical JSON token for protocol v10+."""
    if capacity is None:
        return "0:"
    encoded = json.dumps(
        capacity.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"{len(encoded.encode('utf-8'))}:{encoded}"
