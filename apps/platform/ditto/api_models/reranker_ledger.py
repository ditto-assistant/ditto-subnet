"""Season 2 reranker-track shadow ledger (SN-209), relayed by Platform.

The reranker track never runs on validators. Platform's trusted scorer worker
scores each submitted ONNX reranker against the current champion on pools
derived from a post-commit finalized block, and records an append-only shadow
crown. ``GET /scoring/reranker-ledger`` serves that state to permitted
validators, which only read and log it.

**Shadow only.** Every entry and the champion carry ``weight_eligible=False``
and ``combined_score=0``; Platform clamps both before serving and the
validator's reranker track is hard-coded to shadow, so a served ledger folds to
zero emission, byte-identical to folding no reranker track at all. The real
measurements ride ``shadow_score`` and ``champion_score``.

This file is a hand-maintained twin: ``ditto/api_models/reranker_ledger.py``
and ``apps/platform/ditto/api_models/reranker_ledger.py`` must stay
byte-identical (pinned by the validator contract test and its golden). Unknown
fields are ignored so either side can add one in a rolling upgrade.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SHA256 = r"^[0-9a-f]{64}$"


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("reranker ledger timestamps must be timezone-aware")
    return value.astimezone(UTC)


class RerankerLedgerModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class RerankerLedgerChampion(RerankerLedgerModel):
    """Who holds the shadow crown: the pinned incumbent or a submission."""

    kind: Literal["incumbent", "submission"]
    submission_id: UUID | None = None
    miner_hotkey: str | None = None
    artifact_sha256: Annotated[str | None, Field(pattern=_SHA256)] = None
    crowned_at: datetime | None = None
    weight_eligible: Annotated[
        bool,
        Field(
            description=(
                "False in shadow. The validator's reranker track state is the "
                "authority; this is a defensive echo."
            )
        ),
    ] = False
    combined_score: Annotated[
        float,
        Field(
            ge=0.0,
            le=1.0,
            description="The folded weight input. Forced 0 in shadow.",
        ),
    ] = 0.0

    @field_validator("crowned_at")
    @classmethod
    def crowned_at_is_aware(cls, value: datetime | None) -> datetime | None:
        return _aware(value)

    @model_validator(mode="after")
    def kind_matches_identity(self) -> RerankerLedgerChampion:
        if self.kind == "incumbent":
            if self.submission_id is not None or self.miner_hotkey is not None:
                raise ValueError("the incumbent champion has no submission")
        elif self.submission_id is None or not self.miner_hotkey:
            raise ValueError("a submission champion needs its id and hotkey")
        return self


class RerankerLedgerEntry(RerankerLedgerModel):
    """One scored submission: its comparison against the champion it faced."""

    submission_id: UUID
    miner_hotkey: str
    artifact_sha256: Annotated[str, Field(pattern=_SHA256)]
    status: Literal["scored", "invalid"]
    shadow_score: Annotated[
        float,
        Field(ge=0.0, le=1.0, description="Challenger mean NDCG@10 (measured)."),
    ]
    champion_score: Annotated[
        float,
        Field(ge=0.0, le=1.0, description="Champion mean NDCG@10, same pools."),
    ]
    champion_kind: Literal["incumbent", "submission"]
    mean_diff: float
    threshold: Annotated[float, Field(ge=0.0)]
    dethrones: bool
    latency_passed: bool
    transfer_gate: Annotated[str, Field(min_length=1, max_length=64)]
    crowned: bool
    pools_sha256: Annotated[str, Field(pattern=_SHA256)]
    scored_at: datetime
    weight_eligible: Annotated[
        bool, Field(description="False in shadow (defensive echo).")
    ] = False
    combined_score: Annotated[
        float,
        Field(ge=0.0, le=1.0, description="The folded weight input. 0 in shadow."),
    ] = 0.0

    @field_validator("scored_at")
    @classmethod
    def scored_at_is_aware(cls, value: datetime) -> datetime:
        aware = _aware(value)
        assert aware is not None
        return aware


class RerankerLedgerResponse(BaseModel):
    """``GET /scoring/reranker-ledger``: the shadow crown and recent scores.

    Empty (the default, or an older Platform that lacks the route) folds to
    zero reranker emission, identical to the shadow state.
    """

    model_config = ConfigDict(extra="ignore")

    contract_id: str | None = None
    contract_sha256: Annotated[str | None, Field(pattern=_SHA256)] = None
    champion: RerankerLedgerChampion | None = None
    entries: list[RerankerLedgerEntry] = Field(
        default_factory=list,
        description="Most recently scored submissions, newest first.",
    )
    generated_at: datetime | None = None
    count: Annotated[int, Field(ge=0)] = 0

    @field_validator("generated_at")
    @classmethod
    def generated_at_is_aware(cls, value: datetime | None) -> datetime | None:
        return _aware(value)


__all__ = [
    "RerankerLedgerChampion",
    "RerankerLedgerEntry",
    "RerankerLedgerResponse",
]
