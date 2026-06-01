"""Each PaymentVerifierError subclass must document its trigger conditions."""

from __future__ import annotations

import pytest

from ditto.api_server.payment_verifier import (
    PaymentAmountMismatch,
    PaymentCallTypeMismatch,
    PaymentDestinationMismatch,
    PaymentExtrinsicFailed,
    PaymentNotFoundOnChain,
    PaymentSignerMismatch,
    PaymentVerifierError,
)


class TestErrorDocstrings:
    """Each concrete error carries a 'This can happen when:' bullet list."""

    @pytest.mark.parametrize(
        "cls",
        [
            PaymentNotFoundOnChain,
            PaymentExtrinsicFailed,
            PaymentCallTypeMismatch,
            PaymentAmountMismatch,
            PaymentDestinationMismatch,
            PaymentSignerMismatch,
        ],
    )
    def test_has_trigger_bullets(self, cls: type[Exception]):
        doc = cls.__doc__ or ""
        assert "This can happen when:" in doc, (
            f"{cls.__name__} missing 'This can happen when:' docstring section"
        )
        # At least one bullet entry signals real content rather than an
        # empty placeholder section. ``__doc__`` preserves the indentation
        # uniform across lines, so look for the post-newline bullet marker
        # rather than a specific column.
        assert "\n- " in doc, (
            f"{cls.__name__} 'This can happen when:' has no bullet entries"
        )

    def test_base_class_inheritance(self):
        for cls in (
            PaymentNotFoundOnChain,
            PaymentExtrinsicFailed,
            PaymentCallTypeMismatch,
            PaymentAmountMismatch,
            PaymentDestinationMismatch,
            PaymentSignerMismatch,
        ):
            assert issubclass(cls, PaymentVerifierError)
