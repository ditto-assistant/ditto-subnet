"""Wire models for generator-competition rounds (the agent x generator matrix).

A round is keyed on the chain's epoch counter: it opens at a subnet epoch
and spans ``epochs_per_round`` epochs, and round indexes continue from the
latest round across a cadence change. Platform commits each
round's secret one round ahead (``commitment``; elected validators publish
it on-chain),
derives the seed from the secret and the block the round opened at, and
freezes the round's generator columns and agent panel. The trusted scorer
generates the round dataset and later scores the matrix; validators run every
panel agent on the dataset (``generator_round_validator``). Derivations and
scoring mirror ``research/dittobench-datagen/challenge/round.go``.

Public views never include the secret, seed, dataset, or case content until a
round is revealed (``privacy_window_rounds`` after it opens).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ditto.api_models.generator import (
    GeneratorBuildResult,
    GeneratorSeedOutput,
    GeneratorSubmissionSummary,
    PanelCaseResult,
    SubmissionStatus,
)
from ditto.api_models.generator_ledger import (
    GeneratorLedgerEntry,
    GeneratorLedgerResponse,
)
from ditto.api_models.generator_round_validator import round_commitment_payload
from ditto.api_models.name_claim import PublicNameHandle

RoundStatus = Literal["committed", "generating", "running", "scoring", "scored", "void"]
CommitmentPublication = Literal["pending", "published", "failed", "disabled"]
CommitmentPublicationStatus = Literal["claimed", "confirmed", "failed", "expired"]
RoundCaseStatus = Literal[
    "discriminating", "trivial", "unsolved", "duplicate", "insufficient_panel"
]

REGISTER_DEFAULT_GENERATOR_CONFIRMATION = "REGISTER DEFAULT GENERATOR"
# A starter-kit archive with its vendored datagen is a few hundred KiB, too
# large to inline through an MCP tool argument, so an operator can PUT it to a
# five-minute presigned URL under their own prefix of the private trace bucket
# and register it by key.
DEFAULT_GENERATOR_UPLOAD_PREFIX = "generator-default/v1/"
DEFAULT_GENERATOR_UPLOAD_TTL_SECONDS = 300
DEFAULT_GENERATOR_UPLOAD_CONTENT_TYPE: Literal["application/gzip"] = "application/gzip"
REPUBLISH_COMMITMENT_CONFIRMATION = "REPUBLISH GENERATOR ROUND COMMITMENT"
RELEASE_ROUND_LEASE_CONFIRMATION = "RELEASE GENERATOR ROUND LEASE"

_SHA256 = r"^[0-9a-f]{64}$"
_SS58 = r"^[1-9A-HJ-NP-Za-km-z]{47,48}$"


def void_round_confirmation(round_index: int) -> str:
    return f"VOID GENERATOR ROUND {round_index}"


def onchain_commitment_payload(round_index: int, commitment: str) -> bytes:
    """The bytes an elected validator writes with ``Commitments.set_commitment``."""
    return round_commitment_payload(round_index, commitment).encode("ascii")


class RoundModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


# --- public -----------------------------------------------------------------


class GeneratorRoundSchedule(RoundModel):
    """Where the chain is relative to rounds, as last observed by Platform."""

    mode: str
    epochs_per_round: int
    subnet_epoch_index: int | None = None
    current_round: int | None = None
    next_round_epoch: int | None = None
    tempo: int | None = None
    block: int | None = None
    observed_at: datetime | None = None
    last_error: str | None = None


class GeneratorRoundColumnView(RoundModel):
    column_id: str
    submission_id: UUID
    miner_hotkey: str | None
    """``None`` for the public default generator."""
    counted: bool
    public_default: bool
    generated: bool | None = None
    score: float | None = None
    discriminating: int | None = None
    trivial: int | None = None
    unsolved: int | None = None
    duplicate: int | None = None
    name: str | None = None
    """The generator's public display name (``None`` when unnamed)."""
    name_handle: PublicNameHandle | None = None
    avatar_url: str | None = None
    version: int | None = None
    grading_bench_version: int | None = None
    """The pack grading policy this column's pack declared (13 for a pack
    that declared none); ``None`` until the column is generated."""


class GeneratorRoundStandingView(RoundModel):
    agent_id: UUID
    rank: int
    pairwise_score: float
    wins: int
    ties: int
    losses: int
    opponents: int
    common_mean: float
    common_cases: int
    reference: bool = False
    agent_name: str | None = None
    agent_version: int | None = None
    miner_hotkey: str | None = None
    avatar_url: str | None = None


class GeneratorRoundSummary(RoundModel):
    round_index: int
    status: RoundStatus
    epochs_per_round: int
    commitment: str
    commitment_publication: CommitmentPublication
    commitment_confirmations: int = 0
    """Validators that confirmed the commitment on chain (see
    ``AdminGeneratorRoundDetail.commitment_publications``)."""
    committed_at: datetime
    open_epoch_index: int | None = None
    open_block: int | None = None
    open_block_hash: str | None = None
    opened_at: datetime | None = None
    column_count: int = 0
    panel_size: int = 0
    """Agents frozen on the panel, including any dropped later."""
    panel_dropped: int = 0
    """Panel agents dropped while the round ran (banned, held, or otherwise
    no longer runnable); they are never leased or scored in the round."""
    results: int = 0
    answered_results: int = 0
    """Results from seated panel agents that answered at least one case. A
    round closes void (``insufficient_answered_results``) with fewer than
    ``min_panel``: a run that faulted every case counts as not reported."""
    closed_at: datetime | None = None
    scored_at: datetime | None = None
    void_reason: str | None = None
    revealed_at: datetime | None = None
    secret_hex: str | None = None
    """Revealed secret; ``RoundCommitment(round, secret)`` must equal
    ``commitment`` and ``RoundSeed(round, secret, open_block_hash)`` the seed."""
    seed: str | None = None
    dataset_sha256: str | None = None


class GeneratorRoundDetail(RoundModel):
    summary: GeneratorRoundSummary
    columns: list[GeneratorRoundColumnView]
    standings: list[GeneratorRoundStandingView]


class GeneratorRoundListResponse(RoundModel):
    schedule: GeneratorRoundSchedule
    rounds: list[GeneratorRoundSummary]


# --- public board and submission detail ----------------------------------------


class GeneratorRoundPoint(RoundModel):
    """One scored round column a hotkey's generator held."""

    round_index: int
    submission_id: UUID
    score: float
    rank: int
    """Position among the round's scored columns (1 = best score)."""
    columns: int
    scored_at: datetime | None = None


class PublicGeneratorBoardEntry(GeneratorLedgerEntry):
    """A ledger entry with the public identity and recent form the dashboard
    shows. Validators read the plain ledger (``/scoring/generator-ledger``)."""

    name: str | None = None
    name_handle: PublicNameHandle | None = None
    avatar_url: str | None = None
    version: int | None = None
    status: SubmissionStatus | None = None
    qualified_at: datetime | None = None
    rounds_in_window: int = 0
    """Scored rounds in the freshness window where this hotkey held a column."""
    recent: list[GeneratorRoundPoint] = Field(default_factory=list)
    """The hotkey's latest scored columns in the window, oldest first (at
    most ``PUBLIC_BOARD_RECENT_ROUNDS``)."""


class PublicGeneratorBoardResponse(GeneratorLedgerResponse):
    entries: list[PublicGeneratorBoardEntry] = Field(  # type: ignore[assignment]
        default_factory=list
    )


class GeneratorSubmissionRoundView(RoundModel):
    """One scored round a generator held a column in."""

    round_index: int
    scored_at: datetime | None
    column_id: str
    counted: bool
    score: float
    rank: int
    columns: int
    discriminating: int
    trivial: int
    unsolved: int
    duplicate: int
    panel_size: int
    grading_bench_version: int | None = None
    """The pack grading policy the column's pack declared (13 when none)."""


class PublicGeneratorSubmissionDetail(RoundModel):
    """Everything public about one generator: identity, lifecycle, the
    hotkey's other versions, and its round-by-round results. Never source,
    seeds, or case content."""

    submission: GeneratorSubmissionSummary
    versions: list[GeneratorSubmissionSummary]
    """The hotkey's generators, newest first (this one included)."""
    rounds: list[GeneratorSubmissionRoundView]
    """Scored rounds with a column for this generator, newest first."""


# --- admin ------------------------------------------------------------------


class GeneratorRoundAttemptView(RoundModel):
    agent_id: UUID
    validator_hotkey: str
    ticket_deadline: datetime
    slot_id: str
    status: Literal["leased", "completed", "failed"]
    detail: str | None
    issued_at: datetime
    finished_at: datetime | None


class GeneratorRoundResultView(RoundModel):
    agent_id: UUID
    validator_hotkey: str
    run_id: str
    answered: int
    faulted: int
    mean_score: float | None
    recorded_at: datetime
    excluded_reason: str | None = None
    """Why this result does not count toward the round: its agent was
    dropped from the panel, or it faulted every case."""


class GeneratorRoundCommitmentPublicationView(RoundModel):
    """One elected validator's on-chain publication of a round commitment.

    ``expired`` is a claim whose validator never reported before
    ``claim_expires_at``; it no longer holds an election place.
    """

    validator_hotkey: str
    status: CommitmentPublicationStatus
    claimed_at: datetime
    claim_expires_at: datetime
    reported_at: datetime | None = None
    block_number: int | None = None
    extrinsic_hash: str | None = None
    detail: str | None = None


class GeneratorRoundPanelSlot(RoundModel):
    agent_id: UUID
    owner_key: str
    miner_hotkey: str | None = None
    champion: bool = False
    reference: bool = False
    dropped_reason: str | None = None
    """Set when the agent became ineligible while the round ran. The panel
    stays frozen (nobody takes the seat); the agent is no longer leased and
    its result, if any, is excluded from scoring."""
    dropped_at: datetime | None = None


class GeneratorRoundSkippedAgent(RoundModel):
    """A ranked owner passed over when the panel froze: its agent could not
    run, so the next eligible owner took the seat."""

    agent_id: UUID
    owner_key: str
    miner_hotkey: str | None = None
    reason: str


class GeneratorRoundColumnSlot(RoundModel):
    column_id: str
    submission_id: UUID
    miner_hotkey: str
    owner_key: str
    source_sha256: str
    counted: bool
    public_default: bool = False


class AdminGeneratorRoundDetail(RoundModel):
    """Operator view of a round. The seed is shown once the round closed."""

    summary: GeneratorRoundSummary
    settings_revision: int | None
    cases_per_column: int | None
    bench_version: int | None = None
    """The bench contract the round runs at, frozen when it opened (null before
    it opened, or for a round voided at opening). Rounds opened before rounds
    recorded a version ran at 13; the column is backfilled for them."""
    seed: str | None
    columns: list[GeneratorRoundColumnSlot]
    panel: list[GeneratorRoundPanelSlot]
    generation: dict | None
    column_case_counts: dict[str, int]
    dataset_bytes: int | None
    lease_id: UUID | None
    lease_expires_at: datetime | None
    lease_attempts: int
    commitment_published_at: datetime | None
    commitment_error: str | None
    commitment_publishers_target: int = 0
    """Confirmed validator publications a round collects before it stops asking."""
    commitment_publications: list[GeneratorRoundCommitmentPublicationView] = Field(
        default_factory=list
    )
    """Every validator claim and report, in the round's publisher order."""
    commitment_elected: list[str] = Field(default_factory=list)
    """Validators elected to publish right now (a pending, committed round)."""
    attempts: list[GeneratorRoundAttemptView]
    results: list[GeneratorRoundResultView]
    pending_agents: list[UUID]
    skipped_panel_agents: list[GeneratorRoundSkippedAgent] = Field(default_factory=list)
    """Ranked owners skipped as ineligible when the panel froze."""
    score: dict | None
    scorer_version: str | None
    standings: list[GeneratorRoundStandingView]
    columns_scored: list[GeneratorRoundColumnView]


class AdminGeneratorRoundListResponse(RoundModel):
    schedule: GeneratorRoundSchedule
    rounds: list[GeneratorRoundSummary]
    counts_by_status: dict[str, int]


class AdminGeneratorRoundActionRequest(BaseModel):
    """An audited operator action on one round, guarded by its status."""

    model_config = ConfigDict(extra="ignore", strict=True)

    expected_status: RoundStatus
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str


class AdminGeneratorRoundLeaseReleaseRequest(BaseModel):
    """Release one stuck round panel lease so another validator can take it."""

    model_config = ConfigDict(extra="ignore")

    agent_id: UUID
    validator_hotkey: Annotated[str, Field(pattern=_SS58)]
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str


class AdminDefaultGeneratorRequest(BaseModel):
    """Register (or replace) the public default generator's source."""

    model_config = ConfigDict(extra="ignore", strict=True)

    miner_hotkey: Annotated[str, Field(pattern=_SS58)]
    """The hotkey the default generator's packs echo; any operator hotkey."""
    source_sha256: Annotated[str, Field(pattern=_SHA256)]
    source_base64: Annotated[str, Field(min_length=4, max_length=5_600_000)] | None = (
        None
    )
    upload_key: Annotated[str, Field(min_length=1, max_length=512)] | None = None
    """A key from ``POST /default-generator/upload-url``, instead of inline bytes."""
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str

    @model_validator(mode="after")
    def _one_source(self) -> AdminDefaultGeneratorRequest:
        if (self.source_base64 is None) == (self.upload_key is None):
            raise ValueError("provide exactly one of source_base64 or upload_key")
        return self


class AdminDefaultGeneratorUploadResponse(RoundModel):
    """A presigned PUT for one default-generator source archive."""

    bucket: str
    key: str
    url: str
    method: Literal["PUT"] = "PUT"
    content_type: str
    expires_in: int
    max_bytes: int


class AdminDefaultGeneratorResponse(RoundModel):
    submission_id: UUID
    source_sha256: str
    tree_sha256: str
    status: SubmissionStatus
    superseded: list[UUID] = Field(default_factory=list)


class GeneratorRoundBenchContractPreview(RoundModel):
    """The bench contract a round opened now would freeze, and why."""

    active_version: int
    bench_version: int | None
    """``None`` when no validator can run any round contract (void)."""
    reason: str | None = None
    round_contracts: list[int]
    """``GENERATOR_ROUND_BENCH_VERSIONS``: the pack grading policies."""
    capable_validators: dict[str, int]
    """Validators that could run a round of this column size, per contract."""
    previous_version: int | None = None
    """The version the newest opened round froze. Rounds switch to a newer
    version only with ``min_round_validators`` capable validators."""
    min_round_validators: int | None = None
    scorer_grading_versions: list[int] | None = None
    """The pack grading policies the live generator scorer grades (its
    advertised list, or v13 only when it advertised none)."""


class AdminGeneratorRoundPreview(RoundModel):
    """What a round opened now would freeze, and anything blocking it."""

    schedule: GeneratorRoundSchedule
    columns: list[GeneratorRoundColumnSlot]
    panel: list[GeneratorRoundPanelSlot]
    qualified_generators: int
    awaiting_qualification: int
    blockers: list[str]
    skipped_panel_agents: list[GeneratorRoundSkippedAgent] = Field(default_factory=list)
    bench_contract: GeneratorRoundBenchContractPreview | None = None


class GeneratorRoundLoopStatus(RoundModel):
    """The round driver's last tick, for diagnosing a stalled lane."""

    last_tick_at: datetime | None
    last_success_at: datetime | None
    last_error: str | None
    last_actions: list[str]
    ticks: int


# --- scorer -----------------------------------------------------------------


class RoundSourceColumn(RoundModel):
    column_id: str
    submission_id: UUID
    miner_hotkey: str
    source_sha256: str
    source_base64: str


class RoundCaseRef(RoundModel):
    case_id: str
    fingerprint: str


class RoundMatrixColumn(RoundModel):
    column_id: str
    owner_key: str
    counted: bool
    cases: list[RoundCaseRef]


class RoundMatrixAgent(RoundModel):
    agent_id: str
    owner_key: str
    reference: bool = False
    champion: bool = False
    results: list[PanelCaseResult]


class RoundMatrix(RoundModel):
    """Mirror of ``challenge.RoundInput``."""

    columns: list[RoundMatrixColumn]
    agents: list[RoundMatrixAgent]
    claimed_fingerprints: list[str] = Field(default_factory=list)


class GeneratorRoundWork(RoundModel):
    kind: Literal["generate", "score"]
    lease_id: UUID
    round_index: int
    lease_expires_at: datetime
    seed: str | None = None
    cases_per_column: int | None = None
    columns: list[RoundSourceColumn] | None = None
    # The round's frozen bench contract: each column's request accepts every
    # pack grading policy through it.
    bench_version: int | None = None
    params: dict | None = None
    matrix: RoundMatrix | None = None


class GeneratorRoundWorkResponse(RoundModel):
    work: GeneratorRoundWork | None = None


class RoundColumnResult(RoundModel):
    column_id: str
    submission_id: UUID
    ok: bool = False
    build: GeneratorBuildResult = Field(default_factory=GeneratorBuildResult)
    seed: GeneratorSeedOutput | None = None
    error: Annotated[str | None, Field(max_length=8192)] = None


class GeneratorRoundGenerateResult(RoundModel):
    lease_id: UUID
    round_index: int
    scorer_version: Annotated[str, Field(min_length=1, max_length=64)]
    sandbox: Annotated[str, Field(max_length=32)] = ""
    columns: list[RoundColumnResult]
    dataset_sha256: Annotated[str, Field(pattern=_SHA256)] | None = None
    dataset_base64: Annotated[str | None, Field(max_length=45_000_000)] = None


class RoundCaseVerdict(RoundModel):
    """Mirror of ``challenge.CaseVerdict``."""

    case_id: str
    fingerprint: str
    status: RoundCaseStatus
    panel_size: int
    solvers: int
    solver_owners: int
    mean_score: float
    champion_failed: bool
    credit: float


class RoundPackScore(RoundModel):
    """Mirror of ``challenge.PackScore``."""

    status: str
    score: float
    raw_credit: float
    discriminating: int
    trivial: int
    unsolved: int
    duplicate: int
    unsolved_rate: float
    cases: list[RoundCaseVerdict]


class RoundColumnScore(RoundModel):
    column_id: str
    pack: RoundPackScore


class RoundAgentStanding(RoundModel):
    """Mirror of ``challenge.AgentStanding``."""

    agent_id: str
    rank: int
    pairwise_score: float
    wins: int
    ties: int
    losses: int
    opponents: int
    common_mean: float
    common_cases: int


class RoundScore(RoundModel):
    columns: list[RoundColumnScore]
    agents: list[RoundAgentStanding]


class GeneratorRoundScoreResult(RoundModel):
    lease_id: UUID
    round_index: int
    scorer_version: Annotated[str, Field(min_length=1, max_length=64)]
    score: RoundScore


class GeneratorRoundAck(RoundModel):
    round_index: int
    status: RoundStatus
