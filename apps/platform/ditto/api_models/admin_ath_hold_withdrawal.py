"""Admin contracts for withdrawing an unsupported precautionary ATH hold."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from ditto.api_models.admin_ath_rulings import AdminAthRulingsBoardProjection
from ditto.api_models.admin_copy_review import AdminCopyReviewItem

_Sha256 = Annotated[
    str, StringConstraints(strip_whitespace=True, pattern=r"^[0-9a-f]{64}$")
]
_Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3)]
_Status = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)
]


class AdminAthHoldWithdrawalPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    review_id: UUID
    expected_sha256: _Sha256
    expected_score_count: Annotated[int, Field(ge=0)]
    expected_agent_status: _Status
    reason: _Reason


class AdminAthHoldWithdrawalPreviewResponse(BaseModel):
    """Dry-run of restoring rank without granting a policy clearance."""

    model_config = ConfigDict(extra="ignore")
    agent_id: UUID
    review_id: UUID
    artifact_sha256: str
    score_count: int
    agent_status: str
    restored_status: Literal["scored", "live"]
    board_before: AdminAthRulingsBoardProjection
    board_after: AdminAthRulingsBoardProjection
    would_change_crown: bool
    emission_reward_eligible: bool
    emission_gate: Literal["unavailable"]
    would_change_emission_crown: bool
    emission_reason: str
    preview_token: str
    expires_at: datetime


class AdminAthHoldWithdrawalExecuteRequest(AdminAthHoldWithdrawalPreviewRequest):
    preview_token: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=20)
    ]
    confirmation: _Reason


class AdminAthHoldWithdrawalExecuteResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    review: AdminCopyReviewItem
    agent_status: str
    restored_status: Literal["scored", "live"]
    emission_reward_eligible: bool
    emission_gate: Literal["unavailable"]
    emission_reason: str
