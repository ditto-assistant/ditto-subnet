"""Frozen dataclass inputs / outputs for the payment verifier."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final

PAYMENT_DRIFT_TOLERANCE: Final[Decimal] = Decimal("0.02")
"""Allowed band on ``amount_rao`` around the recomputed quote.

Source constant rather than env var: the band determines whether a
miner's payment is accepted and lives next to the payment-acceptance
logic that consults it. Changes are PR-reviewed + versioned in source
rather than ops-tunable, mirroring how scoring weights are handled.

Default 2% covers the typical TTL-boundary window where the oracle
cache might tip from quote-time price to a fresh fetch in the seconds
between miner finalization and server verify. Sub-percent drift in a
30-60 s window is the norm; 2% gives margin without inviting sybil
under-payment beyond the buffer the upload-pricing formula already
bakes in.
"""


@dataclass(frozen=True)
class PaymentProof:
    """Payment-extrinsic identifiers the orchestrator hands the verifier.

    Pylon's ``get_extrinsic`` is block-number-keyed (not block-hash), so
    the miner CLI carries both ``block_hash`` (used for substrate-side
    event + storage reads) and ``block_number`` (used for the Pylon
    call_args read). The CLI gets both for free from
    ``ExtrinsicReceipt`` after ``wait_for_finalization=True``.
    """

    block_hash: str
    """Hash of the block containing the payment extrinsic."""

    block_number: int
    """Block number of the same block. Used to dispatch the Pylon call."""

    extrinsic_index: int
    """Zero-based index of the extrinsic within the block."""


@dataclass(frozen=True)
class VerifiedPayment:
    """Outcome of a successful :meth:`PaymentVerifier.verify_payment` call.

    Field names mirror :class:`ditto.db.models.EvaluationPayment` columns
    so the next-PR queries layer can map field-for-field without an
    intermediate adapter.
    """

    block_hash: str
    """Hash of the block containing the payment extrinsic."""

    extrinsic_index: int
    """Zero-based index of the extrinsic within the block."""

    miner_hotkey: str
    """SS58 hotkey claimed by the miner; cross-validated against the
    on-chain coldkey owner via the signer match."""

    miner_coldkey: str
    """On-chain coldkey that owns ``miner_hotkey`` at the payment block.
    This is the same value found in the extrinsic's signer field; the
    verifier asserts equality before populating this field."""

    amount_rao: int
    """Payment amount in rao (1 TAO = 1e9 rao). Validated against the
    recomputed quote ±``PAYMENT_DRIFT_TOLERANCE``."""

    dest_address: str
    """SS58 destination of the transfer. Equals the configured upload
    payment address by construction."""

    block_timestamp: datetime
    """Tz-aware UTC block timestamp. Substrate returns milliseconds via
    ``Timestamp.Now``; the chain client converts to seconds before this
    field is populated."""
