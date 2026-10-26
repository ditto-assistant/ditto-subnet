"""Operator governance for the per-track emission split.

The subnet runs several competitions (memory agents, the DittoBench generator
competition, with coding and router reserved), but the chain takes one weight
vector per validator. Validators fold each track separately and blend the
results by a basis-point share (``ditto/validator/weights.py``,
``blend_track_weights``). Their compiled split gives memory the whole pool.

This setting is the platform-owned override, served on the scoring ledger as
``LedgerResponse.track_shares_bps`` exactly like ``burn_share``: one resolved
map every validator folds, not a schedule. It is unset by default, so the
ledger omits the field and every validator keeps its compiled split; adding the
surface changes no weight on deploy.

Safety properties a write relies on:

* Only tracks a shipped validator pays may hold a nonzero share. Today that is
  ``memory`` and ``generator`` (validator protocol 33). A share for ``coding``
  or ``router`` would be unpaid by every validator and burn, so it is refused.
* The map must name ``memory``. A validator reads a missing ``memory`` as the
  full pool, so ``{generator: 1000}`` alone would exceed the cap and every
  validator would silently fall back to the compiled split.
* The eligible sum must not exceed ``10000``. Basis points short of ``10000``
  are unallocated and burn, which the read model reports as ``unallocated_bps``.
* The map is served only once every recently-live weight setter reports
  protocol 33. A protocol-32 validator folds the generator track in shadow and
  would burn the generator share, splitting the fleet.

A generator share pays only ledger entries Platform marks ``weight_eligible``
(generator settings ``mode=enforce``). With no eligible generator a protocol-33
validator hands the generator share back to memory, so serving the share before
enforcing the ledger is byte-identical to not serving it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_Bps = Annotated[int, Field(ge=0, le=10_000)]

BASIS_POINT_SCALE = 10_000
TRACK_SHARES_PROTOCOL = 33
"""Validator heartbeat protocol that pays the generator track a served share."""

KNOWN_TRACKS = ("memory", "coding", "router", "generator")
"""Track ids the validator registry knows, in its registration order."""

PAYABLE_TRACKS = frozenset({"memory", "generator"})
"""Tracks a shipped validator can pay. Others may only appear at ``0``."""

CLEAR_CONFIRMATION = "CLEAR TRACK SHARES"


def track_shares_confirmation(track_shares_bps: dict[str, int] | None) -> str:
    """The exact phrase a write must carry; it spells out the split it applies.

    Naming every nonzero share in the phrase means an operator cannot apply a
    split they did not type, e.g. ``APPLY TRACK SHARES memory=9000
    generator=1000``. Clearing the override is ``CLEAR TRACK SHARES``.
    """
    if not track_shares_bps:
        return CLEAR_CONFIRMATION
    parts = [
        f"{track}={track_shares_bps[track]}"
        for track in KNOWN_TRACKS
        if track_shares_bps.get(track, 0) > 0
    ]
    return "APPLY TRACK SHARES " + " ".join(parts)


class TrackShareSettings(BaseModel):
    """Complete track-share policy stored per revision."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    track_shares_bps: dict[str, _Bps] | None = None
    """``{track_id: bps}`` served to validators, or ``None`` (the default) to
    omit the field and leave every validator on its compiled split."""

    @field_validator("track_shares_bps")
    @classmethod
    def _coherent(cls, value: dict[str, int] | None) -> dict[str, int] | None:
        if value is None:
            return None
        if not value:
            raise ValueError(
                "track_shares_bps must be null (unset) or name at least memory"
            )
        unknown = sorted(set(value) - set(KNOWN_TRACKS))
        if unknown:
            raise ValueError(
                f"unknown track ids {unknown}; known: {list(KNOWN_TRACKS)}"
            )
        if "memory" not in value:
            raise ValueError(
                "track_shares_bps must name memory: validators read a missing "
                "memory share as the full pool"
            )
        unpaid = sorted(
            track
            for track, bps in value.items()
            if bps > 0 and track not in PAYABLE_TRACKS
        )
        if unpaid:
            raise ValueError(
                f"no shipped validator pays {unpaid}; their share would burn. "
                f"Only {sorted(PAYABLE_TRACKS)} may hold a nonzero share"
            )
        total = sum(value.values())
        if total > BASIS_POINT_SCALE:
            raise ValueError(
                f"track shares sum to {total} bps, above {BASIS_POINT_SCALE}"
            )
        return {track: value[track] for track in KNOWN_TRACKS if track in value}


class TrackShareSettingsRevision(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: int
    parent_revision: int
    scope: str
    settings: TrackShareSettings
    reason: str
    actor: str
    created_at: datetime
    checksum: str


class EffectiveTrackShareSettings(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: int
    scope: str
    settings: TrackShareSettings
    checksum: str
    source: Literal["revision", "default"]
    max_age_seconds: float
    """Resolver TTL: the longest a ledger read can still use the previous
    revision. An epoch pin freezes whatever was in force when it was taken."""

    in_force_bps: dict[str, int]
    """The split validators fold now: the served map, or the compiled
    memory-only split while nothing is served (unset, or the fleet gate)."""

    unallocated_bps: int
    """Basis points of the served map no track claims; validators burn them."""

    served: bool | None = None
    """Whether the configured map is on the scoring ledger now: True only when a
    map is set and every recently-live weight setter reports protocol 33.
    ``None`` when fleet readiness could not be read."""

    required_protocol: int = TRACK_SHARES_PROTOCOL
    live_validator_count: int | None = None
    generator_mode: str | None = None
    """The generator competition's settings mode, shown beside the split: a
    generator share pays only ledger entries ``enforce`` marks eligible, and a
    protocol-33 validator hands an unpaid generator share back to memory."""

    confirmation_hint: str | None = None
    """The phrase that would re-apply the configured split, for the console."""


class AdminTrackShareSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    scope: str = "*"
    expected_revision: Annotated[int, Field(ge=0)]
    settings: TrackShareSettings
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str


class AdminTrackShareSettingsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    current: list[TrackShareSettingsRevision]
    history: list[TrackShareSettingsRevision]
    default: TrackShareSettings
    effective: EffectiveTrackShareSettings
