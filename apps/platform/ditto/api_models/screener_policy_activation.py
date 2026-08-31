"""Scheduled screening-policy activation wire models.

Screening policy text ships with the deployed Platform/worker build, but the
version the screening queue REQUIRES is a subnet decision with a fairness
timeline: miners must get equal notice that the rules are changing, so the
newest text activates on a schedule instead of at deploy time.

These wire models back an append-only revision table
(``screener_policy_activations``), exactly like
``ditto.api_models.queue_policy_settings``. Before any revision exists, or
before the first ``activate_at`` passes, the platform requires
``SCREENING_FLOOR_POLICY_VERSION`` and dual-text workers screen under it; when
an activation is due the required version rises to ``target_policy_version``
and every agent screened under a stale version re-enters the screening queue
on the same criteria.

Wire models ignore unknown JSON fields so rolling upgrades can add fields
without breaking older consumers (repository convention: never
``extra="forbid"``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

CONFIRMATION = "SCHEDULE SCREENER POLICY ACTIVATION"
RESTORE_SCORED_CONFIRMATION = "RESTORE SCORED SCREENING SNAPSHOT"
ADVANCE_SCORED_RESCREEN_CONFIRMATION = "ADVANCE SCORED POLICY RESCREEN"

ActivationState = Literal["pending", "due"]


class ScreenerPolicyActivationRevision(BaseModel):
    """One immutable schedule revision."""

    model_config = ConfigDict(extra="ignore")

    revision: int
    parent_revision: int = Field(ge=0)
    target_policy_version: int = Field(ge=1)
    activate_at: datetime
    rescreen_scored: bool
    canary_only: bool = False
    reason: str
    actor: str
    created_at: datetime
    state: ActivationState


class ScheduleScreenerPolicyActivationRequest(BaseModel):
    """Append one optimistic, confirmation-gated schedule revision.

    ``activate_at`` MUST carry a UTC offset (or be an explicit ``Z`` instant):
    a timezone-naive datetime is rejected so an operator who means 9 a.m.
    Eastern cannot silently schedule 9 a.m. server time. The row stores UTC.
    """

    model_config = ConfigDict(extra="ignore")

    expected_revision: int = Field(ge=0)
    target_policy_version: int = Field(ge=1)
    activate_at: datetime
    rescreen_scored: bool = True
    # Keep the ordinary queue at the floor version and allow only explicit
    # scored-rollout releases to attest the target policy.  This is the safe
    # way to exercise a new policy while source review is otherwise off.
    canary_only: bool = False
    reason: str = Field(min_length=8)
    confirmation: str
    # Carried by Backroom MCP writes for the operator audit trail; the
    # authenticated admin identity is authoritative when both are present.
    actor: str | None = Field(default=None, max_length=120)


class ScreenerPolicyActivationView(BaseModel):
    """What the screening queue requires now, plus the governing schedule."""

    model_config = ConfigDict(extra="ignore")

    effective_policy_version: int
    floor_policy_version: int
    builtin_policy_version: int
    latest: ScreenerPolicyActivationRevision | None
    revisions: list[ScreenerPolicyActivationRevision]


ScoredRescreenState = Literal["pending", "running", "paused", "terminal"]


class ScoredPolicyRescreenReleaseView(BaseModel):
    """The one scored submission currently released under a policy rollout."""

    model_config = ConfigDict(extra="ignore")

    activation_revision: int = Field(ge=1)
    target_policy_version: int = Field(ge=1)
    agent_id: UUID
    position: int = Field(ge=1)
    state: ScoredRescreenState
    attempt_id: UUID | None
    review_settings_revision: int | None = None


class ScoredPolicyRescreenView(BaseModel):
    """Read-only rollout checkpoint for the score-preserving rescreen lane."""

    model_config = ConfigDict(extra="ignore")

    activation_revision: int | None
    target_policy_version: int | None
    current: ScoredPolicyRescreenReleaseView | None
    next_agent_id: UUID | None
    next_position: int | None


class AdvanceScoredPolicyRescreenRequest(BaseModel):
    """Release exactly one top-down scored policy rescreen, or retry its pause."""

    model_config = ConfigDict(extra="ignore")

    expected_activation_revision: int = Field(ge=1)
    expected_agent_id: UUID
    retry_paused: bool = False
    review_settings_revision: int | None = Field(default=None, ge=1)
    reason: str = Field(min_length=8)
    confirmation: str
    actor: str | None = Field(default=None, max_length=120)


class RestoreScoredScreeningSnapshotRequest(BaseModel):
    """Restore the last pre-activation pass for an exact scored cohort.

    This is an incident-recovery operation, not a retry: it never creates a
    screening attempt, build, dataset, score, or validator lease.
    """

    model_config = ConfigDict(extra="ignore")

    expected_current_activation_revision: int = Field(ge=1)
    source_activation_revision: int = Field(ge=1)
    source_policy_version: int = Field(ge=1)
    target_policy_version: int = Field(ge=1)
    bench_version: int = Field(ge=1)
    expected_count: int = Field(ge=1, le=500)
    reason: str = Field(min_length=8)
    confirmation: str
    actor: str | None = Field(default=None, max_length=120)


class RestoredScoredSubmission(BaseModel):
    """One immutable restoration audit result."""

    model_config = ConfigDict(extra="ignore")

    agent_id: str
    displaced_attempt_id: str
    restored_attempt_id: str
    restored_policy_version: int
    score_count: int


class RestoreScoredScreeningSnapshotResponse(BaseModel):
    """Result of one atomic scored-snapshot restoration batch."""

    model_config = ConfigDict(extra="ignore")

    batch_id: str
    restored_count: int
    source_activation_revision: int
    current_activation_revision: int
    source_policy_version: int
    target_policy_version: int
    bench_version: int
    submissions: list[RestoredScoredSubmission]
