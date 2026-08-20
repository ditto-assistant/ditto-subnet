"""Three-lane screening compute providers (Kaniko, runtime smoke, L1).

Targon Rentals is primary. Cloud Run is the GCP fallback so a Targon outage
does not stall screening: Jobs for one-shot Kaniko and L1, and a short-lived
internal Service for the miner ``/health`` probe.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class ScreeningProviderError(RuntimeError):
    def __init__(self, *, provider: str, operation: str, reason: str) -> None:
        self.provider = provider
        self.operation = operation
        self.reason = reason
        super().__init__(f"{provider} {operation} failed: {reason}")


@dataclass(frozen=True)
class BuildSpec:
    name: str
    image: str
    env: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class SmokeSpec:
    name: str
    image: str
    env: tuple[tuple[str, str], ...]
    registry_auth: dict[str, str] | None = None


@dataclass(frozen=True)
class ReviewSpec:
    name: str
    image: str
    env: tuple[tuple[str, str], ...]
    commands: tuple[str, ...]
    args: tuple[str, ...]


class ScreeningComputeProvider(Protocol):
    """One execution backend for the three one-shot screening lanes."""

    name: str
    stored_provider: str

    async def capacity_ok(self) -> bool: ...

    async def create_build(self, spec: BuildSpec) -> str: ...

    async def create_smoke(self, spec: SmokeSpec) -> str: ...

    async def create_source_review(self, spec: ReviewSpec) -> str: ...

    async def start(self, resource_id: str) -> None: ...

    async def provision_status(self, resource_id: str) -> str: ...

    async def wait_until_running(self, resource_id: str, timeout_seconds: float) -> str:
        """Return ``running``, ``error``, or ``timeout``."""

    async def probe_smoke(
        self,
        resource_id: str,
        *,
        timeout_seconds: float,
    ) -> bool: ...

    async def delete(self, resource_id: str) -> bool: ...


def provision_error_code(stored_provider: str, result: str) -> str:
    prefix = "TARGON" if stored_provider == "targon" else "CLOUDRUN"
    if result == "timeout":
        return f"{prefix}_PROVISION_TIMEOUT"
    return f"{prefix}_PROVISION_ERROR"
