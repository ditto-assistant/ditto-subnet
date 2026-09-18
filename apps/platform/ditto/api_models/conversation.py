"""Private conversation lane control and observation, separate from rewards."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from ditto.api_models.submission_settings import AdminSubmissionSettingsRequest
from ditto_screening_protocol.conversation import (
    ConversationClaim,
    ConversationReport,
    WireModel,
)

__all__ = ["ConversationClaim", "ConversationReport"]


class ConversationResultRequest(WireModel):
    lease_token: UUID
    report: ConversationReport


class ConversationObservation(WireModel):
    assessment_id: UUID
    agent_id: UUID
    artifact_sha256: str
    bench_version: int
    status: Literal["leased", "completed", "incomplete", "expired"]
    created_at: datetime
    expires_at: datetime
    base_quality_micros: int
    conversation_micros: int | None
    proposed_quality_micros: int | None
    reserved_microusd: int
    spent_microusd: int | None
    error_code: str | None


class ConversationObservations(WireModel):
    mode: Literal["off", "shadow"]
    instrument: str
    judge_model: str
    daily_budget_microusd: int
    reserved_last_day_microusd: int
    proposed_submission_fee_rao: Literal[200_000_000] = 200_000_000
    current_submission_fee_rao: int
    fee_change_request: AdminSubmissionSettingsRequest
    items: Annotated[list[ConversationObservation], Field(max_length=100)]
