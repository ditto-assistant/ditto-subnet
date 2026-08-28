"""Effective required screening-policy version, with a short-TTL cache.

The deployed build implements every policy version up to
``SCREENING_POLICY_VERSION`` (its dual-text workers can screen under any of
them), but the version the queue REQUIRES rises only when a scheduled
activation is due — miners get equal notice that the rules changed, per the
subnet's bench-scaling loop. Until then the queue requires
``SCREENING_FLOOR_POLICY_VERSION`` and workers screen under that older text,
stamping outcomes with the version they actually screened under.

Short-TTL cached, matching ``ditto.api_server.queue_policy_settings``: the
poll/claim/heartbeat paths read this on every request, and an operator who
schedules an activation and immediately re-reads the board must see their own
write. A scheduled write invalidates the cache the same way a queue-policy
write does.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NamedTuple

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.db.queries.screener_policy_activation import (
    governing_screener_policy_activation,
    latest_screener_policy_activation,
)
from ditto.screener_policy_state import update_effective_screener_policy
from ditto_screening_protocol import (
    SCREENING_FLOOR_POLICY_VERSION,
    SCREENING_POLICY_VERSION,
)

DEFAULT_TTL_SECONDS = 5.0


class EffectiveScreenerPolicy(NamedTuple):
    """The version the queue requires now, and what a due activation implies.

    ``rescreen_scored`` is only meaningful when the required version is above
    the floor: the governing activation decided whether scored and live agents
    screened under a stale version re-enter the queue alongside evaluating and
    rejected rows.
    """

    required_policy_version: int
    floor_policy_version: int = SCREENING_FLOOR_POLICY_VERSION
    builtin_policy_version: int = SCREENING_POLICY_VERSION
    governing_revision: int | None = None
    rescreen_scored: bool = False
    latest_revision: int = 0

    @property
    def rescreen_stale_agents(self) -> bool:
        """Whether agents screened under a stale version re-enter the queue.

        With no due activation (required == floor) nothing is stale by
        definition. Once an activation is due, evaluating/rejected rows always
        rescreen; scored/live rows rescreen only when the activation said so.
        """
        return self.required_policy_version > self.floor_policy_version


@dataclass
class _CacheEntry:
    policy: EffectiveScreenerPolicy
    loaded_at: float


class ScreenerPolicyActivationResolver:
    """Short-TTL cache for the per-request required-version reads."""

    def __init__(self, *, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> None:
        self._ttl = max(0.0, ttl_seconds)
        self._cache: _CacheEntry | None = None
        self._lock = asyncio.Lock()

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    def invalidate(self) -> None:
        """Drop the cache so the next read sees a just-written schedule."""
        self._cache = None

    async def resolve(
        self, session_maker: async_sessionmaker | None
    ) -> EffectiveScreenerPolicy:
        if session_maker is None:
            policy = EffectiveScreenerPolicy(
                required_policy_version=SCREENING_FLOOR_POLICY_VERSION
            )
            update_effective_screener_policy(
                policy.required_policy_version, rescreen_scored=policy.rescreen_scored
            )
            return policy
        now = time.monotonic()
        if self._cache is not None and now - self._cache.loaded_at < self._ttl:
            update_effective_screener_policy(
                self._cache.policy.required_policy_version,
                rescreen_scored=self._cache.policy.rescreen_scored,
            )
            return self._cache.policy
        async with self._lock:
            now = time.monotonic()
            if self._cache is not None and now - self._cache.loaded_at < self._ttl:
                update_effective_screener_policy(
                    self._cache.policy.required_policy_version,
                    rescreen_scored=self._cache.policy.rescreen_scored,
                )
                return self._cache.policy
            async with session_maker() as session:
                governing = await governing_screener_policy_activation(
                    session, now=datetime.now(UTC)
                )
                latest = await latest_screener_policy_activation(session)
            if governing is not None:
                policy = EffectiveScreenerPolicy(
                    required_policy_version=max(
                        SCREENING_FLOOR_POLICY_VERSION,
                        min(governing.target_policy_version, SCREENING_POLICY_VERSION),
                    ),
                    governing_revision=governing.revision,
                    rescreen_scored=governing.rescreen_scored,
                    latest_revision=latest.revision if latest is not None else 0,
                )
            else:
                policy = EffectiveScreenerPolicy(
                    required_policy_version=SCREENING_FLOOR_POLICY_VERSION,
                    latest_revision=latest.revision if latest is not None else 0,
                )
            self._cache = _CacheEntry(policy=policy, loaded_at=time.monotonic())
            update_effective_screener_policy(
                policy.required_policy_version, rescreen_scored=policy.rescreen_scored
            )
            return policy


async def resolve_screener_policy_activation(
    session: AsyncSession,
) -> EffectiveScreenerPolicy:
    """Uncached read for admin endpoints and tests (session already open)."""
    governing = await governing_screener_policy_activation(
        session, now=datetime.now(UTC)
    )
    latest = await latest_screener_policy_activation(session)
    if governing is not None:
        policy = EffectiveScreenerPolicy(
            required_policy_version=max(
                SCREENING_FLOOR_POLICY_VERSION,
                min(governing.target_policy_version, SCREENING_POLICY_VERSION),
            ),
            governing_revision=governing.revision,
            rescreen_scored=governing.rescreen_scored,
            latest_revision=latest.revision if latest is not None else 0,
        )
        update_effective_screener_policy(
            policy.required_policy_version, rescreen_scored=policy.rescreen_scored
        )
        return policy
    policy = EffectiveScreenerPolicy(
        required_policy_version=SCREENING_FLOOR_POLICY_VERSION,
        latest_revision=latest.revision if latest is not None else 0,
    )
    update_effective_screener_policy(
        policy.required_policy_version, rescreen_scored=policy.rescreen_scored
    )
    return policy
