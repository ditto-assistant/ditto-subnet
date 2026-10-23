"""Wire models for the shadow DittoBench generator competition.

Generator miners upload **challenge packs**: inert JSON holding a memory
haystack plus cases with deterministic gold answers. The contract, validation
rules, and scoring formula live in
``research/dittobench-datagen/challenge`` (see
``research/dittobench-datagen/docs/generator-competition.md``); Platform stores
packs privately, leases them to a trusted scorer, scores the panel results with
:mod:`ditto.generator_scoring`, and publishes a weight-zero ledger.

Everything here is shadow: ledger entries are ``weight_eligible=False`` and the
settings ``mode`` has no enforcing value yet.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

GENERATOR_SETTINGS_SCOPE = "*"
PACK_CONTRACT_V1 = "dittobench-challenge-pack/v1"
MAX_PACK_BYTES = 4 << 20
MIN_PACK_CASES = 8
MAX_PACK_CASES = 64
PACK_SIGNATURE_DOMAIN = "ditto-generator-pack-v1"

GeneratorMode = Literal["off", "shadow"]
PackStatus = Literal["queued", "leased", "scored", "invalid", "failed"]
CaseStatus = Literal["discriminating", "trivial", "unsolved", "duplicate"]

_SHA256 = r"^[0-9a-f]{64}$"
_SS58 = r"^[1-9A-HJ-NP-Za-km-z]{47,48}$"


def generator_settings_confirmation(mode: str) -> str:
    """The phrase a settings write must carry, naming the mode it applies."""
    return f"APPLY GENERATOR COMPETITION {mode.upper()}"


def pack_signature_payload(
    *, hotkey: str, pack_sha256: str, timestamp: int, nonce: UUID
) -> bytes:
    """The domain-separated bytes a miner signs to upload a pack.

    Distinct from ``ditto-upload-v2`` so an agent-upload signature can never be
    replayed as a pack upload or vice versa.
    """
    return f"{PACK_SIGNATURE_DOMAIN}:{hotkey}:{pack_sha256}:{timestamp}:{nonce}".encode(
        "ascii"
    )


class GeneratorModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class GeneratorCompetitionSettings(BaseModel):
    """The complete generator competition policy (one revision = whole object).

    Scoring fields mirror ``challenge.ScoreParams`` in the Go contract and are
    bounded identically.
    """

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    mode: GeneratorMode = "off"
    """``off`` refuses uploads and leases nothing; ``shadow`` runs the lane with
    zero emission."""
    panel_size: Annotated[int, Field(ge=3, le=16)] = 5
    """Top-ranked owners (one agent each) a pack runs against."""
    min_panel: Annotated[int, Field(ge=2, le=32)] = 3
    min_solver_owners: Annotated[int, Field(ge=1, le=32)] = 2
    solve_threshold: Annotated[float, Field(gt=0.0, le=1.0)] = 0.5
    champion_bonus: Annotated[float, Field(ge=0.0, le=2.0)] = 0.5
    target_cases: Annotated[int, Field(ge=MIN_PACK_CASES, le=MAX_PACK_CASES)] = 32
    window_days: Annotated[int, Field(ge=1, le=30)] = 7
    """A hotkey's ledger score is its best pack scored within this window."""
    max_pending_per_hotkey: Annotated[int, Field(ge=1, le=10)] = 2
    upload_cooldown_seconds: Annotated[int, Field(ge=60, le=86_400)] = 3_600
    lease_ttl_seconds: Annotated[int, Field(ge=600, le=21_600)] = 7_200
    max_attempts: Annotated[int, Field(ge=1, le=20)] = 6
    """Leases before a pack that never reaches a full panel is ``failed``."""
    retry_backoff_seconds: Annotated[int, Field(ge=60, le=86_400)] = 3_600

    @model_validator(mode="after")
    def _coherent(self) -> GeneratorCompetitionSettings:
        if self.min_solver_owners > self.min_panel:
            raise ValueError("min_solver_owners must not exceed min_panel")
        if self.min_panel > self.panel_size:
            raise ValueError("min_panel must not exceed panel_size")
        return self

    def score_params(self) -> dict[str, float | int]:
        """The ``challenge.ScoreParams`` view of this policy."""
        return {
            "min_panel": self.min_panel,
            "min_solver_owners": self.min_solver_owners,
            "solve_threshold": self.solve_threshold,
            "champion_bonus": self.champion_bonus,
            "target_cases": self.target_cases,
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


class GeneratorPackUploadRequest(BaseModel):
    """A signed pack upload. ``pack_base64`` carries the exact pack bytes."""

    model_config = ConfigDict(extra="ignore")

    hotkey: Annotated[str, Field(pattern=_SS58)]
    pack_sha256: Annotated[str, Field(pattern=_SHA256)]
    pack_base64: Annotated[
        str, Field(min_length=4, max_length=(MAX_PACK_BYTES + 2) // 3 * 4)
    ]
    signature: Annotated[str, Field(pattern=r"^(0x)?[0-9a-fA-F]{128}$")]
    signature_timestamp: Annotated[int, Field(ge=1)]
    signature_nonce: UUID


class GeneratorPackUploadResponse(GeneratorModel):
    pack_id: UUID
    pack_sha256: str
    status: PackStatus
    case_count: int


class GeneratorCaseStatusCounts(GeneratorModel):
    discriminating: int = 0
    trivial: int = 0
    unsolved: int = 0
    duplicate: int = 0


class GeneratorPackSummary(GeneratorModel):
    """Public-safe view of one pack: identity, lifecycle, aggregate outcome.

    Never includes case content, case ids, or panel agent identities.
    """

    pack_id: UUID
    miner_hotkey: str
    pack_sha256: str
    generator_name: str
    generator_version: str
    case_count: int
    status: PackStatus
    last_outcome: str | None
    score: float | None
    case_counts: GeneratorCaseStatusCounts | None
    issue_count: int | None
    submitted_at: datetime
    scored_at: datetime | None


class GeneratorPackListResponse(GeneratorModel):
    packs: list[GeneratorPackSummary]


# --- scorer ----------------------------------------------------------------


class GeneratorPanelSlot(GeneratorModel):
    agent_id: str
    owner_key: str
    champion: bool = False


class GeneratorWorkItem(GeneratorModel):
    lease_id: UUID
    pack_id: UUID
    pack_sha256: str
    pack_base64: str
    panel: list[GeneratorPanelSlot]
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

    valid: bool
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


class GeneratorResultRequest(GeneratorModel):
    lease_id: UUID
    pack_id: UUID
    pack_sha256: Annotated[str, Field(pattern=_SHA256)]
    scorer_version: Annotated[str, Field(min_length=1, max_length=64)]
    validation: ValidationReport
    panel: list[PanelMemberResult] | None = None


class GeneratorResultResponse(GeneratorModel):
    pack_id: UUID
    status: PackStatus
    outcome: str
    score: float | None = None


# --- admin -------------------------------------------------------------


class GeneratorCaseVerdictView(GeneratorModel):
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


class AdminGeneratorPackDetail(GeneratorModel):
    """Operator view: everything about one pack except its raw bytes."""

    summary: GeneratorPackSummary
    owner_key: str
    attempts: int
    not_before: datetime
    lease_expires_at: datetime | None
    settings_revision: int | None
    scorer_version: str | None
    panel: list[GeneratorPanelSlot] | None
    validation: ValidationReport | None
    score_summary: dict | None
    verdicts: list[GeneratorCaseVerdictView]


class AdminGeneratorPackListResponse(GeneratorModel):
    packs: list[GeneratorPackSummary]
    counts_by_status: dict[str, int]


class GeneratorPromotionCandidate(GeneratorModel):
    pack_id: UUID
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
