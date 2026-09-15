"""Optional, fail-open Taostats α / pool quote for the public chain surface.

Request handlers never call Taostats. A background task refreshes an in-memory
snapshot when configured; otherwise the public payload reports ``disabled``.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import httpx

logger = logging.getLogger(__name__)

MarketStatus = Literal["disabled", "fresh", "stale", "unavailable"]


@dataclass(frozen=True)
class SubnetMarketConfig:
    """Background refresh policy for the optional SN118 market quote."""

    url: str | None = None
    api_key: str | None = None
    timeout_seconds: float = 1.5
    refresh_seconds: int = 300
    retry_seconds: int = 120
    max_stale_seconds: int = 3600

    @property
    def enabled(self) -> bool:
        return self.url is not None and self.api_key is not None


@dataclass(frozen=True)
class SubnetMarketSnapshot:
    """One non-blocking view of the cached market quote."""

    status: MarketStatus
    refreshed_at: datetime | None
    alpha_tao: float | None = None
    alpha_usd: float | None = None
    market_cap_tao: float | None = None
    market_cap_usd: float | None = None


def parse_subnet_market_config_from_env() -> SubnetMarketConfig:
    """Resolve the optional market quote URL and timings from env."""
    url = os.environ.get("DITTO_TAOSTATS_MARKET_URL") or None
    # Reuse the shared Taostats key only when a market URL is configured so
    # enabling validator-names alone does not trip the market pair check.
    api_key = (os.environ.get("DITTO_TAOSTATS_API_KEY") or None) if url else None
    return SubnetMarketConfig(
        url=url,
        api_key=api_key,
        timeout_seconds=float(
            os.environ.get("DITTO_TAOSTATS_MARKET_TIMEOUT_SECONDS", "1.5")
        ),
        refresh_seconds=int(
            os.environ.get("DITTO_TAOSTATS_MARKET_REFRESH_SECONDS", "300")
        ),
        retry_seconds=int(os.environ.get("DITTO_TAOSTATS_MARKET_RETRY_SECONDS", "120")),
        max_stale_seconds=int(
            os.environ.get("DITTO_TAOSTATS_MARKET_MAX_STALE_SECONDS", "3600")
        ),
    )


def _safe_nonneg(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _first_record(payload: object) -> dict[str, Any] | None:
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list) and data:
            first = data[0]
            return first if isinstance(first, dict) else None
        if isinstance(data, dict):
            return data
        return payload
    if isinstance(payload, list) and payload:
        first = payload[0]
        return first if isinstance(first, dict) else None
    return None


def parse_taostats_market_quote(payload: object) -> dict[str, float | None]:
    """Allowlist numeric quote fields from a Taostats pool/price payload."""
    record = _first_record(payload)
    if record is None:
        raise ValueError("Taostats market response must contain a data object")

    def pick(*keys: str) -> float | None:
        for key in keys:
            value = _safe_nonneg(record.get(key))
            if value is not None:
                return value
        return None

    return {
        "alpha_tao": pick(
            "price",
            "alpha_price",
            "price_in_tao",
            "tao_in",
            "subnet_price",
        ),
        "alpha_usd": pick("price_in_usd", "usd_price", "alpha_usd"),
        "market_cap_tao": pick("market_cap", "marketcap", "market_cap_tao"),
        "market_cap_usd": pick("market_cap_usd", "usd_market_cap"),
    }


class TaostatsSubnetMarket:
    """Rate-limited stale-while-revalidate market quote with no request-path I/O."""

    def __init__(
        self,
        config: SubnetMarketConfig,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._owns_client = client is None and config.enabled
        self._client = client
        if self._client is None and config.enabled:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(config.timeout_seconds),
                follow_redirects=False,
            )
        self._quote: dict[str, float | None] = {
            "alpha_tao": None,
            "alpha_usd": None,
            "market_cap_tao": None,
            "market_cap_usd": None,
        }
        self._refreshed_at: datetime | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def snapshot(self) -> SubnetMarketSnapshot:
        """Return the current cache without performing upstream I/O."""
        if not self._config.enabled:
            return SubnetMarketSnapshot(status="disabled", refreshed_at=None)
        if self._refreshed_at is None:
            return SubnetMarketSnapshot(status="unavailable", refreshed_at=None)
        age = (datetime.now(UTC) - self._refreshed_at).total_seconds()
        status: MarketStatus = (
            "fresh" if age <= self._config.refresh_seconds else "stale"
        )
        if age > self._config.max_stale_seconds:
            return SubnetMarketSnapshot(
                status="unavailable", refreshed_at=self._refreshed_at
            )
        return SubnetMarketSnapshot(
            status=status,
            refreshed_at=self._refreshed_at,
            alpha_tao=self._quote.get("alpha_tao"),
            alpha_usd=self._quote.get("alpha_usd"),
            market_cap_tao=self._quote.get("market_cap_tao"),
            market_cap_usd=self._quote.get("market_cap_usd"),
        )

    async def start(self) -> None:
        if not self._config.enabled or self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._refresh_loop(), name="subnet-market")

    async def aclose(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _refresh_loop(self) -> None:
        while not self._stop.is_set():
            ok = await self._refresh_once()
            delay = (
                self._config.refresh_seconds if ok else self._config.retry_seconds
            )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except TimeoutError:
                continue

    async def _refresh_once(self) -> bool:
        assert self._config.url is not None and self._config.api_key is not None
        if self._client is None:
            return False
        try:
            response = await self._client.get(
                self._config.url,
                headers={"Authorization": self._config.api_key},
            )
            response.raise_for_status()
            quote = parse_taostats_market_quote(response.json())
        except Exception as error:
            logger.warning("subnet market refresh failed: %s", error)
            return False
        self._quote = quote
        self._refreshed_at = datetime.now(UTC)
        return True


def create_subnet_market(config: SubnetMarketConfig) -> TaostatsSubnetMarket:
    return TaostatsSubnetMarket(config)
