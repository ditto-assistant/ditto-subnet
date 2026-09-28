"""Signed, no-retry transport for default-off V13 verification replay leases."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Literal
from uuid import UUID

import httpx

from ditto_screener.errors import PlatformError
from ditto_screener.heartbeat import (
    ScreenerHeartbeatRequest,
    ScreenerHeartbeatResponse,
)
from ditto_screener.platform import PlatformClient
from ditto_screener.replay_process import ReplayProcessIdentity
from ditto_screening_protocol.v13_replay_process_identity import ReplayPurpose


class ReplayApiClient:
    """Bind each request's process proof to its exact bytes and current bearer."""

    def __init__(
        self,
        platform: PlatformClient,
        client: httpx.AsyncClient,
        identity: ReplayProcessIdentity,
    ) -> None:
        self._platform = platform
        self._client = client
        self._identity = identity

    async def _send(
        self,
        *,
        purpose: ReplayPurpose,
        path: str,
        method: Literal["GET", "POST"] = "POST",
        payload: Mapping[str, Any] | None = None,
    ) -> Any:
        body = (
            b""
            if payload is None
            else json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode("ascii")
        )
        headers = {
            **await self._platform._auth_headers(),
            **self._identity.headers(
                purpose=purpose, path=path, body=body, method=method
            ),
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        response = await self._client.request(
            method,
            f"{self._platform._base}/api/v1{path}",
            content=body if method == "POST" else None,
            headers=headers,
            timeout=30.0,
            follow_redirects=False,
        )
        if response.status_code != 200:
            raise PlatformError(
                f"verification replay {purpose} returned HTTP {response.status_code}"
            )
        return response.json()

    async def _request(
        self,
        *,
        purpose: ReplayPurpose,
        route: str,
        method: Literal["GET", "POST"] = "POST",
        payload: Mapping[str, Any] | None = None,
    ) -> Any:
        return await self._send(
            purpose=purpose,
            path=f"/screener/verification-replays{route}",
            method=method,
            payload=payload,
        )

    async def heartbeat(
        self, request: ScreenerHeartbeatRequest
    ) -> ScreenerHeartbeatResponse:
        """Publish one hotkey-signed heartbeat with the separate process proof."""
        value = await self._send(
            purpose="heartbeat",
            path="/screener/heartbeat",
            payload=request.model_dump(mode="json"),
        )
        return ScreenerHeartbeatResponse.model_validate(value)

    async def claim(self) -> dict[str, Any] | None:
        value = await self._request(purpose="claim", route="/claim")
        if value is not None and not isinstance(value, dict):
            raise PlatformError("verification replay claim returned invalid state")
        return value

    async def inputs(self, replay_id: UUID) -> dict[str, Any]:
        value = await self._request(
            purpose="inputs", route=f"/{replay_id}/inputs", method="GET"
        )
        if not isinstance(value, dict):
            raise PlatformError("verification replay inputs returned invalid state")
        return value

    async def renew(self, replay_id: UUID) -> dict[str, Any]:
        return await self._request(purpose="renew", route=f"/{replay_id}/renew")

    async def build_upload(
        self, replay_id: UUID, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        return await self._request(
            purpose="build-upload", route=f"/{replay_id}/build-upload", payload=payload
        )

    async def build_verify(
        self, replay_id: UUID, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        return await self._request(
            purpose="build-verify", route=f"/{replay_id}/build-verify", payload=payload
        )

    async def receipt(
        self, replay_id: UUID, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        return await self._request(
            purpose="receipts", route=f"/{replay_id}/receipts", payload=payload
        )

    async def finish(
        self,
        replay_id: UUID,
        *,
        status: Literal["reported", "failed"],
        artifact_sha256: str,
        image_sha256: str | None = None,
        failure_code: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            purpose="finish",
            route=f"/{replay_id}/finish",
            payload={
                "status": status,
                "failure_code": failure_code,
                "artifact_sha256": artifact_sha256,
                "image_sha256": image_sha256,
            },
        )
