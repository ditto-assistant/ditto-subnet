"""Versioned operator settings for the copy-hold triage court."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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

    # Recording: whenever ``mode`` is not ``off`` every pending copy hold gets
    # a recommendation (per settings revision and triage revision); a class
    # mode gates only what the court may *apply*. ``near_duplicate_mode:
    # enforce`` is the auto-apply switch for the narrow deterministic family
    # CLEAR (overlap fully explained by a proven-public base, none of the
    # unreleased matched agent's distinctive lines present, held artifact
    # screened under its current policy, no crown change). The court never
    # applies a reject or an escalation, and enforce is refused for every
    # other class.
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

    @model_validator(mode="after")
    def _enforce_only_near_duplicate(self) -> CopyCourtSettings:
        for name in (
            "byte_identical_resubmission_mode",
            "repack_resubmission_mode",
            "cross_miner_resubmission_mode",
        ):
            if getattr(self, name) == "enforce":
                raise ValueError(
                    f"{name}=enforce is not supported: the court applies only the "
                    "deterministic near-duplicate family CLEAR"
                )
        return self

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


class AdminCopyCourtRuntime(BaseModel):
    """The in-process court loop's last tick, as this Platform process saw it."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    running: bool
    interval_seconds: float
    last_tick_started_at: datetime | None = None
    last_tick_finished_at: datetime | None = None
    last_tick_counters: dict[str, int] = Field(default_factory=dict)
    last_tick_error: str | None = None
    last_hold_errors: list[dict[str, str]] = Field(default_factory=list)


class AdminCopyCourtSettingsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    current: CopyCourtSettingsRevision | None
    history: list[CopyCourtSettingsRevision]
    runtime: AdminCopyCourtRuntime | None = None


class AdminCopyCourtTriagePreview(BaseModel):
    """A dry-run family triage for one copy-kind ATH review. Records nothing."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    agent_id: str
    review_id: str
    review_status: str
    hold_class: str
    triage_revision: str
    verdict: str
    reason: str
    citations: list
    evidence: dict
    auto_clear_eligible: bool


class AdminCopyCourtRecommendation(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    recommendation_id: str
    # Which engine produced the verdict: ``rules-v1`` for the resubmission
    # classes, ``family-diff-v1`` for the near-duplicate attribution triage.
    triage_revision: str = "rules-v1"
    # Set when the court acted on (or declined to act on) the recommendation
    # in enforce mode: ``{"status": "applied"|"skipped", "detail", ...}``.
    application: dict | None = None
    applied_at: datetime | None = None
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
