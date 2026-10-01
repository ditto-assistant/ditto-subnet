"""Service-first economic primitives shared with the enforcing weight adapter.

Activation must bind this fold to a fleet-supported ledger pin and finalized
collector identity. A shadow treasury revision alone is never such authority.
"""

from __future__ import annotations

from dataclasses import dataclass

from ditto_screening_protocol.treasury_weight_math import (
    service_first_weights as service_first_weights,
)


@dataclass(frozen=True)
class ServiceDestination:
    bucket_id: str
    allocation_bps: int
    holding_coldkey: str


@dataclass(frozen=True)
class ServiceDistribution:
    bucket_id: str
    holding_coldkey: str
    alpha_rao: int


def plan_service_distribution(
    *,
    attributed_alpha_rao: int,
    available_alpha_rao: int,
    collector_coldkey: str,
    destinations: tuple[ServiceDestination, ...],
) -> tuple[ServiceDistribution, ...]:
    """Split independently attributed collector earnings, never its principal.

    Integer largest-remainder rounding conserves the full payout. Exact
    receipt identity, policy revision and durable dispatch claims belong in
    the sweep executor; a balance increase alone is not emission attribution.
    """
    if any(
        type(value) is not int or value < 0
        for value in (attributed_alpha_rao, available_alpha_rao)
    ):
        raise ValueError("amounts must be nonnegative integer alpha rao")
    if attributed_alpha_rao > available_alpha_rao:
        raise ValueError("attributed earnings exceed available collector stake")
    if not collector_coldkey or not destinations or len(destinations) > 20:
        raise ValueError("collector and service destinations are required")
    if len({d.bucket_id for d in destinations}) != len(destinations):
        raise ValueError("duplicate bucket")
    if len({d.holding_coldkey for d in destinations}) != len(destinations):
        raise ValueError("duplicate holding wallet")
    if any(
        not d.bucket_id
        or not d.holding_coldkey
        or d.holding_coldkey == collector_coldkey
        or type(d.allocation_bps) is not int
        or not 0 <= d.allocation_bps <= 1000
        for d in destinations
    ):
        raise ValueError("invalid service destination")
    total = sum(d.allocation_bps for d in destinations)
    if not 0 < total <= 1000:
        raise ValueError("service pool must be between 1 and 1000 bps")
    ordered = sorted(
        (d for d in destinations if d.allocation_bps), key=lambda d: d.bucket_id
    )
    amounts = [attributed_alpha_rao * d.allocation_bps // total for d in ordered]
    dust = attributed_alpha_rao - sum(amounts)
    remainders = sorted(
        range(len(ordered)),
        key=lambda i: (
            -(attributed_alpha_rao * ordered[i].allocation_bps % total),
            ordered[i].bucket_id,
        ),
    )
    for index in remainders[:dust]:
        amounts[index] += 1
    return tuple(
        ServiceDistribution(d.bucket_id, d.holding_coldkey, amount)
        for d, amount in zip(ordered, amounts, strict=True)
        if amount
    )
