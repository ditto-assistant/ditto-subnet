"""Factory for the payment verifier."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ditto.api_server.payment_verifier.verifier import PaymentVerifier

if TYPE_CHECKING:
    from ditto.api_server.pricing import PriceOracle, PricingConfig
    from ditto.chain import ChainClient


def create_payment_verifier(
    chain: ChainClient,
    oracle: PriceOracle,
    pricing_config: PricingConfig,
    send_address: str,
) -> PaymentVerifier:
    """Wire a :class:`PaymentVerifier` against its chain + pricing deps.

    The verifier owns no resources — chain and oracle lifetimes are
    managed by the api_server lifespan. The factory exists for symmetry
    with the rest of ``ditto/api_server`` (and so callers go through one
    name, not a constructor scattered across imports).
    """
    return PaymentVerifier(
        chain=chain,
        oracle=oracle,
        pricing_config=pricing_config,
        send_address=send_address,
    )
