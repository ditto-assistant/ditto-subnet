"""Typed-weights emission allocation: resolver, Gamma guard, writer and read model.

See :mod:`ditto.api_models.emission_allocation` for the operator contract and
:mod:`ditto.api_models.emission_allocation_math` for the exact derivation.

The allocation never becomes a new validator input. A write stores one
allocation revision plus, in the same transaction, a revision of each of the
three existing settings whose served value changed (burn, track shares, Gamma
buckets). Every consumer that reads those settings keeps working unchanged,
and their history pages stay truthful as derived views.

Two guards keep a validator from ever folding half of a split:

* the scoring ledger takes ``burn_share`` and ``track_shares_bps`` from one
  cached allocation row (:func:`overlay_served`) rather than from two
  independently cached settings, so a live read always pairs them;
* the epoch pin is inserted under the same advisory lock as this writer and is
  refused if the allocation revision moved after its policy was read, so a pin
  can never freeze a new Gamma bucket beside an old burn share.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from ditto.api_models.burn_settings import BurnSettings
from ditto.api_models.emission_allocation import (
    AdminEmissionAllocationRequest,
    EmissionAllocation,
    EmissionAllocationControl,
    EmissionAllocationEntry,
    EmissionAllocationRevision,
    EmissionLegacyEquivalent,
    EmissionPinnedSplit,
    EmissionServedFields,
    EmissionShare,
    EmissionSplitView,
)
from ditto.api_models.emission_allocation_math import (
    AllocationEntry,
    AllocationError,
    canonical_entries,
    confirmation_phrase,
    derive_served,
    effective_split,
    entries_from_split,
)
from ditto.api_models.track_share_settings import (
    TRACK_SHARES_PROTOCOL,
    TrackShareSettings,
)
from ditto.api_models.treasury_settings import TreasurySettings
from ditto.db.models import BurnSettingsRevision as BurnRow
from ditto.db.models import EmissionAllocationRevision as AllocationRow
from ditto.db.models import TrackShareSettingsRevision as TrackRow
from ditto.db.models import TreasurySettingsRevision as TreasuryRow
from ditto.db.queries.burn_settings import (
    GLOBAL_SCOPE,
    insert_burn_settings_revision,
    latest_burn_settings_revision,
)
from ditto.db.queries.emission_allocation import (
    latest_emission_allocation_revision,
    list_emission_allocation_revisions,
)
from ditto.db.queries.heartbeats import live_weight_setter_fleet_supports_protocol
from ditto.db.queries.ledger_epochs import latest_pin
from ditto.db.queries.track_share_settings import (
    insert_track_share_settings_revision,
    latest_track_share_settings_revision,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from ditto_screening_protocol.treasury import TreasuryEmissionPolicy

logger = logging.getLogger(__name__)
DEFAULT_TTL_SECONDS = 5.0
TREASURY_ACTOR = "platform_admin_token"
"""The treasury ledger records only the authenticated principal (see
``admin_treasury_settings``); the operator stays on the allocation row."""


class EmissionAllocationRefused(ValueError):
    """A write Platform refuses; ``status`` is the HTTP status to answer."""

    def __init__(self, message: str, *, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


def _checksum(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class ServedAllocation:
    """The fields one allocation revision serves, for the ledger overlay."""

    revision: int
    burn_share: float
    track_shares_bps: dict[str, int] | None
    gamma_bps: dict[str, int]


def served_from_row(row: AllocationRow | None) -> ServedAllocation | None:
    """Parse a stored allocation; ``None`` for no row or an unreadable one.

    An unreadable row falls back to the legacy settings, which hold the same
    values because the writer stored them in the same transaction.
    """
    if row is None:
        return None
    try:
        served = EmissionServedFields.model_validate(row.served)
        if served.track_shares_bps is not None:
            TrackShareSettings(track_shares_bps=dict(served.track_shares_bps))
        BurnSettings(burn_share=served.burn_share)
    except ValidationError:
        logger.warning(
            "emission allocation revision %s is invalid; serving legacy settings",
            getattr(row, "revision", "?"),
            exc_info=True,
        )
        return None
    return ServedAllocation(
        revision=row.revision,
        burn_share=served.burn_share,
        track_shares_bps=(
            dict(served.track_shares_bps)
            if served.track_shares_bps is not None
            else None
        ),
        gamma_bps=dict(served.gamma_bps),
    )


@dataclass
class _CacheEntry:
    served: ServedAllocation | None
    loaded_at: float


class EmissionAllocationResolver:
    """Short-TTL cache of the newest allocation row, one read for both fields."""

    def __init__(self, *, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> None:
        self._ttl = max(0.0, ttl_seconds)
        self._cache: _CacheEntry | None = None
        self._lock = asyncio.Lock()

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    def invalidate(self) -> None:
        self._cache = None

    async def resolve(
        self, session_maker: async_sessionmaker | None
    ) -> ServedAllocation | None:
        if session_maker is None:
            return None
        now = time.monotonic()
        if self._cache is not None and now - self._cache.loaded_at < self._ttl:
            return self._cache.served
        async with self._lock:
            now = time.monotonic()
            if self._cache is not None and now - self._cache.loaded_at < self._ttl:
                return self._cache.served
            async with session_maker() as session:
                row = await latest_emission_allocation_revision(session)
            served = served_from_row(row)
            self._cache = _CacheEntry(served=served, loaded_at=time.monotonic())
            return served


def overlay_served(
    served: ServedAllocation | None,
    *,
    burn: BurnSettings,
    track_shares: TrackShareSettings,
) -> tuple[BurnSettings, TrackShareSettings]:
    """Take ``burn_share`` and the track map from one allocation row.

    Without an allocation the legacy settings pass through untouched, so the
    served ledger is byte-identical until the first allocation write.
    ``rank_shares`` always stays with the burn settings.
    """
    if served is None:
        return burn, track_shares
    return (
        burn.model_copy(update={"burn_share": served.burn_share}),
        TrackShareSettings(track_shares_bps=served.track_shares_bps),
    )


# ─── Gamma ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GammaState:
    mode: str | None
    """Recorded runtime mode, ``None`` when none is recorded."""
    policy: TreasuryEmissionPolicy | None
    """The coldkey-signed policy while Gamma enforces."""
    settings: TreasurySettings | None
    settings_revision: int
    buckets: tuple[str, ...]
    configured_bps: dict[str, int]
    """What an enforcing epoch folds: the in-range operator value under a
    ranged policy, the signed value under a fixed one, and 0 per bucket while
    Gamma does not enforce (shadow values fold nothing)."""
    folding: bool
    """Enforcing and the activation epoch has been pinned."""
    activation_epoch: int | None


async def _latest_treasury_row(session: AsyncSession) -> TreasuryRow | None:
    from sqlalchemy import select

    return await session.scalar(
        select(TreasuryRow).order_by(TreasuryRow.revision.desc()).limit(1)
    )


async def gamma_state(session: AsyncSession, *, netuid: int | None) -> GammaState:
    """Read the Gamma runtime and treasury settings; ``ValueError`` when invalid."""
    from ditto.api_server.treasury_runtime import latest_runtime_row, runtime_revision

    treasury_row = await _latest_treasury_row(session)
    settings = (
        TreasurySettings.model_validate(treasury_row.settings)
        if treasury_row is not None
        else None
    )
    runtime_row = await latest_runtime_row(session)
    runtime = runtime_revision(runtime_row) if runtime_row is not None else None
    mode = runtime.settings.mode if runtime is not None else None
    setting_buckets = (
        {b.bucket_id: b.allocation_bps for b in settings.service_buckets}
        if settings is not None and settings.allocation_version == 2
        else {}
    )
    if runtime is not None and mode == "enforce":
        policy = runtime.settings.approval.policy
        buckets = tuple(sorted(b.bucket_id for b in policy.buckets))
        configured = (
            {bucket: setting_buckets.get(bucket, 0) for bucket in buckets}
            if policy.ranged
            else {b.bucket_id: b.allocation_bps for b in policy.buckets}
        )
        activation = runtime.settings.activation_epoch
        folding = activation is None
        if not folding and netuid is not None:
            pin = await latest_pin(session, netuid=netuid)
            folding = pin is not None and pin.epoch_index >= int(activation or 0)
        return GammaState(
            mode=mode,
            policy=policy,
            settings=settings,
            settings_revision=treasury_row.revision if treasury_row else 0,
            buckets=buckets,
            configured_bps=configured,
            folding=folding,
            activation_epoch=activation,
        )
    buckets = tuple(sorted(setting_buckets))
    return GammaState(
        mode=mode,
        policy=None,
        settings=settings,
        settings_revision=treasury_row.revision if treasury_row else 0,
        buckets=buckets,
        configured_bps=dict.fromkeys(buckets, 0),
        folding=False,
        activation_epoch=None,
    )


def _check_gamma(state: GammaState, gamma: dict[str, int]) -> TreasurySettings | None:
    """Refuse a Gamma split validators would not fold; return settings to write.

    A nonzero Gamma entry is admitted only while Gamma enforces and its
    activation epoch has been pinned, because the burn share is derived from
    the post-Gamma remainder: allocating Gamma that validators do not reserve
    would pay its bps to miners. Under a ranged signed policy each bucket must
    stay inside its coldkey-signed range; a fixed policy admits only its
    signed values. The returned settings, if any, are the treasury revision to
    append (Gamma-only change, ranged policy only).
    """
    from ditto.api_server.treasury_runtime import settings_match_policy

    if state.policy is None:
        if any(gamma.values()):
            raise EmissionAllocationRefused(
                "Gamma is not enforcing (runtime mode "
                f"{state.mode or 'not recorded'}): "
                "validators reserve nothing for it, so a nonzero Gamma entry would "
                "pay its bps to miners. Allocate Gamma 0 until record_treasury_runtime "
                "mode=enforce has pinned its activation epoch"
            )
        return None
    if any(gamma.values()) and not state.folding:
        raise EmissionAllocationRefused(
            f"Gamma enforcement activates at epoch {state.activation_epoch}, which no "
            "ledger pin has reached; until then validators reserve nothing for Gamma. "
            "Allocate Gamma 0 and re-apply after activation"
        )
    policy = state.policy
    if not policy.admits(gamma):
        ranges = {b.bucket_id: [b.minimum_bps, b.maximum_bps] for b in policy.buckets}
        raise EmissionAllocationRefused(
            f"Gamma {gamma} is outside the coldkey-signed policy range {ranges}"
        )
    if gamma == state.configured_bps:
        return None
    if not policy.ranged:  # pragma: no cover - admits() pins fixed values
        raise EmissionAllocationRefused("the signed Gamma policy is fixed")
    if state.settings is None:
        raise EmissionAllocationRefused("no treasury settings to carry Gamma")
    updated = state.settings.model_copy(
        update={
            "service_buckets": [
                bucket.model_copy(
                    update={"allocation_bps": gamma.get(bucket.bucket_id, 0)}
                )
                for bucket in state.settings.service_buckets
            ]
        }
    )
    updated = TreasurySettings.model_validate(updated.model_dump(mode="json"))
    if not settings_match_policy(updated, policy):
        raise EmissionAllocationRefused(
            "treasury settings do not match the signed Gamma policy (collector, "
            "bucket wallets or range); fix them before allocating Gamma"
        )
    return updated


# ─── Write ──────────────────────────────────────────────────────────────────


def _burn_checksum(settings: BurnSettings) -> str:
    return _checksum(settings.model_dump(mode="json"))


def _track_checksum(settings: TrackShareSettings) -> str:
    return _checksum(settings.model_dump(mode="json"))


def _strict_burn(row: BurnRow | None) -> BurnSettings:
    if row is None:
        return BurnSettings()
    try:
        return BurnSettings.model_validate(row.settings)
    except ValidationError as error:
        raise EmissionAllocationRefused(
            f"burn settings revision {row.revision} is unreadable; repair it with "
            "set_burn_settings before allocating"
        ) from error


def _strict_track(row: TrackRow | None) -> TrackShareSettings:
    if row is None:
        return TrackShareSettings()
    try:
        return TrackShareSettings.model_validate(row.settings)
    except ValidationError as error:
        raise EmissionAllocationRefused(
            f"track share settings revision {row.revision} is unreadable"
        ) from error


async def apply_allocation(
    session: AsyncSession,
    payload: AdminEmissionAllocationRequest,
    *,
    netuid: int | None,
) -> AllocationRow:
    """Validate, derive and store one allocation atomically. Caller commits.

    Must run inside the caller's transaction; takes the Gamma advisory lock
    shared with the epoch-pin insert and the treasury writers.
    """
    from ditto.api_server.treasury_runtime import lock_runtime

    reason = payload.reason.strip()
    actor = payload.actor.strip()
    if len(reason) < 8:
        raise EmissionAllocationRefused(
            "reason must be at least 8 characters", status=422
        )
    if not 1 <= len(actor) <= 120:
        raise EmissionAllocationRefused("actor must be 1-120 characters", status=422)
    await lock_runtime(session)
    latest = await latest_emission_allocation_revision(session)
    current_revision = latest.revision if latest is not None else 0
    if payload.expected_revision != current_revision:
        raise EmissionAllocationRefused(
            "emission allocation changed; refresh before applying "
            f"(expected {payload.expected_revision}, current {current_revision})"
        )
    try:
        gamma = await gamma_state(session, netuid=netuid)
    except (ValueError, ValidationError) as error:
        raise EmissionAllocationRefused(
            "Gamma runtime control or treasury settings are invalid"
        ) from error
    try:
        entries = canonical_entries(
            payload.settings.math_entries(), gamma_buckets=gamma.buckets
        )
        derived = derive_served(entries)
    except AllocationError as error:
        raise EmissionAllocationRefused(str(error), status=422) from error
    expected_phrase = confirmation_phrase(entries)
    if payload.confirmation != expected_phrase:
        raise EmissionAllocationRefused(
            f"confirmation must be exactly {expected_phrase}, naming every nonzero "
            "entry this revision applies"
        )
    treasury_update = _check_gamma(gamma, derived.gamma_bps)

    burn_row = await latest_burn_settings_revision(session)
    burn = _strict_burn(burn_row)
    track_row = await latest_track_share_settings_revision(session)
    track = _strict_track(track_row)
    target_map = (
        derived.track_shares_bps
        if derived.track_shares_bps is not None
        else track.track_shares_bps
    )
    allocation = EmissionAllocation(
        entries=[EmissionAllocationEntry.from_math(e) for e in entries]
    )
    allocation_json = allocation.model_dump(mode="json")
    served = EmissionServedFields(
        burn_share=derived.burn_share,
        track_shares_bps=target_map,
        gamma_bps=derived.gamma_bps,
    )
    row = AllocationRow(
        parent_revision=current_revision,
        allocation=allocation_json,
        served=served.model_dump(mode="json"),
        burn_settings_revision=burn_row.revision if burn_row else 0,
        track_share_settings_revision=track_row.revision if track_row else 0,
        treasury_settings_revision=gamma.settings_revision,
        checksum=_checksum(allocation_json),
        reason=reason,
        actor=actor,
    )
    session.add(row)
    await session.flush()
    derived_reason = f"emission allocation revision {row.revision}: {reason}"

    if derived.burn_share != burn.burn_share:
        settings = BurnSettings.model_validate(
            {
                "burn_share": derived.burn_share,
                "rank_shares": (
                    list(burn.rank_shares) if burn.rank_shares is not None else None
                ),
            }
        )
        new_burn = await insert_burn_settings_revision(
            session,
            parent_revision=burn_row.revision if burn_row else 0,
            scope=GLOBAL_SCOPE,
            settings=settings.model_dump(mode="json"),
            checksum=_burn_checksum(settings),
            reason=derived_reason,
            actor=actor,
        )
        row.burn_settings_revision = new_burn.revision
    if target_map != track.track_shares_bps:
        settings_t = TrackShareSettings(track_shares_bps=target_map)
        new_track = await insert_track_share_settings_revision(
            session,
            parent_revision=track_row.revision if track_row else 0,
            scope=GLOBAL_SCOPE,
            settings=settings_t.model_dump(mode="json"),
            checksum=_track_checksum(settings_t),
            reason=derived_reason,
            actor=actor,
        )
        row.track_share_settings_revision = new_track.revision
    if treasury_update is not None:
        settings_json = treasury_update.model_dump(mode="json")
        treasury_row = TreasuryRow(
            parent_revision=gamma.settings_revision,
            settings=settings_json,
            checksum=_checksum(settings_json),
            reason=derived_reason,
            actor=TREASURY_ACTOR,
        )
        session.add(treasury_row)
        await session.flush()
        row.treasury_settings_revision = treasury_row.revision
    await session.flush()
    logger.warning(
        "emission allocation revision %d by %s: %s (%s)",
        row.revision,
        actor,
        expected_phrase,
        reason,
    )
    return row


async def allocation_owner(session: AsyncSession) -> AllocationRow | None:
    """The allocation that owns the split, for the legacy writers' guards."""
    return await latest_emission_allocation_revision(session)


# ─── Read ───────────────────────────────────────────────────────────────────


def revision_from_row(row: AllocationRow) -> EmissionAllocationRevision:
    return EmissionAllocationRevision(
        revision=row.revision,
        parent_revision=row.parent_revision,
        allocation=EmissionAllocation.model_validate(row.allocation),
        served=EmissionServedFields.model_validate(row.served),
        burn_settings_revision=row.burn_settings_revision,
        track_share_settings_revision=row.track_share_settings_revision,
        treasury_settings_revision=row.treasury_settings_revision,
        checksum=row.checksum,
        reason=row.reason,
        actor=row.actor,
        created_at=row.created_at,
    )


def _shares(split: dict[str, float]) -> list[EmissionShare]:
    out: list[EmissionShare] = []
    for label, bps in split.items():
        if label == "burn":
            share = EmissionShare(kind="burn", bps=bps, percent=bps / 100)
        elif label.startswith("gamma:"):
            share = EmissionShare(
                kind="gamma", bucket=label[6:], bps=bps, percent=bps / 100
            )
        else:
            share = EmissionShare(kind="track", id=label, bps=bps, percent=bps / 100)
        out.append(share)
    order = {"gamma": 2, "burn": 0, "track": 1}
    return sorted(out, key=lambda s: order[s.kind])


def split_view(
    *,
    burn_share: float,
    track_shares_bps: dict[str, int] | None,
    gamma_bps: dict[str, int],
) -> EmissionSplitView:
    return EmissionSplitView(
        burn_share=burn_share,
        track_shares_bps=track_shares_bps,
        gamma_bps=gamma_bps,
        split=_shares(
            effective_split(
                burn_share=burn_share,
                track_shares_bps=track_shares_bps,
                gamma_bps=gamma_bps,
            )
        ),
    )


def _nonzero(entries: tuple[AllocationEntry, ...]) -> dict[str, int]:
    return {e.label: e.bps for e in entries if e.bps > 0}


async def configured_split(
    session: AsyncSession, *, netuid: int | None
) -> tuple[EmissionSplitView, GammaState | None]:
    """What the current settings fold to, shared with the public page."""
    from ditto.api_server.burn_settings import settings_from_row as burn_from_row
    from ditto.api_server.track_share_settings import (
        settings_from_row as track_from_row,
    )

    burn = burn_from_row(await latest_burn_settings_revision(session))
    track = track_from_row(await latest_track_share_settings_revision(session))
    try:
        gamma: GammaState | None = await gamma_state(session, netuid=netuid)
    except (ValueError, ValidationError):
        gamma = None
    view = split_view(
        burn_share=burn.burn_share,
        track_shares_bps=(
            dict(track.track_shares_bps) if track.track_shares_bps else None
        ),
        gamma_bps=dict(gamma.configured_bps) if gamma is not None else {},
    )
    return view, gamma


async def _pinned(
    session: AsyncSession, netuid: int | None
) -> EmissionPinnedSplit | None:
    if netuid is None:
        return None
    from ditto.api_server.ledger_pin import (
        LedgerPin,
        served_track_shares,
        treasury_pin_from_context,
    )
    from ditto_screening_protocol.treasury_enforcement import EnforcingTreasuryPin

    row = await latest_pin(session, netuid=netuid)
    if row is None:
        return None
    try:
        pin = LedgerPin.from_row(row)
        served = pin.context.get("served", {})
        treasury = treasury_pin_from_context(pin)
        burn_share = float(served.get("burn_share", 0.0))
    except (AttributeError, TypeError, ValueError, ValidationError):
        return None
    gamma = (
        {b.bucket_id: b.allocation_bps for b in treasury.effective_buckets}
        if isinstance(treasury, EnforcingTreasuryPin)
        else {}
    )
    shares = served_track_shares(served) or None
    view = split_view(burn_share=burn_share, track_shares_bps=shares, gamma_bps=gamma)
    return EmissionPinnedSplit(
        **view.model_dump(), epoch_index=pin.epoch_index, pinned_block=pin.pinned_block
    )


async def _in_force(
    session: AsyncSession,
    configured: EmissionSplitView,
    gamma: GammaState | None,
) -> tuple[bool | None, EmissionSplitView | None]:
    """Apply the protocol-33 track-map gate and Gamma activation to a split."""
    served: bool | None
    if configured.track_shares_bps is None:
        served = False
    else:
        try:
            served = await live_weight_setter_fleet_supports_protocol(
                session, minimum_protocol=TRACK_SHARES_PROTOCOL, now=datetime.now(UTC)
            )
        except SQLAlchemyError:
            logger.warning("could not read track-share fleet readiness", exc_info=True)
            served = None
    if served is None or gamma is None:
        return served, None
    return served, split_view(
        burn_share=configured.burn_share,
        track_shares_bps=configured.track_shares_bps if served else None,
        gamma_bps=(
            configured.gamma_bps
            if gamma.folding
            else dict.fromkeys(configured.gamma_bps, 0)
        ),
    )


@dataclass(frozen=True)
class PublicSplit:
    """The split the public page reports as in force, and where it came from."""

    view: EmissionSplitView | None
    epoch: int | None
    source: Literal["epoch_pin", "settings", "unavailable"]
    """``epoch_pin``: frozen by the newest pin, exactly what validators fold
    for that epoch. ``settings``: no pin yet, the live served settings with
    the fleet gate applied. ``unavailable``: neither could be read."""


async def public_in_force_split(
    session: AsyncSession, *, netuid: int | None
) -> tuple[PublicSplit, EmissionSplitView | None]:
    """In-force split (newest pin, else live settings) and the configured one."""
    try:
        configured, gamma = await configured_split(session, netuid=netuid)
    except (ValueError, ValidationError):
        configured, gamma = None, None
    pinned = await _pinned(session, netuid)
    if pinned is not None:
        view = EmissionSplitView(
            **pinned.model_dump(exclude={"epoch_index", "pinned_block"})
        )
        return PublicSplit(
            view=view, epoch=pinned.epoch_index, source="epoch_pin"
        ), configured
    if configured is None:
        return PublicSplit(view=None, epoch=None, source="unavailable"), None
    _served, in_force = await _in_force(session, configured, gamma)
    if in_force is None:
        return PublicSplit(view=None, epoch=None, source="unavailable"), configured
    return PublicSplit(view=in_force, epoch=None, source="settings"), configured


async def emission_allocation_control(
    session: AsyncSession, *, netuid: int | None, ttl_seconds: float
) -> EmissionAllocationControl:
    from ditto.api_server.burn_settings import settings_from_row as burn_from_row

    latest = await latest_emission_allocation_revision(session)
    history = await list_emission_allocation_revisions(session)
    configured, gamma = await configured_split(session, netuid=netuid)
    served, in_force = await _in_force(session, configured, gamma)
    configured_split_map = {
        (
            "burn"
            if s.kind == "burn"
            else f"gamma:{s.bucket}"
            if s.kind == "gamma"
            else str(s.id)
        ): s.bps
        for s in configured.split
    }
    equivalent_entries = entries_from_split(configured_split_map)
    legacy: EmissionLegacyEquivalent | None = None
    if equivalent_entries is not None:
        try:
            derived = derive_served(equivalent_entries)
        except AllocationError:
            derived = None
        if derived is not None:
            target_map = (
                derived.track_shares_bps
                if derived.track_shares_bps is not None
                else configured.track_shares_bps
            )
            legacy = EmissionLegacyEquivalent(
                allocation=EmissionAllocation(
                    entries=[
                        EmissionAllocationEntry.from_math(e) for e in equivalent_entries
                    ]
                ),
                confirmation=confirmation_phrase(equivalent_entries),
                byte_identical=(
                    derived.burn_share == configured.burn_share
                    and target_map == configured.track_shares_bps
                ),
            )
    current = revision_from_row(latest) if latest is not None else None
    in_sync: bool | None = None
    hint: str | None = None
    if current is not None:
        allocated = current.allocation.math_entries()
        hint = confirmation_phrase(allocated)
        in_sync = equivalent_entries is not None and _nonzero(
            equivalent_entries
        ) == _nonzero(allocated)
    burn = burn_from_row(await latest_burn_settings_revision(session))
    ranges = (
        {b.bucket_id: (b.minimum_bps, b.maximum_bps) for b in gamma.policy.buckets}
        if gamma is not None and gamma.policy is not None
        else None
    )
    return EmissionAllocationControl(
        revision=current.revision if current is not None else 0,
        source="allocation" if current is not None else "legacy_settings",
        current=current,
        history=[revision_from_row(row) for row in history],
        confirmation_hint=hint,
        configured=configured,
        track_shares_served=served,
        in_force=in_force,
        pinned=await _pinned(session, netuid),
        in_sync=in_sync,
        gamma_mode=gamma.mode if gamma is not None else "unavailable",
        gamma_buckets=list(gamma.buckets) if gamma is not None else [],
        gamma_ranges=ranges,
        legacy_equivalent=legacy,
        rank_shares=burn.rank_shares,
        max_age_seconds=ttl_seconds,
    )
