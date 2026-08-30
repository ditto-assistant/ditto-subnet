"""Privacy-bounded screener fleet heartbeat models and host sampling."""

from __future__ import annotations

import os
import platform
import re
import subprocess
import time
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

import psutil
from pydantic import BaseModel, ConfigDict, Field, model_validator

DockerHealthStatus = Literal["healthy", "degraded", "unavailable"]
ScreenerRuntimeState = Literal["polling", "screening", "error", "paused"]
ScreenerProgressStage = Literal[
    "preparing",
    "downloading",
    "validating",
    "building",
    "starting",
    "health_check",
    "source_review_0",
    "source_review_10",
    "source_review_20",
    "source_review_30",
    "source_review_40",
    "source_review_50",
    "source_review_60",
    "source_review_70",
    "source_review_80",
    "source_review_90",
    "source_review_100",
    "submitting",
]
_SOURCE_REVIEW_PROGRESS_STAGES: tuple[ScreenerProgressStage, ...] = (
    "source_review_0",
    "source_review_10",
    "source_review_20",
    "source_review_30",
    "source_review_40",
    "source_review_50",
    "source_review_60",
    "source_review_70",
    "source_review_80",
    "source_review_90",
    "source_review_100",
)
_SS58_PATTERN = r"^[1-9A-HJ-NP-Za-km-z]{47,48}$"
_SIGNATURE_HEX_PATTERN = r"^[0-9a-fA-F]{128}$"
_SOFTWARE_VERSION_PATTERN = r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$"
# Per-instance identity (v3). Excludes ':' so it can never break the
# colon-delimited heartbeat signing message. GCE hostnames fit RFC1035.
_INSTANCE_ID_PATTERN = r"^[a-zA-Z0-9._-]{1,63}$"
# Machine architecture as reported by ``platform.machine()``. Excludes ':'
# and ',' so it can never break the heartbeat signing message.
_ARCHITECTURE_PATTERN = r"^[a-zA-Z0-9_.-]{1,32}$"
_SYSTEM_METRICS_SAMPLE_SECONDS = 120.0


class DockerHealth(BaseModel):
    """Aggregate Docker health without names or image metadata."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    status: DockerHealthStatus
    running_containers: Annotated[int, Field(ge=0, le=1000)]
    unhealthy_containers: Annotated[int, Field(ge=0, le=1000)]

    @model_validator(mode="after")
    def validate_counts(self) -> DockerHealth:
        if self.unhealthy_containers > self.running_containers:
            raise ValueError("unhealthy containers cannot exceed running containers")
        if self.status == "healthy" and self.unhealthy_containers:
            raise ValueError("healthy Docker cannot report unhealthy containers")
        if self.status == "degraded" and not self.unhealthy_containers:
            raise ValueError("degraded Docker requires an unhealthy container")
        if self.status == "unavailable" and (
            self.running_containers or self.unhealthy_containers
        ):
            raise ValueError("unavailable Docker cannot report container counts")
        return self


class SystemMetrics(BaseModel):
    """One bounded and intentionally coarse host-health sample."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    collected_at: Annotated[int, Field(ge=0)]
    cpu_percent: Annotated[int, Field(ge=0, le=100, multiple_of=5)]
    memory_percent: Annotated[int, Field(ge=0, le=100, multiple_of=5)]
    disk_percent: Annotated[int, Field(ge=0, le=100, multiple_of=5)]
    docker: DockerHealth


class HostSpecs(BaseModel):
    """What the screener host is built from, not what it is doing right now.

    ``SystemMetrics`` answers "is this worker under load"; this answers "how
    big is this worker". Specs are fixed for the life of a boot, so the worker
    samples them once at startup: a resized VM announces its new shape on its
    next restart rather than drifting mid-heartbeat.
    """

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    cpu_count: Annotated[int, Field(ge=1, le=1024)]
    # psutil returns None for physical cores on some kernels and containers.
    cpu_physical_cores: Annotated[int, Field(ge=1, le=1024)] | None = None
    memory_total_mib: Annotated[int, Field(ge=1, le=1 << 24)]
    disk_total_gib: Annotated[int, Field(ge=1, le=1 << 20)]
    architecture: Annotated[str, Field(pattern=_ARCHITECTURE_PATTERN)]

    @model_validator(mode="after")
    def physical_cores_within_logical(self) -> HostSpecs:
        if (
            self.cpu_physical_cores is not None
            and self.cpu_physical_cores > self.cpu_count
        ):
            raise ValueError("cpu_physical_cores cannot exceed cpu_count")
        return self


class ScreenerProgress(BaseModel):
    """Small, public-safe description of an active screening job."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    stage: ScreenerProgressStage
    started_at: Annotated[int, Field(ge=0)]


class ReviewSettingsStatus(BaseModel):
    """Secret-free settings revision actually active for the next lease."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    revision: Annotated[int, Field(ge=0)]
    scope: Annotated[str, Field(pattern=r"^(?:bootstrap|\*|[a-zA-Z0-9._-]{1,63})$")]
    mode: Literal["off", "shadow", "enforce"]
    checksum: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    source: Literal["platform", "cache", "bootstrap"]
    policy_manifest_profile: Literal["core", "l1", "l1_l2"] = "l1"
    policy_manifest_rotation_id: Annotated[
        str, Field(pattern=r"^[a-zA-Z0-9._-]{1,80}$")
    ] = "policy-v10-l1"
    policy_manifest_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] = "0" * 64


class ScreenerHeartbeatRequest(BaseModel):
    """Dedicated screener identity, work, and optional coarse host health."""

    model_config = ConfigDict(extra="ignore")

    screener_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    software_version: Annotated[str, Field(pattern=_SOFTWARE_VERSION_PATTERN)]
    protocol_version: Annotated[int, Field(ge=1, le=2**31 - 1)]
    policy_version: Annotated[int, Field(ge=1, le=2**31 - 1)]
    state: ScreenerRuntimeState
    active_agent_id: UUID | None = None
    instance_id: Annotated[str, Field(pattern=_INSTANCE_ID_PATTERN)] | None = None
    progress: ScreenerProgress | None = None
    system_metrics: SystemMetrics | None = None
    review_settings: ReviewSettingsStatus | None = None
    host_specs: HostSpecs | None = None
    timestamp: Annotated[int, Field(ge=0)]
    signature: Annotated[str, Field(pattern=_SIGNATURE_HEX_PATTERN)]

    @model_validator(mode="after")
    def validate_instance_id(self) -> ScreenerHeartbeatRequest:
        if self.protocol_version >= 3 and not self.instance_id:
            raise ValueError("heartbeat protocol v3 requires an instance_id")
        return self

    @model_validator(mode="after")
    def validate_progress(self) -> ScreenerHeartbeatRequest:
        if self.progress is None:
            return self
        if self.protocol_version < 2:
            raise ValueError("progress requires heartbeat protocol v2")
        if self.state != "screening" or self.active_agent_id is None:
            raise ValueError("progress requires active screening work")
        if self.progress.started_at > self.timestamp:
            raise ValueError("progress start cannot be after the heartbeat")
        if self.timestamp - self.progress.started_at > 6 * 60 * 60:
            raise ValueError("progress start is outside the bounded job window")
        return self

    @model_validator(mode="after")
    def validate_review_settings(self) -> ScreenerHeartbeatRequest:
        if self.protocol_version >= 4 and self.review_settings is None:
            raise ValueError("heartbeat protocol v4 requires review settings status")
        if self.protocol_version < 4 and self.review_settings is not None:
            raise ValueError("review settings status requires heartbeat protocol v4")
        return self

    @model_validator(mode="after")
    def validate_host_specs(self) -> ScreenerHeartbeatRequest:
        if self.protocol_version >= 6 and self.host_specs is None:
            raise ValueError("heartbeat protocol v6 requires host specs")
        if self.protocol_version < 6 and self.host_specs is not None:
            raise ValueError("host specs require heartbeat protocol v6")
        return self


def review_settings_signing_token(
    settings: ReviewSettingsStatus | None, *, protocol_version: int = 4
) -> str:
    if settings is None:
        return "-"
    fields = [
        str(settings.revision),
        settings.scope,
        settings.mode,
        settings.checksum,
        settings.source,
    ]
    if protocol_version >= 5:
        fields.extend(
            (
                settings.policy_manifest_profile,
                settings.policy_manifest_rotation_id,
                settings.policy_manifest_digest,
            )
        )
    return ",".join(fields)


class ScreenerHeartbeatResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    accepted: bool
    seen_at: datetime
    lease_deadline: datetime | None = None


def source_review_progress_stage(
    completed_steps: int, total_steps: int
) -> ScreenerProgressStage:
    """Map private reviewer turns to a coarse public-safe progress bucket."""
    if total_steps <= 0:
        raise ValueError("source review total_steps must be positive")
    completed = min(total_steps, max(0, completed_steps))
    bucket = min(10, (completed * 10 + total_steps - 1) // total_steps)
    return _SOURCE_REVIEW_PROGRESS_STAGES[bucket]


def system_metrics_signing_token(metrics: SystemMetrics | None) -> str:
    """Return the exact bounded token accepted by the platform."""
    if metrics is None:
        return "-"
    docker = metrics.docker
    return ",".join(
        str(value)
        for value in (
            metrics.collected_at,
            metrics.cpu_percent,
            metrics.memory_percent,
            metrics.disk_percent,
            docker.status,
            docker.running_containers,
            docker.unhealthy_containers,
        )
    )


def host_specs_signing_token(specs: HostSpecs | None) -> str:
    """Return the canonical v6 token for the announced hardware allowlist."""
    if specs is None:
        return "-"
    return ",".join(
        str(value)
        for value in (
            specs.cpu_count,
            specs.cpu_physical_cores if specs.cpu_physical_cores is not None else "-",
            specs.memory_total_mib,
            specs.disk_total_gib,
            specs.architecture,
        )
    )


def screener_progress_signing_token(progress: ScreenerProgress | None) -> str:
    """Return the canonical v2 token for the optional progress allowlist."""
    if progress is None:
        return "-"
    return f"{progress.stage},{progress.started_at}"


def _coarse_percent(value: float) -> int:
    bounded = min(100.0, max(0.0, float(value)))
    return min(100, int((bounded + 2.5) // 5) * 5)


def probe_docker_health() -> DockerHealth:
    """Read aggregate running-container health without identifying metadata."""
    docker_env = {"PATH": os.environ.get("PATH", "")}
    if docker_host := os.environ.get("DOCKER_HOST"):
        # The fleet uses a dedicated rootless daemon. Preserve only its socket
        # selector; do not copy secrets from the worker environment.
        docker_env["DOCKER_HOST"] = docker_host
    try:
        result = subprocess.run(
            [
                "docker",
                "container",
                "ls",
                "--filter",
                "status=running",
                "--format",
                "{{.Status}}",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=2.0,
            env=docker_env,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return DockerHealth(
            status="unavailable", running_containers=0, unhealthy_containers=0
        )
    if result.returncode != 0:
        return DockerHealth(
            status="unavailable", running_containers=0, unhealthy_containers=0
        )
    statuses = result.stdout.splitlines()[:1000]
    unhealthy = sum("(unhealthy)" in status.lower() for status in statuses)
    return DockerHealth(
        status="degraded" if unhealthy else "healthy",
        running_containers=len(statuses),
        unhealthy_containers=unhealthy,
    )


_MIB = 1024**2
_GIB = 1024**3


def _sanitized_architecture(machine: str) -> str:
    """Coerce ``platform.machine()`` into the signed architecture charset.

    Sanitized rather than validated: an unfamiliar arch string should cost the
    architecture label, never the CPU/RAM/disk numbers next to it.
    """
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]", "-", machine or "")[:32].strip("-")
    return cleaned or "unknown"


def collect_host_specs(
    *,
    cpu_count: Callable[..., int | None] = psutil.cpu_count,
    virtual_memory: Callable[[], Any] = psutil.virtual_memory,
    disk_usage: Callable[[str], Any] = psutil.disk_usage,
    machine: Callable[[], str] = platform.machine,
) -> HostSpecs | None:
    """Describe this host once, or return None if it cannot be described.

    Announcing specs must never cost a heartbeat: every probe here is on the
    fleet-health path, not the screening path, so an unreadable /proc or an
    exotic architecture degrades the worker back to protocol v5 instead of
    dropping the report that carries its liveness.
    """
    try:
        logical = cpu_count()
        physical = cpu_count(logical=False)
        specs = HostSpecs(
            cpu_count=int(logical or 1),
            cpu_physical_cores=int(physical) if physical else None,
            memory_total_mib=max(1, int(virtual_memory().total) // _MIB),
            disk_total_gib=max(1, int(disk_usage("/").total) // _GIB),
            architecture=_sanitized_architecture(machine()),
        )
    except Exception:  # noqa: BLE001 - fleet telemetry never blocks screening
        return None
    return specs


class SystemMetricsCollector:
    """Cache an allowlisted five-point sample for two minutes."""

    def __init__(
        self,
        *,
        sample_seconds: float = _SYSTEM_METRICS_SAMPLE_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        cpu_percent: Callable[[], float] | None = None,
        virtual_memory: Callable[[], Any] = psutil.virtual_memory,
        disk_usage: Callable[[str], Any] = psutil.disk_usage,
        docker_probe: Callable[[], DockerHealth] = probe_docker_health,
    ) -> None:
        self._sample_seconds = sample_seconds
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._cpu_percent = cpu_percent or (lambda: psutil.cpu_percent(interval=0.1))
        self._virtual_memory = virtual_memory
        self._disk_usage = disk_usage
        self._docker_probe = docker_probe
        self._last_sampled = float("-inf")
        self._cached: SystemMetrics | None = None

    def collect(self) -> SystemMetrics:
        now = self._monotonic()
        if self._cached is not None and now - self._last_sampled < self._sample_seconds:
            return self._cached
        sample = SystemMetrics(
            collected_at=int(self._wall_clock()),
            cpu_percent=_coarse_percent(self._cpu_percent()),
            memory_percent=_coarse_percent(self._virtual_memory().percent),
            disk_percent=_coarse_percent(self._disk_usage("/").percent),
            docker=self._docker_probe(),
        )
        self._cached = sample
        self._last_sampled = now
        return sample


__all__ = [
    "DockerHealth",
    "HostSpecs",
    "ScreenerHeartbeatRequest",
    "ScreenerHeartbeatResponse",
    "ScreenerProgress",
    "ScreenerProgressStage",
    "ScreenerRuntimeState",
    "SystemMetrics",
    "SystemMetricsCollector",
    "collect_host_specs",
    "host_specs_signing_token",
    "screener_progress_signing_token",
    "system_metrics_signing_token",
]
