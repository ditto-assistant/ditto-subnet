"""Async client for the platform's ``/screener/*`` HTTP API.

The worker is HTTP-decoupled from the platform: it pulls work and posts verdicts
over the public ``/screener/*`` contract, authenticating every request with a
bearer token and the ``X-Screener-Hotkey`` header. Verdict POSTs additionally
carry an sr25519 signature. It never touches the platform DB.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID

import httpx

from ditto.api_models.screener import (
    SCREENING_POLICY_VERSION,
    ScreenerQueueResponse,
    ScreenResultRequest,
    ScreenResultResponse,
)
from ditto.api_models.validator import ArtifactResponse
from ditto.screener.errors import PlatformError

if TYPE_CHECKING:
    from ditto.screener.config import ScreenerConfig

logger = logging.getLogger(__name__)

_PREFIX = "/api/v1/screener"


class PlatformClient:
    """HTTP client for one platform base URL, screener-flavoured."""

    def __init__(self, config: ScreenerConfig, client: httpx.AsyncClient) -> None:
        self._config = config
        self._client = client
        self._base = config.platform_api_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {config.api_token}",
            "X-Screener-Hotkey": config.screener_hotkey,
        }

    async def get_queue(self) -> ScreenerQueueResponse:
        """Pull agents awaiting screening (status ``uploaded``), oldest first."""
        url = f"{self._base}{_PREFIX}/queue"
        params = {"limit": self._config.queue_limit}
        try:
            resp = await self._client.get(url, params=params, headers=self._headers)
        except httpx.HTTPError as e:
            raise PlatformError(f"queue fetch failed: {e}") from e
        if resp.status_code != 200:
            raise PlatformError(
                f"queue rejected ({resp.status_code}): {resp.text[:200]}"
            )
        return ScreenerQueueResponse.model_validate(resp.json())

    async def get_artifact(self, agent_id: UUID) -> ArtifactResponse:
        """Get a presigned tarball download URL for ``agent_id``."""
        url = f"{self._base}{_PREFIX}/agent/{agent_id}/artifact"
        try:
            resp = await self._client.get(url, headers=self._headers)
        except httpx.HTTPError as e:
            raise PlatformError(f"artifact fetch failed: {e}") from e
        if resp.status_code != 200:
            raise PlatformError(
                f"artifact rejected ({resp.status_code}): {resp.text[:200]}"
            )
        return ArtifactResponse.model_validate(resp.json())

    async def submit_result(
        self,
        agent_id: UUID,
        *,
        signature: str,
        passed: bool,
        policy_version: int = SCREENING_POLICY_VERSION,
        detail: str = "",
    ) -> ScreenResultResponse:
        """Report a signed pass/fail verdict for ``agent_id``."""
        url = f"{self._base}{_PREFIX}/agent/{agent_id}/result"
        payload = ScreenResultRequest(
            screener_hotkey=self._config.screener_hotkey,
            signature=signature,
            passed=passed,
            policy_version=policy_version,
            detail=detail,
        )
        try:
            resp = await self._client.post(
                url, json=payload.model_dump(mode="json"), headers=self._headers
            )
        except httpx.HTTPError as e:
            raise PlatformError(f"verdict submit failed: {e}") from e
        if resp.status_code != 200:
            raise PlatformError(
                f"verdict rejected ({resp.status_code}): {resp.text[:200]}"
            )
        return ScreenResultResponse.model_validate(resp.json())
