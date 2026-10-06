"""Canonical service-first weight arithmetic; no identity or dispatch authority."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass


def service_first_weights(
    weights: Mapping[str, float],
    *,
    service_bps: int,
    burn_share: float,
    collector_hotkey: str,
    collector_verified: bool,
    burn_hotkey: str,
    paid_miner_fraction: float = 1.0,
) -> dict[str, float]:
    """Reserve services first, burn only the remaining competitive miner share.

    Unknown collector identity stops the fold, preserving the previous vector
    at the caller. Empty/withheld miner share burns, never enlarges the pool.
    A configured collector remains a treasury role when service funding is
    paused, so it never competes for the ordinary miner remainder.
    """
    if type(service_bps) is not int or not 0 <= service_bps <= 1000:
        raise ValueError("service allocation must be between 0 and 1000 bps")
    for share in (burn_share, paid_miner_fraction):
        if isinstance(share, bool) or not math.isfinite(share) or not 0 <= share <= 1:
            raise ValueError("invalid burn or paid miner share")
    if service_bps and (
        collector_verified is not True
        or not collector_hotkey
        or collector_hotkey == burn_hotkey
    ):
        raise ValueError("service collector identity is not independently verified")
    if any(
        isinstance(w, bool) or not math.isfinite(w) or w < 0 for w in weights.values()
    ):
        raise ValueError("invalid miner vector")
    excluded = {burn_hotkey} if burn_hotkey else set()
    if collector_hotkey:
        excluded.add(collector_hotkey)
    miners = {h: w for h, w in weights.items() if h not in excluded and w > 0}
    service = service_bps / 10_000
    miner = (1 - service) * (1 - burn_share) * paid_miner_fraction if miners else 0
    result: dict[str, float] = {}
    if miner:
        # Scaling first avoids finite individual weights overflowing their sum.
        maximum = max(miners.values())
        scaled = {h: w / maximum for h, w in miners.items()}
        total = math.fsum(scaled.values())
        result = {h: w / total * miner for h, w in scaled.items()}
    if service:
        result[collector_hotkey] = service
    residual = 1 - service - miner
    if residual > 0:
        if not burn_hotkey:
            raise ValueError("burn hotkey is required")
        result[burn_hotkey] = residual
    return result


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
