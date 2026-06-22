"""Chain payment submission via the raw bittensor SDK.

Submits a ``Balances.transfer_keep_alive`` extrinsic signed by the
miner's coldkey, waits for finalisation, and returns the proof tuple
``(block_hash, block_number, extrinsic_index)`` that the server's
:class:`ditto.api_server.payment_verifier.PaymentVerifier` consumes.

The CLI never uses Pylon: balance transfers are one of the Pylon
capability gaps. The raw bittensor SDK is the architecture-locked
choice for this module.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ditto.miner_cli.errors import (
    PaymentFinalizationTimeoutError,
    PaymentSubmissionError,
)
from ditto.miner_cli.models import PaymentReceipt

if TYPE_CHECKING:
    import bittensor

logger = logging.getLogger(__name__)


def submit_eval_payment(
    *,
    live_wallet: bittensor.Wallet,
    subtensor_network: str,
    amount_rao: int,
    dest_address: str,
) -> PaymentReceipt:
    """Sign + submit the upload-fee extrinsic, await finalisation.

    Args:
        live_wallet: Live bittensor wallet object; ``.coldkey`` is used
            to sign the extrinsic (balance transfers spend the coldkey
            balance, not the hotkey).
        subtensor_network: Network identifier passed to
            :class:`bittensor.Subtensor`. ``"finney"`` for mainnet,
            ``"test"`` for testnet, ``"local"`` for devnet, or a full
            WebSocket URL.
        amount_rao: Transfer amount in rao (1 TAO = 1e9 rao).
        dest_address: SS58 address that receives the payment.

    Returns:
        :class:`PaymentReceipt` populated with the proof tuple the API
        server's verifier consumes.

    Raises:
        PaymentSubmissionError: Coldkey lacks funds, the substrate node
            rejects the extrinsic, or the network is unreachable.
        PaymentFinalizationTimeoutError: The extrinsic was accepted but
            did not finalise within the SDK's default window.
    """
    import bittensor

    logger.info(
        f"submitting payment: {amount_rao} rao to {dest_address} "
        f"on subtensor={subtensor_network}"
    )

    try:
        subtensor = bittensor.Subtensor(network=subtensor_network)
    except Exception as e:
        raise PaymentSubmissionError(
            f"could not connect to subtensor {subtensor_network!r}: {e}"
        ) from e

    try:
        response = subtensor.transfer(
            wallet=live_wallet,
            destination_ss58=dest_address,
            amount=bittensor.Balance.from_rao(amount_rao),
            wait_for_inclusion=True,
            wait_for_finalization=True,
            raise_error=True,
        )
    except TimeoutError as e:
        raise PaymentFinalizationTimeoutError(f"extrinsic did not finalise: {e}") from e
    except Exception as e:
        raise PaymentSubmissionError(f"transfer extrinsic rejected: {e}") from e

    if not response.success:
        # raise_error=True should have raised already; defensive.
        raise PaymentSubmissionError(f"transfer reported failure: {response.message}")

    receipt = response.extrinsic_receipt
    if receipt is None:
        raise PaymentSubmissionError(
            "transfer succeeded but no extrinsic_receipt was returned"
        )

    # Substrate SDK leaves receipt.block_number as None unless the
    # caller explicitly populates it at construction; bittensor's
    # transfer() doesn't. Fall back to a chain lookup keyed by the
    # block_hash, which IS populated. Without this fallback the API's
    # payment_block_number=ge=1 validator rejects the post-payment
    # /upload/agent submission with HTTP 422.
    block_hash = _normalize_block_hash(getattr(receipt, "block_hash", None))
    extrinsic_index = int(getattr(receipt, "extrinsic_idx", 0) or 0)

    raw_block_number = getattr(receipt, "block_number", None)
    if raw_block_number:
        block_number = int(raw_block_number)
    else:
        try:
            block_number = int(subtensor.substrate.get_block_number(block_hash))
        except Exception as e:
            raise PaymentSubmissionError(
                f"transfer finalised but block_number could not be resolved "
                f"from block_hash={block_hash}: {e}"
            ) from e

    logger.info(
        f"payment finalised: block={block_number} ext_idx={extrinsic_index} "
        f"block_hash={block_hash}"
    )

    return PaymentReceipt(
        block_hash=block_hash,
        block_number=block_number,
        extrinsic_index=extrinsic_index,
    )


def _normalize_block_hash(raw: str | None) -> str:
    """Ensure the block hash is ``0x``-prefixed lowercase hex.

    Server side enforces ``^0x[0-9a-fA-F]{64}$`` via
    :data:`ditto.api_models.upload._BLOCK_HASH_PATTERN`; the substrate
    SDK can return either with or without the prefix depending on
    version, so we normalise here.
    """
    if not raw:
        raise PaymentSubmissionError(
            "transfer finalised but block_hash was empty on the receipt"
        )
    h = raw if raw.startswith("0x") else f"0x{raw}"
    return h
