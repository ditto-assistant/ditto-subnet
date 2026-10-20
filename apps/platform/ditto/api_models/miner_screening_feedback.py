"""Miner-private screening failure feedback wire models."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ditto.api_models.upload import _SS58_PATTERN

MinerReviewOutcome = Literal["cleared", "rejected", "held_for_operator_review"]
"""What the automated source review settled for one attempt."""

MinerReviewNextStep = Literal[
    "none", "await_operator_review", "resubmit_after_fix", "contact_operators"
]
"""Neutral next step for the submitter; the copy for each lives client-side."""


class MinerScreeningReviewOutcome(BaseModel):
    """Bounded source-review outcome for the submitter of one attempt.

    Deliberately two closed enums and nothing else. The notes ledger, cited
    ``path:line`` locations, breached invariant, published clear clause, court
    reason text, and refusal code stay operator-side: together they describe
    what the screener inspects, so they are not part of the miner contract.
    """

    model_config = ConfigDict(extra="ignore")

    outcome: MinerReviewOutcome
    next_step: MinerReviewNextStep


class MinerScreeningFailure(BaseModel):
    model_config = ConfigDict(extra="ignore")

    attempt_id: UUID
    status: str
    policy_version: Annotated[int, Field(ge=1)]
    started_at: datetime
    finished_at: datetime | None = None
    reason_code: str | None = None
    public_reason: str | None = None
    provider: str | None = None
    lane: str | None = None
    detail: str | None = None
    log_tail: str | None = None
    captured_at: datetime | None = None
    review_outcome: MinerScreeningReviewOutcome | None = None
    """Bounded outcome of a digest-verified automated court decision; null
    when the attempt has none or it does not verify."""


class MinerScreeningFeedbackResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    agent_id: UUID
    miner_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    agent_status: str
    attempts: list[MinerScreeningFailure]
