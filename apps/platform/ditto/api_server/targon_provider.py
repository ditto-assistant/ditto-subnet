"""Targon Rentals adapter for the three screening lanes."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import httpx

from ditto.api_server.config import TargonRentalConfig
from ditto.api_server.screening_provider import (
    BuildSpec,
    ProvisionObservation,
    ReviewSpec,
    ScreeningProviderError,
    SmokeSpec,
)
from ditto.api_server.targon_client import TargonAPIError

_PROVISION_FAILED = frozenset({"error", "deleted", "suspended"})


async def _https_health(url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{url}/health")
    except httpx.HTTPError:
        return False
    return 200 <= response.status_code < 300


class TargonRentals(Protocol):
    async def inventory(self) -> list[dict[str, Any]]: ...

    async def create_rental(self, **payload: Any) -> dict[str, Any]: ...

    async def deploy(self, uid: str) -> None: ...

    async def state(self, uid: str) -> dict[str, Any]: ...

    async def logs(self, uid: str, *, tail: int = 400) -> str: ...

    async def delete(self, uid: str) -> None: ...


class TargonComputeProvider:
    name = "targon"
    stored_provider = "targon"

    def __init__(
        self,
        targon: TargonRentals,
        config: TargonRentalConfig,
        *,
        health_probe: Callable[[str], Awaitable[bool]] | None = None,
    ) -> None:
        self._targon = targon
        self._config = config
        self._health_probe = health_probe or _https_health

    async def capacity_ok(self) -> bool:
        try:
            rows = await self._targon.inventory()
        except TargonAPIError:
            return False
        available = sum(
            max(0, int(row.get("available", 0)))
            for row in rows
            if str(row.get("name", "")) == self._config.resource
        )
        return available >= 1

    async def create_build(self, spec: BuildSpec) -> str:
        return await self._create(
            name=spec.name,
            image=spec.image,
            env=spec.env,
        )

    async def create_smoke(self, spec: SmokeSpec) -> str:
        payload: dict[str, Any] = {
            "name": spec.name,
            "image": spec.image,
            "resource_name": self._config.resource,
            "envs": [{"name": key, "value": value} for key, value in spec.env],
            "ports": [{"port": 8080, "protocol": "TCP", "routing": "PROXIED"}],
        }
        if spec.registry_auth is not None:
            payload["registry_auth"] = spec.registry_auth
        return await self._create_payload(payload)

    async def create_source_review(self, spec: ReviewSpec) -> str:
        return await self._create(
            name=spec.name,
            image=spec.image,
            env=spec.env,
            commands=list(spec.commands),
            args=list(spec.args),
        )

    async def start(self, resource_id: str) -> None:
        try:
            await self._targon.deploy(resource_id)
        except TargonAPIError as error:
            raise ScreeningProviderError(
                provider=self.name,
                operation="deploy",
                reason=type(error).__name__,
            ) from error

    async def provision_status(self, resource_id: str) -> str:
        return (await self.observe_provision(resource_id)).status

    async def observe_provision(self, resource_id: str) -> ProvisionObservation:
        try:
            state = await self._targon.state(resource_id)
        except TargonAPIError:
            return ProvisionObservation(status="")
        return ProvisionObservation(
            status=str(state.get("status", "")).casefold(),
            message=str(state.get("message", "") or ""),
        )

    async def replica_logs(self, resource_id: str, *, tail: int = 400) -> str:
        try:
            return await self._targon.logs(resource_id, tail=tail)
        except TargonAPIError:
            return ""

    async def wait_until_running(self, resource_id: str, timeout_seconds: float) -> str:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while True:
            status = await self.provision_status(resource_id)
            if status == "running":
                return "running"
            if status in _PROVISION_FAILED:
                return "error"
            remaining = deadline - loop.time()
            if remaining <= 0:
                return "timeout"
            await asyncio.sleep(min(1.0, remaining))

    async def probe_smoke(self, resource_id: str, *, timeout_seconds: float) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while loop.time() < deadline:
            try:
                state = await self._targon.state(resource_id)
            except TargonAPIError:
                await asyncio.sleep(1)
                continue
            urls = state.get("urls")
            if isinstance(urls, list):
                for row in urls:
                    if not isinstance(row, dict) or row.get("port") != 8080:
                        continue
                    url = str(row.get("url", "")).rstrip("/")
                    if url.startswith("https://") and await self._health_probe(url):
                        return True
            if str(state.get("status", "")).casefold() == "error":
                return False
            await asyncio.sleep(1)
        return False

    async def delete(self, resource_id: str) -> bool:
        try:
            await self._targon.delete(resource_id)
        except TargonAPIError:
            return False
        return True

    async def _create(
        self,
        *,
        name: str,
        image: str,
        env: tuple[tuple[str, str], ...],
        commands: list[str] | None = None,
        args: list[str] | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "name": name,
            "image": image,
            "resource_name": self._config.resource,
            "envs": [{"name": key, "value": value} for key, value in env],
        }
        if commands is not None:
            payload["commands"] = commands
        if args is not None:
            payload["args"] = args
        return await self._create_payload(payload)

    async def _create_payload(self, payload: dict[str, Any]) -> str:
        try:
            created = await self._targon.create_rental(**payload)
            return str(created["uid"])
        except (TargonAPIError, KeyError, ValueError) as error:
            raise ScreeningProviderError(
                provider=self.name,
                operation="create",
                reason=type(error).__name__,
            ) from error
