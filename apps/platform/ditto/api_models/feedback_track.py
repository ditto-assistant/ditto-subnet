"""Wire models for Feedback Track contributions.

Plumbing only: who contributed what, credited to a Ditto account and read
through the miner's Ditto link. No reward, weight policy or emission math is
defined here; ``weight`` is stored for a future policy and consumed by nothing.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

FeedbackTrackSource = Literal["ditto_feedback"]
FeedbackTrackKind = Literal["report", "follow_up", "shipped"]


class FeedbackTrackContributionRequest(BaseModel):
    """One contribution recorded by the Ditto backend (operator bearer)."""

    model_config = ConfigDict(extra="ignore")

    ditto_user_id: str = Field(min_length=1, max_length=128)
    source: FeedbackTrackSource = "ditto_feedback"
    external_ref: str = Field(min_length=1, max_length=200)
    kind: FeedbackTrackKind
    weight: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=6)
    note: str | None = Field(default=None, max_length=500)


class FeedbackTrackContributionView(BaseModel):
    model_config = ConfigDict(extra="ignore")

    contribution_id: UUID
    source: FeedbackTrackSource
    external_ref: str
    kind: FeedbackTrackKind
    weight: Decimal | None = None
    note: str | None = None
    recorded_at: datetime


class FeedbackTrackContributionResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    created: bool
    contribution: FeedbackTrackContributionView


class FeedbackTrackMeResponse(BaseModel):
    """The signed-in miner's contributions, via the account linked to the hotkey."""

    model_config = ConfigDict(extra="ignore")

    linked: bool
    ditto_user_id: str | None = None
    contributions: list[FeedbackTrackContributionView]
    counts: dict[str, int]


class PublicFeedbackTrackResponse(BaseModel):
    """Counts only. Never the account, the email, or the report text."""

    model_config = ConfigDict(extra="ignore")

    miner_hotkey: str
    linked: bool
    counts: dict[str, int]
    total: int
