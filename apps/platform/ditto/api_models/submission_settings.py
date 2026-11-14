"""Audited operator settings for miner submission admission.

One append-only revision holds the coldkey cooldown, the default submission
fee (``fee_amount_rao``) and per-competition fee overrides
(``competition_fees``). The default fee is what the memory (agent) lane
charges; every other competition inherits it unless its override sets its own
amount. An override's ``fee_required``, when set, decides whether that
competition charges at all; unset, the competition's own policy decides (the
generator competition's ``upload_fee_required``). The memory lane always
charges, so it has no override: change ``fee_amount_rao`` instead.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MIN_SUBMISSION_FEE_RAO = 1
MAX_SUBMISSION_FEE_RAO = 1_000_000_000_000

MEMORY_COMPETITION = "memory"
"""Priced by ``fee_amount_rao`` itself, the default every other competition
inherits."""
OVERRIDABLE_COMPETITIONS = ("generator", "coding", "router")
"""Competition ids (the track ids of track share settings) that may carry a
fee override. Only ``generator`` has an upload lane today; an override for a
track without one is stored and applies when its lane resolves it."""
UPLOAD_COMPETITIONS = (MEMORY_COMPETITION, "generator")
"""Competitions with a miner upload lane, reported in the effective view."""

FeeAmountRao = Annotated[
    int, Field(ge=MIN_SUBMISSION_FEE_RAO, le=MAX_SUBMISSION_FEE_RAO)
]


class CompetitionFeePolicy(BaseModel):
    """One competition's override. ``null`` inherits.

    Unknown keys are ignored like every wire model (additive JSON across
    rolling upgrades). A misspelled field therefore reads as "inherit", and
    the confirmation phrase, which echoes only the recognized fields, is
    what catches it before anything is stored.
    """

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    fee_amount_rao: FeeAmountRao | None = None
    """This competition's own price in rao; ``null`` charges the default
    ``fee_amount_rao``."""
    fee_required: bool | None = None
    """Whether this competition charges at all; ``null`` leaves it to the
    competition's own policy (generator: ``upload_fee_required``)."""


def _validate_competition_fees(
    value: dict[str, CompetitionFeePolicy],
) -> dict[str, CompetitionFeePolicy]:
    if MEMORY_COMPETITION in value:
        raise ValueError(
            "memory is priced by fee_amount_rao (the default every other "
            "competition inherits); set that instead of a memory override"
        )
    unknown = sorted(set(value) - set(OVERRIDABLE_COMPETITIONS))
    if unknown:
        raise ValueError(
            f"unknown competition ids {unknown}; known: "
            f"{list(OVERRIDABLE_COMPETITIONS)}"
        )
    # An entry that inherits both fields is no override at all: drop it so
    # the stored map and the confirmation phrase have one canonical form.
    return {
        competition: value[competition]
        for competition in OVERRIDABLE_COMPETITIONS
        if competition in value
        and (
            value[competition].fee_amount_rao is not None
            or value[competition].fee_required is not None
        )
    }


class EffectiveCompetitionFee(BaseModel):
    """What one competition's upload lane charges right now."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    competition: str
    fee_amount_rao: int
    amount_source: Literal["default", "override"]
    fee_required: bool
    required_source: Literal["always", "competition_settings", "override"]
    """``always`` for the memory lane, which cannot admit an unpaid upload;
    ``competition_settings`` when the competition's own policy decides (the
    generator ``upload_fee_required``); ``override`` when this revision's
    ``competition_fees`` entry decides."""


class SubmissionSettingsRevision(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: int
    parent_revision: int
    cooldown_seconds: int
    fee_amount_rao: int
    competition_fees: dict[str, CompetitionFeePolicy] = Field(default_factory=dict)
    """Overrides as written; a revision from before per-competition fees
    reads as ``{}`` (everything inherits)."""
    readmission_announced_at: datetime | None = None
    """When the readmission fee was announced (SN-485); ``null`` keeps
    readmission inactive. Only the Platform admin API writes it."""
    reason: str
    actor: str
    created_at: datetime | None


ReadmissionInactiveReason = Literal[
    "announcement_unset", "bench_version_below_minimum", "notice_period"
]


class ReadmissionActivationState(BaseModel):
    """Whether the readmission fee and self-serve readmission are live.

    Active only when ``readmission_announced_at`` is set, the active
    benchmark is at least ``min_bench_version`` and ``effective_at``
    (announcement plus the notice period) has passed. Read-only: the price
    is always the upload default fee, never a setting of its own.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    active: bool
    inactive_reasons: list[ReadmissionInactiveReason] = Field(default_factory=list)
    """Every unmet condition; empty when active. ``announcement_unset``: no
    announcement is recorded. ``bench_version_below_minimum``: the active
    benchmark predates the first charged era. ``notice_period``: the
    announcement is less than ``notice_period_seconds`` old."""
    announced_at: datetime | None = None
    effective_at: datetime | None = None
    """The earliest moment a readmission can be charged."""
    active_bench_version: int
    min_bench_version: int
    notice_period_seconds: int
    fee_amount_rao: int
    """What one readmission costs once active: exactly the upload fee."""
    fee_source: Literal["upload_default"] = "upload_default"
    settings_revision: int


class AdminSubmissionSettingsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    current: SubmissionSettingsRevision
    history: list[SubmissionSettingsRevision]
    effective_competition_fees: list[EffectiveCompetitionFee] = Field(
        default_factory=list
    )
    """The resolved fee of every competition with an upload lane, under the
    current revision and the generator competition's current policy."""
    readmission: ReadmissionActivationState | None = None
    """The readmission activation state under the current revision and the
    live active benchmark."""


class AdminSubmissionSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, str_strip_whitespace=True)

    expected_revision: Annotated[int, Field(ge=0)]
    cooldown_seconds: Annotated[int, Field(ge=60, le=86400)]
    fee_amount_rao: FeeAmountRao | None = None
    competition_fees: dict[str, CompetitionFeePolicy] | None = None
    """The WHOLE override map for this revision, or ``null`` to carry the
    current one forward unchanged. ``{}`` clears every override. Sending it
    adds the overrides to the confirmation phrase."""
    readmission_announced_at: Annotated[datetime | None, Field(strict=False)] = None
    """Omitted: carry the current announcement forward unchanged. A
    timezone-aware time: record that announcement (at most an hour in the
    past, at most 30 days ahead). Explicit ``null``: clear it, which turns
    readmission off. Sending the field adds it to the confirmation phrase
    (:func:`readmission_announcement_confirmation`)."""
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str

    @field_validator("competition_fees")
    @classmethod
    def _known_competitions(
        cls, value: dict[str, CompetitionFeePolicy] | None
    ) -> dict[str, CompetitionFeePolicy] | None:
        return None if value is None else _validate_competition_fees(value)


def readmission_announcement_confirmation(announced_at: datetime | None) -> str:
    """The suffix an explicit ``readmission_announced_at`` adds to the phrase.

    ``; READMISSION ANNOUNCED 2026-11-20T00:00:00Z`` (UTC, whole seconds) or
    ``; READMISSION ANNOUNCEMENT CLEARED`` for ``null``.
    """
    if announced_at is None:
        return "; READMISSION ANNOUNCEMENT CLEARED"
    stamp = announced_at.astimezone(UTC).replace(microsecond=0, tzinfo=None).isoformat()
    return f"; READMISSION ANNOUNCED {stamp}Z"


def competition_fees_confirmation(
    competition_fees: dict[str, CompetitionFeePolicy],
) -> str:
    """The suffix an explicit override map adds to the confirmation phrase.

    ``; GENERATOR FEE 500000000 RAO REQUIRED YES`` per overridden competition
    (``FEE INHERIT`` / ``REQUIRED INHERIT`` for an inherited field, ``REQUIRED
    NO`` for a free competition), or ``; ALL COMPETITIONS INHERIT`` for ``{}``.
    """
    parts = []
    for competition in OVERRIDABLE_COMPETITIONS:
        policy = competition_fees.get(competition)
        if policy is None:
            continue
        amount = (
            "INHERIT"
            if policy.fee_amount_rao is None
            else f"{policy.fee_amount_rao} RAO"
        )
        required = (
            "INHERIT"
            if policy.fee_required is None
            else ("YES" if policy.fee_required else "NO")
        )
        parts.append(f"{competition.upper()} FEE {amount} REQUIRED {required}")
    return "; " + ("; ".join(parts) if parts else "ALL COMPETITIONS INHERIT")
