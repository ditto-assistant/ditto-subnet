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
_MAX_SLICE_BASE64 = 45_000_000


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
