"""Short-TTL resolver for the operator-controlled continual-retest policy."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import ValidationError

from ditto.api_models.continual_retest_settings import ContinualRetestSettings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from ditto.db.models import ContinualRetestSettingsRevision

logger = logging.getLogger(__name__)
DEFAULT_SETTINGS = ContinualRetestSettings()
DEFAULT_SETTINGS_TTL_SECONDS = 5.0


def settings_from_row(
    row: ContinualRetestSettingsRevision | None,
) -> ContinualRetestSettings:
    if row is None:
        return DEFAULT_SETTINGS
    try:
        return ContinualRetestSettings.model_validate(row.settings)
    except ValidationError:
        logger.warning(
            "continual retest settings revision %s is invalid; using defaults",
            getattr(row, "revision", "?"),
            exc_info=True,
        )
        return DEFAULT_SETTINGS


def aggregate_is_active(
    settings: ContinualRetestSettings, *, fleet_protocol_ready: bool
) -> bool:
    if settings.aggregate_mode == "enabled":
        return True
    if settings.aggregate_mode == "disabled":
        return False
    return fleet_protocol_ready


def tie_weighting_is_active(
    settings: ContinualRetestSettings, *, fleet_protocol_ready: bool
) -> bool:
    """Resolve the consensus-safe tie pooling marker.

    The policy cannot override fleet readiness: exposing a fold-changing ledger
    field to only part of the fleet would make validators submit different
    vectors for identical evidence.
    """
    return settings.tie_weighting_mode == "fleet_ready" and fleet_protocol_ready


def rollout_standdown_reason(
    settings: ContinualRetestSettings,
    *,
    open_rollout_desired_version: int | None,
    validator_supports_desired_version: bool,
) -> str | None:
    """Why this validator must not start a previous-generation retest now.

    Continual retests re-score the ACTIVE benchmark generation. While a rollout
    collects, the fleet's scarce benchmark slots are the same ones the cohort
    needs to reach quorum on the DESIRED generation, so a fresh retest lease
    directly delays the thing the rollout exists to do. Returning a reason keeps
    the refusal self-describing at the call site and in the operator API.

    ``capable_validators`` (the default) yields only the capacity the rollout can
    actually use: a validator that does not advertise the desired version can
    never take cohort work, so standing it down would idle real capacity for zero
    rollout benefit. It keeps confirming the active generation as usual.

    This is deliberately a stand-down at *issuance* only. An already-leased wave
    runs and reports to completion, so shared-seed wave semantics are never torn
    in half, and the stand-down lifts on its own the moment ``open_rollout``
    stops returning a row -- that is, on activation or supersede.
    """
    if open_rollout_desired_version is None:
        return None
    if settings.rollout_standdown == "off":
        return None
    if (
        settings.rollout_standdown == "capable_validators"
        and not validator_supports_desired_version
    ):
        return None
    return (
        "continual retests are standing down while benchmark version "
        f"{open_rollout_desired_version} is collecting; validator capacity is "
        "reserved for rollout qualification and retests resume automatically "
        "once the rollout activates or is superseded"
    )


@dataclass
class _CacheEntry:
    settings: ContinualRetestSettings
    loaded_at: float


class ContinualRetestSettingsResolver:
    def __init__(self, *, ttl_seconds: float = DEFAULT_SETTINGS_TTL_SECONDS) -> None:
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
    ) -> ContinualRetestSettings:
        if session_maker is None:
            return DEFAULT_SETTINGS
        now = time.monotonic()
        if self._cache is not None and now - self._cache.loaded_at < self._ttl:
            return self._cache.settings
        async with self._lock:
            now = time.monotonic()
            if self._cache is not None and now - self._cache.loaded_at < self._ttl:
                return self._cache.settings
            from ditto.db.queries.continual_retest_settings import (
                latest_continual_retest_settings_revision,
            )

            async with session_maker() as session:
                row = await latest_continual_retest_settings_revision(session)
            settings = settings_from_row(row)
            self._cache = _CacheEntry(settings=settings, loaded_at=time.monotonic())
            return settings
