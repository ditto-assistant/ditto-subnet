"""Generator-track ledger relayed to validators (shadow, weight-zero).

Platform is the generator competition's system of record: it scores each pack
from the trusted scorer's panel results and folds every hotkey to its best pack
scored within the freshness window. Validators read this ledger through
``GET /scoring/generator-ledger`` and fold it as the ``generator`` track, which
ships in ``SHADOW`` at 0 bps. Field discipline mirrors
``ditto.api_models.router_ledger``: unknown fields are ignored, ``score`` is the
raw double, and an empty ledger folds to zero emission.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class GeneratorLedgerModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class GeneratorLedgerEntry(GeneratorLedgerModel):
    miner_hotkey: str
    pack_id: UUID
    generator_contract_version: Literal[1] = 1
    weight_eligible: Annotated[
        bool,
        Field(
            description=(
                "Whether this result may contribute emissions. Always False "
                "while the competition is in shadow; the validator's track "
                "state is the authority and this is a defensive echo."
            )
        ),
    ] = False
    score: Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]
    discriminating_cases: Annotated[int, Field(ge=0)] = 0
    first_seen: datetime
    scored_at: datetime


class GeneratorLedgerResponse(GeneratorLedgerModel):
    entries: list[GeneratorLedgerEntry] = Field(default_factory=list)
    count: int = 0
    window_days: int = 0
    mode: Literal["off", "shadow"] = "off"
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
