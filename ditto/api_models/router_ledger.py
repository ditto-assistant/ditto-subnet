"""Router-track score ledger published by the centralized router scorer.

The router track (SN118 "DittoBench" third competition) does **not** run its
eval on validators. One trusted ``dittobench-api`` scorer instance drives the
four third-party coding harnesses (Claude Code, Codex, opencode, Grok) against
each miner's router, computes a **correctness-floor-gated, token-cost-dominant**
score per miner, and publishes it as this ledger. Every validator only reads and
folds it — the same trust model as the memory ``LedgerResponse`` (each validator
folds one identical, scorer-decided number), so no provider secrets or heavy
harness containers ever touch a validator.

Field discipline mirrors ``ditto.api_models.validator.LedgerResponse``: unknown
fields are ignored (forward compatible), ``combined_score`` is the raw double the
scorer reported (never rounded, so every validator folds identical bytes), and an
absent/older feed simply yields no router entries — which folds to zero router
emission, exactly what the shadow ``weight_eligible=False`` state already does.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class RouterHarness(StrEnum):
    """The big-four third-party coding harnesses the router must power.

    The string values are the stable ledger keys; the centralized scorer and the
    validator's failure classifier both key per-harness state on them.
    """

    CLAUDE_CODE = "claude_code"
    CODEX = "codex"
    OPENCODE = "opencode"
    GROK = "grok"


class RouterLedgerModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class RouterHarnessResult(RouterLedgerModel):
    """One harness's outcome for one miner router, as decided by the scorer.

    ``operational`` (the router powered the harness end-to-end) and ``floor_pass``
    (the run cleared the deterministic frontier-quality correctness floor) are the
    two gates; ``efficiency`` is the token-cost-dominant rank term in ``(0, 1]``,
    and ``upstream_token_cost_micros`` is the router's *upstream* provider token
    cost for the harness's tasks (informational — what compression bought). A
    harness contributes to the combined score only when both gates hold.
    """

    harness: RouterHarness
    operational: Annotated[
        bool,
        Field(description="The miner router powered this harness end-to-end."),
    ]
    floor_pass: Annotated[
        bool,
        Field(
            description=(
                "The run cleared the deterministic correctness floor at the "
                "frontier-quality bar (task build/tests). The gate, not the rank."
            )
        ),
    ]
    efficiency: Annotated[
        float,
        Field(
            ge=0.0,
            le=1.0,
            description=(
                "Token-cost-dominant efficiency score in [0, 1] the scorer "
                "assigned this harness slice (eff_h). Only meaningful when the "
                "harness was operational and cleared the floor."
            ),
        ),
    ]
    upstream_token_cost_micros: Annotated[
        int,
        Field(
            ge=0,
            description=(
                "Router's upstream provider token cost for this harness's tasks, "
                "in micro-units of the canonical cost model. Informational: the "
                "basis for the dominant token axis of eff_h."
            ),
        ),
    ]


class RouterLedgerEntry(RouterLedgerModel):
    """One miner's router-track result, published by the centralized scorer.

    ``combined_score`` is the scorer's soft, per-harness-weighted, floor-gated
    aggregate in ``[0, 1]`` (``Σ_h weight_h × (eff_h if operational and floor else
    0)``). A failed harness forfeits only its slice, so the weight destination is
    always this ``miner_hotkey`` and the fold ranks by this one number.
    """

    miner_hotkey: Annotated[str, Field(description="Miner's SS58 hotkey.")]
    agent_id: Annotated[UUID, Field(description="The miner's scored router agent.")]
    router_contract_version: Literal[1]
    weight_eligible: Annotated[
        bool,
        Field(
            description=(
                "Whether this router result may contribute emissions. False in "
                "shadow (v1). Tightened at promotion; the validator's track state "
                "is the authority, this is a defensive echo."
            )
        ),
    ]
    combined_score: Annotated[
        float,
        Field(
            ge=0.0,
            le=1.0,
            description=(
                "Soft per-harness-weighted, floor-gated aggregate in [0, 1]. The "
                "raw double the scorer reported (never rounded), the sole rank key."
            ),
        ),
    ]
    harnesses: Annotated[
        tuple[RouterHarnessResult, ...],
        Field(
            description=(
                "Per-harness outcomes (one entry per harness the scorer ran). "
                "Read by the validator's failure classifier for telemetry; the "
                "combined score already reflects any soft-forfeited slices."
            )
        ),
    ]
    first_seen: Annotated[
        datetime,
        Field(
            description=(
                "First-seen tie-break (UTC): when this miner's router lineage "
                "first reached the score it defends. The scorer resolves it; the "
                "validator folds it as served, so the original beats a later copy."
            )
        ),
    ]

    @field_validator("first_seen")
    @classmethod
    def first_seen_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("router ledger first_seen must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def identifiers_are_nonzero(self) -> RouterLedgerEntry:
        if self.agent_id.int == 0:
            raise ValueError("router ledger agent_id is nil")
        if len({result.harness for result in self.harnesses}) != len(self.harnesses):
            raise ValueError("router ledger repeats a harness")
        return self


class RouterLedgerResponse(BaseModel):
    """Published by the centralized router scorer; read by every validator.

    Ordered highest-``combined_score`` first (ties broken by ``first_seen`` then
    ``agent_id``), the same deterministic order the router fold uses, so the
    exposed pool and the computed router weights agree by construction.
    """

    model_config = ConfigDict(extra="ignore")

    entries: list[RouterLedgerEntry] = Field(
        default_factory=list,
        description=(
            "One router result per miner, highest combined score first. "
            "Empty (or an older feed that omits it) folds to zero router "
            "emission, identical to the shadow state."
        ),
    )
    router_contract_version: Annotated[
        int | None,
        Field(
            default=None,
            ge=1,
            description=(
                "Router contract version this feed was scored under. None means "
                "the responding scorer predates the field."
            ),
        ),
    ] = None
    generated_at: Annotated[
        datetime | None,
        Field(default=None, description="When the router ledger was produced (UTC)."),
    ] = None
    stale: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "True when the scorer served a last-known-good snapshot because a "
                "live read failed. The fold may still use it but should log it."
            ),
        ),
    ] = False
    count: Annotated[
        int, Field(default=0, ge=0, description="Number of entries returned.")
    ] = 0


__all__ = [
    "RouterHarness",
    "RouterHarnessResult",
    "RouterLedgerEntry",
    "RouterLedgerResponse",
]
