"""model_context (CPM) ledger relayed to validators.

Platform is the model_context competition's system of record: a trusted CPM
scorer scores each submitted context model on the season's seeds, and Platform
serves the open season's scored submissions through
``GET /scoring/model-context-ledger`` (contract
``docs/research/cpm/competition-v1.md`` sections 5.5 and 7). Validators fold
it as the ``model_context`` track, which ships in ``SHADOW``: it is folded and
logged, and it pays nothing.

Field discipline mirrors :mod:`ditto.api_models.router_ledger`: unknown fields
are ignored, ``score`` is the raw double the scorer reported, and an empty or
unreadable ledger folds to nothing. ``mode`` is a plain string and
``rank_shares`` is unvalidated here, so an unknown future mode or a curve this
validator cannot use never makes the whole ledger unreadable; the fold decides
what it can use (:func:`ditto.validator.weights.resolve_model_context_rank_shares`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelContextLedgerModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class ModelContextLedgerEntry(ModelContextLedgerModel):
    hotkey: str
    submission_id: UUID
    score: Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]
    eligible: bool = False
    """The score clears this season's ``floor(base) + floor_margin``
    (contract section 4.5). The fold ranks only eligible entries."""
    commitment_block: Annotated[int, Field(ge=0)]
    """The block of the miner's ``dcpm1:`` commitment: the tie-break priority
    (earlier wins)."""
    season: Annotated[int, Field(ge=0)]


class ModelContextLedgerResponse(ModelContextLedgerModel):
    entries: list[ModelContextLedgerEntry] = Field(default_factory=list)
    count: int = 0
    season: int | None = None
    cpm_score_version: int = 1
    mode: str = "off"
    rank_shares: list[float] | None = None
    """The operator's payout curve. ``None`` (or a curve the fold cannot use)
    falls back to the compiled default."""
    generated_at: datetime | None = None
