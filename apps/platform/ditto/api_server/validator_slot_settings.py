"""Short-TTL resolver for the operator-controlled validator slot cap.

Ticket dispatch resolves the *effective* slot policy through
:class:`ValidatorSlotSettingsResolver`, which reads the latest append-only
revision and caches it for a short TTL. A backroom write therefore lands on the
next ticket issue with no redeploy -- this is the instant kill switch for
multi-slot dispatch as well as the gradual-ramp control.

**Why an independent session:** the resolver never touches the request session
(which may already have autobegun a transaction on the dispatch path). It reads
the tiny, indexed latest-revision row on a short-lived session from the app's
session maker; when no maker is wired (unit tests without lifespan) it returns
:data:`DEFAULT_SETTINGS`.

**Fail closed, always.** Every path that cannot produce a stored policy -- no
revision, no session maker, unparseable JSON, *any* failure of the read --
returns :data:`DEFAULT_SETTINGS`, i.e. a cap of 2. There is deliberately no code
path here that answers "uncapped", and none that raises: a database outage must
neither silently hand the fleet eight-way parallelism nor 500 the ticket-issue
path. See the comment on the read's ``except`` arm for why it is broad.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pydantic import ValidationError

from ditto.api_models.validator_slot_settings import (
    CEILING_DISABLED,
    EffectiveValidatorSlotSettings,
    ValidatorSlotSettings,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from ditto.db.models import ValidatorSlotSettingsRevision

logger = logging.getLogger(__name__)

DEFAULT_SETTINGS = ValidatorSlotSettings()
"""The policy in force when no revision has ever been written -- and the
fallback for every failure path. ``max_concurrent_slots=2``,
``disk_percent_ceiling=90``, ``memory_percent_ceiling=90``,
``cpu_percent_ceiling=0`` (off), ``resource_block_percent_ceiling=95``."""

DEFAULT_SETTINGS_TTL_SECONDS = 5.0
"""Upper bound on how long a backroom change takes to reach the dispatch path
(the admin endpoint also invalidates its own worker's cache immediately on
write). Small so a kill-switch flip is observable within seconds; nonzero so a
busy dispatch loop does not issue a settings read on literally every ticket."""

DISK_RESTRICTED_SLOTS = 1
"""Slots a validator is held to once any per-resource ceiling is tripped.

Named for disk because disk was the only breaker when it landed, and the name
is on the wire in ``EffectiveValidatorSlotSettings.disk_restricted_slots``.
It now governs the memory and CPU ceilings too."""


def settings_from_row(
    row: ValidatorSlotSettingsRevision | None,
) -> ValidatorSlotSettings:
    """Parse a persisted revision's JSON, falling back to
    :data:`DEFAULT_SETTINGS` when the row is absent or no longer parses (schema
    drift / hand edit). Dispatch must never crash on a settings read, and the
    fallback is the conservative cap, never an uncapped fleet.
    """
    if row is None:
        return DEFAULT_SETTINGS
    try:
        return ValidatorSlotSettings.model_validate(row.settings)
    except ValidationError:
        logger.warning(
            "validator slot settings revision %s is invalid; using defaults",
            getattr(row, "revision", "?"),
            exc_info=True,
        )
        return DEFAULT_SETTINGS


HostResource = Literal["cpu", "memory", "disk"]


@dataclass(frozen=True)
class HostResourceSample:
    """The host utilisation a validator's most recent heartbeat reported.

    Each field is ``None`` when that reading is unavailable -- a pre-v3
    heartbeat, no telemetry at all, or a malformed metrics blob. Absence of
    evidence is never evidence of pressure: an unknown reading is treated as
    within every ceiling, exactly as the disk-only breaker always did.
    """

    cpu_percent: int | None = None
    memory_percent: int | None = None
    disk_percent: int | None = None


def _ceilings(
    settings: ValidatorSlotSettings, sample: HostResourceSample
) -> tuple[tuple[HostResource, int | None, int], ...]:
    """Pair each resource's reading with its configured throttle ceiling."""
    return (
        ("disk", sample.disk_percent, settings.disk_percent_ceiling),
        ("memory", sample.memory_percent, settings.memory_percent_ceiling),
        ("cpu", sample.cpu_percent, settings.cpu_percent_ceiling),
    )


def throttled_resources(
    settings: ValidatorSlotSettings, sample: HostResourceSample
) -> tuple[HostResource, ...]:
    """Resources at or above their own ceiling: hold this validator to one slot."""
    return tuple(
        resource
        for resource, observed, ceiling in _ceilings(settings, sample)
        if ceiling != CEILING_DISABLED and observed is not None and observed >= ceiling
    )


def blocked_resources(
    settings: ValidatorSlotSettings, sample: HostResourceSample
) -> tuple[HostResource, ...]:
    """Resources past the shared hard stop: issue this validator nothing.

    Only resources that have a per-resource ceiling configured are consulted, so
    disabling a resource disables it for both tiers. That is what keeps a pinned
    CPU -- the ordinary state of a working benchmark host -- from blocking a
    validator on a fleet that never asked for CPU gating.
    """
    hard_stop = settings.resource_block_percent_ceiling
    if hard_stop == CEILING_DISABLED:
        return ()
    return tuple(
        resource
        for resource, observed, ceiling in _ceilings(settings, sample)
        if ceiling != CEILING_DISABLED
        and observed is not None
        and observed >= hard_stop
    )


def validator_issuance_paused(
    settings: ValidatorSlotSettings, *, validator_hotkey: str
) -> bool:
    """Whether ``validator_hotkey`` may receive a new lease.

    Heartbeat admission and software compatibility remain separate facts. This
    is the exact, reversible operator decision stored in the append-only slot
    policy. Callers must still let an already-live lease resume and report.
    """
    return validator_hotkey in settings.paused_validator_hotkeys


def allowed_slot_count(
    settings: ValidatorSlotSettings,
    *,
    advertised_slots: int,
    sample: HostResourceSample,
) -> int:
    """How many slots this validator may hold live tickets for right now.

    The operator cap only ever narrows what the validator advertises -- it can
    never grant a slot the validator did not offer. Above a per-resource ceiling
    the result is clamped to :data:`DISK_RESTRICTED_SLOTS`; above the shared
    hard stop it is ``0``, which is the difference between "one at a time" and
    "not until you have calmed down".

    Evaluate this at ticket ISSUE time only. It describes how many leases may
    exist, so applying it to already-live leases would revoke work in flight;
    the caller compares the result against the validator's current live-lease
    count instead. A validator that crosses a ceiling mid-benchmark therefore
    still finishes and reports the work it already holds.
    """
    if blocked_resources(settings, sample):
        return 0
    allowed = min(max(advertised_slots, 0), settings.max_concurrent_slots)
    if throttled_resources(settings, sample):
        return min(allowed, DISK_RESTRICTED_SLOTS)
    return allowed


@dataclass
class _CacheEntry:
    settings: ValidatorSlotSettings
    loaded_at: float


class ValidatorSlotSettingsResolver:
    """TTL-cached read of the effective validator slot policy.

    One instance lives on ``app.state.validator_slot_settings``. Safe for
    concurrent use: a single in-flight reload is serialized behind a lock (with a
    double-check inside it) so a burst of dispatches issues at most one settings
    query per TTL window.
    """

    def __init__(self, *, ttl_seconds: float = DEFAULT_SETTINGS_TTL_SECONDS) -> None:
        self._ttl = max(0.0, ttl_seconds)
        self._cache: _CacheEntry | None = None
        self._lock = asyncio.Lock()

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    def invalidate(self) -> None:
        """Drop the cache so the next read re-reads the DB. Called by the admin
        endpoint after a write so a change lands immediately on this worker
        (other workers converge within the TTL)."""
        self._cache = None

    async def resolve(
        self, session_maker: async_sessionmaker | None
    ) -> ValidatorSlotSettings:
        """The slot policy governing the ticket about to be issued.

        Returns :data:`DEFAULT_SETTINGS` when no session maker is available
        (unit tests without lifespan), when no revision has been written, or
        when the read fails.
        """
        if session_maker is None:
            return DEFAULT_SETTINGS
        now = time.monotonic()
        cache = self._cache
        if cache is not None and (now - cache.loaded_at) < self._ttl:
            return cache.settings
        async with self._lock:
            cache = self._cache
            now = time.monotonic()
            if cache is not None and (now - cache.loaded_at) < self._ttl:
                return cache.settings
            from ditto.db.queries.validator_slot_settings import (
                latest_validator_slot_settings_revision,
            )

            try:
                async with session_maker() as session:
                    row = await latest_validator_slot_settings_revision(session)
            except Exception:
                # Deliberately broad, and deliberately NOT `SQLAlchemyError`.
                # The invariant this arm exists to hold is "no failure path
                # uncaps the fleet" (#433), and that is a statement about every
                # way the read can fail -- not about one library's exception
                # tree. Enumerating types gets this wrong in practice: asyncpg
                # raises a bare `ConnectionRefusedError` (an `OSError`) when
                # Postgres is down, which SQLAlchemy never wraps, so a
                # type-enumerated guard let the single most likely outage --
                # the database being unreachable -- escape this arm entirely.
                # Anything else the read can raise (a DNS failure, a pool
                # timeout, an SSL handshake error, a driver bug) has the same
                # correct answer: hold the conservative cap. `BaseException` is
                # deliberately still allowed through, so `CancelledError` and
                # shutdown are not swallowed.
                #
                # Fail CLOSED and do not cache: the conservative default stands
                # until the database answers again. Never uncap on an outage.
                logger.warning(
                    "validator slot settings read failed; holding the default "
                    "cap of %s slots",
                    DEFAULT_SETTINGS.max_concurrent_slots,
                    exc_info=True,
                )
                return DEFAULT_SETTINGS
            settings = settings_from_row(row)
            self._cache = _CacheEntry(settings=settings, loaded_at=time.monotonic())
            return settings


async def resolve_slot_settings(app_state: Any) -> ValidatorSlotSettings:
    """Resolve the policy dispatch is applying, from a request's ``app.state``.

    The one place a read path should reach for the cap. An app built without
    lifespan (unit tests, and any future entry point that forgets to wire the
    resolver) has no resolver on its state, and answering "uncapped" there would
    both over-issue tickets and make the fleet view claim capacity dispatch will
    never fund -- so the conservative default is returned instead, exactly as
    every other failure path in this module does.
    """
    resolver: ValidatorSlotSettingsResolver | None = getattr(
        app_state, "validator_slot_settings", None
    )
    if resolver is None:
        return DEFAULT_SETTINGS
    return await resolver.resolve(getattr(app_state, "session_maker", None))


def effective_view(
    row: ValidatorSlotSettingsRevision | None, *, ttl_seconds: float
) -> EffectiveValidatorSlotSettings:
    """The operator-console view of what dispatch resolves to, built from the
    freshly-read latest revision (never the TTL cache) so the console always
    reflects current DB truth.

    A row that no longer parses reports ``source="default"`` -- the console shows
    what dispatch is really doing (holding the default), not the revision number
    dispatch had to ignore.
    """
    settings = settings_from_row(row)
    revision = 0
    scope = "*"
    checksum = ""
    source: Literal["revision", "default"] = "default"
    if row is not None and _row_parses(row):
        revision = row.revision
        scope = row.scope
        checksum = row.checksum
        source = "revision"
    return EffectiveValidatorSlotSettings(
        revision=revision,
        scope=scope,
        settings=settings,
        checksum=checksum,
        source=source,
        disk_restricted_slots=DISK_RESTRICTED_SLOTS,
        max_age_seconds=ttl_seconds,
    )


def _row_parses(row: ValidatorSlotSettingsRevision) -> bool:
    try:
        ValidatorSlotSettings.model_validate(row.settings)
    except ValidationError:
        return False
    return True
