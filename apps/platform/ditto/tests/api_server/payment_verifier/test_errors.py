"""Payment verifier error hierarchy tests."""

from __future__ import annotations

from ditto.api_server.payment_verifier import (
    PaymentAmountMismatch,
    PaymentCallTypeMismatch,
    PaymentDestinationMismatch,
    PaymentExtrinsicFailed,
    PaymentNotFoundOnChain,
    PaymentReplayedError,
    PaymentSignerMismatch,
    PaymentVerifierError,
)


class TestErrorHierarchy:
    def test_base_class_inheritance(self):
        for cls in (
            PaymentNotFoundOnChain,
            PaymentExtrinsicFailed,
            PaymentCallTypeMismatch,
            PaymentAmountMismatch,
            PaymentDestinationMismatch,
            PaymentSignerMismatch,
            PaymentReplayedError,
        ):
            assert issubclass(cls, PaymentVerifierError)
