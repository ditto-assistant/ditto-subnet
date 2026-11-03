"""Wire models for dueling-bench seasons (bench v16+).

A season freezes up to five generator columns for every bench v16+ agent run
(``research/dittobench-datagen/docs/dueling-bench.md`` section 7). Platform
commits each season's secret before its anchor block exists, opens it at a
generator-round boundary, and asks the trusted generator scorer for two kinds
of work (section 7.4):

- ``battery``: a candidate generator's season qualification battery (32 fresh
  secret seeds at the slice column size plus a determinism re-run);
- ``slice``: one run's generator slice, keyed ``(season, run_seed)``.

The season secret and seed are Platform-only until the season is revealed;
none of these models carries them, and no miner-facing view serves a slice.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

DuelingWorkKind = Literal["battery", "slice"]
DuelingSeasonStatus = Literal["committed", "open", "closed", "revealed", "void"]
DuelingBatteryStatus = Literal["pending", "leased", "passed", "failed"]
DuelingSliceStatus = Literal["pending", "leased", "ready", "failed"]

_SHA256 = r"^[0-9a-f]{64}$"
# A slice is at most challenge.MaxRoundDatasetBytes (32 MiB) before base64.
_MAX_SLICE_BASE64 = 4 * (((32 << 20) + 2) // 3)


class DuelingModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


# --- scorer work ------------------------------------------------------------


class DuelingBatteryCandidate(DuelingModel):
    submission_id: UUID
    miner_hotkey: str
    owner_key: str
    source_sha256: str
    source_base64: str


class DuelingSliceColumnWork(DuelingModel):
    column_id: str
    submission_id: UUID
    miner_hotkey: str
    owner_key: str
    source_sha256: str
    source_base64: str
    tree_sha256: str | None = None
    """The archive's tree digest; the scorer refuses to build on a mismatch."""
    grading_bench_version: int
    slice_seed: str
    public_default: bool


class DuelingSliceFallback(DuelingModel):
    """The public default generator a failed column is regenerated with."""

    submission_id: UUID
    miner_hotkey: str
    source_sha256: str
    source_base64: str
    tree_sha256: str | None = None


class DuelingWork(DuelingModel):
    """One leased dueling work item (``kind`` names which fields are set)."""

    kind: DuelingWorkKind
    lease_id: UUID
    season_id: UUID
    bench_version: int
    lease_expires_at: datetime
    # battery
    cases: int | None = None
    seeds: list[str] | None = None
    candidates: list[DuelingBatteryCandidate] | None = None
    # slice
    preparation_id: UUID | None = None
    season_index: int | None = None
    run_seed: int | None = None
    cases_per_column: int | None = None
    columns: list[DuelingSliceColumnWork] | None = None
    fallback: DuelingSliceFallback | None = None


class DuelingWorkResponse(DuelingModel):
    work: DuelingWork | None = None


class DuelingBatteryCandidateResult(DuelingModel):
    submission_id: UUID
    ok: bool
    grading_bench_version: int | None = None
    error: Annotated[str | None, Field(max_length=8192)] = None


class DuelingBatteryResult(DuelingModel):
    lease_id: UUID
    season_id: UUID
    scorer_version: Annotated[str, Field(min_length=1, max_length=64)]
    candidates: Annotated[list[DuelingBatteryCandidateResult], Field(max_length=64)]


class DuelingSliceColumnResult(DuelingModel):
    column_id: str
    ok: bool
    substituted: bool = False
    error: Annotated[str | None, Field(max_length=8192)] = None


class DuelingSliceResult(DuelingModel):
    lease_id: UUID
    preparation_id: UUID
    scorer_version: Annotated[str, Field(min_length=1, max_length=64)]
    columns: Annotated[list[DuelingSliceColumnResult], Field(max_length=16)]
    slice_sha256: Annotated[str, Field(pattern=_SHA256)] | None = None
    slice_base64: Annotated[str | None, Field(max_length=_MAX_SLICE_BASE64)] = None


class DuelingBatteryAck(DuelingModel):
    season_id: UUID
    passed: list[UUID]
    failed: list[UUID]
    released: list[UUID]


class DuelingSliceAck(DuelingModel):
    preparation_id: UUID
    status: DuelingSliceStatus
    slice_sha256: str | None = None


# --- admin ------------------------------------------------------------------

ROTATE_DUELING_SEASON_CONFIRMATION = "ROTATE DUELING SEASON"
DUELING_COLUMN_FLOOR = 0.15
"""A column whose in-season mean falls below this is flagged for an early
rotation (``dueling-bench.md`` section 3, solvability layer 4)."""


class DuelingTargetView(DuelingModel):
    bench_version: int | None
    shadow: bool
    blocked: str | None
    """Why no new season can open now, or None."""
    live_versions: list[int]


class DuelingBatteryView(DuelingModel):
    submission_id: UUID
    miner_hotkey: str
    owner_key: str
    tree_sha256: str
    rank: int
    """Candidate rank by ledger score; -1 is the public default generator."""
    evidence: dict | None
    status: DuelingBatteryStatus
    attempts: int
    grading_bench_version: int | None
    error: str | None
    scorer_version: str | None
    finished_at: datetime | None


class DuelingColumnMean(DuelingModel):
    column_id: str
    submission_id: str
    owner_key: str
    public_default: bool
    scores: int
    """In-season score rows carrying this column's aggregate."""
    cases: int
    mean: float | None
    default_gap: float | None
    """Column mean minus the public default columns' mean (the drift anchor)."""
    below_floor: bool


class DuelingSeasonView(DuelingModel):
    id: UUID
    bench_version: int
    season_index: int
    shadow: bool
    status: DuelingSeasonStatus
    commitment: str
    committed_at: datetime
    rounds_per_season: int
    settings_revision: int | None
    anchor_block: int | None
    anchor_block_hash: str | None
    opened_at: datetime | None
    closes_at_round: int | None
    closed_at: datetime | None
    reveal_at_epoch: int | None
    revealed_at: datetime | None
    void_reason: str | None
    rotation_requested_at: datetime | None
    rotation_immediate: bool | None
    rotation_actor: str | None
    rotation_reason: str | None
    columns: list[dict] | None
    fallback: dict | None
    detail: dict | None
    """Lifecycle notes: close reason, skipped candidates, substituted
    generators. Never the battery seeds, secret or season seed."""
    secret_hex: str | None = None
    """Only once the season is revealed."""
    season_seed: str | None = None
    """Only once the season is revealed."""
    batteries: list[DuelingBatteryView] = Field(default_factory=list)
    column_means: list[DuelingColumnMean] = Field(default_factory=list)
    slices: dict[str, int] = Field(default_factory=dict)
    """Slice preparations by status."""


class AdminDuelingSeasonsResponse(DuelingModel):
    target: DuelingTargetView
    current: list[DuelingSeasonView]
    """Open seasons (at most one per bench version)."""
    next: list[DuelingSeasonView]
    """Committed seasons waiting for their opening round."""
    recent: list[DuelingSeasonView]
    """The newest closed, revealed and void seasons (no batteries or means)."""
    scores_without_dueling: int = 0
    """Bench v16+ score rows with no season or aggregates. Ingestion refuses
    them, so this should stay 0; any such row never ranks or pays."""
    confirmation_scores_without_dueling: int = 0
    """The same for bench v16+ continual-retest seed records."""


class DuelingSliceView(DuelingModel):
    id: UUID
    season_id: UUID
    season_index: int
    bench_version: int
    run_seed: str
    """The int64 run seed as decimal text, exact in every JSON client."""
    status: DuelingSliceStatus
    attempts: int
    slice_sha256: str | None
    size_bytes: int | None
    bytes_pruned: bool
    columns: list[dict] | None
    error: str | None
    scorer_version: str | None
    created_at: datetime
    first_leased_at: datetime | None
    ready_at: datetime | None
    failed_at: datetime | None
    latency_seconds: float | None
    """Reservation to ready (or failed)."""
    retries: int = 0
    """Operator re-queues (``retry_dueling_slice_preparation``)."""
    last_retry_at: datetime | None = None
    last_retry_actor: str | None = None
    last_retry_reason: str | None = None
    retryable: bool = False
    """Whether ``retry_dueling_slice_preparation`` would accept it now
    (without ``force``)."""
    retry_refusal: str | None = None
    """Why it is not retryable, for a failed preparation."""


class AdminDuelingSlicesResponse(DuelingModel):
    slices: list[DuelingSliceView]
    counts: dict[str, int]
    """Every preparation in the filter by status (not only this page)."""
    latency_p50_seconds: float | None
    latency_p95_seconds: float | None
    """Over the ready preparations in this page."""


class AdminDuelingRotateRequest(BaseModel):
    """Close an open dueling season early (an audited mini-rollout)."""

    model_config = ConfigDict(extra="ignore")

    season_id: UUID | None = None
    """The open season to rotate; default the open season of the dueling
    target version."""
    immediate: bool = False
    """Close now instead of at the next round boundary. No season serves
    slices until the next season opens at the following round boundary."""
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str


def retry_dueling_slice_confirmation(preparation_id: UUID) -> str:
    return f"RETRY DUELING SLICE {preparation_id}"


class AdminDuelingSliceRetryRequest(BaseModel):
    """Re-queue exactly one failed slice preparation (audited)."""

    model_config = ConfigDict(extra="ignore")

    expected_status: Literal["failed"]
    expected_error: str | None
    """The preparation's current ``error``, exactly as read: an optimistic
    guard so a retry never lands on a different failure than the one read."""
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str
    force: bool = False
    """Retry a failure the scorer refused as an unsupported request (a digest
    mismatch on the pinned source), which normally needs a rotation. The
    reason is recorded with the force."""
