"""Async Targon Rentals client used by the Platform screening loop.

The API key is an in-memory constructor value only. Never log it.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx


class TargonAPIError(RuntimeError):
    def __init__(self, *, operation: str, status: int | None, reason: str) -> None:
        self.operation = operation
        self.status = status
        self.reason = reason
        status_text = "transport" if status is None else str(status)
        super().__init__(f"Targon {operation} failed ({status_text}): {reason}")


class AsyncTargonClient:
    def __init__(
        self,
        *,
        api_key: str,
        org_slug: str,
        base_url: str = "https://api.targon.com/tha/v3",
        timeout_seconds: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._org_slug = org_slug
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout_seconds)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _workloads(self, suffix: str = "") -> str:
        slug = quote(self._org_slug, safe="")
        return f"{self._base_url}/orgs/{slug}/workloads{suffix}"

    async def _request(
        self,
        method: str,
        url: str,
        *,
        payload: dict[str, Any] | None = None,
        authenticated: bool = True,
    ) -> Any:
        headers = {"Accept": "application/json"}
        if authenticated:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            response = await self._client.request(
                method, url, json=payload, headers=headers
            )
        except httpx.HTTPError as error:
            raise TargonAPIError(
                operation=f"{method} {url}",
                status=None,
                reason=type(error).__name__,
            ) from error
        if response.status_code == 204 or not response.content:
            return None
        if response.status_code >= 400:
            reason = "rate limited" if response.status_code == 429 else "HTTP error"
            raise TargonAPIError(
                operation=f"{method} {url.split('?', 1)[0]}",
                status=response.status_code,
                reason=reason,
            )
        try:
            return response.json()
        except ValueError:
            raise TargonAPIError(
                operation=f"{method} {url}",
                status=response.status_code,
                reason="non-JSON response",
            ) from None

    async def inventory(self) -> list[dict[str, Any]]:
        value = await self._request(
            "GET",
            f"{self._base_url}/inventory?type=rental&gpu=false",
            authenticated=False,
        )
        if not isinstance(value, list):
            raise TargonAPIError(
                operation="GET /inventory", status=None, reason="invalid response shape"
            )
        return value

    async def create_rental(self, **payload: Any) -> dict[str, Any]:
        body: dict[str, Any] = {"type": "RENTAL", **payload}
        value = await self._request("POST", self._workloads(), payload=body)
        if not isinstance(value, dict) or not isinstance(value.get("uid"), str):
            raise TargonAPIError(
                operation="POST /workloads",
                status=None,
                reason="invalid response shape",
            )
        return value

    async def deploy(self, uid: str) -> None:
        await self._request("POST", self._workloads(f"/{uid}/deploy"))

    async def state(self, uid: str) -> dict[str, Any]:
        value = await self._request("GET", self._workloads(f"/{uid}/state"))
        if not isinstance(value, dict):
            raise TargonAPIError(
                operation="GET /workloads/state",
                status=None,
                reason="invalid response shape",
            )
        return value

    async def delete(self, uid: str) -> None:
        try:
            await self._request("DELETE", self._workloads(f"/{uid}"))
        except TargonAPIError as error:
            if error.status != 404:
                raise
