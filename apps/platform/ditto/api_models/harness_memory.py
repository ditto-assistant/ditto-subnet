"""Operator models for per-agent harness memory (SN-500 step 1).

Backroom-only. Built from validator-reported, unsigned scorer telemetry; it
schedules nothing and scores nothing. The public per-agent view is
``harness_memory`` on the agent's pipeline.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

_BYTES = Annotated[int, Field(ge=0)]


class HarnessMemoryRollupView(BaseModel):
    """One agent version's memory across every observed run."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    agent_id: UUID
    agent_name: str | None = None
    miner_hotkey: str | None = None
    runs: Annotated[int, Field(ge=0)]
    scored_runs: Annotated[int, Field(ge=0)]
    failed_runs: Annotated[int, Field(ge=0)]
    oom_runs: Annotated[
        int,
        Field(
            ge=0,
            description=(
                "Failed runs whose detail is `harness_oom` (charged) or "
                "`harness_oom_grace` (the notice period's no-fault form)."
            ),
        ),
    ]
    peak_bytes_max: Annotated[
        int, Field(ge=0, description="Highest per-run `memory.peak`.")
    ]
    p50_bytes_median: Annotated[
        int, Field(ge=0, description="Median of the per-run time-weighted P50s.")
    ]
    p95_bytes_median: Annotated[
        int, Field(ge=0, description="Median of the per-run time-weighted P95s.")
    ]
    p95_bytes_max: Annotated[
        int, Field(ge=0, description="Highest per-run time-weighted P95.")
    ]
    limit_bytes_max: Annotated[
        int | None,
        Field(description="Largest harness cap any observed run ran under."),
    ] = None
    last_recorded_at: datetime


class HarnessMemoryRunView(BaseModel):
    """One lease's observation."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    validator_hotkey: str
    bench_version: Annotated[int, Field(ge=1)]
    lease_issued_at: datetime
    outcome: Literal["scored", "failed"]
    failure_reason: str | None = None
    failure_detail: str | None = None
    peak_bytes: _BYTES
    p50_bytes: _BYTES
    p95_bytes: _BYTES
    samples: Annotated[int, Field(ge=0)]
    sampled_seconds: Annotated[float, Field(ge=0)]
    limit_bytes: Annotated[int | None, Field(ge=0)] = None
    recorded_at: datetime


class AgentHarnessMemory(BaseModel):
    """One agent's rollup and newest runs. ``rollup`` is null with no runs."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    agent_id: UUID
    rollup: HarnessMemoryRollupView | None
    runs: list[HarnessMemoryRunView]


class HarnessMemoryList(BaseModel):
    """Agents with observations, heaviest P95 first."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    generated_at: datetime
    agents: list[HarnessMemoryRollupView]
    offset: Annotated[int, Field(ge=0)] = 0
    has_more: Annotated[
        bool, Field(description="More agents follow; page with offset.")
    ] = False
    note: Annotated[
        str,
        Field(
            description=(
                "Coverage reminder: observations start with the release that "
                "added them; no earlier run has memory data."
            )
        ),
    ]
