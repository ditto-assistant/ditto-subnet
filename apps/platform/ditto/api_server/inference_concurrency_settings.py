"""Resolver for the operator-controlled hosted-inference admission policy.

Short-TTL cached, matching ``ditto.api_server.queue_policy_settings`` and
``ditto.api_server.efficiency_settings``. The admission path
(:func:`ditto.db.queries.inference.begin_inference_request`) runs on **every**
proxied embedding -- 671 of them per v7 run -- so an uncached settings SELECT
there would add a query to the hottest path in the service for a value that
changes a handful of times a week. Five seconds is the whole latency an operator
sees between a backroom write and the fleet obeying it.

The resolver deliberately reads on its **own** session, not the caller's. The
admission transaction holds two ``FOR UPDATE`` row locks (the grant and its
ticket); borrowing that session to read a settings row would extend the
critical section of the most contended transaction in the system by a query.

Fails **open onto the shipped defaults** on a corrupt or unreadable row. The
defaults are a working configuration, so the worst case of a bad revision is the
shipped concurrency -- never a stalled or unbounded lane.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from ditto.api_models.inference_concurrency_settings import (
    InferenceConcurrencySettings,
)
from ditto.db.queries.inference_concurrency_settings import (
    latest_inference_concurrency_settings_revision,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from ditto.api_server.config import InferenceProxyConfig
    from ditto.db.models import InferenceConcurrencySettingsRevision

logger = logging.getLogger(__name__)
DEFAULT_SETTINGS = InferenceConcurrencySettings()
DEFAULT_SETTINGS_TTL_SECONDS = 5.0


def settings_from_row(
    row: InferenceConcurrencySettingsRevision | None,
) -> InferenceConcurrencySettings:
    """Decode a revision, falling back to the defaults on a corrupt payload."""
    if row is None:
        return DEFAULT_SETTINGS
    try:
        return InferenceConcurrencySettings.model_validate(row.settings)
    except ValidationError:
        logger.warning(
            "inference concurrency settings revision %s is invalid; using defaults",
            getattr(row, "revision", "?"),
            exc_info=True,
        )
        return DEFAULT_SETTINGS


def apply_settings(
    config: InferenceProxyConfig, settings: InferenceConcurrencySettings
) -> InferenceProxyConfig:
    """Overlay the resolved hosted-inference policy onto a proxy config.

    Returns a new frozen config so the admission query keeps its existing
    signature and every field this board does not own -- the embedding token
    budget, the upstream URLs, the routing weights -- is carried through
    untouched by construction rather than by remembering to copy it. Chat
    and embedding request-per-minute limits *are* owned: they return the same
    503 as concurrency, and leaving them boot-time is what made 8-wide runs
    look like a saturated lane.

    ``request_budget`` and ``token_budget`` are overlaid here too, but note where
    they are *consumed*: the grant-minting path (``ensure_inference_grant``)
    stamps both onto the new grant's row. The admission path compares against
    ``grant.request_budget`` and ``grant.token_budget``, the stamped columns, and
    so is unaffected by this overlay -- which is what keeps a live lease's
    allowances immutable.
    """
    return dataclasses.replace(
        config,
        request_budget=settings.chat_request_budget,
        token_budget=settings.chat_token_budget,
        per_ticket_concurrency=settings.chat_per_ticket_concurrency,
        per_validator_concurrency=settings.chat_per_validator_concurrency,
        global_concurrency=settings.chat_global_concurrency,
        per_ticket_requests_per_minute=settings.chat_per_ticket_requests_per_minute,
        per_validator_requests_per_minute=(
            settings.chat_per_validator_requests_per_minute
        ),
        global_requests_per_minute=settings.chat_global_requests_per_minute,
        embedding_per_ticket_concurrency=settings.embedding_per_ticket_concurrency,
        embedding_per_validator_concurrency=(
            settings.embedding_per_validator_concurrency
        ),
        embedding_global_concurrency=settings.embedding_global_concurrency,
        embedding_per_ticket_requests_per_minute=(
            settings.embedding_per_ticket_requests_per_minute
        ),
        embedding_per_validator_requests_per_minute=(
            settings.embedding_per_validator_requests_per_minute
        ),
        embedding_global_requests_per_minute=(
            settings.embedding_global_requests_per_minute
        ),
    )


@dataclass
class _CacheEntry:
    settings: InferenceConcurrencySettings
    loaded_at: float


class InferenceConcurrencySettingsResolver:
    """Short-TTL cache for the per-request admission read."""

    def __init__(self, *, ttl_seconds: float = DEFAULT_SETTINGS_TTL_SECONDS) -> None:
        self._ttl = max(0.0, ttl_seconds)
        self._cache: _CacheEntry | None = None
        self._lock = asyncio.Lock()

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    def invalidate(self) -> None:
        """Drop the cache so the next admission sees a just-written revision."""
        self._cache = None

    async def resolve(
        self, session_maker: async_sessionmaker | None
    ) -> InferenceConcurrencySettings:
        if session_maker is None:
            return DEFAULT_SETTINGS
        now = time.monotonic()
        if self._cache is not None and now - self._cache.loaded_at < self._ttl:
            return self._cache.settings
        async with self._lock:
            now = time.monotonic()
            if self._cache is not None and now - self._cache.loaded_at < self._ttl:
                return self._cache.settings
            try:
                async with session_maker() as session:
                    row = await latest_inference_concurrency_settings_revision(session)
            except Exception:
                # A database blip must not fail an inference request. Serve the
                # defaults and do NOT cache them, so the resolver recovers on the
                # next admission rather than pinning defaults for a full TTL.
                logger.warning(
                    "could not read inference concurrency settings; using defaults",
                    exc_info=True,
                )
                return DEFAULT_SETTINGS
            settings = settings_from_row(row)
            self._cache = _CacheEntry(settings=settings, loaded_at=time.monotonic())
            return settings

    async def resolve_config(
        self,
        config: InferenceProxyConfig,
        session_maker: async_sessionmaker | None,
    ) -> InferenceProxyConfig:
        """The admission-path convenience: resolved limits overlaid on config."""
        return apply_settings(config, await self.resolve(session_maker))


async def resolved_proxy_config(
    state: Any, config: InferenceProxyConfig
) -> InferenceProxyConfig:
    """``config`` with the operator's live policy overlaid, given an app state.

    With no resolver bound (unit tests, or a deployment predating the board) the
    config fallback is returned untouched. The board's shipped defaults are the
    same numbers ``config.py`` seeds, so an empty settings table and an absent
    resolver produce identical behaviour.

    **Call this before opening the caller's transaction, never inside it.** The
    resolver reads on its own session; doing that while the caller holds a grant
    or ticket row lock would borrow a second pool connection for the duration of
    the most contended transaction in the service.
    """
    resolver: InferenceConcurrencySettingsResolver | None = getattr(
        state, "inference_concurrency_settings", None
    )
    if resolver is None:
        return config
    return await resolver.resolve_config(config, getattr(state, "session_maker", None))
