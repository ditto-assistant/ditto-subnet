"""One typed-weights allocation of SN118 miner emission (SN-350).

Before this surface, the split lived in three independently revisioned
settings with nested semantics: ``burn_share`` (a fraction of the remainder
after Gamma), ``track_shares_bps`` (shares of the post-burn pool, short of
10000 burning) and the Gamma service bucket ``allocation_bps`` (carved before
the burn). Getting "burn 50% / memory 35% / generator 15%" meant writing
``memory=7000 generator=3000`` and then ``burn_share=0.5`` in the order that
avoided a transient overpay.

An allocation is one revision that names every destination as integer basis
points of total miner emission, summing to exactly 10000::

    [{"kind": "burn", "bps": 5000},
     {"kind": "track", "id": "memory", "bps": 3500},
     {"kind": "track", "id": "generator", "bps": 1500},
     {"kind": "gamma", "bucket": "gm", "bps": 0}]

Platform derives the three fields validators already fold
(:mod:`ditto.api_models.emission_allocation_math`) and writes any that changed
as ordinary revisions of their own settings, in the same transaction as the
allocation revision. No validator release is involved.

Once an allocation revision exists it is the source of truth:

* the track-share writer refuses every write;
* the burn writer refuses a ``burn_share`` other than the allocation's (resend
  the current share to change only ``rank_shares``, which stays separate);
* the treasury writer refuses a bucket ``allocation_bps`` change while Gamma
  enforces (shadow-mode bucket values fold nothing and stay editable).

Until the first allocation write nothing changes: every served value still
comes from the three settings exactly as before.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ditto.api_models.emission_allocation_math import (
    AllocationEntry,
    AllocationError,
    canonical_entries,
)

_Bps = Annotated[int, Field(ge=0, le=10_000)]
_BucketId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{1,47}$")]


class EmissionAllocationEntry(BaseModel):
    """One destination of total miner emission."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal["burn", "track", "gamma"]
    id: str | None = Field(default=None, exclude_if=lambda value: value is None)
    """Track id for ``kind=track`` (memory, generator; coding/router only at 0)."""
    bucket: _BucketId | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    """Treasury service bucket id for ``kind=gamma``."""
    bps: _Bps

    @model_validator(mode="after")
    def _keyed_by_kind(self) -> EmissionAllocationEntry:
        if self.kind == "burn" and (self.id is not None or self.bucket is not None):
            raise ValueError("the burn entry takes no id or bucket")
        if self.kind == "track" and (self.id is None or self.bucket is not None):
            raise ValueError("a track entry takes an id and no bucket")
        if self.kind == "gamma" and (self.bucket is None or self.id is not None):
            raise ValueError("a gamma entry takes a bucket and no id")
        return self

    def to_math(self) -> AllocationEntry:
        key = self.id if self.kind == "track" else self.bucket
        return AllocationEntry(self.kind, key, self.bps)

    @classmethod
    def from_math(cls, entry: AllocationEntry) -> EmissionAllocationEntry:
        if entry.kind == "track":
            return cls(kind="track", id=entry.key, bps=entry.bps)
        if entry.kind == "gamma":
            return cls(kind="gamma", bucket=entry.key, bps=entry.bps)
        return cls(kind="burn", bps=entry.bps)


class EmissionAllocation(BaseModel):
    """A complete allocation, stored in canonical order."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    entries: Annotated[
        list[EmissionAllocationEntry], Field(min_length=3, max_length=32)
    ]

    @model_validator(mode="after")
    def _accounts_for_everything(self) -> EmissionAllocation:
        try:
            ordered = canonical_entries(entry.to_math() for entry in self.entries)
        except AllocationError as error:
            raise ValueError(str(error)) from error
        object.__setattr__(
            self, "entries", [EmissionAllocationEntry.from_math(e) for e in ordered]
        )
        return self

    def math_entries(self) -> tuple[AllocationEntry, ...]:
        return tuple(entry.to_math() for entry in self.entries)


class EmissionServedFields(BaseModel):
    """The three fields validators fold, as one allocation derives them."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    burn_share: float
    """``burn / (10000 - gamma)``: the share of the post-Gamma remainder burnt."""
    track_shares_bps: dict[str, int] | None
    """Served track map. Claims the whole pool, so unallocated bps never carry
    the burn. ``None`` only when no allocation-derived or prior map exists."""
    gamma_bps: dict[str, int]
    """Treasury service bucket allocation, of total emission."""


class EmissionAllocationRevision(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: int
    parent_revision: int
    allocation: EmissionAllocation
    served: EmissionServedFields
    burn_settings_revision: int
    """Burn settings revision in force after this write (0: none)."""
    track_share_settings_revision: int
    treasury_settings_revision: int
    checksum: str
    reason: str
    actor: str
    created_at: datetime


class EmissionShare(BaseModel):
    """One destination as a share of total miner emission."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    kind: Literal["burn", "track", "gamma"]
    id: str | None = None
    bucket: str | None = None
    bps: float
    """Basis points of total miner emission (float: the fold is float)."""
    percent: float


class EmissionSplitView(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    burn_share: float
    track_shares_bps: dict[str, int] | None
    """``None``: no map served, validators fold memory at the whole pool."""
    gamma_bps: dict[str, int]
    split: list[EmissionShare]


class EmissionPinnedSplit(EmissionSplitView):
    epoch_index: int
    pinned_block: int


class EmissionLegacyEquivalent(BaseModel):
    """The allocation that reproduces the split the three settings fold now."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    allocation: EmissionAllocation
    confirmation: str
    byte_identical: bool
    """Whether writing it would serve exactly today's ``burn_share`` and track
    map, so the first allocation write changes no served byte."""


class EmissionAllocationControl(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: int
    """Allocation revision in force; 0 before the first allocation write."""
    source: Literal["allocation", "legacy_settings"]
    """``legacy_settings``: no allocation yet, the three settings govern."""
    current: EmissionAllocationRevision | None
    history: list[EmissionAllocationRevision]
    confirmation_hint: str | None
    """Phrase that re-applies the current allocation."""
    configured: EmissionSplitView
    """What the current settings fold to: burn and track revisions, plus Gamma
    buckets only while Gamma enforces (shadow buckets fold nothing), assuming
    the track map is served and every paid track has a payable miner."""
    track_shares_served: bool | None
    """Whether the configured map passes the protocol-33 fleet gate now; when
    false validators fold memory at the whole pool. ``None``: unreadable."""
    in_force: EmissionSplitView | None
    """``configured`` with the fleet gate applied; ``None`` when unknown."""
    pinned: EmissionPinnedSplit | None
    """What the newest epoch pin freezes for every validator."""
    in_sync: bool | None
    """Whether ``configured`` equals the current allocation; ``None`` before the
    first allocation. False after, e.g., Gamma enforcement started with a
    shadow bucket value the allocation did not name."""
    gamma_mode: str | None
    """Recorded Gamma runtime mode (observe, enforce, pause) or ``None``."""
    gamma_buckets: list[str]
    """Bucket ids an allocation must name."""
    gamma_ranges: dict[str, tuple[int, int]] | None
    """Signed per-bucket ``[min, max]`` while Gamma enforces."""
    legacy_equivalent: EmissionLegacyEquivalent | None
    """Allocation equivalent to the configured split, or ``None`` when that
    split is not an exact integer allocation."""
    rank_shares: tuple[float, ...] | None
    """The KOTH payout curve, still its own burn setting (``set_burn_settings``)."""
    max_age_seconds: float


class AdminEmissionAllocationRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    expected_revision: Annotated[int, Field(ge=0)]
    settings: EmissionAllocation
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str
