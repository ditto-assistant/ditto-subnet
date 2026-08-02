"""create_payment_verifier wiring smoke test."""

from __future__ import annotations

from unittest.mock import MagicMock

from ditto.api_server.payment_verifier import (
    PaymentVerifier,
    create_payment_verifier,
)


class TestCreatePaymentVerifier:
    def test_returns_payment_verifier(self):
        chain = MagicMock()
        oracle = MagicMock()
        verifier = create_payment_verifier(
            chain=chain,
            oracle=oracle,
            send_address="5Address",
        )
        assert isinstance(verifier, PaymentVerifier)
        assert verifier._chain is chain
        assert verifier._oracle is oracle
        assert verifier._send_address == "5Address"
