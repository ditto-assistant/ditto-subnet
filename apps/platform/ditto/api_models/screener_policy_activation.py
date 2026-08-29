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

from pydantic import BaseModel, ConfigDict, Field

CONFIRMATION = "SCHEDULE SCREENER POLICY ACTIVATION"

ActivationState = Literal["pending", "due"]


class ScreenerPolicyActivationRevision(BaseModel):
    """One immutable schedule revision."""

    model_config = ConfigDict(extra="ignore")

    revision: int
    parent_revision: int = Field(ge=0)
    target_policy_version: int = Field(ge=1)
    activate_at: datetime
    rescreen_scored: bool
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
