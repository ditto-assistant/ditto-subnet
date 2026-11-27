"""Operator policy for the clean-board partial burn (ditto-subnet #2911).

Pay for reviewed work only. A paid slot whose agent has not passed operator
board review is paid ``1 - unattested_slot_burn_share`` of its normal share
once its grace window has run out, and the unpaid part is burned. Ranking is
untouched: the crown, the dethrone band, the tail order, tie pooling and the
score-ceiling cohort are folded exactly as before, and only the payout of an
unattested recipient shrinks.

The fraction is served already-resolved on the scoring ledger
(``LedgerResponse.unpaid_fractions``), never derived by validators, because
every validator must fold the identical vector.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

CLEAN_BOARD_PROTOCOL = 36
"""Validator heartbeat protocol whose fold reads ``unpaid_fractions``."""

MIN_UNATTESTED_SLOT_BURN_SHARE = 0.0
MAX_UNATTESTED_SLOT_BURN_SHARE = 1.0
DEFAULT_UNATTESTED_SLOT_BURN_SHARE = 0.5
MIN_ATTESTATION_GRACE_SECONDS = 0
MAX_ATTESTATION_GRACE_SECONDS = 604_800
DEFAULT_ATTESTATION_GRACE_SECONDS = 86_400
"""One day of full pay after an agent finalizes, so operators can review it."""


class CleanBoardSettings(BaseModel):
    """Complete subnet-global clean-board policy stored per revision."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    enabled: bool = False
    """Ships disabled. While disabled the fractions are still computed, logged
    and projected on the public board as shadow, but the ledger serves an empty
    map, so the fold is byte-identical to the pre-#2911 one."""

    unattested_slot_burn_share: Annotated[
        float,
        Field(ge=MIN_UNATTESTED_SLOT_BURN_SHARE, le=MAX_UNATTESTED_SLOT_BURN_SHARE),
    ] = DEFAULT_UNATTESTED_SLOT_BURN_SHARE
    """Fraction of an unattested recipient's slot that is burned after grace."""

    attestation_grace_seconds: Annotated[
        int,
        Field(ge=MIN_ATTESTATION_GRACE_SECONDS, le=MAX_ATTESTATION_GRACE_SECONDS),
    ] = DEFAULT_ATTESTATION_GRACE_SECONDS
    """Full pay after the agent first reaches its score quorum."""


class CleanBoardSettingsRevision(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: int
    parent_revision: int
    scope: str
    settings: CleanBoardSettings
    reason: str
    actor: str
    created_at: datetime
    checksum: str


class EffectiveCleanBoardSettings(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: int
    scope: str
    settings: CleanBoardSettings
    checksum: str
    source: Literal["revision", "default"]
    max_age_seconds: float
    """Resolver TTL: the longest a ledger read can still answer from the
    previous revision."""
    required_protocol: int = CLEAN_BOARD_PROTOCOL
    served: bool | None = None
    """Whether the ledger serves the fractions now: ``True`` only when the
    policy is enabled and every recently-live weight setter reports
    :data:`CLEAN_BOARD_PROTOCOL`. ``None`` when fleet readiness could not be
    read."""


class AdminCleanBoardSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    scope: str = "*"
    expected_revision: Annotated[int, Field(ge=0)]
    settings: CleanBoardSettings
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str


class AdminCleanBoardSettingsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    current: list[CleanBoardSettingsRevision]
    history: list[CleanBoardSettingsRevision]
    default: CleanBoardSettings
    effective: EffectiveCleanBoardSettings
