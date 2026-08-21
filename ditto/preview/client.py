"""HTTP client for preview-control cheatcodes."""

from __future__ import annotations

from typing import Any

import httpx


class PreviewClientError(RuntimeError):
    """A bounded preview-control transport or response failure."""


class PreviewClient:
    """Talk to a running preview-control server."""

    def __init__(self, base_url: str, timeout: float = 5.0, *, token: str = ""):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.token = token

    def health(self) -> dict[str, Any]:
        return self._get("/health")

    def state(self) -> dict[str, Any]:
        return self._get("/v1/state")

    def register(
        self, hotkey: str, *, permit: bool = False, stake: float = 0.0
    ) -> dict[str, Any]:
        return self._post(
            "/v1/cheat/register",
            {"hotkey": hotkey, "permit": permit, "stake": stake},
        )

    def permit(self, hotkey: str, enabled: bool = True) -> dict[str, Any]:
        return self._post("/v1/cheat/permit", {"hotkey": hotkey, "enabled": enabled})

    def warp_block(self, n: int) -> dict[str, Any]:
        return self._post("/v1/cheat/warp_block", {"n": n})

    def warp_tempo(self, n: int = 1) -> dict[str, Any]:
        return self._post("/v1/cheat/warp_tempo", {"n": n})

    def issue_lease(self, hotkey: str, lifetime_blocks: int = 100) -> dict[str, Any]:
        return self._post(
            "/v1/cheat/issue_lease",
            {"hotkey": hotkey, "lifetime_blocks": lifetime_blocks},
        )

    def expire_lease(self, lease_id: str | None = None) -> dict[str, Any]:
        return self._post("/v1/cheat/expire_lease", {"lease_id": lease_id})

    def issue_grant(self) -> dict[str, Any]:
        return self._post("/v1/cheat/issue_grant", {})

    def exhaust_allowance(self, grant_id: str | None = None) -> dict[str, Any]:
        return self._post("/v1/cheat/exhaust_allowance", {"grant_id": grant_id})

    def inject_provider(self, status: int | None) -> dict[str, Any]:
        return self._post("/v1/cheat/inject_provider", {"status": status})

    def drop_relay(self, dropped: bool = True) -> dict[str, Any]:
        return self._post("/v1/cheat/drop_relay", {"dropped": dropped})

    def snapshot(self, name: str) -> dict[str, Any]:
        return self._post("/v1/cheat/snapshot", {"name": name})

    def revert(self, name: str) -> dict[str, Any]:
        return self._post("/v1/cheat/revert", {"name": name})

    def align_from_db(
        self,
        *,
        hotkeys: list[str] | None = None,
        json_path: str | None = None,
        database_url: str | None = None,
    ) -> dict[str, Any]:
        return self._post(
            "/v1/cheat/align_from_db",
            {
                "hotkeys": hotkeys or [],
                "json_path": json_path,
                "database_url": database_url,
            },
        )

    def _get(self, path: str) -> dict[str, Any]:
        return self._request("GET", path)

    def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            response = httpx.request(
                method,
                self.base_url + path,
                json=body if method != "GET" else None,
                headers=headers,
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise PreviewClientError(f"preview-control request failed: {exc}") from exc
        if response.status_code >= 400:
            detail = response.text
            try:
                parsed = response.json()
                if isinstance(parsed, dict) and parsed.get("error"):
                    detail = str(parsed["error"])
            except ValueError:
                pass
            raise PreviewClientError(detail)
        try:
            payload = response.json()
        except ValueError as exc:
            raise PreviewClientError("preview-control returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise PreviewClientError("preview-control returned a non-object")
        return payload

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", path, body)
