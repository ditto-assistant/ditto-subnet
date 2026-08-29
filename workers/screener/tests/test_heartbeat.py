"""Privacy and bounds for optional fleet-health reporting."""

from __future__ import annotations

import os
from collections.abc import Callable
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from ditto_screener.heartbeat import (
    DockerHealth,
    HostSpecs,
    ScreenerHeartbeatRequest,
    ScreenerProgress,
    SystemMetricsCollector,
    collect_host_specs,
    probe_docker_health,
    source_review_progress_stage,
)

_HOTKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
_AGENT = "550e8400-e29b-41d4-a716-446655440000"


@pytest.mark.parametrize(
    "stage",
    [
        "preparing",
        "downloading",
        "validating",
        "building",
        "starting",
        "health_check",
        "source_review_0",
        "source_review_50",
        "source_review_100",
        "submitting",
    ],
)
def test_v2_accepts_every_public_progress_stage(stage: str) -> None:
    heartbeat = ScreenerHeartbeatRequest.model_validate(
        {
            "screener_hotkey": _HOTKEY,
            "software_version": "0.2.0",
            "protocol_version": 2,
            "policy_version": 6,
            "state": "screening",
            "active_agent_id": _AGENT,
            "progress": {"stage": stage, "started_at": 100},
            "timestamp": 120,
            "signature": "ab" * 64,
        }
    )
    assert heartbeat.progress == ScreenerProgress(stage=stage, started_at=100)


@pytest.mark.parametrize(
    ("completed", "total", "expected"),
    [
        (0, 10, "source_review_0"),
        (1, 10, "source_review_10"),
        (3, 20, "source_review_20"),
        (10, 10, "source_review_100"),
        (30, 10, "source_review_100"),
    ],
)
def test_source_review_progress_is_coarse_and_bounded(
    completed: int, total: int, expected: str
) -> None:
    assert source_review_progress_stage(completed, total) == expected


@pytest.mark.parametrize(
    "overrides",
    [
        {"progress": {"stage": "docker_layer", "started_at": 100}},
        {"progress": {"stage": "building", "started_at": 121}},
        {"protocol_version": 1, "progress": {"stage": "building", "started_at": 100}},
        {"state": "polling", "progress": {"stage": "building", "started_at": 100}},
        {"active_agent_id": None, "progress": {"stage": "building", "started_at": 100}},
        {"timestamp": 21602, "progress": {"stage": "building", "started_at": 1}},
    ],
)
def test_progress_rejects_invalid_or_unbounded_fields(overrides: dict) -> None:
    payload = {
        "screener_hotkey": _HOTKEY,
        "software_version": "0.2.0",
        "protocol_version": 2,
        "policy_version": 6,
        "state": "screening",
        "active_agent_id": _AGENT,
        "progress": {"stage": "building", "started_at": 100},
        "timestamp": 120,
        "signature": "ab" * 64,
    }
    payload.update(overrides)
    with pytest.raises(ValidationError):
        ScreenerHeartbeatRequest.model_validate(payload)


def test_collector_rounds_and_caches_without_identifying_metadata() -> None:
    times = iter((10.0, 20.0, 200.0))
    collector = SystemMetricsCollector(
        monotonic=lambda: next(times),
        wall_clock=lambda: 123.0,
        cpu_percent=lambda: 12.6,
        virtual_memory=lambda: SimpleNamespace(percent=41.9),
        disk_usage=lambda _path: SimpleNamespace(percent=57.4),
        docker_probe=lambda: DockerHealth(
            status="healthy", running_containers=4, unhealthy_containers=0
        ),
    )
    first = collector.collect()
    assert collector.collect() is first
    refreshed = collector.collect()
    assert (first.cpu_percent, first.memory_percent, first.disk_percent) == (
        15,
        40,
        55,
    )
    assert refreshed.model_dump() == first.model_dump()
    assert set(first.model_dump()) == {
        "collected_at",
        "cpu_percent",
        "memory_percent",
        "disk_percent",
        "docker",
    }


def test_docker_probe_preserves_only_rootless_socket_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_run(*_args: object, **kwargs: object) -> SimpleNamespace:
        observed.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="Up 5 minutes\n", stderr="")

    monkeypatch.setenv("DOCKER_HOST", "unix:///run/ditto-screener-docker/docker.sock")
    monkeypatch.setenv("SCREENER_API_TOKEN", "must-not-reach-docker-cli")
    monkeypatch.setattr("ditto_screener.heartbeat.subprocess.run", fake_run)

    health = probe_docker_health()

    assert health.status == "healthy"
    assert health.running_containers == 1
    assert observed["env"] == {
        "PATH": os.environ.get("PATH", ""),
        "DOCKER_HOST": "unix:///run/ditto-screener-docker/docker.sock",
    }


def test_heartbeat_drops_arbitrary_private_host_fields() -> None:
    heartbeat = ScreenerHeartbeatRequest.model_validate(
        {
            "screener_hotkey": "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
            "software_version": "0.1.0",
            "protocol_version": 1,
            "policy_version": 6,
            "state": "polling",
            "timestamp": 1,
            "signature": "ab" * 64,
            "hostname": "must-not-leave-the-host",
        }
    )
    assert "hostname" not in heartbeat.model_dump()


def test_v4_requires_bounded_review_settings_status() -> None:
    payload = {
        "screener_hotkey": _HOTKEY,
        "software_version": "0.14.1",
        "protocol_version": 4,
        "policy_version": 9,
        "state": "polling",
        "instance_id": "ditto-screener-prod",
        "timestamp": 120,
        "signature": "ab" * 64,
        "review_settings": {
            "revision": 42,
            "scope": "ditto-screener-prod",
            "mode": "shadow",
            "checksum": "cd" * 32,
            "source": "platform",
        },
    }
    assert ScreenerHeartbeatRequest.model_validate(payload).review_settings is not None
    del payload["review_settings"]
    with pytest.raises(ValidationError, match="requires review settings"):
        ScreenerHeartbeatRequest.model_validate(payload)


_V6_REVIEW_SETTINGS = {
    "revision": 43,
    "scope": "*",
    "mode": "enforce",
    "checksum": "cd" * 32,
    "source": "platform",
    "policy_manifest_profile": "l1_l2",
    "policy_manifest_rotation_id": "policy-v11-l1-l2",
    "policy_manifest_digest": "ef" * 32,
}


def _cpu_counter(
    logical_cpus: int, physical_cores: int | None = None
) -> Callable[..., int | None]:
    """Stand in for ``psutil.cpu_count``, which selects on a ``logical`` kwarg."""

    def count(logical: bool = True) -> int | None:
        return logical_cpus if logical else physical_cores

    return count


def _v6_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "screener_hotkey": _HOTKEY,
        "software_version": "0.16.0",
        "protocol_version": 6,
        "policy_version": 11,
        "state": "polling",
        "instance_id": "ditto-screener-prod",
        "timestamp": 120,
        "signature": "ab" * 64,
        "review_settings": dict(_V6_REVIEW_SETTINGS),
        "host_specs": {
            "cpu_count": 16,
            "cpu_physical_cores": 8,
            "memory_total_mib": 64000,
            "disk_total_gib": 500,
            "architecture": "x86_64",
        },
    }
    payload.update(overrides)
    return payload


def test_v6_requires_the_host_specs_it_announces() -> None:
    assert ScreenerHeartbeatRequest.model_validate(_v6_payload()).host_specs is not None
    payload = _v6_payload()
    del payload["host_specs"]
    with pytest.raises(ValidationError, match="requires host specs"):
        ScreenerHeartbeatRequest.model_validate(payload)


def test_host_specs_are_refused_below_v6() -> None:
    with pytest.raises(ValidationError, match="require heartbeat protocol v6"):
        ScreenerHeartbeatRequest.model_validate(_v6_payload(protocol_version=5))


def test_host_specs_stay_inside_the_announced_allowlist() -> None:
    """The specs are a fixed shape, not a channel for host identity."""
    specs = ScreenerHeartbeatRequest.model_validate(
        _v6_payload(
            host_specs={
                "cpu_count": 16,
                "cpu_physical_cores": 8,
                "memory_total_mib": 64000,
                "disk_total_gib": 500,
                "architecture": "x86_64",
                "hostname": "must-not-leave-the-host",
                "serial_number": "must-not-leave-the-host",
            }
        )
    ).host_specs
    assert specs is not None
    assert set(specs.model_dump()) == {
        "cpu_count",
        "cpu_physical_cores",
        "memory_total_mib",
        "disk_total_gib",
        "architecture",
    }


@pytest.mark.parametrize(
    "override",
    [
        {"cpu_count": 0},
        {"cpu_count": 2048},
        {"memory_total_mib": 0},
        {"disk_total_gib": 0},
        {"architecture": "x86_64:with:delimiters"},
        {"architecture": "x86_64,with,commas"},
        {"architecture": ""},
        # Physical cores above logical CPUs cannot describe a real host, and
        # would let two different machines share one signing token.
        {"cpu_count": 4, "cpu_physical_cores": 8},
    ],
)
def test_host_specs_reject_unrepresentable_hardware(override: dict) -> None:
    specs = {
        "cpu_count": 16,
        "cpu_physical_cores": 8,
        "memory_total_mib": 64000,
        "disk_total_gib": 500,
        "architecture": "x86_64",
    }
    specs.update(override)
    with pytest.raises(ValidationError):
        ScreenerHeartbeatRequest.model_validate(_v6_payload(host_specs=specs))


def test_collect_host_specs_reports_whole_units() -> None:
    specs = collect_host_specs(
        cpu_count=_cpu_counter(16, 8),
        virtual_memory=lambda: SimpleNamespace(total=64 * 1024**3),
        disk_usage=lambda _path: SimpleNamespace(total=500 * 1024**3),
        machine=lambda: "x86_64",
    )
    assert specs == HostSpecs(
        cpu_count=16,
        cpu_physical_cores=8,
        memory_total_mib=65536,
        disk_total_gib=500,
        architecture="x86_64",
    )


def test_collect_host_specs_tolerates_a_kernel_without_physical_cores() -> None:
    specs = collect_host_specs(
        cpu_count=_cpu_counter(4),
        virtual_memory=lambda: SimpleNamespace(total=8 * 1024**3),
        disk_usage=lambda _path: SimpleNamespace(total=80 * 1024**3),
        machine=lambda: "aarch64",
    )
    assert specs is not None
    assert specs.cpu_physical_cores is None


def test_an_unfamiliar_architecture_costs_only_its_own_label() -> None:
    specs = collect_host_specs(
        cpu_count=_cpu_counter(4),
        virtual_memory=lambda: SimpleNamespace(total=8 * 1024**3),
        disk_usage=lambda _path: SimpleNamespace(total=80 * 1024**3),
        machine=lambda: "riscv64:experimental",
    )
    assert specs is not None
    assert specs.architecture == "riscv64-experimental"
    assert specs.cpu_count == 4


def test_a_host_that_reports_no_architecture_still_announces_its_size() -> None:
    specs = collect_host_specs(
        cpu_count=_cpu_counter(4),
        virtual_memory=lambda: SimpleNamespace(total=8 * 1024**3),
        disk_usage=lambda _path: SimpleNamespace(total=80 * 1024**3),
        machine=lambda: "",
    )
    assert specs is not None
    assert specs.architecture == "unknown"


def test_unreadable_hardware_never_costs_a_heartbeat() -> None:
    def explode() -> object:
        raise OSError("/proc is not readable in this sandbox")

    assert (
        collect_host_specs(
            cpu_count=_cpu_counter(4),
            virtual_memory=explode,
            disk_usage=lambda _path: SimpleNamespace(total=80 * 1024**3),
            machine=lambda: "x86_64",
        )
        is None
    )
