"""Operator wire models for the unreleased-copy signal (Backroom only).

Evidence names other owners' unreleased files and text; it is served only on
admin routes and never on a public or miner surface.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class _Model(BaseModel):
    """Additive fields stay forward compatible (no ``extra="forbid"``)."""


class UnreleasedCopyItem(_Model):
    kind: str
    text: str
    """Normalized item text (lower-case words, truncated)."""
    candidate: str
    """``path:line`` of the item in the candidate artifact."""
    matched: str
    """``path:line`` of the same item in the matched artifact."""


class UnreleasedCopyMatch(_Model):
    agent_id: UUID
    agent_name: str
    agent_version: int | None
    miner_hotkey: str
    agent_status: str
    uploaded_at: datetime
    released_at: datetime | None
    release_state: Literal["unreleased", "released_after_upload"]
    """Release state as of the candidate's upload: never released before it."""
    score: float
    probable_same_owner: bool = False
    """Both reference a rare personal external namespace (Hugging Face or
    GitHub account, registry user): listed, never flagged."""
    shared_count: int
    shared_by_kind: dict[str, int]
    items: list[UnreleasedCopyItem]


class UnreleasedCopyEnforcement(_Model):
    outcome: str
    """``held``, ``deferred:held_at_score_finalization`` or ``not_held:<why>``."""
    settings_revision: int
    at: datetime


class UnreleasedCopyEvaluation(_Model):
    signal_version: str
    items_version: str
    threshold: float
    flagged: bool
    top_score: float
    candidate_item_count: int
    compared_agent_count: int
    excluded: dict[str, int]
    evidence_status: Literal["attached", "not_needed", "unavailable"]
    matches: list[UnreleasedCopyMatch]
    enforcement: UnreleasedCopyEnforcement | None = None


class AdminUnreleasedCopyIndex(_Model):
    items_version: str
    item_count: int
    error: str | None
    indexed_at: datetime


class AdminUnreleasedCopyRuntime(_Model):
    running: bool
    mode: str
    signal_version: str
    threshold: float
    interval_seconds: float
    storage_configured: bool
    last_tick_started_at: datetime | None
    last_tick_finished_at: datetime | None
    last_tick_counters: dict[str, int]
    last_tick_error: str | None


class AdminUnreleasedCopyReleasedOverlap(_Model):
    """One agent released before the candidate's upload that shares its items."""

    agent_id: UUID
    agent_name: str
    agent_version: int | None
    released_at: datetime
    shared: int


class AdminUnreleasedCopySignal(_Model):
    agent_id: UUID
    agent_name: str
    agent_version: int | None
    miner_hotkey: str
    agent_status: str
    uploaded_at: datetime
    index: AdminUnreleasedCopyIndex | None
    mode: str | None
    """``shadow`` while the signal only records; ``None`` before evaluation."""
    computed_at: datetime | None
    recorded: UnreleasedCopyEvaluation | None
    live: UnreleasedCopyEvaluation | None
    """Read-only recompute against the current index (``live=true`` only)."""
    live_unavailable_reason: str | None
    runtime: AdminUnreleasedCopyRuntime | None
    released_overlap: list[AdminUnreleasedCopyReleasedOverlap] = []
    """Live only: the released-before-upload agents that explain the most
    candidate items (the ``released_base`` exclusion), at most 10."""


class AdminUnreleasedCopySummary(_Model):
    agent_id: UUID
    agent_name: str
    agent_version: int | None
    miner_hotkey: str
    agent_status: str
    uploaded_at: datetime
    mode: str
    flagged: bool
    top_score: float
    top_matched_agent_id: UUID | None
    top_matched_name: str | None
    top_matched_version: int | None
    computed_at: datetime
    enforcement_outcome: str | None = None
    excluded: dict[str, int] | None = None
    """Candidate items each filter removed (released_base, common,
    released_fuzzy): why a score is what it is."""
    candidate_item_count: int | None = None
    compared_agent_count: int | None = None


class AdminUnreleasedCopyCoverage(_Model):
    agents: int
    indexed: int
    unreadable: int
    oldest_unindexed_at: datetime | None
    evaluated: int
    flagged: int


class AdminUnreleasedCopySignalList(_Model):
    items: list[AdminUnreleasedCopySummary]
    total: int
    limit: int
    offset: int
    coverage: AdminUnreleasedCopyCoverage
    runtime: AdminUnreleasedCopyRuntime | None


UnreleasedCopyMode = Literal["off", "shadow", "enforce"]


class UnreleasedCopySettings(_Model):
    mode: UnreleasedCopyMode
    threshold: float = Field(default=40.0, gt=0, le=100000)


class AdminUnreleasedCopySettingsRevision(_Model):
    revision: int
    parent_revision: int
    settings: UnreleasedCopySettings
    reason: str
    actor: str
    created_at: datetime


class AdminUnreleasedCopyEffectiveSettings(_Model):
    mode: UnreleasedCopyMode
    threshold: float
    revision: int
    """0 when no revision exists (shadow at the default threshold)."""
    enforce_since: datetime | None
    """Only agents uploaded at or after this are held; enforcement is not
    retroactive."""


class AdminUnreleasedCopySettingsResponse(_Model):
    effective: AdminUnreleasedCopyEffectiveSettings
    history: list[AdminUnreleasedCopySettingsRevision]
    runtime: AdminUnreleasedCopyRuntime | None


class AdminUnreleasedCopySettingsRequest(_Model):
    expected_revision: int = Field(ge=0)
    settings: UnreleasedCopySettings
    reason: str = Field(min_length=8)
    actor: str = Field(min_length=1, max_length=120)
    confirmation: str


class AdminUnreleasedCopyOverview(_Model):
    """The signal's posture beside the copy court that rules on its holds."""

    effective: AdminUnreleasedCopyEffectiveSettings
    coverage: AdminUnreleasedCopyCoverage
    runtime: AdminUnreleasedCopyRuntime | None
    flagged: list[AdminUnreleasedCopySummary]
    """Highest-scoring flagged agents (operator review list), at most 50."""
    near_misses: list[AdminUnreleasedCopySummary] = []
    """Highest-scoring unflagged agents, at most 25: calibration headroom."""
