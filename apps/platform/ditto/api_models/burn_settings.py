"""Hot-swappable operator policy for the subnet-owner emission burn.

The validator's weight fold reserves ``burn_share`` of the miner emission for
Subtensor's owner-burn path and releases the rest through KOTH
(``ditto-subnet`` ``ditto/validator/weights.py:apply_miner_emission_cap``).
That share used to be the frozen constant ``MINER_EMISSION_SHARE = 1.0``
compiled into every validator, so moving it meant shipping a validator release
and waiting for the fleet to take it. It is now platform-owned and served on
the scoring ledger, which is the same read every validator already makes before
every weight submission.

Two properties make this safe to hand to an operator:

* It grants the platform no authority it did not already have. The platform
  already decides the entire eligible pool; serving an empty ledger routes 100%
  of miner emission to burn today. A burn share is the same power expressed
  continuously instead of as a cliff.
* It is served as an already-resolved scalar rather than a schedule. Every
  validator polling the ledger reads the identical number, so the fleet does not
  have to agree about clocks -- only about the ledger, which it must agree about
  anyway.

What it does **not** remove is the epoch. A validator that submitted weights
just before a change keeps the old vector until its next epoch (an hour by
default, unsynchronized across the fleet), so a burn change is fully applied
across the subnet only after one epoch has passed everywhere. Yuma consensus
tolerates that the same way it tolerates a score landing mid-epoch, but it means
a burn change is not an instantaneous switch and must not be tuned as if it
were.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MIN_BURN_SHARE = 0.0
MAX_BURN_SHARE = 1.0
DEFAULT_BURN_SHARE = 0.0
"""No burn: the full miner emission is released through KOTH.

This mirrors the validator's frozen ``MINER_EMISSION_SHARE = 1.0``, so a
platform with no revision stored serves exactly what an unconfigured fleet
already does and adding this surface changes no weights on deploy.
"""

RANK_SHARES_SLOTS = 5
"""Champion plus the four-runner tail the KOTH fold pays."""

RANK_SHARES_PROTOCOL = 31
"""Validator heartbeat protocol that folds a served ``rank_shares`` curve."""


def curve_increases(shares: tuple[float, ...] | None) -> bool:
    """True when a lower slot would be paid more than the one above it."""
    return shares is not None and any(
        later > earlier for earlier, later in zip(shares, shares[1:], strict=False)
    )


class BurnSettings(BaseModel):
    """Complete subnet-global emission-burn policy stored per revision."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    burn_share: Annotated[float, Field(ge=MIN_BURN_SHARE, le=MAX_BURN_SHARE)] = (
        DEFAULT_BURN_SHARE
    )
    """Fraction of miner emission routed to the subnet owner's burn hotkey.

    ``0.0`` (the default) releases everything through KOTH. ``1.0`` burns all of
    it, which is the same vector the fold already submits when no agent has a
    positive score -- it is a full emission stop, not a chain-zeroing one, so it
    is a legitimate (if drastic) operator setting rather than a footgun to
    forbid.

    The remainder, ``1 - burn_share``, is normalized across the eligible miner
    weights *before* the burn residual is added, so the champion's share of the
    miner half is unchanged by this dial: it scales the whole miner vector
    rather than re-ranking it.
    """

    rank_shares: Annotated[
        # The outer tuple accepts a JSON array; each share must be a number,
        # never a numeric string.
        tuple[Annotated[float, Field(strict=True)], ...] | None,
        Field(default=None, strict=False, exclude_if=lambda value: value is None),
    ] = None
    """Operator payout curve for the champion then each tail slot.

    ``None`` (the default) leaves every validator on its frozen 65/14/10/7/4
    curve. A curve is served on the scoring ledger only once every
    recently-live weight setter reports protocol 31, so a mixed fleet never
    folds two curves. It must have exactly five positive shares summing to one
    within the validator fold's own 1e-12 tolerance, or validators would refuse
    it. A write must also be non-increasing from the champion down; reads stay
    permissive so a stored revision can never fail into the default policy.
    Omitting it on a write keeps the current curve; an explicit ``null``
    clears it.
    """

    @field_validator("rank_shares")
    @classmethod
    def _valid_curve(cls, value: tuple[float, ...] | None) -> tuple[float, ...] | None:
        if value is None:
            return None
        shares = tuple(float(share) for share in value)
        if len(shares) != RANK_SHARES_SLOTS:
            raise ValueError(
                f"rank_shares must have exactly {RANK_SHARES_SLOTS} entries"
            )
        if any(not math.isfinite(share) or share <= 0.0 for share in shares):
            raise ValueError("rank_shares must contain only finite positive values")
        if not math.isclose(sum(shares), 1.0, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError("rank_shares must sum to 1")
        return shares


class BurnSettingsRevision(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: int
    parent_revision: int
    scope: str
    settings: BurnSettings
    reason: str
    actor: str
    created_at: datetime
    checksum: str


class EffectiveBurnSettings(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: int
    scope: str
    settings: BurnSettings
    checksum: str
    source: Literal["revision", "default"]
    max_age_seconds: float
    """Resolver TTL: the longest a validator's next ledger read can still be
    answered from the previous revision."""

    miner_emission_share: float
    """``1 - burn_share``, the share the fold releases through KOTH. Derived
    rather than stored so the two can never disagree, and reported because it is
    the number the weight fold actually takes."""

    min_burn_share: float = MIN_BURN_SHARE
    max_burn_share: float = MAX_BURN_SHARE
    """Bounds served to the console so it cannot offer a share the platform
    would refuse."""

    rank_shares_served: bool | None = None
    """Whether the configured curve is on the scoring ledger now: True only
    when a curve is set and every recently-live weight setter reports protocol
    31. ``None`` when fleet readiness could not be read."""

    live_validator_count: int | None = None
    """Validators that have heartbeated recently enough to be folding weights.

    A burn change reaches the chain only through validators that are actually
    submitting, so an operator staring at a page that says ``0`` should know the
    dial they just moved is not attached to anything. ``None`` when the count
    could not be read."""


class AdminBurnSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    scope: str = "*"
    expected_revision: Annotated[int, Field(ge=0)]
    settings: BurnSettings
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str

    @model_validator(mode="after")
    def _curve_does_not_increase(self) -> AdminBurnSettingsRequest:
        # Write-only: a stored revision that predates this rule must still
        # read, or settings_from_row would fall back to the full default.
        if curve_increases(self.settings.rank_shares):
            raise ValueError("rank_shares must not increase from the champion down")
        return self


class AdminBurnSettingsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    current: list[BurnSettingsRevision]
    history: list[BurnSettingsRevision]
    default: BurnSettings
    effective: EffectiveBurnSettings
