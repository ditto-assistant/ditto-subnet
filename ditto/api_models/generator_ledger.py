"""Generator-track ledger relayed to validators.

Platform is the generator competition's system of record: generators compete
in chain-epoch rounds (their cases run by validators against the top agents),
and Platform folds every hotkey to its latest round column score within the
freshness window. Validators read this ledger through
``GET /scoring/generator-ledger`` and fold it as the ``generator`` track, which
is active from protocol 33 but pays only a Platform-governed share
(``LedgerResponse.track_shares_bps``) and only to entries marked
``weight_eligible`` (generator settings ``mode=enforce``). Field discipline mirrors
``ditto.api_models.router_ledger``: unknown fields are ignored, ``score`` is the
raw double, and an empty ledger folds to zero emission.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class GeneratorLedgerModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class GeneratorLedgerEntry(GeneratorLedgerModel):
    miner_hotkey: str
    submission_id: UUID
    # The round that scored this entry. Optional so a ledger from a Platform
    # still on the run-based shape (``run_id``, ignored) folds unchanged during
    # a staggered rollout; the fold never reads it.
    round_index: Annotated[int, Field(ge=0)] | None = None
    generator_contract_version: Literal[1] = 1
    weight_eligible: Annotated[
        bool,
        Field(
            description=(
                "Whether this result may contribute emissions. False while "
                "the competition is in shadow; under ``mode=enforce`` Platform "
                "marks qualified miner generators eligible. The fold pays only "
                "eligible entries, and only from a governed generator share."
            )
        ),
    ] = False
    score: Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]
    discriminating_cases: Annotated[int, Field(ge=0)] = 0
    # Aware only: the fold orders entries by these, and mixing naive and aware
    # datetimes cannot be ordered.
    first_seen: AwareDatetime
    scored_at: AwareDatetime


class GeneratorLedgerResponse(GeneratorLedgerModel):
    entries: list[GeneratorLedgerEntry] = Field(default_factory=list)
    count: int
    window_days: int = 0
    # A plain string, not a closed literal: an unknown future mode must not
    # make the whole ledger unreadable (the fold never reads it; it pays by
    # each entry's ``weight_eligible``). ``enforce`` marks entries eligible.
    mode: str = "off"
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
