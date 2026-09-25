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


class ConversationRetryAuthorization(WireModel):
    actor: Annotated[str, Field(min_length=3, max_length=200)]
    reason: Annotated[str, Field(min_length=10)]
    report_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    authorized_at: datetime
    expires_at: datetime


class ConversationRetryRequest(WireModel):
    expected_revision: Annotated[int, Field(strict=True, ge=0)]
    assessment_id: UUID
    expected_artifact_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    expected_report_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    actor: Annotated[str, Field(min_length=3, max_length=200)]
    reason: Annotated[str, Field(min_length=10)]
    confirmation: Literal["AUTHORIZE ONE CONVERSATION RETRY"]


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
    report_sha256: str | None = None
    retry_of: UUID | None = None
    retry_authorization: ConversationRetryAuthorization | None = None
    retry_assessment_id: UUID | None = None


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
    settings_revision: int = 0
    settings_actor: str | None = None
    settings_reason: str | None = None
    settings_updated_at: datetime | None = None
    next_budget_slot_at: datetime | None = None


class ConversationSettingsRequest(WireModel):
    expected_revision: Annotated[int, Field(strict=True, ge=0)]
    mode: Literal["off", "shadow"]
    actor: Annotated[str, Field(min_length=3, max_length=200)]
    reason: Annotated[str, Field(min_length=10)]
    confirmation: Literal["APPLY CONVERSATION SHADOW SETTINGS"]
