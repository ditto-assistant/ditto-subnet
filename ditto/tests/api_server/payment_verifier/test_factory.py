"""create_payment_verifier wiring smoke test."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

from ditto.api_server.payment_verifier import (
    PaymentVerifier,
    create_payment_verifier,
)
from ditto.api_server.pricing import PricingConfig


def _make_pricing_config() -> PricingConfig:
    return PricingConfig(
        fee_usd=Decimal("5"),
        fee_buffer=Decimal("1.4"),
        cache_ttl_seconds=3600,
        max_stale_seconds=86400,
        coingecko_timeout_seconds=5.0,
        override_tao_usd=None,
    )


class TestCreatePaymentVerifier:
    def test_returns_payment_verifier(self):
        chain = MagicMock()
        oracle = MagicMock()
        verifier = create_payment_verifier(
            chain=chain,
            oracle=oracle,
            pricing_config=_make_pricing_config(),
            send_address="5Address",
        )
        assert isinstance(verifier, PaymentVerifier)
        assert verifier._chain is chain
        assert verifier._oracle is oracle
        assert verifier._send_address == "5Address"
