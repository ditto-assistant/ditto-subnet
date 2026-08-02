"""Small, dependency-free client for Targon's v2 workload API.

The API key is intentionally accepted only as an in-memory constructor value.
Callers must obtain it from Secret Manager or a mode-0600 file; this module
never places it in argv, environment variables, exception text, or logs.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, cast


class TargonAPIError(RuntimeError):
    """A redacted Targon API failure safe to emit in controller logs."""

    def __init__(self, *, operation: str, status: int | None, reason: str) -> None:
        self.operation = operation
        self.status = status
        self.reason = reason
        status_text = "transport" if status is None else str(status)
        super().__init__(f"Targon {operation} failed ({status_text}): {reason}")


@dataclass(frozen=True)
class WorkloadSummary:
    uid: str
    name: str
    resource_name: str | None
    status: str | None
    created_at: str | None


class TargonClient:
    """Bounded client for inventory and Rental lifecycle operations."""

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str = "https://api.targon.com/tha/v2",
        timeout_seconds: float = 15.0,
    ) -> None:
        key = api_key.strip() if api_key is not None else None
        self._api_key = key or None
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        authenticated: bool = True,
        expect_text: bool = False,
        retryable: bool = False,
    ) -> Any:
        if authenticated and self._api_key is None:
            raise TargonAPIError(
                operation=f"{method} {path}", status=None, reason="API key unavailable"
            )
        headers = {"Accept": "application/json"}
        if authenticated:
            headers["Authorization"] = f"Bearer {self._api_key}"
        data = None
        if payload is not None:
            data = json.dumps(payload, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self._base_url}{path}", data=data, headers=headers, method=method
        )
        operation = f"{method} {path.split('?', 1)[0]}"
        attempts = 3 if retryable else 1
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(
                    request, timeout=self._timeout_seconds
                ) as response:
                    body = response.read()
                break
            except urllib.error.HTTPError as error:
                transient = error.code == 429 or error.code >= 500
                if transient and attempt + 1 < attempts:
                    time.sleep(0.5 * (2**attempt))
                    continue
                # Never copy provider response bodies: they may echo workload
                # env configuration. Status is enough for operator routing.
                reason = "rate limited" if error.code == 429 else "HTTP error"
                raise TargonAPIError(
                    operation=operation, status=error.code, reason=reason
                ) from None
            except (TimeoutError, urllib.error.URLError, OSError) as error:
                if attempt + 1 < attempts:
                    time.sleep(0.5 * (2**attempt))
                    continue
                raise TargonAPIError(
                    operation=operation,
                    status=None,
                    reason=type(error).__name__,
                ) from None
        if not body:
            return None
        if expect_text:
            return body.decode(errors="replace")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            raise TargonAPIError(
                operation=operation,
                status=None,
                reason="non-JSON response",
            ) from None

    def inventory(self) -> list[dict[str, Any]]:
        value = self._request(
            "GET",
            "/inventory?type=rental&gpu=false",
            authenticated=False,
            retryable=True,
        )
        if not isinstance(value, list):
            raise TargonAPIError(
                operation="GET /inventory", status=None, reason="invalid response shape"
            )
        return value

    def list_workloads(self, *, name: str | None = None) -> list[dict[str, Any]]:
        query = "?limit=1000"
        if name:
            query += "&" + urllib.parse.urlencode({"name": name})
        value = self._request("GET", f"/workloads{query}", retryable=True)
        if not isinstance(value, dict) or not isinstance(value.get("items"), list):
            raise TargonAPIError(
                operation="GET /workloads", status=None, reason="invalid response shape"
            )
        return cast(list[dict[str, Any]], value["items"])

    def create_rental(
        self,
        *,
        name: str,
        image: str,
        resource_name: str,
        envs: list[dict[str, str]] | None = None,
        commands: list[str] | None = None,
        args: list[str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": "RENTAL",
            "name": name,
            "image": image,
            "resource_name": resource_name,
            "ports": [],
        }
        if envs:
            payload["envs"] = envs
        if commands:
            payload["commands"] = commands
        if args:
            payload["args"] = args
        value = self._request("POST", "/workloads", payload=payload)
        if not isinstance(value, dict) or not isinstance(value.get("uid"), str):
            raise TargonAPIError(
                operation="POST /workloads",
                status=None,
                reason="invalid response shape",
            )
        return value

    def deploy(self, uid: str) -> dict[str, Any]:
        value = self._request("POST", f"/workloads/{uid}/deploy")
        return value if isinstance(value, dict) else {}

    def update(self, uid: str, *, envs: list[dict[str, str]]) -> dict[str, Any]:
        value = self._request(
            "PATCH", f"/workloads/{uid}", payload={"envs": envs}, retryable=True
        )
        return value if isinstance(value, dict) else {}

    def state(self, uid: str) -> dict[str, Any]:
        value = self._request("GET", f"/workloads/{uid}/state", retryable=True)
        if not isinstance(value, dict):
            raise TargonAPIError(
                operation="GET /workloads/state",
                status=None,
                reason="invalid response shape",
            )
        return value

    def exec(self, uid: str, command: list[str]) -> str:
        query = urllib.parse.urlencode([("command", part) for part in command])
        value = self._request(
            "POST", f"/workloads/{uid}/exec?{query}", expect_text=True
        )
        return value if isinstance(value, str) else ""

    def logs(self, uid: str, *, tail: int = 100) -> str:
        query = urllib.parse.urlencode({"tail": tail})
        value = self._request(
            "GET",
            f"/workloads/{uid}/logs?{query}",
            expect_text=True,
            retryable=True,
        )
        return value if isinstance(value, str) else ""

    def suspend(self, uid: str) -> dict[str, Any]:
        value = self._request("POST", f"/workloads/{uid}/suspend")
        return value if isinstance(value, dict) else {}

    def delete(self, uid: str) -> None:
        try:
            self._request("DELETE", f"/workloads/{uid}", retryable=True)
        except TargonAPIError as error:
            if error.status != 404:
                raise


def workload_summary(workload: dict[str, Any]) -> WorkloadSummary:
    """Project a workload onto fields that cannot contain environment secrets."""
    resource = workload.get("resource")
    state = workload.get("state")
    return WorkloadSummary(
        uid=str(workload.get("uid", "")),
        name=str(workload.get("name", "")),
        resource_name=(
            str(resource.get("name"))
            if isinstance(resource, dict) and resource.get("name")
            else None
        ),
        status=(
            str(state.get("status"))
            if isinstance(state, dict) and state.get("status")
            else None
        ),
        created_at=(
            str(workload.get("created_at")) if workload.get("created_at") else None
        ),
    )
