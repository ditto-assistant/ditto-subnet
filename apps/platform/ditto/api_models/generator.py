"""Wire models for the DittoBench generator competition.

Generator miners upload the Go source of a CPU-only **generator**: a program
that reads a ``GenerateRequest`` (a secret seed, the miner hotkey, a case
count) on stdin and writes a challenge pack (a memory haystack plus cases with
deterministic gold answers) to stdout. The contract, sandbox, validation
rules, and scoring formula live in ``research/dittobench-datagen`` (see
``docs/generator-competition.md``). Platform stores source privately and has a
trusted scorer **qualify** each upload (build, valid packs on fresh secret
seeds, determinism, probe copy check). Qualified generators then compete in
chain-epoch **rounds** (:mod:`ditto.api_models.generator_rounds`): the top
generators' cases are run by validators against the top agents, and the
weight-zero ledger publishes each generator's latest round score.

Settings ``mode`` is ``off``, ``shadow`` or ``enforce``. In ``shadow`` every
ledger entry is ``weight_eligible=False``. In ``enforce`` the ledger marks
qualified miner generators eligible, which still pays nothing until the
operator also serves a generator track share through track-share governance
(:mod:`ditto.api_models.track_share_settings`). Every other behavior of the
lane is identical in ``shadow`` and ``enforce``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

GENERATOR_SETTINGS_SCOPE = "*"
GENERATOR_CONTRACT_V1 = "dittobench-generator/v1"
MAX_SOURCE_ARCHIVE_BYTES = 4 << 20
MIN_PACK_CASES = 8
# The pack contract's challenge.MaxCases. 80 lets a round reach ~70 cases per
# column, so five counted columns give about the 350 memory cases of a bench
# v13 run. A validator runs a round only when its scorer advertises at least
# the round's column size (see ``generator_round_validator``).
MAX_CASES_PER_SEED = 80
SOURCE_SIGNATURE_DOMAIN = "ditto-generator-v1"

GeneratorMode = Literal["off", "shadow", "enforce"]
SubmissionStatus = Literal["active", "superseded", "rejected"]
RunStatus = Literal[
    "leased",
    "scored",
    "qualified",
    "invalid",
    "insufficient_panel",
    "copy",
    "abandoned",
]
CaseStatus = Literal["discriminating", "trivial", "unsolved", "duplicate"]

_SHA256 = r"^[0-9a-f]{64}$"
_SS58 = r"^[1-9A-HJ-NP-Za-km-z]{47,48}$"
_BLOCK_HASH = r"^0x[0-9a-fA-F]{64}$"


def generator_settings_confirmation(mode: str) -> str:
    """The phrase a settings write must carry, naming the mode it applies."""
    return f"APPLY GENERATOR COMPETITION {mode.upper()}"


PROBE_ROTATION_CONFIRMATION = "ROTATE GENERATOR PROBE SEED"
REJECT_SUBMISSION_CONFIRMATION = "REJECT GENERATOR SUBMISSION"


def source_signature_payload(
    *, hotkey: str, source_sha256: str, timestamp: int, nonce: UUID
) -> bytes:
    """The domain-separated bytes a miner signs to upload generator source.

    Distinct from ``ditto-upload-v2`` so an agent-upload signature can never be
    replayed as a generator upload or vice versa.
    """
    return (
        f"{SOURCE_SIGNATURE_DOMAIN}:{hotkey}:{source_sha256}:{timestamp}:{nonce}"
    ).encode("ascii")


class GeneratorModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class GeneratorCompetitionSettings(BaseModel):
    """The complete generator competition policy (one revision = whole object).

    Scoring fields mirror ``challenge.ScoreParams`` in the Go contract and are
    bounded identically; ``cases_per_seed`` is both the exact case count every
    pack (qualification seed or round column) must contain and the score's
    ``target_cases``.
    """

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    mode: GeneratorMode = "off"
    """``off`` refuses uploads, leases nothing, and opens no rounds; ``shadow``
    runs the lane with every ledger entry ``weight_eligible=False``;
    ``enforce`` runs it identically but marks qualified miner generators
    ``weight_eligible``. Validators pay those entries only from a governed
    generator track share, so ``enforce`` alone moves no emission."""
    panel_size: Annotated[int, Field(ge=3, le=16)] = 5
    """Top-ranked agent owners (one agent each) every round runs."""
    min_panel: Annotated[int, Field(ge=2, le=32)] = 3
    min_solver_owners: Annotated[int, Field(ge=1, le=32)] = 2
    solve_threshold: Annotated[float, Field(gt=0.0, le=1.0)] = 0.5
    champion_bonus: Annotated[float, Field(ge=0.0, le=2.0)] = 0.5
    seeds_per_run: Annotated[int, Field(ge=1, le=4)] = 2
    """Fresh secret seeds per qualification run; every one must be valid."""
    cases_per_seed: Annotated[int, Field(ge=MIN_PACK_CASES, le=MAX_CASES_PER_SEED)] = 16
    window_days: Annotated[int, Field(ge=1, le=30)] = 7
    """A hotkey's ledger score is its latest round column score within this
    window."""
    epochs_per_round: Annotated[int, Field(ge=1, le=48)] = 4
    """Chain epochs (``SubnetEpochIndex``) per round:
    ``round = SubnetEpochIndex // epochs_per_round``."""
    counted_columns: Annotated[int, Field(ge=1, le=6)] = 5
    """Generator columns every agent is compared on: the best qualified
    generators by latest round score, filled by the public default generator
    while too few qualify."""
    challenger_columns: Annotated[int, Field(ge=0, le=2)] = 1
    """Extra columns for qualified generators that have never held a column.
    They are scored as generators but never move an agent."""
    reference_agent_ids: Annotated[list[UUID], Field(max_length=3)] = Field(
        default_factory=list
    )
    """Subnet-run baseline agents added to every round panel. Each counts as
    its own independent solver owner."""
    privacy_window_rounds: Annotated[int, Field(ge=1, le=180)] = 6
    """Rounds after a round opens before its secret, seed, and dataset are
    revealed publicly."""
    onchain_commitment: bool = True
    """Also publish each round's seed commitment on-chain: Platform elects up
    to three fresh validators per round to write ``dgr1:{round}:{commitment}``
    with the Commitments pallet ``set_commitment`` from their own Pylon
    identities. Best effort; a round opens either way."""
    validator_rounds_enabled: bool = False
    """Issue round panel runs to validators. Off, rounds generate and wait
    without consuming validator capacity."""
    copy_overlap_threshold: Annotated[float, Field(ge=0.2, le=1.0)] = 0.5
    """Share of a new submission's probe fingerprints that may match an earlier
    submission from another owner before it is rejected as a copy."""
    upload_cooldown_seconds: Annotated[int, Field(ge=60, le=86_400)] = 3_600
    lease_ttl_seconds: Annotated[int, Field(ge=600, le=21_600)] = 7_200
    max_attempts: Annotated[int, Field(ge=1, le=20)] = 6
    """Consecutive unfinished leases before a run backs off."""
    retry_backoff_seconds: Annotated[int, Field(ge=60, le=86_400)] = 3_600
    upload_fee_required: bool = False
    """Require a finalized TAO payment proof on every miner upload. The
    amount is the generator competition's fee in the submission settings
    (``competition_fees.generator.fee_amount_rao``, inheriting the default
    agent-upload fee while unset), paid to the submission deposit address; a
    proof funds exactly one upload across the agent and generator lanes. A
    ``competition_fees.generator.fee_required`` override, when set, decides
    instead of this switch. Off by default, and a revision written before
    this field existed reads as off."""

    @model_validator(mode="after")
    def _coherent(self) -> GeneratorCompetitionSettings:
        if self.min_solver_owners > self.min_panel:
            raise ValueError("min_solver_owners must not exceed min_panel")
        if self.min_panel > self.panel_size + len(self.reference_agent_ids):
            raise ValueError("min_panel must not exceed the round panel size")
        if len(set(self.reference_agent_ids)) != len(self.reference_agent_ids):
            raise ValueError("reference_agent_ids must be unique")
        if self.counted_columns + self.challenger_columns > 8:
            raise ValueError("a round holds at most 8 columns")
        return self

    def score_params(self) -> dict[str, float | int]:
        """The ``challenge.ScoreParams`` view of this policy."""
        return {
            "min_panel": self.min_panel,
            "min_solver_owners": self.min_solver_owners,
            "solve_threshold": self.solve_threshold,
            "champion_bonus": self.champion_bonus,
            "target_cases": self.cases_per_seed,
        }


class GeneratorSettingsRevision(GeneratorModel):
    revision: int
    parent_revision: int
    scope: str
    settings: GeneratorCompetitionSettings
    reason: str
    actor: str
    created_at: datetime
    checksum: str
    settings_valid: bool = True


class EffectiveGeneratorSettings(GeneratorModel):
    revision: int
    settings: GeneratorCompetitionSettings
    checksum: str
    source: Literal["revision", "default"]
    settings_valid: bool = True


class AdminGeneratorSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    scope: str = GENERATOR_SETTINGS_SCOPE
    expected_revision: Annotated[int, Field(ge=0)]
    settings: GeneratorCompetitionSettings
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str

    @model_validator(mode="after")
    def _require_complete_policy(self) -> AdminGeneratorSettingsRequest:
        missing = sorted(
            set(GeneratorCompetitionSettings.model_fields)
            - self.settings.model_fields_set
        )
        if missing:
            raise ValueError(
                "a generator competition revision stores the WHOLE policy, so "
                f"every field must be sent explicitly; missing {missing}. Read "
                "GET /admin/generator-competition/settings and send back the "
                "complete object."
            )
        return self


class AdminGeneratorSettingsResponse(GeneratorModel):
    current: list[GeneratorSettingsRevision]
    history: list[GeneratorSettingsRevision]
    default: GeneratorCompetitionSettings
    effective: EffectiveGeneratorSettings


# --- miner upload --------------------------------------------------------


class GeneratorSubmissionUploadRequest(BaseModel):
    """A signed generator source upload (a gzip-compressed tar)."""

    model_config = ConfigDict(extra="ignore")

    hotkey: Annotated[str, Field(pattern=_SS58)]
    source_sha256: Annotated[str, Field(pattern=_SHA256)]
    source_base64: Annotated[
        str, Field(min_length=4, max_length=(MAX_SOURCE_ARCHIVE_BYTES + 2) // 3 * 4)
    ]
    signature: Annotated[str, Field(pattern=r"^(0x)?[0-9a-fA-F]{128}$")]
    signature_timestamp: Annotated[int, Field(ge=1)]
    signature_nonce: UUID
    payment_block_hash: Annotated[str | None, Field(pattern=_BLOCK_HASH)] = None
    payment_block_number: Annotated[int | None, Field(ge=1)] = None
    payment_extrinsic_index: Annotated[int | None, Field(ge=0)] = None
    """The finalized ``Balances.transfer_keep_alive`` that pays the upload
    fee, required only while the generator fee is required (see
    ``GeneratorUploadTerms``). All three or none."""

    @model_validator(mode="after")
    def _payment_all_or_none(self) -> GeneratorSubmissionUploadRequest:
        supplied = {
            self.payment_block_hash is not None,
            self.payment_block_number is not None,
            self.payment_extrinsic_index is not None,
        }
        if len(supplied) != 1:
            raise ValueError(
                "payment_block_hash, payment_block_number and "
                "payment_extrinsic_index are required together"
            )
        return self

    @property
    def has_payment(self) -> bool:
        return self.payment_block_hash is not None


class GeneratorUploadTerms(GeneratorModel):
    """What an upload must satisfy right now, read before paying anything.

    ``blockers`` lists every refusal Platform can decide without the archive
    (lane closed, banned hotkey, upload cooldown, an archive already
    uploaded); a client must not pay while it is non-empty. Registration,
    archive structure and duplicate source trees are only checked on upload,
    and an unspent fee stays reusable for 24 hours after a refused upload.
    """

    accepting: bool
    competition: Literal["generator"] = "generator"
    """The competition these terms price. A client checks it before paying,
    so a quote for another lane can never be paid as a generator fee."""
    payment_required: bool
    payment_amount_rao: int | None = None
    """The generator competition's own price, exact: an upload whose proof
    pays any other amount (less or more) is refused, and the transfer does
    not become a credit."""
    payment_send_address: str | None = None
    payment_amount_source: Literal["default", "override"] | None = None
    """``override`` when the generator competition has its own price,
    ``default`` when it inherits the agent-upload fee; null when no payment
    is required."""
    upload_cooldown_seconds: int
    retry_at: datetime | None = None
    blockers: list[str] = Field(default_factory=list)


class GeneratorSubmissionUploadResponse(GeneratorModel):
    submission_id: UUID
    source_sha256: str
    tree_sha256: str
    status: SubmissionStatus
    superseded: list[UUID] = Field(default_factory=list)


class GeneratorCaseStatusCounts(GeneratorModel):
    discriminating: int = 0
    trivial: int = 0
    unsolved: int = 0
    duplicate: int = 0


class GeneratorRunSummary(GeneratorModel):
    """Public-safe view of one run: lifecycle and aggregate outcome.

    Never includes seeds, case content, case ids, or panel agent identities.
    """

    run_id: UUID
    attempt: int
    status: RunStatus
    outcome: str | None
    score: float | None
    case_counts: GeneratorCaseStatusCounts | None
    leased_at: datetime
    finished_at: datetime | None


class GeneratorSubmissionSummary(GeneratorModel):
    """Public-safe view of one submission. Never includes source."""

    submission_id: UUID
    miner_hotkey: str
    source_sha256: str
    tree_sha256: str
    module: str
    go_version: str
    file_count: int
    size_bytes: int
    status: SubmissionStatus
    status_reason: str | None
    next_run_at: datetime | None
    submitted_at: datetime
    latest_run: GeneratorRunSummary | None
    qualified_at: datetime | None = None
    public_default: bool = False
    round_score: float | None = None
    round_scored_index: int | None = None


class GeneratorSubmissionListResponse(GeneratorModel):
    submissions: list[GeneratorSubmissionSummary]


# --- scorer ----------------------------------------------------------------


class GeneratorPanelSlot(GeneratorModel):
    agent_id: str
    owner_key: str
    champion: bool = False


class GeneratorWorkItem(GeneratorModel):
    lease_id: UUID
    run_id: UUID
    submission_id: UUID
    miner_hotkey: str
    source_sha256: str
    source_base64: str
    seeds: list[str]
    probe_seed: str
    cases_per_seed: int
    lease_expires_at: datetime


class GeneratorWorkResponse(GeneratorModel):
    work: GeneratorWorkItem | None = None


class ValidationIssue(GeneratorModel):
    case_id: str | None = None
    rule: str
    message: str


class ValidationCaseRef(GeneratorModel):
    case_id: Annotated[str, Field(min_length=1, max_length=64)]
    fingerprint: Annotated[str, Field(pattern=_SHA256)]


class ValidationReport(GeneratorModel):
    """Mirror of ``challenge.Report``."""

    valid: bool = False
    pack_sha256: str | None = None
    case_count: int = 0
    pair_count: int = 0
    cases: list[ValidationCaseRef] | None = None
    issues: list[ValidationIssue] | None = None


class PanelCaseResult(GeneratorModel):
    case_id: str
    score: Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)] = 0.0
    fault: bool = False


class PanelMemberResult(GeneratorModel):
    agent_id: str
    owner_key: str = ""
    champion: bool = False
    results: list[PanelCaseResult] | None = None


class GeneratorBuildResult(GeneratorModel):
    """Mirror of ``genrun.BuildOutput``."""

    ok: bool = False
    binary_sha256: str | None = None
    error: Annotated[str | None, Field(max_length=8192)] = None
    duration_ms: int = 0


class GeneratorSeedOutput(GeneratorModel):
    """Mirror of ``genrun.SeedOutput`` (the probe and each scored seed)."""

    seed: Annotated[str, Field(pattern=_SHA256)]
    pack_sha256: str | None = None
    validation: ValidationReport = Field(default_factory=ValidationReport)
    error: Annotated[str | None, Field(max_length=8192)] = None
    duration_ms: int = 0

    @property
    def valid(self) -> bool:
        return not self.error and self.validation.valid


class GeneratorResultRequest(GeneratorModel):
    lease_id: UUID
    run_id: UUID
    submission_id: UUID
    source_sha256: Annotated[str, Field(pattern=_SHA256)]
    scorer_version: Annotated[str, Field(min_length=1, max_length=64)]
    sandbox: Annotated[str, Field(max_length=32)] = ""
    build: GeneratorBuildResult
    deterministic: bool | None = None
    seeds: list[GeneratorSeedOutput] | None = None
    probe: GeneratorSeedOutput | None = None


ScorerFaultKind = Literal["unsupported_request", "sandbox"]


class GeneratorScorerFault(GeneratorModel):
    """The scorer could not do a leased work item, and it is not the generator's fault.

    ``unsupported_request``: the lease asks for something this scorer build's
    pack contract refuses (for example a case count above its
    ``challenge.MaxCases``), detected before any generator ran.
    ``sandbox``: the sandbox itself failed (docker, image, host I/O).
    Neither ever rejects a submission: a qualification backs off and a round
    is voided (unsupported request) or re-leased (sandbox).
    """

    work: Literal["qualification", "round"]
    lease_id: UUID
    run_id: UUID | None = None
    submission_id: UUID | None = None
    round_index: int | None = None
    error_kind: ScorerFaultKind
    detail: Annotated[str, Field(max_length=4000)] = ""
    scorer_version: Annotated[str, Field(min_length=1, max_length=64)]


class GeneratorScorerFaultAck(GeneratorModel):
    work: Literal["qualification", "round"]
    action: Literal["backed_off", "voided", "released"]
    submission_status: SubmissionStatus | None = None
    round_status: str | None = None


class GeneratorResultResponse(GeneratorModel):
    run_id: UUID
    status: RunStatus
    outcome: str
    score: float | None = None
    submission_status: SubmissionStatus


# --- admin -------------------------------------------------------------


class GeneratorCaseVerdictView(GeneratorModel):
    seed_index: int
    case_id: str
    position: int
    fingerprint: str
    status: CaseStatus
    panel_size: int
    solvers: int
    solver_owners: int
    mean_score: float
    champion_failed: bool
    credit: float


class AdminGeneratorRunDetail(GeneratorModel):
    """Operator view of one run. Seeds are revealed once the run finished."""

    summary: GeneratorRunSummary
    submission_id: UUID
    miner_hotkey: str
    seeds: list[str] | None
    probe_epoch: int
    cases_per_seed: int
    settings_revision: int
    scorer_version: str | None
    panel: list[GeneratorPanelSlot]
    evaluation: dict | None
    score_summary: dict | None
    verdicts: list[GeneratorCaseVerdictView]


class AdminGeneratorSubmissionDetail(GeneratorModel):
    """Operator view of one submission and its runs (never the source)."""

    summary: GeneratorSubmissionSummary
    owner_key: str
    attempts: int
    lease_run_id: UUID | None
    lease_expires_at: datetime | None
    runs: list[GeneratorRunSummary]


class AdminGeneratorSubmissionListResponse(GeneratorModel):
    submissions: list[GeneratorSubmissionSummary]
    counts_by_status: dict[str, int]


class GeneratorProbeEpochView(GeneratorModel):
    """A probe epoch's public facts; the seed itself is never served."""

    epoch: int
    reason: str
    actor: str
    created_at: datetime
    retired_at: datetime | None
    probed_submissions: int


class AdminGeneratorProbeResponse(GeneratorModel):
    current: GeneratorProbeEpochView | None
    history: list[GeneratorProbeEpochView]


class AdminGeneratorProbeRotateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str


class AdminGeneratorSubmissionActionRequest(BaseModel):
    """An audited operator action on one submission.

    ``expected_status`` guards against acting on a submission whose state moved
    since the operator read it.
    """

    model_config = ConfigDict(extra="ignore", strict=True)

    expected_status: SubmissionStatus
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str | None = None


class GeneratorPromotionCandidate(GeneratorModel):
    """A discriminating case from a scored round."""

    round_index: int
    column_id: str
    submission_id: UUID
    miner_hotkey: str
    case_id: str
    fingerprint: str
    mean_score: float
    solver_owners: int
    champion_failed: bool
    credit: float
    scored_at: datetime


class AdminGeneratorPromotionCandidatesResponse(GeneratorModel):
    candidates: list[GeneratorPromotionCandidate]
