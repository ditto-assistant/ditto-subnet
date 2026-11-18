"""Wire models for the SN118 ``model_context`` (CPM) competition.

Miners submit small open-weight models (a LoRA or a full fine-tune of an
allowlisted Qwen3.5 base) that build Ditto's context packet. The build contract
is ``docs/research/cpm/competition-v1.md``: section 2 (model contract), 3
(submission, commitment, priority, duplicates, limits), 4 (seasons and the
``cpm_score_version = 1`` score) and 5 (Platform tables, settings and
endpoints).

Platform is the system of record: it admits signed submissions whose on-chain
commitment ``dcpm1:<manifest_sha256>`` matches, screens the pinned Hugging Face
revision, opens seasons, leases runs to the trusted CPM scorer, and publishes
the ledger validators fold. Settings ``mode`` is ``off`` (the shipped default:
no uploads, no seasons, no leases, an empty ledger), ``shadow`` or
``enforce``. The track is ``KNOWN`` but not ``PAYABLE``, so no mode moves
emission until a validator release pays it and an owner sets a share.
"""

from __future__ import annotations

import hashlib
import math
from datetime import datetime
from typing import Annotated, Literal, get_args
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MODEL_CONTEXT_COMPETITION = "model_context"
MODEL_CONTEXT_SETTINGS_SCOPE = "*"
CPM_SCORE_VERSION = 1
MANIFEST_SCHEMA = "ditto-cpm-manifest-v1"
MANIFEST_FILENAME = "cpm-manifest.json"
SIGNATURE_DOMAIN = "ditto-model-context-v1"
COMMITMENT_PREFIX = "dcpm1:"
SEASON_SEED_DOMAIN = "dcpm-season"
SEEDS_PER_SEASON = 3
GIB = 1 << 30
# The manifest is a few file entries and a short attestation; anything near
# this cap is not a manifest.
MAX_MANIFEST_BYTES = 64 << 10
MAX_MANIFEST_FILES = 64
MAX_ATTESTATION_CHARS = 4_000
MAX_REJECT_REASON_CHARS = 1_000
MAX_FAULT_DETAIL_CHARS = 2_000
ALLOWED_LICENSES = ("apache-2.0", "mit")

ModelContextMode = Literal["off", "shadow", "enforce"]
SubmissionKind = Literal["lora", "full"]
SubmissionStatus = Literal["screening", "queued", "scored", "rejected", "superseded"]
RunKind = Literal["submission", "reference"]
RunStatus = Literal["queued", "leased", "scored", "failed", "abandoned"]
FaultKind = Literal["infra", "nondeterministic", "model_load", "invalid_model"]
# ``model_context_runs.fault`` stores ``"{error_kind}: {detail}"`` in at most
# MAX_FAULT_DETAIL_CHARS, so a request's detail may use what the longest kind
# leaves: 2000 - len("nondeterministic: ") = 1982 characters.
MAX_FAULT_REQUEST_DETAIL_CHARS = MAX_FAULT_DETAIL_CHARS - max(
    len(f"{kind}: ") for kind in get_args(FaultKind)
)
"""``infra``: the scorer host failed (released and retried, never blames the
model). ``nondeterministic``: the re-run seed differed bitwise (contract
section 2.4; the run fails). ``model_load`` / ``invalid_model``: the weights do
not load or do not behave as the declared base (the run fails and the
submission is rejected)."""

_SHA256 = r"^[0-9a-f]{64}$"
_REVISION = r"^[0-9a-f]{40}$"
_SS58 = r"^[1-9A-HJ-NP-Za-km-z]{47,48}$"
_BASE_ID = r"^[a-z0-9][a-z0-9._-]{0,63}$"
# Hugging Face ``namespace/name``: each part 1-96 of [A-Za-z0-9._-], not
# starting with a separator. No ``..``, so a repo id never walks a URL path.
HF_REPO_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}/[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$"


def signature_payload(
    *,
    hotkey: str,
    hf_repo: str,
    revision: str,
    manifest_sha256: str,
    ts: int,
    nonce: UUID | str,
) -> bytes:
    """The exact bytes a miner signs (contract section 3.2)."""
    return (
        f"{SIGNATURE_DOMAIN}:{hotkey}:{hf_repo}:{revision}:"
        f"{manifest_sha256}:{ts}:{nonce}"
    ).encode("ascii")


def commitment_value(manifest_sha256: str) -> str:
    """The on-chain ``Commitments`` text a submission needs (section 3.1)."""
    return f"{COMMITMENT_PREFIX}{manifest_sha256}"


def season_seeds(season: int, open_block_hash: str) -> list[int]:
    """The season's seeds (contract section 4.1).

    ``seed_k = uint64(sha256("dcpm-season:{season}:{k}:{hash}")[:8])``: the
    first 8 digest bytes read big-endian, ``hash`` the season-open block hash
    as Platform stores it (``0x`` plus 64 lowercase hex digits).
    """
    return [
        int.from_bytes(
            hashlib.sha256(
                f"{SEASON_SEED_DOMAIN}:{season}:{k}:{open_block_hash}".encode("ascii")
            ).digest()[:8],
            "big",
        )
        for k in range(SEEDS_PER_SEASON)
    ]


def generate_seed(seed: int) -> int:
    """A uint64 season seed as ``cmd/generate -seed`` takes it (a signed
    int64): seeds at or above 2**63 become their two's-complement value."""
    if not 0 <= seed < 1 << 64:
        raise ValueError(f"season seed {seed} is not a uint64")
    return seed - (1 << 64) if seed >= 1 << 63 else seed


def seed_score(
    memory_recall: float, distractor_rate: float, tool_recall: float
) -> float:
    """One seed's score (contract section 4.4)."""
    return 0.6 * memory_recall * (1.0 - 0.5 * distractor_rate) + 0.4 * tool_recall


def model_context_settings_confirmation(mode: str) -> str:
    """The phrase a settings write carries, naming the mode it applies."""
    return f"APPLY MODEL CONTEXT COMPETITION {mode.upper()}"


ROTATE_SEASON_CONFIRMATION = "ROTATE MODEL CONTEXT SEASON"


class ModelContextModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


# --- settings (contract section 5.2) ----------------------------------------

# Attention and MLP projections, plus Qwen3.5's linear-attention (Gated
# DeltaNet) projections (contract section 2.2).
QWEN35_LORA_TARGETS = (
    "q_proj",
    "k_proj",
    "v_proj",
    "o_proj",
    "gate_proj",
    "up_proj",
    "down_proj",
    "in_proj_qkv",
    "in_proj_z",
    "in_proj_a",
    "in_proj_b",
    "out_proj",
)


class ModelContextBase(BaseModel):
    """One allowlisted base model (contract section 2.1)."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    base_id: Annotated[str, Field(pattern=_BASE_ID)]
    hf_repo: Annotated[str, Field(pattern=HF_REPO_PATTERN)]
    revision: Annotated[str, Field(pattern=_REVISION)]
    parameters: Annotated[int, Field(ge=1)]
    """The base's parameter count; a ``full`` submission must be within 1%."""
    architectures: Annotated[list[str], Field(min_length=1, max_length=4)]
    """``config.json`` ``architectures`` a ``full`` submission must equal."""
    lora_target_modules: Annotated[list[str], Field(min_length=1, max_length=32)]
    """Module names a LoRA may target."""


DEFAULT_BASES = (
    ModelContextBase(
        base_id="qwen3.5-0.8b",
        hf_repo="Qwen/Qwen3.5-0.8B",
        revision="2fc06364715b967f1860aea9cf38778875588b17",
        parameters=873_441_376,
        architectures=["Qwen3_5ForConditionalGeneration"],
        lora_target_modules=list(QWEN35_LORA_TARGETS),
    ),
    ModelContextBase(
        base_id="qwen3.5-2b",
        hf_repo="Qwen/Qwen3.5-2B",
        revision="15852e8c16360a2fea060d615a32b45270f8a8fc",
        parameters=2_274_072_416,
        architectures=["Qwen3_5ForConditionalGeneration"],
        lora_target_modules=list(QWEN35_LORA_TARGETS),
    ),
)
DEFAULT_RANK_SHARES = (0.50, 0.20, 0.13, 0.10, 0.07)


class ModelContextSettings(BaseModel):
    """The whole model_context policy (one revision = the whole object)."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    mode: ModelContextMode = "off"
    """``off`` refuses uploads, opens no season, leases nothing and serves an
    empty ledger. ``shadow`` and ``enforce`` run the lane identically; the
    track pays nothing until a validator release pays it."""
    base_allowlist: Annotated[
        list[ModelContextBase], Field(min_length=1, max_length=8)
    ] = Field(default_factory=lambda: list(DEFAULT_BASES))
    max_lora_rank: Annotated[int, Field(ge=1, le=256)] = 64
    max_total_bytes: Annotated[int, Field(ge=1 << 20, le=16 * GIB)] = 6 * GIB
    upload_cooldown_seconds: Annotated[int, Field(ge=60, le=7 * 86_400)] = 21_600
    upload_fee_required: bool = False
    """The fee path is not built in v1; a write that sets it is refused."""
    season_days: Annotated[int, Field(ge=1, le=60)] = 7
    floor_margin: Annotated[float, Field(ge=0.0, le=1.0)] = 0.01
    budget_tokens: Annotated[int, Field(ge=128, le=65_536)] = 2_048
    tool_k: Annotated[int, Field(ge=1, le=128)] = 8
    rank_shares: Annotated[list[float], Field(min_length=1, max_length=16)] = Field(
        default_factory=lambda: list(DEFAULT_RANK_SHARES)
    )
    bench_version: Annotated[int, Field(ge=1, le=999)] = 18
    """The DittoBench version a newly opened season pins (section 4.2)."""
    lease_ttl_seconds: Annotated[int, Field(ge=600, le=86_400)] = 14_400
    """How long a scorer holds a leased run before it can be re-leased."""
    max_attempts: Annotated[int, Field(ge=1, le=20)] = 3
    """Leases (expired or faulted ``infra``) before a run is abandoned."""

    @field_validator("rank_shares")
    @classmethod
    def _shares(cls, value: list[float]) -> list[float]:
        if any(not math.isfinite(s) or s < 0.0 or s > 1.0 for s in value):
            raise ValueError("rank_shares must each be in [0, 1]")
        if sum(value) > 1.0 + 1e-9:
            raise ValueError("rank_shares must sum to at most 1")
        if any(b > a for a, b in zip(value, value[1:], strict=False)):
            raise ValueError("rank_shares must not increase with rank")
        return value

    @model_validator(mode="after")
    def _unique_bases(self) -> ModelContextSettings:
        ids = [base.base_id for base in self.base_allowlist]
        if len(set(ids)) != len(ids):
            raise ValueError("base_allowlist repeats a base_id")
        return self

    def base(self, base_id: str) -> ModelContextBase | None:
        return next((b for b in self.base_allowlist if b.base_id == base_id), None)


class ModelContextSettingsRevision(ModelContextModel):
    revision: int
    parent_revision: int
    scope: str
    settings: ModelContextSettings
    reason: str
    actor: str
    created_at: datetime
    checksum: str
    settings_valid: bool = True


class EffectiveModelContextSettings(ModelContextModel):
    revision: int
    settings: ModelContextSettings
    checksum: str
    source: Literal["revision", "default"]
    settings_valid: bool = True


class AdminModelContextSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    scope: str = MODEL_CONTEXT_SETTINGS_SCOPE
    expected_revision: Annotated[int, Field(ge=0)]
    settings: ModelContextSettings
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str

    @model_validator(mode="after")
    def _require_complete_policy(self) -> AdminModelContextSettingsRequest:
        missing = sorted(
            set(ModelContextSettings.model_fields) - self.settings.model_fields_set
        )
        if missing:
            raise ValueError(
                "a model_context revision stores the WHOLE policy, so every "
                f"field must be sent explicitly; missing {missing}. Read GET "
                "/admin/model-context/settings and send back the complete object."
            )
        return self


class AdminModelContextSettingsResponse(ModelContextModel):
    current: list[ModelContextSettingsRevision]
    history: list[ModelContextSettingsRevision]
    default: ModelContextSettings
    effective: EffectiveModelContextSettings


class AdminRotateSeasonRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str


# --- manifest (contract section 2.3) ----------------------------------------


class ManifestFile(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    path: Annotated[str, Field(min_length=1, max_length=200)]
    sha256: Annotated[str, Field(pattern=_SHA256)]
    bytes: Annotated[int, Field(ge=1)]


class CpmManifest(BaseModel):
    """``cpm-manifest.json`` at the repo root."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    schema_: Annotated[Literal["ditto-cpm-manifest-v1"], Field(alias="schema")]
    track: Literal["model_context"]
    base_id: Annotated[str, Field(pattern=_BASE_ID)]
    kind: SubmissionKind
    files: Annotated[
        list[ManifestFile], Field(min_length=1, max_length=MAX_MANIFEST_FILES)
    ]
    license: str
    training_data_attestation: Annotated[str, Field(max_length=MAX_ATTESTATION_CHARS)]

    @property
    def total_bytes(self) -> int:
        return sum(f.bytes for f in self.files)


# --- miner upload (contract sections 3 and 5.3) -----------------------------


class ModelContextSubmissionRequest(BaseModel):
    """A signed submission.

    ``manifest`` is the exact UTF-8 text of ``cpm-manifest.json`` at
    ``revision``: ``manifest_sha256`` is computed over these bytes, so a
    client sends the file as it is, not a re-serialized object.
    """

    model_config = ConfigDict(extra="ignore")

    hotkey: Annotated[str, Field(pattern=_SS58)]
    hf_repo: Annotated[str, Field(pattern=HF_REPO_PATTERN)]
    revision: Annotated[str, Field(pattern=_REVISION)]
    """A full 40-hex commit SHA, never a branch name."""
    manifest: Annotated[str, Field(min_length=2, max_length=MAX_MANIFEST_BYTES)]
    signature: Annotated[str, Field(pattern=r"^(0x)?[0-9a-fA-F]{128}$")]
    ts: Annotated[int, Field(ge=1)]
    nonce: UUID

    @property
    def manifest_sha256(self) -> str:
        return hashlib.sha256(self.manifest.encode("utf-8")).hexdigest()


class ModelContextSubmissionResponse(ModelContextModel):
    submission_id: UUID
    status: SubmissionStatus
    manifest_sha256: str
    commitment_block: int
    base_id: str
    kind: SubmissionKind


class ModelContextUploadTerms(ModelContextModel):
    """What a submission must satisfy right now (read before committing).

    ``blockers`` lists every refusal Platform can decide without the
    manifest: lane closed, banned hotkey, cooldown, a fee path that is not
    available.
    """

    accepting: bool
    competition: Literal["model_context"] = "model_context"
    mode: ModelContextMode
    payment_required: bool
    upload_cooldown_seconds: int
    retry_at: datetime | None = None
    blockers: list[str] = Field(default_factory=list)
    signature_payload: str = (
        f"{SIGNATURE_DOMAIN}:{{hotkey}}:{{hf_repo}}:{{revision}}:"
        "{manifest_sha256}:{ts}:{nonce}"
    )
    commitment_format: str = f"{COMMITMENT_PREFIX}{{manifest_sha256}}"
    manifest_schema: str = MANIFEST_SCHEMA
    manifest_filename: str = MANIFEST_FILENAME
    licenses: list[str] = Field(default_factory=lambda: list(ALLOWED_LICENSES))
    bases: list[ModelContextBase]
    max_lora_rank: int
    max_total_bytes: int
    cpm_score_version: int = CPM_SCORE_VERSION


class ModelContextRunSummary(ModelContextModel):
    run_id: UUID
    season: int
    cpm_score_version: int
    status: RunStatus
    score: float | None
    fault: str | None
    started_at: datetime | None
    finished_at: datetime | None


class ModelContextSubmissionSummary(ModelContextModel):
    """Public view of one submission (weights are public by contract)."""

    submission_id: UUID
    hotkey: str
    hf_repo: str
    revision: str
    manifest_sha256: str
    weights_sha256: str | None
    base_id: str
    kind: SubmissionKind
    commitment_block: int
    status: SubmissionStatus
    reject_reason: str | None
    mirror_repo: str | None
    model_url: str
    """The mirror when Ditto has one, else the miner's repo at the pinned
    revision (contract section 6.4)."""
    created_at: datetime
    latest_run: ModelContextRunSummary | None = None


class ModelContextSubmissionListResponse(ModelContextModel):
    submissions: list[ModelContextSubmissionSummary]


# --- seasons -----------------------------------------------------------------


class ModelContextSeasonView(ModelContextModel):
    season: int
    open_block: int
    open_block_hash: str
    seeds: list[int]
    bench_version: int
    cpm_score_version: int
    base_floors: dict[str, float]
    """Scores of each unmodified base this season, as the scorer reports them."""
    gpu_sku: str | None
    opened_at: datetime
    closes_at: datetime
    closed_at: datetime | None


class ModelContextSeasonListResponse(ModelContextModel):
    current: int | None
    seasons: list[ModelContextSeasonView]


# --- ledger and board (contract section 5.5) ---------------------------------


class ModelContextLedgerEntry(ModelContextModel):
    hotkey: str
    submission_id: UUID
    score: Annotated[float, Field(ge=0.0, le=1.0)]
    eligible: bool
    """Score at least ``floor(base) + floor_margin`` for this season."""
    commitment_block: int
    season: int


class ModelContextLedgerResponse(BaseModel):
    """The model_context ledger validators fold (in shadow, folded to nothing).

    Entries are the current season's scored active submissions, best first:
    score descending, then commitment block ascending, then submission id.
    Empty while ``mode`` is ``off`` or no season is open.
    """

    model_config = ConfigDict(extra="ignore")

    entries: list[ModelContextLedgerEntry] = Field(default_factory=list)
    count: int = 0
    season: int | None = None
    cpm_score_version: int = CPM_SCORE_VERSION
    mode: ModelContextMode = "off"
    rank_shares: list[float] = Field(default_factory=lambda: list(DEFAULT_RANK_SHARES))
    generated_at: datetime | None = None


class ModelContextBoardEntry(ModelContextModel):
    rank: int
    hotkey: str
    submission_id: UUID
    score: float
    eligible: bool
    commitment_block: int
    season: int
    base_id: str
    kind: SubmissionKind
    hf_repo: str
    revision: str
    model_url: str
    components: dict[str, float | int | None] = Field(default_factory=dict)
    floor: float | None = None


class ModelContextBoardResponse(ModelContextModel):
    entries: list[ModelContextBoardEntry]
    count: int
    season: int | None
    mode: ModelContextMode
    floor_margin: float
    base_floors: dict[str, float]
    generated_at: datetime


# --- scorer (contract section 5.4) -------------------------------------------


class ModelContextWorkSubmission(ModelContextModel):
    submission_id: UUID
    hotkey: str
    hf_repo: str
    revision: str
    manifest: dict
    manifest_sha256: str
    weights_sha256: str | None
    base_id: str
    kind: SubmissionKind


class ModelContextWorkSettings(ModelContextModel):
    budget_tokens: int
    tool_k: int
    floor_margin: float


class ModelContextWorkItem(ModelContextModel):
    """One leased run: a submission, or (``kind=reference``) an unmodified
    base whose score becomes the season floor for that base."""

    run_id: UUID
    lease_id: UUID
    lease_expires_at: datetime
    kind: RunKind
    submission: ModelContextWorkSubmission | None
    base: ModelContextBase
    season: int
    seeds: list[int]
    """The season's seeds as uint64 (contract section 4.1)."""
    generate_seeds: list[int]
    """The same seeds as ``cmd/generate -seed`` takes them (signed int64,
    two's complement): pass these to the generator, in order."""
    bench_version: int
    cpm_score_version: int
    gpu_sku: str | None
    """The season's GPU SKU once its first result landed; a result from a
    different SKU is refused (one SKU per season)."""
    settings: ModelContextWorkSettings


class ScoreComponents(BaseModel):
    """The metrics of contract section 4.3, averaged over cases."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    memory_recall: Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]
    distractor_rate: Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]
    tool_recall: Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]
    memory_cases: Annotated[int, Field(ge=0)]
    tool_cases: Annotated[int, Field(ge=0)]
    packed_tokens: Annotated[int | None, Field(ge=0)] = None
    """Total base-tokenizer tokens packed across memory cases."""


class SeedResult(ScoreComponents):
    seed: Annotated[int, Field(ge=-(1 << 63), lt=1 << 64)]
    """The season seed: its uint64 value, or the int64 one it was generated
    with (both name the same seed)."""
    score: Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]


class ModelContextResultRequest(ModelContextModel):
    run_id: UUID
    lease_id: UUID
    cpm_score_version: int
    score: Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]
    components: ScoreComponents
    per_seed: Annotated[list[SeedResult], Field(min_length=1, max_length=8)]
    scorer_id: Annotated[str, Field(min_length=1, max_length=120)]
    scorer_version: Annotated[str, Field(min_length=1, max_length=64)]
    gpu_sku: Annotated[str, Field(min_length=1, max_length=64)]


class ModelContextResultResponse(ModelContextModel):
    run_id: UUID
    status: RunStatus
    score: float
    submission_status: SubmissionStatus | None = None
    base_floor: float | None = None


class ModelContextFaultRequest(ModelContextModel):
    run_id: UUID
    lease_id: UUID
    error_kind: FaultKind
    detail: Annotated[str, Field(max_length=MAX_FAULT_REQUEST_DETAIL_CHARS)] = ""
    """Stored intact; longer details are refused (422), never truncated."""
    scorer_id: Annotated[str, Field(min_length=1, max_length=120)]
    scorer_version: Annotated[str, Field(min_length=1, max_length=64)]


class ModelContextFaultAck(ModelContextModel):
    run_id: UUID
    action: Literal["released", "abandoned", "failed"]
    run_status: RunStatus
    submission_status: SubmissionStatus | None = None


class ModelContextReferenceWork(ModelContextModel):
    """The season's base floors and the reference runs behind them."""

    season: int | None
    bench_version: int | None
    seeds: list[int]
    floor_margin: float
    base_floors: dict[str, float]
    bases: list[ModelContextBase]
    pending: list[str]
    """Allowlisted bases with no floor yet this season."""


class ModelContextScorerHeartbeatView(ModelContextModel):
    scorer_id: str
    scorer_version: str | None
    gpu_sku: str | None
    last_poll: str
    first_seen_at: datetime
    last_seen_at: datetime


class AdminModelContextScorerStatus(ModelContextModel):
    scorers: list[ModelContextScorerHeartbeatView]
    queued_runs: int
    leased_runs: int
    screening_submissions: int
    current_season: int | None
