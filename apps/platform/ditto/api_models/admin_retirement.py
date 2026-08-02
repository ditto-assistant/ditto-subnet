"""Private operator contract for retiring closed-generation submissions.

Separate from :mod:`ditto.api_models.admin_validation_retry` on purpose. A
retry or a withdrawal is a statement about *this* submission's slots; a
retirement is a statement about the *benchmark generation* it was queued
against. Keeping the wire shapes apart keeps the two readable in an audit log.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

RetirementPopulation = Literal["never_scored", "partially_scored", "finalized"]

#: The phrase an operator must type to apply a retirement.
RETIREMENT_CONFIRMATION = "RETIRE PREVIOUS GENERATION"


class AdminSubmissionRetirement(BaseModel):
    """One durable retirement record."""

    retirement_id: UUID
    agent_id: UUID
    bench_version: int
    superseded_by_version: int
    actor: str
    reason: str
    expected_snapshot: str
    score_count: int
    created_at: datetime


class AdminRetirementCandidate(BaseModel):
    """One previous-generation submission, and whether it may be retired.

    ``population`` is the field to read before acting. "Previous generation" is
    not one group:

    * ``finalized`` reached quorum in its own era. It already has a real score
      and is **never** retirement-eligible. This is the group described by
      "these were scored previously, they had a chance".
    * ``partially_scored`` collected one or two accepted scores and then the era
      closed before quorum. It was never finalized and never paid out.
    * ``never_scored`` collected nothing at all.

    Only the last two ever appear as eligible, and the counts on
    :class:`AdminRetirementPreviewResponse` exist so an operator can see the
    split before deciding.
    """

    agent_id: UUID
    miner_hotkey: str
    agent_name: str
    agent_version: int | None
    agent_status: str
    bench_version: int
    active_bench_version: int
    score_count: int
    quorum: int
    population: RetirementPopulation
    attempts_used: int
    snapshot: str
    retirement_allowed: bool
    blocking_reason: str | None
    retirement: AdminSubmissionRetirement | None
    submitted_at: datetime


class AdminRetirementPreviewResponse(BaseModel):
    """Everything an operator needs to decide, including what NOT to touch."""

    generated_at: datetime
    active_bench_version: int
    quorum: int
    eligible_count: int
    already_retired_count: int
    blocked_count: int
    finalized_prev_gen_count: int
    """Previous-generation submissions that already reached quorum.

    Deliberately NOT part of ``population_counts``: none of these are eligible
    and this action never touches one. It is reported so that an instruction
    like "retire the ones that were already scored" can be checked against the
    number it actually describes, which is this one, rather than being applied
    to the eligible set below.
    """
    population_counts: dict[str, int]
    """Eligible candidates grouped by :class:`AdminRetirementCandidate` population."""
    bench_version_counts: dict[str, int]
    """Eligible candidates grouped by the benchmark era that closed under them."""
    candidates: list[AdminRetirementCandidate]


class AdminRetirementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    expected_snapshot: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    reason: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=8),
    ]
    confirmation: Literal["RETIRE PREVIOUS GENERATION"]


class AdminRetirementResponse(BaseModel):
    retirement: AdminSubmissionRetirement
    idempotent: bool


class AdminRetirementBatchItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: UUID
    request_id: UUID
    expected_snapshot: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class AdminRetirementBatchRequest(BaseModel):
    """Retire several closed-generation submissions in one operator action.

    Each item carries its own ``expected_snapshot`` and idempotency
    ``request_id`` so the batch is exactly as safe as N single-agent
    retirements: an item whose state moved is skipped with a reason, never
    force-applied. The confirmation phrase is required once for the whole set.
    """

    model_config = ConfigDict(extra="forbid")

    reason: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=8),
    ]
    confirmation: Literal["RETIRE PREVIOUS GENERATION"]
    items: Annotated[
        list[AdminRetirementBatchItem], Field(min_length=1, max_length=100)
    ]

    @field_validator("items")
    @classmethod
    def _unique(
        cls, items: list[AdminRetirementBatchItem]
    ) -> list[AdminRetirementBatchItem]:
        if len({item.agent_id for item in items}) != len(items):
            raise ValueError("duplicate agent_id in batch")
        if len({item.request_id for item in items}) != len(items):
            raise ValueError("duplicate request_id in batch")
        return items


class AdminRetirementBatchResult(BaseModel):
    agent_id: UUID
    status: Literal["retired", "idempotent", "skipped"]
    detail: str | None
    retirement: AdminSubmissionRetirement | None


class AdminRetirementBatchResponse(BaseModel):
    retired: int
    idempotent: int
    skipped: int
    results: list[AdminRetirementBatchResult]
