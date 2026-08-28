"""Three-lane screening compute providers (Kaniko, runtime smoke, L1).

Backroom selects one provider for each lane. A failed provider operation is
terminal for that screening attempt; Platform never fails over automatically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

_SUBMISSION_EXIT_CODE = re.compile(
    r"(?:^|\D)exit(?: code)?[:\s(]+(71|72|73|74|75|76)(?:\D|$)"
)
_SUBMISSION_STAGE_MARKER = re.compile(
    r"DITTO_SUBMISSION_BUILD_FAILED=(SOURCE|KANIKO|ARCHIVE|UPLOAD|COMPLETE|CONTRACT)"
)
_SUBMISSION_STAGE_BY_EXIT_CODE = {
    "71": "SOURCE",
    "72": "KANIKO",
    "73": "ARCHIVE",
    "74": "UPLOAD",
    "75": "COMPLETE",
    "76": "CONTRACT",
}
_PROVIDER_TERMINAL = frozenset({"error", "deleted", "suspended"})


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


@dataclass(frozen=True)
class ProvisionObservation:
    """Latest provider replica status, plus any public Targon state message."""

    status: str
    message: str = ""
    ready: bool | None = None
    """Replica readiness when the provider exposes it.

    ``None`` means the provider has no separate readiness signal.  Targon
    reports ``status=running`` before it owns a replica, so its adapter sets
    this explicitly and callers must not treat ``running`` plus ``False`` as
    provisioned.
    """


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

    async def observe_provision(self, resource_id: str) -> ProvisionObservation:
        """Return status plus any public replica message (exit code, etc.)."""

    async def wait_until_running(self, resource_id: str, timeout_seconds: float) -> str:
        """Return ``running``, ``error``, or ``timeout``."""

    async def probe_smoke(
        self,
        resource_id: str,
        *,
        timeout_seconds: float,
    ) -> bool: ...

    async def delete(self, resource_id: str) -> bool: ...

    async def replica_logs(self, resource_id: str, *, tail: int = 400) -> str: ...


def provision_error_code(stored_provider: str, result: str) -> str:
    prefix = "TARGON" if stored_provider == "targon" else "CLOUDRUN"
    if result == "timeout":
        return f"{prefix}_PROVISION_TIMEOUT"
    return f"{prefix}_PROVISION_ERROR"


def inflight_failure_code(stored_provider: str, status: str, message: str = "") -> str:
    """Classify a dead or never-running replica.

    Submission-builder exit codes 71-76 are the same public contract the
    dedicated orchestrator builder maps. A generic Targon ``error`` without
    those codes stays ``TARGON_PROVISION_ERROR``.
    """
    if status in _PROVIDER_TERMINAL:
        marker = _SUBMISSION_STAGE_MARKER.search(message)
        if marker is not None:
            prefix = "TARGON" if stored_provider == "targon" else "CLOUDRUN"
            return f"{prefix}_SUBMISSION_{marker.group(1)}_FAILED"
        match = _SUBMISSION_EXIT_CODE.search(message)
        if match is not None:
            prefix = "TARGON" if stored_provider == "targon" else "CLOUDRUN"
            stage = _SUBMISSION_STAGE_BY_EXIT_CODE[match.group(1)]
            return f"{prefix}_SUBMISSION_{stage}_FAILED"
        return provision_error_code(stored_provider, "error")
    return provision_error_code(stored_provider, "timeout")
