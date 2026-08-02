"""Optional, fail-open Taostats validator-name decoration.

The public dashboard must never wait on Taostats.  This module owns a bounded
in-memory cache refreshed only by a background task; request handlers read a
snapshot synchronously and fall back to hotkeys when the source is unavailable.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import unicodedata
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx

logger = logging.getLogger(__name__)

_MAX_NAMES = 512
_MAX_NAME_LENGTH = 80
_SS58_ALPHABET = frozenset("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")

ValidatorNameStatus = Literal["disabled", "fresh", "stale", "unavailable"]


@dataclass(frozen=True)
class ValidatorNamesConfig:
    """Background refresh policy for optional Taostats decoration."""

    url: str | None = None
    api_key: str | None = None
    timeout_seconds: float = 1.5
    refresh_seconds: int = 3600
    retry_seconds: int = 300
    max_stale_seconds: int = 86400

    @property
    def enabled(self) -> bool:
        return self.url is not None and self.api_key is not None


@dataclass(frozen=True)
class ValidatorNamesSnapshot:
    """One non-blocking, allowlisted view of the cached external data."""

    status: ValidatorNameStatus
    refreshed_at: datetime | None
    names: dict[str, str]
    stake_weights: dict[str, float]


def parse_validator_names_config_from_env() -> ValidatorNamesConfig:
    """Resolve the optional source URL and bounded cache timings from env."""
    return ValidatorNamesConfig(
        url=os.environ.get("DITTO_TAOSTATS_VALIDATOR_NAMES_URL") or None,
        api_key=os.environ.get("DITTO_TAOSTATS_API_KEY") or None,
        timeout_seconds=float(os.environ.get("DITTO_TAOSTATS_TIMEOUT_SECONDS", "1.5")),
        refresh_seconds=int(os.environ.get("DITTO_TAOSTATS_REFRESH_SECONDS", "3600")),
        retry_seconds=int(os.environ.get("DITTO_TAOSTATS_RETRY_SECONDS", "300")),
        max_stale_seconds=int(
            os.environ.get("DITTO_TAOSTATS_MAX_STALE_SECONDS", "86400")
        ),
    )


def _valid_hotkey(value: object) -> str | None:
    if not isinstance(value, str) or len(value) not in {47, 48}:
        return None
    if any(character not in _SS58_ALPHABET for character in value):
        return None
    return value


def _safe_name(value: object) -> str | None:
    """Bound display text and drop control/bidirectional spoofing characters."""
    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFC", value).strip()
    safe = "".join(
        character
        for character in normalized
        if unicodedata.category(character) not in {"Cc", "Cf", "Cs"}
    )
    if not safe:
        return None
    return safe[:_MAX_NAME_LENGTH]


def _extract_hotkey(item: dict[str, Any]) -> str | None:
    """Read only documented Taostats address fields; ignore the rest."""
    for key in ("hotkey", "address", "validator_hotkey"):
        candidate = item.get(key)
        if isinstance(candidate, dict):
            candidate = candidate.get("ss58")
        hotkey = _valid_hotkey(candidate)
        if hotkey is not None:
            return hotkey
    return None


def _safe_stake_weight(value: object) -> float | None:
    """Normalize a public chain stake value without accepting invalid numbers."""
    if isinstance(value, bool):
        return None
    try:
        stake_weight = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(stake_weight) or stake_weight < 0:
        return None
    return stake_weight


def parse_taostats_validator_metadata(
    payload: object,
) -> tuple[dict[str, str], dict[str, float]]:
    """Strictly allowlist public names and stake weights keyed by hotkey."""
    records = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        raise ValueError("Taostats response must contain a data list")

    names: dict[str, str] = {}
    stake_weights: dict[str, float] = {}
    hotkeys: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        hotkey = _extract_hotkey(record)
        if hotkey is None:
            continue
        name = _safe_name(record.get("name"))
        stake_weight = _safe_stake_weight(record.get("hotkey_alpha"))
        if name is not None:
            names[hotkey] = name
        if stake_weight is not None:
            stake_weights[hotkey] = stake_weight
        hotkeys.add(hotkey)
        if len(hotkeys) >= _MAX_NAMES:
            break
    return names, stake_weights


def parse_taostats_names(payload: object) -> dict[str, str]:
    """Return the name-only projection retained for existing callers."""
    names, _ = parse_taostats_validator_metadata(payload)
    return names


class TaostatsValidatorNames:
    """Rate-limited stale-while-revalidate cache with no request-path I/O."""

    def __init__(
        self,
        config: ValidatorNamesConfig,
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
        self._names: dict[str, str] = {}
        self._stake_weights: dict[str, float] = {}
        self._refreshed_at: datetime | None = None
        self._next_attempt_at = datetime.min.replace(tzinfo=UTC)
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the refresher without waiting for its first network request."""
        if self._config.enabled and self._task is None:
            self._task = asyncio.create_task(
                self._refresh_loop(), name="taostats-validator-names"
            )

    async def aclose(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    def snapshot(
        self, hotkeys: list[str] | tuple[str, ...], *, now: datetime | None = None
    ) -> ValidatorNamesSnapshot:
        """Return cached names immediately, restricted to platform reporters."""
        current = now or datetime.now(UTC)
        if not self._config.enabled:
            return ValidatorNamesSnapshot("disabled", None, {}, {})
        if self._refreshed_at is None:
            return ValidatorNamesSnapshot("unavailable", None, {}, {})

        age = max(0.0, (current - self._refreshed_at).total_seconds())
        if age <= self._config.refresh_seconds:
            status: ValidatorNameStatus = "fresh"
        elif age <= self._config.max_stale_seconds:
            status = "stale"
        else:
            return ValidatorNamesSnapshot("unavailable", self._refreshed_at, {}, {})
        allowed = set(hotkeys)
        return ValidatorNamesSnapshot(
            status,
            self._refreshed_at,
            {hotkey: name for hotkey, name in self._names.items() if hotkey in allowed},
            {
                hotkey: stake_weight
                for hotkey, stake_weight in self._stake_weights.items()
                if hotkey in allowed
            },
        )

    async def refresh(self, *, now: datetime | None = None) -> bool:
        """Attempt one bounded refresh; failures retain still-valid stale data."""
        current = now or datetime.now(UTC)
        if not self._config.enabled:
            return False
        if self._client is None:  # Defensive: configured instances always own one.
            return False
        async with self._lock:
            if current < self._next_attempt_at:
                return False
            self._next_attempt_at = current + timedelta(
                seconds=self._config.retry_seconds
            )
            try:
                assert self._config.url is not None
                assert self._config.api_key is not None
                response = await self._client.get(
                    self._config.url,
                    headers={"Authorization": self._config.api_key},
                )
                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    try:
                        retry_seconds = math.ceil(float(retry_after or ""))
                    except ValueError:
                        retry_seconds = self._config.retry_seconds
                    retry_seconds = max(
                        self._config.retry_seconds,
                        min(retry_seconds, self._config.refresh_seconds),
                    )
                    self._next_attempt_at = current + timedelta(seconds=retry_seconds)
                    return False
                response.raise_for_status()
                names, stake_weights = parse_taostats_validator_metadata(
                    response.json()
                )
            except (httpx.HTTPError, ValueError) as error:
                logger.warning("Taostats validator-name refresh failed: %s", error)
                return False

            self._names = names
            self._stake_weights = stake_weights
            self._refreshed_at = current
            self._next_attempt_at = current + timedelta(
                seconds=self._config.refresh_seconds
            )
            return True

    async def _refresh_loop(self) -> None:
        while not self._stop.is_set():
            await self.refresh()
            delay = max(
                1.0,
                (self._next_attempt_at - datetime.now(UTC)).total_seconds(),
            )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except TimeoutError:
                continue


def create_validator_names(
    config: ValidatorNamesConfig,
) -> TaostatsValidatorNames:
    return TaostatsValidatorNames(config)
