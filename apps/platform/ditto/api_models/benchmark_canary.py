"""Explicit, compare-and-swap guarded single-run diagnostic authority."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class BenchmarkCanaryIssue(BaseModel):
    model_config = ConfigDict(extra="ignore")

    canary_id: UUID
    agent_id: UUID
    bench_version: Annotated[int, Field(ge=1)]
    validator_hotkey: Annotated[str, Field(pattern=r"^[1-9A-HJ-NP-Za-km-z]{48}$")]
    slot_id: Annotated[str, Field(pattern=r"^slot-[0-7]$")]
    expected_artifact_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    expected_screened_image_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    expected_active_version: Annotated[int, Field(ge=1)]
    actor: Annotated[str, Field(min_length=1, max_length=120)]
    reason: Annotated[str, Field(min_length=8)]
    confirmation: str


class BenchmarkCanaryView(BaseModel):
    model_config = ConfigDict(extra="ignore", from_attributes=True)

    canary_id: UUID
    agent_id: UUID
    bench_version: int
    validator_hotkey: str
    slot_id: str
    artifact_sha256: str
    screened_image_sha256: str
    seed: str = Field(coerce_numbers_to_str=True)
    dataset_sha256: str
    run_size: str
    actor: str
    reason: str
    issued_at: datetime
    deadline: datetime
    status: str
    finished_at: datetime | None
    result: dict | None
    failure_detail: str | None
    authoritative: Literal[False] = False


class BenchmarkCanaryCancel(BaseModel):
    model_config = ConfigDict(extra="ignore")
    actor: Annotated[str, Field(min_length=1, max_length=120)]
    reason: Annotated[str, Field(min_length=8)]
    confirmation: str
