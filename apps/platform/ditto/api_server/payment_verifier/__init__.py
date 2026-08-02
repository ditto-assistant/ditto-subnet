"""On-chain upload-payment verifier.

Reads the ``Balances.transfer_keep_alive`` extrinsic the miner claims
in their upload, confirms it succeeded, paid the configured address,
matched the recomputed quote within the drift band, and was signed by
the hotkey's on-chain coldkey owner at payment time. Returns a
:class:`VerifiedPayment` that the ``/upload/agent`` orchestrator binds
into ``evaluation_payments``. PK violations at insert time surface as
:class:`PaymentReplayedError` (3207) via the queries layer.

Usage:
    from ditto.api_server.payment_verifier import (
        PaymentProof,
        create_payment_verifier,
    )

    verifier = create_payment_verifier(
        chain=chain_client,
        oracle=price_oracle,
        send_address=config.upload_payment_address,
    )
    verified = await verifier.verify_payment(
        PaymentProof(block_hash=h, block_number=n, extrinsic_index=i),
        expected_hotkey=hotkey,
        expected_amount_rao=40_000_000,
    )
"""

from __future__ import annotations

from ditto.api_server.payment_verifier.errors import (
    PaymentAmountMismatch,
    PaymentCallTypeMismatch,
    PaymentDestinationMismatch,
    PaymentExtrinsicFailed,
    PaymentNotFoundOnChain,
    PaymentRecoveryExpired,
    PaymentReplayedError,
    PaymentSignerMismatch,
    PaymentVerifierError,
)
from ditto.api_server.payment_verifier.factory import create_payment_verifier
from ditto.api_server.payment_verifier.models import PaymentProof, VerifiedPayment
from ditto.api_server.payment_verifier.verifier import PaymentVerifier

__all__ = [
    # Main components
    "PaymentVerifier",
    # Inputs / outputs
    "PaymentProof",
    "VerifiedPayment",
    # Errors
    "PaymentVerifierError",
    "PaymentNotFoundOnChain",
    "PaymentRecoveryExpired",
    "PaymentExtrinsicFailed",
    "PaymentCallTypeMismatch",
    "PaymentAmountMismatch",
    "PaymentDestinationMismatch",
    "PaymentSignerMismatch",
    "PaymentReplayedError",
    # Factory
    "create_payment_verifier",
]
