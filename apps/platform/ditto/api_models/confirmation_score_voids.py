"""Wire shapes for infrastructure voids of retest samples and canonical scores."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

VOID_CONFIRMATION_PHRASE = "VOID INFRA SCORES"

_SEED = r"^(0|[1-9][0-9]*)$"

SampleKind = Literal["auto", "continual_retest", "canonical"]
InfraVoidDisposition = Literal[
    "ready",
    "applied",
    "already_voided",
    "already_queued",
    "already_replaced",
    "not_found",
    "stale_guard",
    "insufficient_evidence",
    "retest_busy",
    "retest_closed",
    "not_retestable",
    "auto_cap_reached",
]


class InfraVoidItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    agent_id: UUID
    validator_hotkey: Annotated[str, Field(min_length=1, max_length=64)]
    seed: Annotated[str, Field(pattern=_SEED)]
    """Exact decimal seed from ``retest_samples`` (or the canonical score's)."""
    expected_run_id: Annotated[str, Field(min_length=1, max_length=200)]
    expected_composite: Annotated[float, Field(ge=0, le=1)]
    sample_kind: SampleKind = "auto"
    """``auto`` resolves by the run id: the key's canonical score run (or one an
    outage void already named) is ``canonical``; anything else is
    ``continual_retest``."""


class InfraVoidRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: Annotated[list[InfraVoidItem], Field(min_length=1, max_length=50)]
    reason: Annotated[str, Field(min_length=8)]
    dry_run: bool = True
    """Default: report dispositions and evidence without writing."""
    confirmation: str | None = None
    """Must equal ``VOID INFRA SCORES`` when ``dry_run`` is false."""


class InfraVoidOutcome(BaseModel):
    agent_id: UUID
    validator_hotkey: str
    seed: str
    sample_kind: Literal["continual_retest", "canonical"] = "continual_retest"
    disposition: InfraVoidDisposition
    detail: str | None = None
    void_id: UUID | None = None
    retest_request_id: UUID | None = None
    """Canonical only: the same-validator replacement re-test request. Applied
    means queued; the score is swapped (and medians recompute) when it lands."""
    void_generation: int | None = None
    evidence: dict[str, object] = {}
    evidence_digest: str | None = None


class InfraVoidResponse(BaseModel):
    bench_version: int
    dry_run: bool
    applied_count: int
    outcomes: list[InfraVoidOutcome]


class InfraVoidRecord(BaseModel):
    void_id: UUID
    agent_id: UUID
    bench_version: int
    validator_hotkey: str
    seed: str
    void_generation: int
    sample_kind: str
    run_id: str
    composite: float
    reason: str
    evidence: dict[str, object]
    evidence_digest: str
    actor: str
    source: Literal["operator", "outage_sweep"]
    outage_started_at: datetime | None = None
    outage_ended_at: datetime | None = None
    created_at: datetime
    rerun_composite: float | None = None
    """The re-run's composite on the same key once it has landed."""
    rerun_run_id: str | None = None


class InfraVoidList(BaseModel):
    count: int
    limit: int
    offset: int
    items: list[InfraVoidRecord]
