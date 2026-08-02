"""Allowlisted, coarse host telemetry shared by fleet heartbeat contracts."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DockerHealthStatus = Literal["healthy", "degraded", "unavailable"]


class DockerHealth(BaseModel):
    """Aggregate Docker health without container identities or image metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

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

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    collected_at: Annotated[int, Field(ge=0, description="Unix sample time (UTC).")]
    cpu_percent: Annotated[int, Field(ge=0, le=100, multiple_of=5)]
    memory_percent: Annotated[int, Field(ge=0, le=100, multiple_of=5)]
    disk_percent: Annotated[int, Field(ge=0, le=100, multiple_of=5)]
    docker: DockerHealth


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
