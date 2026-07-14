"""Contract and collector tests for privacy-bounded system telemetry."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from ditto import system_health
from ditto.api_models.system_health import DockerHealth, SystemMetrics
from ditto.system_health import SystemMetricsCollector


def test_system_metrics_forbid_arbitrary_keys_and_invalid_ranges() -> None:
    payload: dict[str, Any] = {
        "collected_at": 1_752_443_200,
        "cpu_percent": 15,
        "memory_percent": 40,
        "disk_percent": 55,
        "docker": {
            "status": "healthy",
            "running_containers": 4,
            "unhealthy_containers": 0,
        },
    }
    assert SystemMetrics.model_validate(payload).cpu_percent == 15
    with pytest.raises(ValidationError):
        SystemMetrics.model_validate({**payload, "hostname": "private-host"})
    with pytest.raises(ValidationError):
        SystemMetrics.model_validate({**payload, "cpu_percent": 101})
    with pytest.raises(ValidationError):
        SystemMetrics.model_validate({**payload, "memory_percent": 42})
    with pytest.raises(ValidationError):
        SystemMetrics.model_validate({**payload, "disk_percent": "55"})
    with pytest.raises(ValidationError):
        SystemMetrics.model_validate(
            {
                **payload,
                "docker": {**payload["docker"], "running_containers": False},
            }
        )


def test_docker_health_is_aggregate_and_consistent() -> None:
    with pytest.raises(ValidationError):
        DockerHealth(status="healthy", running_containers=2, unhealthy_containers=1)
    with pytest.raises(ValidationError):
        DockerHealth(status="unavailable", running_containers=1, unhealthy_containers=0)


def test_docker_probe_reports_its_own_hardened_container(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        system_health.subprocess,
        "run",
        Mock(side_effect=FileNotFoundError),
    )
    monkeypatch.setattr(system_health, "_running_in_container", lambda: True)

    assert system_health.probe_docker_health() == DockerHealth(
        status="healthy", running_containers=1, unhealthy_containers=0
    )


def test_docker_probe_remains_unavailable_on_an_unobservable_host(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(
        system_health.subprocess,
        "run",
        Mock(return_value=SimpleNamespace(returncode=1, stdout="")),
    )
    monkeypatch.setattr(system_health, "_running_in_container", lambda: False)

    assert system_health.probe_docker_health() == DockerHealth(
        status="unavailable", running_containers=0, unhealthy_containers=0
    )


def test_collector_rounds_and_caches_for_two_minutes() -> None:
    monotonic = iter((0.0, 30.0, 121.0))
    wall_clock = iter((1_752_443_200.0, 1_752_443_321.0))
    cpu = iter((17.4, 96.0))
    memory = iter((SimpleNamespace(percent=91.0), SimpleNamespace(percent=20.0)))
    disk = iter((SimpleNamespace(percent=83.0), SimpleNamespace(percent=10.0)))
    docker = DockerHealth(
        status="healthy", running_containers=4, unhealthy_containers=0
    )
    collector = SystemMetricsCollector(
        monotonic=lambda: next(monotonic),
        wall_clock=lambda: next(wall_clock),
        cpu_percent=lambda: next(cpu),
        virtual_memory=lambda: next(memory),
        disk_usage=lambda _path: next(disk),
        docker_probe=lambda: docker,
    )

    first = collector.collect()
    cached = collector.collect()
    refreshed = collector.collect()

    assert first is cached
    assert first.model_dump() == {
        "collected_at": 1_752_443_200,
        "cpu_percent": 15,
        "memory_percent": 90,
        "disk_percent": 85,
        "docker": {
            "status": "healthy",
            "running_containers": 4,
            "unhealthy_containers": 0,
        },
    }
    assert refreshed.collected_at == 1_752_443_321
    assert refreshed.cpu_percent == 95
