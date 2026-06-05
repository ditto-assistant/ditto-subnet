"""Exception hierarchy for the upload-payment verifier."""

from __future__ import annotations


class PaymentVerifierError(Exception):
    """Base exception for :mod:`ditto.api_server.payment_verifier`.

    Concrete subclasses cover the specific failure modes the verifier
    discriminates between. A generic catch-all handler in
    :mod:`ditto.api_server.middleware.error_envelope` maps this base
    class so future subclasses surface a typed envelope code even if a
    specific handler is not yet registered for them.
    """


# --- Chain-side discovery ---


class PaymentNotFoundOnChain(PaymentVerifierError):
    """Raised when the claimed payment extrinsic does not exist on chain.

    This can happen when:
    - The miner sent a ``block_hash`` / ``block_number`` / ``extrinsic_index``
      triple that never corresponded to a real extrinsic (typo or forgery).
    - The block was reorganised out from under the miner before the upload
      reached the API (rare on finney; documented as miner risk).
    - The extrinsic index sits past the last extrinsic in the block.
    - Pylon's archive node has not caught up to the claimed block yet.
    """


class PaymentExtrinsicFailed(PaymentVerifierError):
    """Raised when the extrinsic exists but emitted ``ExtrinsicFailed``.

    This can happen when:
    - The miner's coldkey held insufficient balance at signing time.
    - The destination address was malformed and the runtime rejected the
      transfer post-finalization.
    - Any other dispatch-time failure landed an ``ExtrinsicFailed`` event
      in the same block instead of ``ExtrinsicSuccess``.
    """


# --- Claim correctness ---


class PaymentCallTypeMismatch(PaymentVerifierError):
    """Raised when the extrinsic is not a ``Balances.transfer_keep_alive``.

    This can happen when:
    - The miner pointed the upload at the wrong extrinsic in the block
      (e.g. a ``System.remark`` or ``Subtensor.set_weights`` extrinsic).
    - A modified miner CLI attempted to substitute a no-op or alternative
      transfer call (``transfer_allow_death``, ``Transfer``) hoping the
      verifier would accept it.
    """


class PaymentAmountMismatch(PaymentVerifierError):
    """Raised when the paid ``amount_rao`` is outside the accepted band.

    The accepted band is ``±PAYMENT_DRIFT_TOLERANCE`` around the
    recomputed quote at verify time. The lower bound is the sybil-
    deterrent floor; the upper bound protects miners against overpaying
    being recorded as wrong-amount.

    This can happen when:
    - TAO/USD price moved more than the drift tolerance between the
      quote and the verify-time recompute (rare under the 1 h cache TTL).
    - A modified miner CLI tried to pay a lower amount hoping the
      verifier would accept underpayment.
    - The oracle override was changed between quote and verify time.
    """


class PaymentDestinationMismatch(PaymentVerifierError):
    """Raised when the transfer destination is not the configured send address.

    This can happen when:
    - The miner CLI was misconfigured and paid to a different address.
    - A modified miner CLI redirected the payment to a third-party
      address while still attempting to upload.
    - The platform rotated its receive address between the quote and the
      payment without communicating the change (operator-side regression).
    """


class PaymentSignerMismatch(PaymentVerifierError):
    """Raised when the extrinsic signer is not the hotkey's on-chain coldkey.

    This can happen when:
    - A different coldkey funded the upload for a hotkey it does not own
      (collusion attempt or compromised-coldkey scenario).
    - The miner's wallet was misconfigured and signed the transfer from
      a sibling coldkey rather than the hotkey owner.
    - The chain returned an SS58 string with a different network prefix
      than the extrinsic signer (canonicalisation bug; investigate
      before treating as adversarial).
    """
