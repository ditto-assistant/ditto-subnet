"""The validator's self-imposed host-resource ceilings.

These pin the arithmetic and the config envelope. The behavioural half -- that a
constrained worker declines to claim while still heartbeating -- lives in
``test_worker.py`` next to the sweep it changes.
"""

from __future__ import annotations

import pytest

from ditto.api_models.system_health import DockerHealth, SystemMetrics
from ditto.validator.errors import ValidatorConfigError
from ditto.validator.resource_gate import (
    CEILING_DISABLED,
    DEFAULT_RESOURCE_CEILINGS,
    ResourceCeilings,
    check_resource_ceilings_config,
    parse_resource_ceilings_from_env,
)

_DOCKER = DockerHealth(status="healthy", running_containers=1, unhealthy_containers=0)


def _metrics(*, cpu: int = 0, memory: int = 0, disk: int = 0) -> SystemMetrics:
    return SystemMetrics(
        collected_at=1_700_000_000,
        cpu_percent=cpu,
        memory_percent=memory,
        disk_percent=disk,
        docker=_DOCKER,
    )


class TestExceeded:
    def test_the_live_fleets_worst_reading_is_untouched(self) -> None:
        """5Cg3DiRf runs at 65% memory and is doing exactly its job."""
        assert DEFAULT_RESOURCE_CEILINGS.exceeded(_metrics(memory=65, disk=45)) == ()

    def test_the_busiest_disk_in_the_fleet_still_claims(self) -> None:
        """Rizzo at 90% disk is throttled by the platform, not stopped here.

        The self-gate is the last line, not the first. Leaving 90 below the
        ceiling is deliberate: the platform already holds that host to one slot,
        and refusing outright would cost the quorum a member it can still serve.
        """
        assert DEFAULT_RESOURCE_CEILINGS.exceeded(_metrics(disk=90)) == ()

    def test_a_nearly_full_disk_declines(self) -> None:
        assert DEFAULT_RESOURCE_CEILINGS.exceeded(_metrics(disk=95)) == ("disk",)

    def test_nearly_exhausted_memory_declines(self) -> None:
        assert DEFAULT_RESOURCE_CEILINGS.exceeded(_metrics(memory=100)) == ("memory",)

    def test_a_pinned_cpu_is_not_a_reason_to_stop(self) -> None:
        """A benchmark host is supposed to run pinned; CPU ships disabled."""
        assert DEFAULT_RESOURCE_CEILINGS.cpu_percent == CEILING_DISABLED
        assert DEFAULT_RESOURCE_CEILINGS.exceeded(_metrics(cpu=100)) == ()

    def test_cpu_gates_once_an_operator_enables_it(self) -> None:
        ceilings = ResourceCeilings(cpu_percent=95)

        assert ceilings.exceeded(_metrics(cpu=95)) == ("cpu",)
        assert ceilings.exceeded(_metrics(cpu=90)) == ()

    def test_every_exceeded_resource_is_named(self) -> None:
        ceilings = ResourceCeilings(cpu_percent=95)

        assert ceilings.exceeded(_metrics(cpu=100, memory=100, disk=100)) == (
            "disk",
            "memory",
            "cpu",
        )

    def test_unobservable_metrics_are_not_pressure(self) -> None:
        """No collector wired up must behave exactly as it did before the gate."""
        assert DEFAULT_RESOURCE_CEILINGS.exceeded(None) == ()

    def test_describe_names_the_reading_and_the_ceiling(self) -> None:
        assert (
            DEFAULT_RESOURCE_CEILINGS.describe(_metrics(disk=95)) == "disk 95% >= 95%"
        )
        assert (
            DEFAULT_RESOURCE_CEILINGS.describe(_metrics(disk=45))
            == "within every configured ceiling"
        )
        assert DEFAULT_RESOURCE_CEILINGS.describe(None) == "no host metrics"


class TestConfigEnvelope:
    @pytest.mark.parametrize("value", [45, 101, 87, -5])
    def test_a_ceiling_that_cannot_mean_what_it_says_is_refused(
        self, value: int
    ) -> None:
        with pytest.raises(ValidatorConfigError):
            check_resource_ceilings_config(ResourceCeilings(disk_percent=value))

    def test_off_grid_ceilings_are_refused_because_the_sample_is_quantised(
        self,
    ) -> None:
        """87 would fire at 90 and then misdescribe itself in the logs."""
        with pytest.raises(ValidatorConfigError, match="multiple of 5"):
            check_resource_ceilings_config(ResourceCeilings(memory_percent=87))

    def test_zero_disables_rather_than_failing_the_envelope(self) -> None:
        check_resource_ceilings_config(
            ResourceCeilings(
                cpu_percent=CEILING_DISABLED,
                memory_percent=CEILING_DISABLED,
                disk_percent=CEILING_DISABLED,
            )
        )


class TestParseFromEnv:
    def test_defaults_when_nothing_is_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name in (
            "VALIDATOR_CPU_PERCENT_CEILING",
            "VALIDATOR_MEMORY_PERCENT_CEILING",
            "VALIDATOR_DISK_PERCENT_CEILING",
        ):
            monkeypatch.delenv(name, raising=False)

        assert parse_resource_ceilings_from_env() == DEFAULT_RESOURCE_CEILINGS

    def test_every_ceiling_is_operator_configurable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VALIDATOR_CPU_PERCENT_CEILING", "90")
        monkeypatch.setenv("VALIDATOR_MEMORY_PERCENT_CEILING", "85")
        monkeypatch.setenv("VALIDATOR_DISK_PERCENT_CEILING", "100")

        assert parse_resource_ceilings_from_env() == ResourceCeilings(
            cpu_percent=90, memory_percent=85, disk_percent=100
        )

    def test_garbage_fails_at_boot_rather_than_silently_defaulting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VALIDATOR_DISK_PERCENT_CEILING", "ninety-five")

        with pytest.raises(ValidatorConfigError, match="whole percentage"):
            parse_resource_ceilings_from_env()

    def test_an_out_of_range_env_value_fails_at_boot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VALIDATOR_MEMORY_PERCENT_CEILING", "120")

        with pytest.raises(ValidatorConfigError):
            parse_resource_ceilings_from_env()
