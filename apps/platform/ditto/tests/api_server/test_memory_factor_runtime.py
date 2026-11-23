"""SN-500 step 2: the memory-factor policy on the lease runtime."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ditto.api_models.inference_concurrency_settings import (
    BenchmarkRuntimeSettings,
    InferenceConcurrencySettings,
)
from ditto.api_server.endpoints.validator_generator_round import (
    _generator_round_runtime,
)


def test_the_default_policy_is_shadow_with_the_published_curve() -> None:
    runtime = BenchmarkRuntimeSettings()
    assert runtime.memory_factor_posture == "shadow"
    assert runtime.memory_factor_free_mib == 512
    assert runtime.memory_factor_floor_bps == 9_000


def test_a_stored_policy_without_the_fields_reads_as_shadow() -> None:
    stored = {"case_concurrency": 8, "sandbox_memory_limit_mib": 2048}
    runtime = BenchmarkRuntimeSettings.model_validate(stored)
    assert runtime.memory_factor_posture == "shadow"


@pytest.mark.parametrize(
    "bad",
    [
        {"memory_factor_posture": "maybe"},
        {"memory_factor_free_mib": -1},
        {"memory_factor_floor_bps": 4_999},
        {"memory_factor_floor_bps": 10_001},
    ],
)
def test_out_of_range_policy_is_refused(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        BenchmarkRuntimeSettings.model_validate(bad)


def test_generator_rounds_never_carry_the_memory_factor() -> None:
    settings = InferenceConcurrencySettings(
        benchmark_runtime=BenchmarkRuntimeSettings(
            memory_factor_posture="enforce", sandbox_memory_limit_mib=2048
        )
    )
    runtime = _generator_round_runtime(settings)
    assert runtime["memory_factor_posture"] == "off"
    # Everything else on the lease runtime is unchanged.
    assert runtime["sandbox_memory_limit_mib"] == 2048
    assert settings.benchmark_runtime.memory_factor_posture == "enforce"
