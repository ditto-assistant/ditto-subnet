"""Allowlisted, coarse host telemetry shared by fleet heartbeat contracts."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DockerHealthStatus = Literal["healthy", "degraded", "unavailable"]


class DockerHealth(BaseModel):
    """Aggregate Docker health without container identities or image metadata."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    status: DockerHealthStatus
    running_containers: Annotated[int, Field(ge=0, le=1000)]
    unhealthy_containers: Annotated[int, Field(ge=0, le=1000)]

    @model_validator(mode="after")
    def unhealthy_cannot_exceed_running(self) -> DockerHealth:
        if self.unhealthy_containers > self.running_containers:
            raise ValueError("unhealthy_containers cannot exceed running_containers")
        if self.status == "healthy" and self.unhealthy_containers:
            raise ValueError("healthy Docker status cannot report unhealthy containers")
        if self.status == "degraded" and self.unhealthy_containers == 0:
            raise ValueError("degraded Docker status requires an unhealthy container")
        if self.status == "unavailable" and (
            self.running_containers or self.unhealthy_containers
        ):
            raise ValueError("unavailable Docker status cannot report container counts")
        return self


class SystemMetrics(BaseModel):
    """One bounded and intentionally coarse host-health sample."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    collected_at: Annotated[int, Field(ge=0, description="Unix sample time (UTC).")]
    cpu_percent: Annotated[int, Field(ge=0, le=100, multiple_of=5)]
    memory_percent: Annotated[int, Field(ge=0, le=100, multiple_of=5)]
    disk_percent: Annotated[int, Field(ge=0, le=100, multiple_of=5)]
    docker: DockerHealth


# Machine architecture as reported by ``platform.machine()`` on the reporting
# host. Excludes ':' and ',' so it can never break a signing message.
_ARCHITECTURE_PATTERN = r"^[a-zA-Z0-9_.-]{1,32}$"


class HostSpecs(BaseModel):
    """What a fleet host is built from, not what it is doing right now.

    ``SystemMetrics`` is a coarse load sample; this is the fixed hardware the
    worker announces once per boot. Mirrors ``ditto_screener.heartbeat``.
    """

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    cpu_count: Annotated[
        int, Field(ge=1, le=1024, description="Logical CPUs visible to the worker.")
    ]
    cpu_physical_cores: (
        Annotated[
            int,
            Field(
                ge=1, le=1024, description="Physical cores, when the kernel reports."
            ),
        ]
        | None
    ) = None
    memory_total_mib: Annotated[
        int, Field(ge=1, le=1 << 24, description="Total RAM in MiB.")
    ]
    disk_total_gib: Annotated[
        int, Field(ge=1, le=1 << 20, description="Total size of / in GiB.")
    ]
    architecture: Annotated[
        str, Field(pattern=_ARCHITECTURE_PATTERN, description="e.g. x86_64, aarch64.")
    ]

    @model_validator(mode="after")
    def physical_cores_within_logical(self) -> HostSpecs:
        if (
            self.cpu_physical_cores is not None
            and self.cpu_physical_cores > self.cpu_count
        ):
            raise ValueError("cpu_physical_cores cannot exceed cpu_count")
        return self


def host_specs_signing_token(specs: HostSpecs | None) -> str:
    """Return an unambiguous bounded token for a heartbeat signature payload."""
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


def system_metrics_signing_token(metrics: SystemMetrics | None) -> str:
    """Return an unambiguous bounded token for a heartbeat signature payload."""
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


def host_specs_from_heartbeat_envelope(raw: dict | None) -> HostSpecs | None:
    """Revalidate announced hardware out of a stored heartbeat telemetry blob.

    Legacy rows store the bare metrics object and pre-v6 rows store an envelope
    with no ``host_specs`` key; both simply have nothing to announce. Anything
    that no longer validates stays private rather than reaching an operator as
    an unchecked number.
    """
    if not isinstance(raw, dict):
        return None
    value = raw.get("host_specs")
    if not isinstance(value, dict):
        return None
    try:
        return HostSpecs.model_validate(value)
    except Exception:  # noqa: BLE001 - malformed historical rows stay private
        return None
