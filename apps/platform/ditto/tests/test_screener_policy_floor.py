"""lease_screening_policy_floor: the only screening-policy gate on leasing."""

from __future__ import annotations

from ditto.api_models.benchmark_contract import benchmark_contracts
from ditto.screener_policy_state import (
    lease_screening_policy_floor,
    update_effective_screener_policy,
)


def test_floor_is_the_contract_minimum_and_ignores_activations() -> None:
    # The root conftest restores the effective policy after every test.
    update_effective_screener_policy(99, rescreen_scored=False)
    for contract in benchmark_contracts():
        assert (
            lease_screening_policy_floor(contract.version)
            == contract.minimum_screening_policy_version
        )
    assert lease_screening_policy_floor() == min(
        contract.minimum_screening_policy_version
        for contract in benchmark_contracts()
        if contract.requires_screened_image
    )


def test_unknown_era_falls_back_to_the_era_free_floor() -> None:
    assert lease_screening_policy_floor(10_000) == lease_screening_policy_floor()
