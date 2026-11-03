"""Wire models for operator per-owner submission cooldown overrides.

An override requires a longer minimum interval between one owner's
submissions than the global coldkey cooldown, keyed by a payment coldkey or a
hotkey, until it expires or is revoked. Revisions are append-only; the newest
revision of an owner identity is its current state.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

OwnerScope = Literal["coldkey", "hotkey"]
OwnerKey = Annotated[
    str,
    Field(min_length=32, max_length=64, pattern=r"^[1-9A-HJ-NP-Za-km-z]{32,64}$"),
]
OwnerCooldownSeconds = Annotated[int, Field(ge=60, le=30 * 86400)]
OwnerCooldownDays = Annotated[int, Field(ge=1, le=90)]


def set_owner_cooldown_confirmation(
    *, scope: str, owner_key: str, cooldown_seconds: int, expires_in_days: int
) -> str:
    return (
        f"SET OWNER SUBMISSION COOLDOWN {scope.upper()} {owner_key} "
        f"{cooldown_seconds} SECONDS FOR {expires_in_days} DAYS"
    )


def revoke_owner_cooldown_confirmation(*, scope: str, owner_key: str) -> str:
    return f"REVOKE OWNER SUBMISSION COOLDOWN {scope.upper()} {owner_key}"


class OwnerCooldownRevision(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    scope: OwnerScope
    owner_key: str
    revision: int
    action: Literal["set", "revoke"]
    cooldown_seconds: int | None
    expires_at: datetime | None
    reason: str
    actor: str
    created_at: datetime
    state: Literal["active", "expired", "revoked"]
    """``active`` while a ``set`` revision has not expired; ``expired`` after
    ``expires_at``; ``revoked`` for a ``revoke`` revision."""


class OwnerCooldownLookup(BaseModel):
    """One owner identity's override and what it means for them right now."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    scope: OwnerScope
    owner_key: str
    current: OwnerCooldownRevision | None
    """The newest revision, or ``null`` when this identity was never
    overridden (``expectedRevision`` 0 for the first set)."""
    global_cooldown_seconds: int
    latest_submission_at: datetime | None
    """Newest submission by this hotkey, or paid by this coldkey."""
    next_allowed_at: datetime | None
    """When this override next lets the owner submit, ``null`` if it does not
    hold them now. The global coldkey cooldown still applies separately."""
    history: list[OwnerCooldownRevision]


class AdminOwnerCooldownsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    active_count: int
    items: list[OwnerCooldownRevision]
    """The current revision of every overridden identity, newest first.
    Expired and revoked identities are included only with
    ``include_inactive``."""
    truncated: bool = False
    """More identities matched than ``limit``; raise it to see them."""
    lookup: OwnerCooldownLookup | None = None
    """Present when ``scope`` and ``owner_key`` name one identity."""


class AdminSetOwnerCooldownRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, str_strip_whitespace=True)

    scope: OwnerScope
    owner_key: OwnerKey
    cooldown_seconds: OwnerCooldownSeconds
    expires_in_days: OwnerCooldownDays
    expected_revision: Annotated[int, Field(ge=0)]
    """The identity's current revision (0 when it has none): compare-and-set."""
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str


class AdminRevokeOwnerCooldownRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, str_strip_whitespace=True)

    scope: OwnerScope
    owner_key: OwnerKey
    expected_revision: Annotated[int, Field(ge=1)]
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str
