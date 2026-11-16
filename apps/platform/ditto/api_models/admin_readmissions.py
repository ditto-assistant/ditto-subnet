"""Operator read of every SN-485 readmission, paid, credited or classified.

``GET /admin/readmissions`` (Backroom ``list_readmissions``) lists the paid
readmission lane (``readmission_payments``: admitted, or credited when refused
after payment) and the uploads classified as readmissions
(``upload_readmissions``), with totals and the free operator admissions of the
active era by category, so free admissions can be counted against paid ones.
Read-only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ditto.api_models.submission_settings import ReadmissionActivationState
from ditto.api_models.submission_terms import ReadmissionCostBasis


class PaidReadmissionRow(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    block_hash: str
    extrinsic_index: int
    agent_id: UUID
    agent_name: str | None = None
    miner_hotkey: str
    miner_coldkey: str
    bench_version: int
    rollout_id: UUID | None = None
    outcome: Literal["admitted", "credited"]
    funding: Literal["fresh", "credit"]
    amount_rao: int
    refusal_reason: str | None = None
    credit_audit: dict[str, Any] | None = None
    credit_spent: bool | None = None
    """For ``credited``: whether a later upload already spent the banked credit
    (``null`` while it is unspent or for an admitted row)."""
    credit_expires_at: datetime | None = None
    recorded_at: datetime
    updated_at: datetime


class UploadReadmissionRow(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    agent_id: UUID
    agent_name: str | None = None
    kind: Literal["identical_rescore", "near_duplicate"]
    lineage_agent_id: UUID
    lineage_evaluation_completed: bool
    bench_version: int
    miner_coldkey: str
    amount_rao: int | None = None
    """The upload fee its ``evaluation_payments`` row recorded."""
    created_at: datetime


class ReadmissionTotals(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    admitted: int = 0
    admitted_amount_rao: int = 0
    credit_funded: int = 0
    credited: int = 0
    credited_amount_rao: int = 0
    upload_readmissions: int = 0
    upload_readmission_amount_rao: int = 0


class AdminReadmissionsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    activation: ReadmissionActivationState
    cost_basis: ReadmissionCostBasis | None = None
    bench_version: int | None = None
    """The filter, when one was given."""
    totals: ReadmissionTotals
    free_admissions_by_category: dict[str, int] = Field(default_factory=dict)
    """Stranded admissions on the active era by audited category: an
    operator's ``ditto_fault``/``owner_policy``/``unspecified`` (free) against
    ``paid``. Always the whole active era: the ``bench_version`` and
    ``miner_coldkey`` filters apply to ``totals``, ``paid`` and ``uploads``
    only."""
    paid: list[PaidReadmissionRow] = Field(default_factory=list)
    uploads: list[UploadReadmissionRow] = Field(default_factory=list)
    truncated: bool = False
    generated_at: datetime


class MinerFeeReadmissionLane(BaseModel):
    """The readmission lane's share of miner fee revenue (SN-485).

    Each proof is counted once: an admitted readmission here, a credited one
    in the upload lane (its banked credit row holds the proof).
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    admitted: int = 0
    admitted_amount_rao: int = 0
    credit_funded: int = 0
    """Admitted readmissions paid by spending an earlier credit."""
    credited: int = 0
    credited_amount_rao: int = 0
    """Refused after payment and banked as credits (already in the upload
    lane's gross; shown for reconciliation)."""
    upload_readmissions: int = 0
    upload_readmission_amount_rao: int = 0
    """Uploads classified as readmissions (already in the upload lane)."""


__all__ = [
    "AdminReadmissionsResponse",
    "MinerFeeReadmissionLane",
    "PaidReadmissionRow",
    "ReadmissionTotals",
    "UploadReadmissionRow",
]
