"""Read-only operator projection of the finalized-block confirmation seed anchors.

"Is the reign pinned yet?" is the first live-diagnosis question at a binding
bench version: until the anchor pins, the continual lane issues catch-up only
and every validator-derived lane defers (or, under the observe posture, falls
back to the legacy family). The ledger serves pinned anchors only, so the
waiting rows are visible nowhere else. This is the Backroom read surface for
them; it never mutates.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AdminConfirmationSeedAnchor(BaseModel):
    """One ``(champion, bench_version)`` reign anchor, pinned or still waiting."""

    model_config = ConfigDict(extra="ignore")

    champion_agent_id: UUID
    champion_name: str | None = None
    champion_miner_hotkey: str | None = None
    bench_version: Annotated[int, Field(ge=1)]
    ready_block: Annotated[int, Field(ge=0)]
    """``B_ready``: the latest block when the reign first asked for a seed."""
    anchor_block: Annotated[int, Field(ge=0)]
    """``B_ready + Δ``: the height whose finalized hash binds the family."""
    anchor_block_hash: str | None = None
    """Pinned hash, or null while the reign waits for finality."""
    pinned: bool
    pinned_at: datetime | None = None
    created_at: datetime


class AdminConfirmationSeedAnchorList(BaseModel):
    """Every anchor of one bench version, oldest anchor block first."""

    model_config = ConfigDict(extra="ignore")

    bench_version: Annotated[int, Field(ge=1)]
    """The version listed (the active benchmark unless one was requested)."""
    binding_active: bool
    """Whether this version binds at all (``bench_version >= binding_floor``)."""
    binding_floor_bench_version: Annotated[int, Field(ge=1)]
    """``CRN_BLOCK_BINDING_MIN_BENCH_VERSION``: the floor, never an enumeration."""
    anchor_block_delta: Annotated[int, Field(ge=1)]
    """``CRN_ANCHOR_BLOCK_DELTA``: blocks past ``ready_block`` the pin waits."""
    items: list[AdminConfirmationSeedAnchor] = Field(default_factory=list)
    count: Annotated[int, Field(ge=0)]
    pinned_count: Annotated[int, Field(ge=0)]
    waiting_count: Annotated[int, Field(ge=0)]
