"""Canonical service-first weight arithmetic; no identity or dispatch authority."""

from __future__ import annotations

import math
from collections.abc import Mapping


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
    if not burn_hotkey:
        raise ValueError("burn hotkey is required")
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
    excluded = {burn_hotkey}
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
        result[burn_hotkey] = residual
    return result
