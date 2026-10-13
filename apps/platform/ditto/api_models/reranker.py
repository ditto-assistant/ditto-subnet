"""Wire models for the Season 2 reranker track (shadow; SN-207 intake, SN-209 scoring).

The track is shadow only: an accepted artifact is stored and later scored by
the trusted scorer worker, and a shadow champion may be recorded, but nothing
here pays, ranks for weights, or touches the ``agents`` pipeline. Tarball URLs,
pool contents and the held surface salt are never part of any response.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

RerankerSubmissionStatus = Literal["accepted", "scoring", "invalid", "scored", "failed"]


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
    # Finalized block number the seed was derived from (SN-209).
    seed_block: int | None = None
    # Short allowlisted code when status is ``failed`` (never paths/secrets).
    failure_reason: str | None = None


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
    # Scorer lease (SN-209): attempts so far and, while scoring, its expiry.
    scoring_attempts: int = 0
    scoring_lease_expires_at: datetime | None = None


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
    # Current shadow champion; null while the pinned incumbent holds it.
    champion: RerankerChampionEntry | None = None


class RerankerIncumbent(BaseModel):
    """The pinned product model every challenger is measured against."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    base_model: str
    quantization: str
    model_sha256: str
    vocab_sha256: str


class RerankerChampionEntry(BaseModel):
    """One shadow crown. Shadow only: a crown pays nothing and sets no weight."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    submission_id: UUID
    miner_hotkey: str
    artifact_sha256: str
    # Null when the crown was taken from the incumbent.
    previous_submission_id: UUID | None
    crowned_at: datetime
    superseded_at: datetime | None
    contract_sha256: str
    pools_sha256: str
    comparison: dict[str, Any]


class RerankerChampionResponse(BaseModel):
    """``GET /public/reranker/champion``: who holds the shadow crown."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    contract_id: str
    contract_sha256: str
    incumbent: RerankerIncumbent
    # Null while the incumbent holds the crown.
    champion: RerankerChampionEntry | None
    shadow: bool = True


class AdminRerankerChampionList(BaseModel):
    """Backroom read of the append-only crown history, newest first."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    contract_id: str
    contract_sha256: str
    incumbent: RerankerIncumbent
    current: RerankerChampionEntry | None
    total: int
    limit: int
    offset: int
    history: list[RerankerChampionEntry]


AdminRerankerSubmissionList.model_rebuild()
