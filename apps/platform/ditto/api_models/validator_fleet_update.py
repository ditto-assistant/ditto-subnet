"""Audited operator control for interrupting and updating managed validators."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

CONFIRMATION = "FORCE UPDATE VALIDATOR FLEET"


class ValidatorFleetUpdateTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    validator_hotkey: str
    software_version: str
    stack_revision: str | None = None
    active_lease_count: int = 0
    acknowledged: bool = False


class ValidatorFleetUpdateOperationView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: UUID
    expected_snapshot: str
    targets: list[ValidatorFleetUpdateTarget]
    revoked_lease_count: int
    acknowledged_count: int
    actor: str
    reason: str
    created_at: datetime


class ValidatorFleetUpdatePreview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    generated_at: datetime
    snapshot: str
    target_count: int
    active_lease_count: int
    targets: list[ValidatorFleetUpdateTarget]
    latest_operation: ValidatorFleetUpdateOperationView | None = None
    confirmation: Literal["FORCE UPDATE VALIDATOR FLEET"] = (
        "FORCE UPDATE VALIDATOR FLEET"
    )


class AdminValidatorFleetUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    expected_snapshot: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str


class AdminValidatorFleetUpdateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: ValidatorFleetUpdateOperationView
    idempotent: bool
