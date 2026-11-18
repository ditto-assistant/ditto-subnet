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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ditto.miner_cli.errors import (
    PaymentFinalizationTimeoutError,
    PaymentSignerMismatchError,
    PaymentSubmissionError,
)
from ditto.miner_cli.models import PaymentReceipt

if TYPE_CHECKING:
    import bittensor

logger = logging.getLogger(__name__)


def preflight_payment_signer(
    *,
    live_wallet: bittensor.Wallet,
    hotkey_ss58: str,
    subtensor_network: str,
    chain_endpoint: str | None = None,
) -> None:
    """Verify the payment signer owns the claimed hotkey on chain.

    Wallet directory layout is not evidence of ownership: a hotkey file can
    be stored beneath any named coldkey wallet. The upload API checks the
    authoritative ``SubtensorModule.Owner`` entry at the payment block, so the
    CLI performs the same check before showing the payment confirmation.

    Raises:
        PaymentSignerMismatchError: The selected coldkey is not the hotkey's
            current on-chain owner, or the hotkey has no on-chain owner.
        PaymentSubmissionError: The signer address or on-chain owner could not
            be resolved. The check fails closed so no payment is risked.
    """
    chain_target = chain_endpoint or subtensor_network

    try:
        signer_ss58 = str(live_wallet.coldkeypub.ss58_address)
    except Exception as e:
        raise PaymentSubmissionError(
            f"could not read the selected wallet's coldkey address: {e}; "
            "refusing payment because signer ownership could not be verified"
        ) from e

    try:
        subtensor = _connect_subtensor(chain_target)
        owner_ss58 = subtensor.get_hotkey_owner(hotkey_ss58)
    except PaymentSubmissionError:
        raise
    except Exception as e:
        raise PaymentSubmissionError(
            f"could not verify on-chain owner for hotkey {hotkey_ss58} "
            f"on subtensor {chain_target!r}: {e}; refusing payment"
        ) from e

    if owner_ss58 is None:
        raise PaymentSignerMismatchError(
            f"hotkey {hotkey_ss58} has no on-chain owner on subtensor "
            f"{chain_target!r}; refusing payment before funds are sent"
        )

    if signer_ss58 != str(owner_ss58):
        raise PaymentSignerMismatchError(
            "payment signer mismatch detected before payment:\n"
            f"  hotkey:                {hotkey_ss58}\n"
            f"  on-chain owner:         {owner_ss58}\n"
            f"  selected wallet signer: {signer_ss58}\n"
            "The wallet folder association does not establish ownership. "
            "Select the coldkey that owns this hotkey on chain. "
            "No funds were sent."
        )


def submit_eval_payment(
    *,
    live_wallet: bittensor.Wallet,
    subtensor_network: str,
    amount_rao: int,
    dest_address: str,
    chain_endpoint: str | None = None,
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
        chain_endpoint: Optional explicit chain URL override. When
            provided, used in place of ``subtensor_network`` as the
            value passed to :class:`bittensor.Subtensor`'s ``network``
            arg (the SDK accepts either a known identifier or a full
            WebSocket URL on that arg). Used for smoke testing against
            a non-canonical endpoint (a hosted local subtensor, a
            testnet pre-DNS WebSocket, etc.). ``None`` means use the
            ``subtensor_network`` identifier and let the SDK resolve
            the default URL.

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
        + (f" via chain_endpoint={chain_endpoint}" if chain_endpoint else "")
    )

    # The bittensor SDK's ``network`` arg accepts either a known
    # identifier ("finney", "test", "local") or a full WebSocket URL
    # (e.g. "ws://example.org:9944"). When the caller supplies an
    # explicit ``chain_endpoint``, pass the URL directly; otherwise
    # use the network identifier from the locked-pair lookup.
    chain_target = chain_endpoint or subtensor_network
    subtensor = _connect_subtensor(chain_target)

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


@dataclass(frozen=True)
class ChainTransfer:
    """What one finalized extrinsic on chain actually did (read-only)."""

    call: str
    """``Module.function``, e.g. ``Balances.transfer_keep_alive``."""
    signer: str
    dest: str
    amount_rao: int | None
    timestamp_ms: int | None


def read_transfer(
    *,
    receipt: PaymentReceipt,
    subtensor_network: str,
    chain_endpoint: str | None = None,
) -> ChainTransfer:
    """Read the extrinsic a proof points at, without signing or sending anything.

    Binds the block number to the block hash first, as Platform's verifier
    does, so a proof cannot pair one block's number with another's hash.

    Raises:
        PaymentSubmissionError: the chain is unreachable, the hash does not
            belong to that block number, or no extrinsic sits at the index.
    """
    from ditto.chain.models import _call_args_to_dict, normalize_address_to_ss58

    subtensor = _connect_subtensor(chain_endpoint or subtensor_network)
    try:
        substrate = subtensor.substrate
        canonical = str(substrate.get_block_hash(receipt.block_number) or "")
        if canonical.lower() != receipt.block_hash.lower():
            raise PaymentSubmissionError(
                f"block {receipt.block_number} is {canonical or 'unknown'}, "
                f"not {receipt.block_hash}"
            )
        extrinsics = substrate.get_extrinsics(block_hash=receipt.block_hash) or []
        if receipt.extrinsic_index >= len(extrinsics):
            raise PaymentSubmissionError(
                f"no extrinsic {receipt.extrinsic_index} in block "
                f"{receipt.block_number}"
            )
        raw = extrinsics[receipt.extrinsic_index]
        value: Any = getattr(raw, "value", raw)
        call = value.get("call") if isinstance(value, dict) else None
        call = call if isinstance(call, dict) else {}
        args = _call_args_to_dict(call.get("call_args"))
        timestamp = substrate.query("Timestamp", "Now", block_hash=receipt.block_hash)
    except PaymentSubmissionError:
        raise
    except Exception as error:
        raise PaymentSubmissionError(
            f"could not read the transfer at block {receipt.block_number} "
            f"index {receipt.extrinsic_index}: {error}"
        ) from error
    dest = args.get("dest")
    if isinstance(dest, dict):
        dest = dest.get("Id")
    amount = args.get("value")
    stamp = getattr(timestamp, "value", timestamp)
    return ChainTransfer(
        call=f"{call.get('call_module')}.{call.get('call_function')}",
        signer=normalize_address_to_ss58(str(value.get("address") or "")),
        dest=normalize_address_to_ss58(dest) if isinstance(dest, str) else "",
        amount_rao=amount
        if isinstance(amount, int) and not isinstance(amount, bool)
        else None,
        timestamp_ms=stamp if isinstance(stamp, int) else None,
    )


def _connect_subtensor(chain_target: str) -> bittensor.Subtensor:
    """Construct a Subtensor client and translate connection failures."""
    import bittensor

    try:
        return bittensor.Subtensor(network=chain_target)
    except Exception as e:
        raise PaymentSubmissionError(
            f"could not connect to subtensor {chain_target!r}: {e}"
        ) from e


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
