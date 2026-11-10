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

from ditto.api_models.generator_rounds import GeneratorRoundCommitmentPublicationView

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
REPUBLISH_DUELING_COMMITMENT_CONFIRMATION = "REPUBLISH DUELING SEASON COMMITMENT"
DuelingCommitmentPublication = Literal["pending", "published", "failed", "disabled"]
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


class DuelingRotationView(DuelingModel):
    """The season-open mini-rollout and how far its re-scoring has got."""

    standing: Literal["current", "provisional", "expired", "not_opened"]
    """Whether this season's scores count now: ``current`` (the version's
    latest opened season), ``provisional`` (the one before it, visible for one
    more season), ``expired`` (out of the authoritative ledger), or
    ``not_opened``."""
    source: str | None = None
    """Where the cohort came from: ``continual_cohort`` (authoritative),
    ``rollout_members`` (shadow during a rollout), or ``none``."""
    cohort: list[dict] = Field(default_factory=list)
    """``{agent_id, rank}`` in queue order, champion (rank 0) first."""
    queued_at: datetime | None = None
    queued: int = 0
    """Rotation re-tests queued at open, one per accepted score."""
    already_current: int = 0
    busy: int = 0
    """Scores left alone at open because another re-test was live."""
    error: str | None = None
    pending: int = 0
    """Rotation re-tests for this season still waiting for a slot."""
    running: int = 0
    """Rotation re-tests for this season holding a lease now."""
    closed: int = 0
    """Rotation re-tests for this season that ended (scored or released)."""
    cohort_scores_in_season: int = 0
    """Cohort score rows already re-scored in this season."""
    cohort_quorum_in_season: int = 0
    """Cohort agents with a full quorum of scores in this season."""
    scores_in_season: int = 0
    """Every score row (any agent) recorded in this season."""
    confirmation_scores_in_season: int = 0
    """Every confirmation (continual retest) row recorded in this season."""


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
    rotation: DuelingRotationView | None = None
    """Season-open re-scoring of the champion and cohort (full views only)."""
    commitment_publication: DuelingCommitmentPublication = "disabled"
    """On-chain ``dds1:{season_index}:{commitment}`` publication by elected
    validators: ``pending`` while committed with ``onchain_commitment`` on,
    then ``published``, ``failed`` (none confirmed before the season left
    committed) or ``disabled``."""
    commitment_published_at: datetime | None = None
    commitment_block: int | None = None
    """The earliest block a validator confirmed the commitment on chain at."""
    commitment_extrinsic_hash: str | None = None
    """That publication's extrinsic, when the validator reported one."""
    commitment_error: str | None = None
    commitment_publications: list[GeneratorRoundCommitmentPublicationView] = Field(
        default_factory=list
    )
    """Every validator claim and report, in the season's publisher order
    (full views only)."""
    commitment_publishers_elected: list[str] = Field(default_factory=list)
    """Validators elected to publish right now (full views only)."""


class DuelingRotationSpend(DuelingModel):
    """Rotation re-test leases started and their metered inference cost."""

    runs: int = 0
    """Leases started (one per validator re-score attempt)."""
    priced_runs: int = 0
    """Leases with an inference grant; the rest ended before any call."""
    cost_usd: float = 0.0
    """The grants' metered chat, embedding and decision cost: what the relay
    booked, including runs that failed. Never an estimate."""


class DuelingSeasonSpendView(DuelingModel):
    season_id: UUID
    season_index: int
    opening: DuelingRotationSpend
    """The re-tests the season queued for its opening cohort."""
    catchup: DuelingRotationSpend
    """The season-boundary catch-up re-tests it queued (``detail.catchup``)."""


class DuelingRotationSpendView(DuelingModel):
    """Opening rotation and catch-up spend side by side, so the catch-up's
    incremental cost reads directly."""

    last_24h_opening: DuelingRotationSpend
    """Opening rotation leases started in the last 24 hours: dollars/day."""
    last_24h_catchup: DuelingRotationSpend
    """Catch-up leases started in the last 24 hours: dollars/day."""
    seasons: list[DuelingSeasonSpendView] = Field(default_factory=list)
    """The open season and the recent ones, newest first."""


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
    rotation_spend: DuelingRotationSpendView | None = None
    """What season rotation re-tests really cost, opening and catch-up."""


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


class AdminDuelingCommitmentRepublishRequest(BaseModel):
    """Re-elect validators to publish a committed season's commitment."""

    model_config = ConfigDict(extra="ignore")

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


# --- public reveal and generator source release -----------------------------

GeneratorSourceReleaseStatus = Literal[
    "waiting_close", "waiting_reveal", "embargoed", "due", "withheld", "released"
]


class PublicDuelingPin(DuelingModel):
    """One column pin, exactly as ``challenge-scorer reproduce-slice -pins``
    reads it."""

    column_id: str
    submission_id: str
    owner_key: str
    miner_hotkey: str
    tree_sha256: str
    grading_bench_version: int
    public_default: bool
    substituted: bool
    slice_seed: str | None = None
    """Set on a slice's pins: ``slice_seed(season_seed, run_seed, column)``."""


class PublicDuelingPins(DuelingModel):
    """The ``reproduce-slice -pins`` file: save it as JSON and pass it."""

    season_id: str
    columns: list[PublicDuelingPin]


class PublicGeneratorSource(DuelingModel):
    """Where one pinned generator's exact source stands."""

    submission_id: UUID
    miner_hotkey: str
    owner_key: str
    tree_sha256: str
    source_sha256: str
    public_default: bool
    release_at: datetime | None = None
    """``max(last pinning season's reveal, first pinning season's close +
    embargo_hours)``; null while a pinning season is still open or unrevealed."""
    released_at: datetime | None = None
    available: bool
    """Whether ``archive_path`` serves the archive now."""
    archive_path: str
    scorer_version: str | None = None
    """The challenge-scorer revision that built it for its first season."""


class PublicDuelingSeason(DuelingModel):
    """A revealed season: everything a slice reproduction needs but the run
    seed and the generator sources."""

    id: UUID
    bench_version: int
    season_index: int
    shadow: bool
    commitment: str
    secret_hex: str
    anchor_block: int | None
    anchor_block_hash: str | None
    season_seed: str
    opened_at: datetime | None
    closed_at: datetime | None
    revealed_at: datetime | None
    pins: PublicDuelingPins
    """The frozen columns; a slice's own pins add substitution and seeds."""
    fallback: dict | None
    """The public default generator that fills a column its pinned generator
    failed on: ``submission_id``, ``miner_hotkey``, ``tree_sha256``,
    ``grading_bench_version``."""
    sources: list[PublicGeneratorSource] = Field(default_factory=list)
    slices: int = 0
    """Slices with a published digest (``.../slices`` lists them)."""


class PublicDuelingSeasonList(DuelingModel):
    seasons: list[PublicDuelingSeason]


class PublicDuelingSlice(DuelingModel):
    """One revealed slice: ``-run-seed``, ``-expect-sha256`` and ``-pins``."""

    run_seed: Annotated[str, Field(pattern=r"^(0|[1-9][0-9]{0,18})$")]
    """Decimal text: a run seed is an int64, beyond JavaScript's exact range."""
    slice_sha256: str
    ready_at: datetime | None
    pins: PublicDuelingPins


class PublicDuelingSlicePage(DuelingModel):
    season_id: UUID
    total: int
    offset: int
    limit: int
    slices: list[PublicDuelingSlice]


class GeneratorSourceReleaseView(DuelingModel):
    submission_id: UUID
    miner_hotkey: str
    owner_key: str
    tree_sha256: str
    source_sha256: str
    bench_version: int
    first_season_id: UUID
    last_season_id: UUID
    seasons: int
    first_closed_at: datetime | None
    last_revealed_at: datetime | None
    embargo_hours: int
    disclosure: str
    release_at: datetime | None
    released_at: datetime | None
    scorer_version: str | None
    status: GeneratorSourceReleaseStatus
    updated_at: datetime


class AdminGeneratorSourceReleasesResponse(DuelingModel):
    items: list[GeneratorSourceReleaseView]
    counts: dict[str, int] = Field(default_factory=dict)
    """Rows by status across the whole table."""
