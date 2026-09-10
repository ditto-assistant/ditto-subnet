"""Versioned operator settings for the copy-hold triage court."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

CourtMode = Literal["off", "shadow", "enforce"]


class CopyCourtSettings(BaseModel):
    """Strict, secret-free triage posture for copy-kind ATH holds.

    The master ``mode`` caps every per-class mode: a class never exceeds the
    master posture, so enabling one class never re-enables a class an operator
    left off. ``off`` collects nothing; ``shadow`` records recommendations and
    keeps holding; ``enforce`` lets the court resolve through the same guarded
    callable an operator uses, one class at a time.
    """

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    mode: CourtMode = "off"
    byte_identical_resubmission_mode: CourtMode = "off"
    repack_resubmission_mode: CourtMode = "off"
    cross_miner_resubmission_mode: CourtMode = "off"
    near_duplicate_mode: CourtMode = "off"
    interval_seconds: Annotated[int, Field(ge=60, le=3_600)] = 300
    max_recommendations_per_tick: Annotated[int, Field(ge=1, le=50)] = 10
    # Reserved for an LLM court over the evidence-bundle classes. Null means
    # the mechanical rule engine alone; shipping the fields now keeps the
    # settings shape stable when a model court is added.
    model: str | None = None
    prompt_revision: str | None = None

    def effective_mode(self, hold_class: str) -> CourtMode:
        """Master-capped posture for one hold class."""
        by_class: dict[str, CourtMode] = {
            "rejected_resubmission_byte_identical": (
                self.byte_identical_resubmission_mode
            ),
            "rejected_resubmission_repack": self.repack_resubmission_mode,
            "rejected_resubmission_cross_miner": (self.cross_miner_resubmission_mode),
            "near_duplicate": self.near_duplicate_mode,
        }
        class_mode = by_class.get(hold_class, "off")
        ordering: dict[CourtMode, int] = {"off": 0, "shadow": 1, "enforce": 2}
        return class_mode if ordering[class_mode] <= ordering[self.mode] else self.mode


class AdminCopyCourtSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    expected_revision: Annotated[int, Field(ge=0)]
    settings: CopyCourtSettings
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str


class CopyCourtSettingsRevision(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: int
    parent_revision: int
    settings: CopyCourtSettings
    reason: str
    actor: str
    created_at: datetime
    checksum: str


class AdminCopyCourtSettingsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    current: CopyCourtSettingsRevision | None
    history: list[CopyCourtSettingsRevision]


class AdminCopyCourtRecommendation(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    recommendation_id: str
    review_id: str
    agent_id: str
    verdict: str
    hold_class: str
    reason: str
    citations: list
    evidence: dict
    settings_revision: int
    model: str | None = None
    prompt_revision: str | None = None
    created_at: datetime


class AdminCopyCourtRecommendationList(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    items: list[AdminCopyCourtRecommendation]
    count: int
    limit: int
    offset: int


def copy_court_checksum(settings: CopyCourtSettings) -> str:
    encoded = json.dumps(
        settings.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
