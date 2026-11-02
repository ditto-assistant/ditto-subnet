"""Relay reader for the shadow router-track ledger.

The router eval never runs on a validator or on Platform. One trusted,
offloaded ``dittobench-api`` scorer drives the third-party coding harnesses
against each miner's router, scores it, and publishes a
:class:`~ditto.api_models.router_ledger.RouterLedgerResponse` on its
control-plane (``GET /v1/router/ledger``, behind the scorer's validator-facing
control token). Platform relays that published ledger to permitted validators
through ``GET /scoring/router-ledger``.

This module owns the outbound read. It is deliberately thin and snapshot-free
(no Platform DB table, so no migration and no generated-artifact churn): the
scorer is the ledger's system of record. A short in-process last-known cache
smooths a transient scorer blip; past a bounded staleness the reader falls back
to an **empty** ledger, which folds to zero router emission — the same shadow
default as no feed at all.

Security: the scorer control token is a secret read from the environment
(injected from Secret Manager in deployment). It is sent only as a ``Bearer``
header to the configured scorer URL and is never logged, echoed onto the relay
wire, or attached to any error surfaced to a validator.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import httpx

from ditto.api_models.router_ledger import RouterLedgerResponse

logger = logging.getLogger(__name__)

# How long a successfully-read ledger may be reused after a live read fails
# before it is dropped in favor of an empty ledger. The router track is shadow
# (zero emission) and moves slowly, so a couple of minutes of last-known-good is
# safe and far better than flapping the folded pool on a transient scorer blip.
_MAX_STALE = timedelta(minutes=5)

# Bound every outbound read so a slow or hung scorer never stalls a validator's
# ledger fetch (which itself sits on the sweep's critical path).
_READ_TIMEOUT_SECONDS = 15.0

# Cap the scorer response so a hostile or buggy endpoint cannot exhaust memory.
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


@dataclass
class _CachedLedger:
    ledger: RouterLedgerResponse
    fetched_at: datetime


class RouterLedgerReader:
    """Reads the shadow router ledger the offloaded scorer publishes.

    Constructed only when a scorer URL and control token are configured; when
    either is absent :func:`create_router_ledger_reader_from_env` returns
    ``None`` and the endpoint serves an empty ledger directly.
    """

    def __init__(self, *, base_url: str, control_token: str) -> None:
        self._url = base_url.rstrip("/") + "/v1/router/ledger"
        # Held in a private attribute and only ever written to the Authorization
        # header; never logged or surfaced in an error.
        self._token = control_token
        self._client = httpx.AsyncClient(timeout=_READ_TIMEOUT_SECONDS)
        self._last_known: _CachedLedger | None = None

    async def read(self) -> RouterLedgerResponse:
        """Return the current shadow ledger, or an empty one on failure.

        Never raises: a router-track read problem must not fail a validator's
        fold. On a live failure the most recent still-fresh snapshot is served;
        if there is none (or it is too old), an empty ledger is returned.
        """
        try:
            resp = await self._client.get(
                self._url,
                headers={"Authorization": f"Bearer {self._token}"},
            )
            if resp.status_code != 200:
                # Do not echo the scorer body: it is operator-internal.
                raise RuntimeError(f"scorer returned status {resp.status_code}")
            raw = resp.content[:_MAX_RESPONSE_BYTES]
            ledger = RouterLedgerResponse.model_validate_json(raw)
        except Exception:
            logger.warning(
                "router ledger scorer read failed; falling back to last-known",
                exc_info=True,
            )
            return self._serve_last_known()
        self._last_known = _CachedLedger(ledger=ledger, fetched_at=datetime.now(UTC))
        return ledger

    def _serve_last_known(self) -> RouterLedgerResponse:
        cached = self._last_known
        if cached is None:
            return RouterLedgerResponse()
        age = datetime.now(UTC) - cached.fetched_at
        if age > _MAX_STALE:
            logger.warning(
                "router ledger last-known is %ds old (> %ds); serving empty",
                int(age.total_seconds()),
                int(_MAX_STALE.total_seconds()),
            )
            return RouterLedgerResponse()
        # Flag the reused snapshot so the fold can log that it is stale.
        return cached.ledger.model_copy(update={"stale": True})

    async def aclose(self) -> None:
        await self._client.aclose()


def create_router_ledger_reader_from_env() -> RouterLedgerReader | None:
    """Build the reader from the environment, or ``None`` when unconfigured.

    Requires both ``DITTOBENCH_ROUTER_SCORER_URL`` (the offloaded scorer's
    control-plane base, e.g. ``https://scorer.internal:8000``) and
    ``DITTOBENCH_ROUTER_SCORER_CONTROL_TOKEN`` (the scorer's validator-facing
    control token, a secret). Either absent → ``None`` → the relay serves an
    empty ledger, which is the safe shadow default: reading the router ledger is
    off until a scorer feed is deliberately wired in.
    """
    base_url = (os.environ.get("DITTOBENCH_ROUTER_SCORER_URL") or "").strip()
    token = (os.environ.get("DITTOBENCH_ROUTER_SCORER_CONTROL_TOKEN") or "").strip()
    if not base_url or not token:
        return None
    parsed = urlsplit(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        logger.warning(
            "DITTOBENCH_ROUTER_SCORER_URL is not a valid http(s) URL; "
            "router ledger relay disabled"
        )
        return None
    logger.info("router ledger relay enabled (scorer host=%s)", parsed.hostname)
    return RouterLedgerReader(base_url=base_url, control_token=token)
