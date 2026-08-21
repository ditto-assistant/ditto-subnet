"""HTTP client for preview-control cheatcodes."""

from __future__ import annotations

from typing import Any

import httpx


class PreviewClient:
    """Talk to a running preview-control server."""

    def __init__(self, base_url: str, timeout: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

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
        response = httpx.get(self.base_url + path, timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("preview-control returned a non-object")
        return payload

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        response = httpx.post(self.base_url + path, json=body, timeout=self.timeout)
        if response.status_code >= 400:
            detail = response.text
            try:
                parsed = response.json()
                if isinstance(parsed, dict) and parsed.get("error"):
                    detail = str(parsed["error"])
            except ValueError:
                pass
            raise RuntimeError(detail)
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("preview-control returned a non-object")
        return payload
