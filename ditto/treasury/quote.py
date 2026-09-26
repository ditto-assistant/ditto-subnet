"""Read-only DITTO conversion quotes from explicitly pinned pool snapshots.

No RPC client, wallet, signer, or transaction submission is present here.  GM's
final USD credit is determined when its confirmed deposit settles, so projected
credit is deliberately advisory.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

RAO = 1_000_000_000


@dataclass(frozen=True)
class Pool:
    netuid: int
    block_hash: str
    tao_rao: int
    alpha_rao: int
    fee_bps: int

    def __post_init__(self) -> None:
        if self.netuid <= 0 or not self.block_hash:
            raise ValueError("pool needs a pinned chain identity")
        if self.tao_rao <= 0 or self.alpha_rao <= 0:
            raise ValueError("pool reserves must be positive")
        if not 0 <= self.fee_bps <= 1000:
            raise ValueError("pool fee must be in [0, 1000] bps")

    def sell_alpha(self, amount_rao: int) -> tuple[int, int]:
        """Return TAO out and price-impact bps against the pool spot."""
        if amount_rao <= 0:
            raise ValueError("amount must be positive")
        after_fee = amount_rao * (10_000 - self.fee_bps) // 10_000
        output = self.tao_rao * after_fee // (self.alpha_rao + after_fee)
        spot = Decimal(amount_rao) * self.tao_rao / self.alpha_rao
        return output, _impact(spot, output)

    def buy_alpha(self, tao_rao: int) -> tuple[int, int]:
        if tao_rao <= 0:
            raise ValueError("amount must be positive")
        after_fee = tao_rao * (10_000 - self.fee_bps) // 10_000
        output = self.alpha_rao * after_fee // (self.tao_rao + after_fee)
        spot = Decimal(tao_rao) * self.alpha_rao / self.tao_rao
        return output, _impact(spot, output)


def _impact(spot: Decimal, actual: int) -> int:
    if actual <= 0:
        raise ValueError("quote rounds to zero")
    return int(((spot - actual) * 10_000 / spot).to_integral_value(rounding=ROUND_DOWN))


@dataclass(frozen=True)
class TopUpQuote:
    source_alpha_rao: int
    tao_rao: int
    gm_alpha_rao: int
    tao_path_impact_bps: int
    gm_alpha_path_impact_bps: int
    source_block_hash: str
    route_tao: str = "DITTO->TAO->GM credit"
    route_gm_alpha: str = "DITTO->TAO->GM alpha->GM credit"


def quote_topup(
    ditto: Pool,
    gm: Pool,
    *,
    source_alpha_rao: int,
    max_source_alpha_rao: int,
    max_impact_bps: int,
) -> TopUpQuote:
    """Compare both routes against one finalized block; fail closed on bounds."""
    if ditto.netuid != 118 or gm.netuid != 28:
        raise ValueError("expected DITTO SN118 and GM SN28 pools")
    if ditto.block_hash != gm.block_hash:
        raise ValueError("pool snapshots must come from one finalized block")
    if source_alpha_rao <= 0 or source_alpha_rao > max_source_alpha_rao:
        raise ValueError("source amount exceeds the approved limit")
    if not 0 <= max_impact_bps <= 500:
        raise ValueError("impact limit must be at most 500 bps")
    tao, ditto_impact = ditto.sell_alpha(source_alpha_rao)
    gm_alpha, gm_impact = gm.buy_alpha(tao)
    combined = 10_000 - (10_000 - ditto_impact) * (10_000 - gm_impact) // 10_000
    if ditto_impact > max_impact_bps or combined > max_impact_bps:
        raise ValueError("quote exceeds the price-impact circuit breaker")
    return TopUpQuote(
        source_alpha_rao=source_alpha_rao,
        tao_rao=tao,
        gm_alpha_rao=gm_alpha,
        tao_path_impact_bps=ditto_impact,
        gm_alpha_path_impact_bps=combined,
        source_block_hash=ditto.block_hash,
    )
