"""Redacted operator projection for the shadow Coding control plane."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from ditto.api_models.coding_evaluation import CodingEvaluationModel, Sha256

CodingHostedOperationState = Literal[
    "pending_admission",
    "admitted",
    "running",
    "completed",
    "failed",
    "aborted",
    "expired",
]


class CodingHostedOperationRecord(CodingEvaluationModel):
    evaluation_id: UUID
    attempt_id: UUID
    release_row_id: UUID
    registration_sha256: Sha256
    agent_id: UUID
    validator_hotkey: Annotated[str, Field(pattern=r"^[1-9A-HJ-NP-Za-km-z]{47,48}$")]
    artifact_sha256: Sha256
    screened_image_sha256: Sha256
    assignment_sha256: Sha256
    state: CodingHostedOperationState
    expires_at: datetime
    created_at: datetime
    admitted_at: datetime | None
    started_at: datetime | None
    frozen: bool
    closed_at: datetime | None
    close_reason: Literal["completed", "failed", "aborted"] | None
    registered_actor: str
    registered_reason: str
    shadow_only: Literal[True] = True
    weight_eligible: Literal[False] = False


class AdminCodingControlPlaneResponse(CodingEvaluationModel):
    total_native_operations: Annotated[int, Field(ge=0)]
    native_operations: list[CodingHostedOperationRecord]
    hosted_control_configured: bool
    contract_v1_reconciliation_enabled: bool
    contract_v1_ticket_set_enabled: bool
    contract_v1_ticket_lease_seconds: Annotated[int, Field(ge=60, le=7200)]
    native_v2_selectable: Literal[False] = False
    shadow_only: Literal[True] = True
    weight_eligible: Literal[False] = False


__all__ = [
    "AdminCodingControlPlaneResponse",
    "CodingHostedOperationRecord",
    "CodingHostedOperationState",
]
