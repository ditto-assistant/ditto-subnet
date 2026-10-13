"""Wire models for the Season 2 reranker track (shadow intake, SN-207).

The track is shadow only: an accepted artifact is stored and later scored,
but nothing here pays, ranks for weights, or touches the ``agents`` pipeline.
Tarball URLs are never part of any response.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

RerankerSubmissionStatus = Literal["accepted", "invalid", "scored", "failed"]


class RerankerUploadResponse(BaseModel):
    """``POST /upload/reranker`` result for a newly accepted artifact."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    submission_id: UUID
    status: RerankerSubmissionStatus
    artifact_sha256: str
    upload_sha256: str
    contract_id: str
    contract_sha256: str
    created_at: datetime
    # Non-fatal findings from static validation, e.g. ignored extra outputs.
    warnings: list[str] = []


class RerankerSubmission(BaseModel):
    """Public view of one submission. ``score`` is null until scored."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    submission_id: UUID
    miner_hotkey: str
    status: RerankerSubmissionStatus
    artifact_sha256: str
    contract_sha256: str
    size_bytes: int
    base_model: str
    base_model_license: str
    invalid_reason: str | None
    # Decimal string: a JSON number would round a 64-bit seed above 2^53.
    seed: str | None
    seed_block_hash: str | None
    score: dict[str, Any] | None
    scored_at: datetime | None
    created_at: datetime


class RerankerSubmissionList(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    total: int
    limit: int
    offset: int
    submissions: list[RerankerSubmission]


class AdminRerankerSubmission(RerankerSubmission):
    """Operator view: adds the owner coldkey and the uploaded tarball digest."""

    miner_coldkey: str | None
    upload_sha256: str


class AdminRerankerSubmissionList(BaseModel):
    """Backroom read: recent rows plus a zero-filled count per status."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    contract_id: str
    contract_sha256: str
    total: int
    status_counts: dict[str, int]
    # Effective per-owner intake window (env-configured, read per request).
    upload_window_seconds: int
    uploads_per_window: int
    limit: int
    offset: int
    submissions: list[AdminRerankerSubmission]
