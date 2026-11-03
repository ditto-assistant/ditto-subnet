import pytest

from ditto.treasury.service_allocation import (
    ServiceDestination,
    plan_service_distribution,
    service_first_weights,
)
from ditto.validator.weights import owner_burn_destination_required


@pytest.mark.parametrize("service_bps", [2500, 7500, 10000])
@pytest.mark.parametrize("burn", [0, 0.25, 1])
@pytest.mark.parametrize("paid", [0, 0.5, 1])
def test_full_range_service_split_conserves_emissions(service_bps, burn, paid):
    vector = service_first_weights(
        {"miner": 1},
        service_bps=service_bps,
        burn_share=burn,
        paid_miner_fraction=paid,
        collector_hotkey="collector",
        collector_verified=True,
        burn_hotkey="burn",
    )
    service = service_bps / 10000
    assert vector["collector"] == service
    assert vector.get("miner", 0) == pytest.approx((1 - service) * (1 - burn) * paid)
    assert sum(vector.values()) == pytest.approx(1)
    assert owner_burn_destination_required(
        {"miner": 1},
        miner_share=1 - burn,
        paid_miner_fraction=paid,
        service_bps=service_bps,
    ) == (vector.get("burn", 0) > 0)


def test_full_service_allocation_with_empty_miners_needs_no_burn_destination():
    assert service_first_weights(
        {},
        service_bps=10000,
        burn_share=1,
        paid_miner_fraction=0,
        collector_hotkey="collector",
        collector_verified=True,
        burn_hotkey="",
    ) == {"collector": 1}
    assert not owner_burn_destination_required({}, miner_share=0, service_bps=10000)


@pytest.mark.parametrize("invalid", [-1, 10001, True, 7500.0])
def test_invalid_service_split_is_refused(invalid):
    with pytest.raises(ValueError, match="service allocation"):
        service_first_weights(
            {},
            service_bps=invalid,
            burn_share=0,
            collector_hotkey="collector",
            collector_verified=True,
            burn_hotkey="burn",
        )


@pytest.mark.parametrize(
    "burn, expected",
    [
        (1, {"collector": 0.1, "burn": 0.9}),
        (0.5, {"miner": 0.45, "collector": 0.1, "burn": 0.45}),
    ],
)
def test_service_pool_survives_full_burn(burn, expected):
    vector = service_first_weights(
        {"miner": 1},
        service_bps=1000,
        burn_share=burn,
        collector_hotkey="collector",
        collector_verified=True,
        burn_hotkey="burn",
    )
    assert vector == pytest.approx(expected)


def test_empty_and_unpaid_miner_share_does_not_enlarge_collector():
    common = {
        "service_bps": 1000,
        "burn_share": 0,
        "collector_hotkey": "collector",
        "collector_verified": True,
        "burn_hotkey": "burn",
    }
    assert service_first_weights({}, **common) == pytest.approx(
        {"collector": 0.1, "burn": 0.9}
    )
    assert service_first_weights(
        {"miner": 1}, paid_miner_fraction=0.5, **common
    ) == pytest.approx({"miner": 0.45, "collector": 0.1, "burn": 0.45})
    assert service_first_weights({"collector": 1}, **common) == pytest.approx(
        {"collector": 0.1, "burn": 0.9}
    )


def test_finite_large_weights_cannot_lose_the_miner_share_to_sum_overflow():
    vector = service_first_weights(
        {"miner-a": 1e308, "miner-b": 1e308},
        service_bps=1000,
        burn_share=0,
        collector_hotkey="collector",
        collector_verified=True,
        burn_hotkey="burn",
    )
    assert vector == pytest.approx({"miner-a": 0.45, "miner-b": 0.45, "collector": 0.1})
    assert sum(vector.values()) == pytest.approx(1)


@pytest.mark.parametrize("burn", [0, 0.5, 1])
@pytest.mark.parametrize("paid", [0, 0.5, 1])
@pytest.mark.parametrize("verified", [False, True])
def test_paused_collector_never_competes_for_miner_remainder(burn, paid, verified):
    vector = service_first_weights(
        {"miner": 1, "collector": 100},
        service_bps=0,
        burn_share=burn,
        paid_miner_fraction=paid,
        collector_hotkey="collector",
        collector_verified=verified,
        burn_hotkey="burn",
    )
    miner = (1 - burn) * paid
    expected = {}
    if miner:
        expected["miner"] = miner
    if miner < 1:
        expected["burn"] = 1 - miner
    assert vector == pytest.approx(expected)
    assert "collector" not in vector
    assert sum(vector.values()) == pytest.approx(1)


def test_paused_collector_only_vector_burns_the_remainder():
    assert service_first_weights(
        {"collector": 1},
        service_bps=0,
        burn_share=0,
        collector_hotkey="collector",
        collector_verified=True,
        burn_hotkey="burn",
    ) == {"burn": 1}


def test_zero_residual_does_not_require_a_burn_hotkey():
    assert service_first_weights(
        {"miner": 1},
        service_bps=0,
        burn_share=0,
        collector_hotkey="",
        collector_verified=False,
        burn_hotkey="",
    ) == {"miner": 1}
    with pytest.raises(ValueError, match="burn hotkey is required"):
        service_first_weights(
            {"miner": 1},
            service_bps=0,
            burn_share=0.4,
            collector_hotkey="",
            collector_verified=False,
            burn_hotkey="",
        )


def test_no_configured_collector_preserves_ordinary_zero_service_payout():
    assert service_first_weights(
        {"miner": 1},
        service_bps=0,
        burn_share=0,
        collector_hotkey="",
        collector_verified=False,
        burn_hotkey="burn",
    ) == {"miner": 1}


def test_unverified_or_burn_collector_cannot_receive_funds():
    with pytest.raises(ValueError, match="independently verified"):
        service_first_weights(
            {},
            service_bps=1000,
            burn_share=1,
            collector_hotkey="collector",
            collector_verified=False,
            burn_hotkey="burn",
        )
    with pytest.raises(ValueError, match="independently verified"):
        service_first_weights(
            {},
            service_bps=1000,
            burn_share=1,
            collector_hotkey="burn",
            collector_verified=True,
            burn_hotkey="burn",
        )


def test_distribution_conserves_atomic_earnings_and_does_not_spend_principal():
    destinations = (
        ServiceDestination("gm", 600, "gm-holder"),
        ServiceDestination("bitsec", 200, "audit-holder"),
        ServiceDestination("bitcast", 200, "ads-holder"),
    )
    result = plan_service_distribution(
        attributed_alpha_rao=11,
        available_alpha_rao=100,
        collector_coldkey="collector",
        destinations=destinations,
    )
    assert sum(d.alpha_rao for d in result) == 11
    assert {d.bucket_id: d.alpha_rao for d in result} == {
        "gm": 7,
        "bitsec": 2,
        "bitcast": 2,
    }
    assert result == plan_service_distribution(
        attributed_alpha_rao=11,
        available_alpha_rao=100,
        collector_coldkey="collector",
        destinations=tuple(reversed(destinations)),
    )
    with pytest.raises(ValueError, match="available"):
        plan_service_distribution(
            attributed_alpha_rao=101,
            available_alpha_rao=100,
            collector_coldkey="collector",
            destinations=destinations,
        )

    with pytest.raises(ValueError, match="invalid service destination"):
        plan_service_distribution(
            attributed_alpha_rao=11,
            available_alpha_rao=100,
            collector_coldkey="gm-holder",
            destinations=destinations,
        )


@pytest.mark.parametrize("allocation", [2500, 7500, 10000])
def test_large_service_pool_distributes_only_attributed_earnings(allocation):
    result = plan_service_distribution(
        attributed_alpha_rao=11,
        available_alpha_rao=100,
        collector_coldkey="collector",
        destinations=(ServiceDestination("gm", allocation, "gm-holder"),),
    )
    assert len(result) == 1
    assert result[0].alpha_rao == 11
