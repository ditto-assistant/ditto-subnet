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


def test_system_metrics_ignore_arbitrary_keys_and_reject_invalid_ranges() -> None:
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
    parsed = SystemMetrics.model_validate({**payload, "hostname": "private-host"})
    assert parsed.cpu_percent == 15
    assert "hostname" not in parsed.model_dump()
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


@pytest.mark.parametrize(
    "probe_result",
    (
        Mock(side_effect=OSError("docker probe failed")),
        Mock(side_effect=system_health.subprocess.TimeoutExpired("docker", 2.0)),
        Mock(return_value=SimpleNamespace(returncode=1, stdout="")),
    ),
)
def test_docker_probe_failure_remains_unavailable_inside_container(
    monkeypatch: Any, probe_result: Mock
) -> None:
    monkeypatch.setattr(system_health.subprocess, "run", probe_result)
    monkeypatch.setattr(system_health, "_running_in_container", lambda: True)

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


def test_host_specs_describe_fixed_hardware_in_whole_units() -> None:
    def cpu_count(logical: bool = True) -> int:
        return 16 if logical else 8

    specs = system_health.collect_host_specs(
        cpu_count=cpu_count,
        virtual_memory=lambda: SimpleNamespace(total=64 * 1024**3 + 5),
        disk_usage=lambda _path: SimpleNamespace(total=500 * 1024**3),
        machine=lambda: "x86_64",
    )

    assert specs is not None
    assert specs.model_dump() == {
        "cpu_count": 16,
        "cpu_physical_cores": 8,
        "memory_total_mib": 65_536,
        "disk_total_gib": 500,
        "architecture": "x86_64",
    }


def test_host_specs_probe_failure_is_omitted_not_raised() -> None:
    def broken() -> Any:
        raise OSError("no /proc")

    assert system_health.collect_host_specs(virtual_memory=broken) is None


def test_collector_probes_host_specs_once_per_process() -> None:
    probe = Mock(return_value=None)
    collector = SystemMetricsCollector(host_specs_probe=probe)

    assert collector.host_specs() is None
    assert collector.host_specs() is None
    probe.assert_called_once_with()


def test_host_specs_unknown_cpu_count_is_omitted_not_one() -> None:
    assert (
        system_health.collect_host_specs(
            cpu_count=lambda **_kwargs: None,
            virtual_memory=lambda: SimpleNamespace(total=1024**3),
            disk_usage=lambda _path: SimpleNamespace(total=1024**3),
            machine=lambda: "x86_64",
        )
        is None
    )
