"""Audited operator settings for miner submission admission and pricing.

The submission fee is an explicit, revisioned pricing policy. Its only reviewed
denomination today is ``fixed_tao``: the operator names an exact amount of rao,
and that exact amount is what a miner is quoted and what payment verification
requires. A USD figure is never authoritative for admission; TAO/USD is
recorded only as revenue-reporting metadata. A future denomination must be
added deliberately to the literal below, the database CHECK, and every quote
producer (Python and the Go upload relay), which refuse unknown values.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

RAO_PER_TAO = 1_000_000_000

SubmissionFeeDenomination = Literal["fixed_tao"]
SUBMISSION_FEE_DENOMINATION_FIXED_TAO: SubmissionFeeDenomination = "fixed_tao"

# Operator-safe bounds for a *new* fee. The database keeps the wider historical
# CHECK (1 rao .. 1,000 TAO) so older revisions stay readable; these bounds stop
# a unit slip (rao typed as TAO or the reverse) from reaching miners.
MIN_SUBMISSION_FEE_RAO = 1_000_000  # 0.001 TAO
MAX_SUBMISSION_FEE_RAO = 10_000_000_000  # 10 TAO
MIN_SUBMISSION_COOLDOWN_SECONDS = 60
MAX_SUBMISSION_COOLDOWN_SECONDS = 86400


def format_rao_as_tao(amount_rao: int) -> str:
    """Render rao as an exact nine-decimal TAO string without floating point."""
    sign = "-" if amount_rao < 0 else ""
    whole, fraction = divmod(abs(amount_rao), RAO_PER_TAO)
    return f"{sign}{whole}.{fraction:09d}"


def submission_settings_confirmation(cooldown_seconds: int, fee_amount_rao: int) -> str:
    return (
        f"SET SUBMISSION COOLDOWN {cooldown_seconds} SECONDS FEE {fee_amount_rao} RAO"
    )


class SubmissionSettingsRevision(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: int
    parent_revision: int
    cooldown_seconds: int
    fee_amount_rao: int
    reason: str
    actor: str
    created_at: datetime | None
    """When this revision took effect. Revisions activate on commit."""
    fee_denomination: SubmissionFeeDenomination = SUBMISSION_FEE_DENOMINATION_FIXED_TAO
    fee_amount_tao: str | None = None
    """Exact nine-decimal rendering of ``fee_amount_rao``."""
    previous_fee_amount_rao: int | None = None
    """Fee of ``parent_revision``; ``None`` for the first revision."""
    previous_cooldown_seconds: int | None = None
    """Cooldown of ``parent_revision``; ``None`` for the first revision."""


class SubmissionFeeBounds(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    min_fee_amount_rao: int = MIN_SUBMISSION_FEE_RAO
    max_fee_amount_rao: int = MAX_SUBMISSION_FEE_RAO
    min_cooldown_seconds: int = MIN_SUBMISSION_COOLDOWN_SECONDS
    max_cooldown_seconds: int = MAX_SUBMISSION_COOLDOWN_SECONDS


class AdminSubmissionSettingsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    current: SubmissionSettingsRevision
    history: list[SubmissionSettingsRevision]
    bounds: SubmissionFeeBounds = SubmissionFeeBounds()
    quote_lifetime_seconds: int | None = None
    """How long a reserved quote stays payable after it is issued."""


class AdminSubmissionSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, str_strip_whitespace=True)

    expected_revision: Annotated[int, Field(ge=0)]
    cooldown_seconds: Annotated[
        int,
        Field(ge=MIN_SUBMISSION_COOLDOWN_SECONDS, le=MAX_SUBMISSION_COOLDOWN_SECONDS),
    ]
    fee_amount_rao: (
        Annotated[int, Field(ge=MIN_SUBMISSION_FEE_RAO, le=MAX_SUBMISSION_FEE_RAO)]
        | None
    ) = None
    fee_denomination: SubmissionFeeDenomination = SUBMISSION_FEE_DENOMINATION_FIXED_TAO
    """Explicit denomination; any value other than ``fixed_tao`` is rejected."""
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str


class SubmissionSettingsProposal(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    cooldown_seconds: int
    fee_amount_rao: int
    fee_amount_tao: str
    fee_denomination: SubmissionFeeDenomination


class AdminSubmissionSettingsPreview(BaseModel):
    """Read-only dry run of one proposed revision. Previewing changes nothing."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    current: SubmissionSettingsRevision
    proposed: SubmissionSettingsProposal
    expected_revision: int
    stale: bool
    """``expected_revision`` is not the current revision; an apply returns 409."""
    fee_changed: bool
    cooldown_changed: bool
    fee_change_ratio: str | None
    """Proposed fee divided by the current fee, four decimals; ``None`` if equal."""
    applicable: bool
    """Not stale and changes something; an apply with the confirmation succeeds."""
    required_confirmation: str
    bounds: SubmissionFeeBounds
    quote_lifetime_seconds: int
    in_flight_quotes: int
    """Unexpired reserved quotes; each keeps the fee it was issued at."""
    in_flight_quotes_at_other_fees: int
    """Of those, how many were issued at a fee other than the proposed one."""
    in_flight_quotes_expire_by: datetime | None
    """Latest in-flight expiry; after it only the new fee can be paid."""


class PublicSubmissionFeeRevision(BaseModel):
    """One source-safe fee revision. Operator identity and reasons stay private."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: int
    fee_denomination: SubmissionFeeDenomination
    fee_amount_rao: int
    fee_amount_tao: str
    previous_fee_amount_rao: int | None
    previous_fee_amount_tao: str | None
    effective_at: datetime | None


class PublicSubmissionFee(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    policy_revision: int
    """Latest submission-settings revision; a cooldown-only change also bumps it."""
    fee_denomination: SubmissionFeeDenomination
    fee_amount_rao: int
    fee_amount_tao: str
    fee_revision: int
    """Revision in which the current fee amount took effect."""
    fee_effective_at: datetime | None
    quote_lifetime_seconds: int
    """A quote reserved before payment stays payable for this long."""
    history: list[PublicSubmissionFeeRevision]
    """Fee changes, newest first. Cooldown-only revisions are omitted."""
    history_truncated: bool = False
