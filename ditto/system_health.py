"""Privacy-bounded host telemetry for validator heartbeats."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable
from typing import Any

import psutil

from ditto.api_models.system_health import DockerHealth, SystemMetrics

SYSTEM_METRICS_SAMPLE_SECONDS = 120.0


def _coarse_percent(value: float) -> int:
    """Clamp and round a percentage to five-point buckets."""
    bounded = min(100.0, max(0.0, float(value)))
    return min(100, int((bounded + 2.5) // 5) * 5)


def probe_docker_health() -> DockerHealth:
    """Read aggregate running-container health without names or image metadata."""
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
            env={"PATH": os.environ.get("PATH", "")},
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


class SystemMetricsCollector:
    """Cache one coarse, allowlisted sample for a two-minute reporting cadence."""

    def __init__(
        self,
        *,
        sample_seconds: float = SYSTEM_METRICS_SAMPLE_SECONDS,
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
        """Return a fresh sample when due, otherwise the cached coarse sample."""
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
