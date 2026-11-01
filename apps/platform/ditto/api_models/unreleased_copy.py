"""Operator wire models for the unreleased-copy signal (Backroom only).

Evidence names other owners' unreleased files and text; it is served only on
admin routes and never on a public or miner surface.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


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
    shared_count: int
    shared_by_kind: dict[str, int]
    items: list[UnreleasedCopyItem]


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
