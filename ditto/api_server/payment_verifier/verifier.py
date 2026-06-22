"""Payment-verifier core: chain-side validation of upload-fee extrinsics."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from ditto.api_server.payment_verifier.errors import (
    PaymentAmountMismatch,
    PaymentCallTypeMismatch,
    PaymentDestinationMismatch,
    PaymentExtrinsicFailed,
    PaymentNotFoundOnChain,
    PaymentSignerMismatch,
)
from ditto.api_server.payment_verifier.models import (
    PAYMENT_DRIFT_TOLERANCE,
    PaymentProof,
    VerifiedPayment,
)
from ditto.chain.errors import ExtrinsicNotFoundError

if TYPE_CHECKING:
    from typing import Any

    from ditto.api_server.pricing import PriceOracle, PricingConfig
    from ditto.chain import ChainClient

logger = logging.getLogger(__name__)

_EXPECTED_CALL_MODULE = "Balances"
_EXPECTED_CALL_FUNCTION = "transfer_keep_alive"

_RAO_PER_TAO = Decimal("1e9")


class PaymentVerifier:
    """Verifies a miner-supplied payment proof against the on-chain extrinsic.

    The orchestrator (``/upload/agent``, next PR) calls
    :meth:`verify_payment` exactly once per upload attempt. All chain
    I/O is delegated to the injected :class:`ChainClient` +
    :class:`PriceOracle`; the verifier itself owns no resources and is
    safe to share across requests.

    Verification flow (single async path):

    1. Fetch the extrinsic via Pylon by ``(block_number, extrinsic_index)``.
    2. Confirm the call is ``Balances.transfer_keep_alive``.
    3. Confirm the chain emitted ``ExtrinsicSuccess`` at the matching index.
    4. Confirm the destination equals the configured upload-payment address.
    5. Confirm the paid ``amount_rao`` falls in the band around the
       recomputed quote (``±PAYMENT_DRIFT_TOLERANCE``).
    6. Confirm the extrinsic signer equals the on-chain coldkey owner of
       the claimed hotkey at the payment block.
    7. Pull the block timestamp and return a :class:`VerifiedPayment`
       ready for the orchestrator to bind into ``evaluation_payments``.

    Each failure path raises a distinct
    :class:`~ditto.api_server.payment_verifier.errors.PaymentVerifierError`
    subclass so the error envelope can surface a typed 32xx response.

    Usage:
        verifier = create_payment_verifier(chain, oracle, config, address)
        try:
            verified = await verifier.verify_payment(proof, hotkey)
        except PaymentVerifierError:
            # envelope handler maps to a 402 response with a typed code
            raise
    """

    def __init__(
        self,
        chain: ChainClient,
        oracle: PriceOracle,
        pricing_config: PricingConfig,
        send_address: str,
    ) -> None:
        self._chain = chain
        self._oracle = oracle
        self._pricing_config = pricing_config
        self._send_address = send_address

    async def verify_payment(
        self, proof: PaymentProof, expected_hotkey: str
    ) -> VerifiedPayment:
        """Verify a payment proof end-to-end. See class docstring for flow."""
        # 1. Pylon: fetch the extrinsic.
        try:
            ext = await self._chain.get_extrinsic(
                proof.block_number, proof.extrinsic_index
            )
        except ExtrinsicNotFoundError as e:
            raise PaymentNotFoundOnChain(
                f"extrinsic at block_number={proof.block_number} "
                f"index={proof.extrinsic_index} not found on chain"
            ) from e

        # 2. Call must be Balances.transfer_keep_alive.
        if (
            ext.call_module != _EXPECTED_CALL_MODULE
            or ext.call_function != _EXPECTED_CALL_FUNCTION
        ):
            raise PaymentCallTypeMismatch(
                f"expected {_EXPECTED_CALL_MODULE}.{_EXPECTED_CALL_FUNCTION}, "
                f"got {ext.call_module}.{ext.call_function}"
            )

        # 3. Substrate event read: confirm success.
        succeeded = await self._chain.check_extrinsic_success(
            proof.block_hash, proof.extrinsic_index
        )
        if not succeeded:
            raise PaymentExtrinsicFailed(
                f"extrinsic at block_hash={proof.block_hash} "
                f"index={proof.extrinsic_index} emitted ExtrinsicFailed"
            )

        # 4. Destination address.
        dest = _decode_dest(ext.call_args.get("dest"))
        if dest != self._send_address:
            raise PaymentDestinationMismatch(
                f"destination {dest!r} does not match configured "
                f"send_address {self._send_address!r}"
            )

        # 5. Amount within recomputed-quote band.
        try:
            value = int(ext.call_args["value"])
        except (KeyError, TypeError, ValueError) as e:
            raise PaymentCallTypeMismatch(
                f"extrinsic call_args missing or non-integer value: {ext.call_args!r}"
            ) from e
        quote_rao = await self._recompute_quote_rao()
        lower = int(quote_rao * (Decimal(1) - PAYMENT_DRIFT_TOLERANCE))
        upper = int(quote_rao * (Decimal(1) + PAYMENT_DRIFT_TOLERANCE))
        if value < lower or value > upper:
            raise PaymentAmountMismatch(
                f"paid {value} rao outside band [{lower}, {upper}] "
                f"(quote {quote_rao}, tolerance {PAYMENT_DRIFT_TOLERANCE})"
            )

        # 6. Signer must equal the on-chain coldkey owner of the hotkey.
        on_chain_coldkey = await self._chain.get_coldkey_for_hotkey(
            expected_hotkey, proof.block_hash
        )
        if ext.signer_address != on_chain_coldkey:
            raise PaymentSignerMismatch(
                f"extrinsic signer {ext.signer_address!r} does not match "
                f"on-chain coldkey {on_chain_coldkey!r} for hotkey "
                f"{expected_hotkey} at block {proof.block_hash}"
            )

        # 7. Block timestamp.
        block_ts_seconds = await self._chain.get_block_timestamp(proof.block_hash)
        block_ts = datetime.fromtimestamp(block_ts_seconds, tz=UTC)

        verified = VerifiedPayment(
            block_hash=proof.block_hash,
            extrinsic_index=proof.extrinsic_index,
            miner_hotkey=expected_hotkey,
            miner_coldkey=on_chain_coldkey,
            amount_rao=value,
            dest_address=dest,
            block_timestamp=block_ts,
        )
        logger.info(
            f"payment verified hotkey={expected_hotkey} amount_rao={value} "
            f"block_hash={proof.block_hash} idx={proof.extrinsic_index}"
        )
        return verified

    async def _recompute_quote_rao(self) -> Decimal:
        """Recompute the current upload-fee quote in rao.

        Reuses the same formula as ``/upload/eval-pricing`` so a verify-
        time recompute that hits the same oracle cache entry produces an
        identical integer rao value. Recompute deliberately stays in
        :class:`Decimal` end-to-end; the band-bounds cast back to ``int``
        only at the comparison boundary.
        """
        price_usd = await self._oracle.get_tao_usd()
        fee_tao = (
            self._pricing_config.fee_usd * self._pricing_config.fee_buffer
        ) / price_usd
        return fee_tao * _RAO_PER_TAO


# Bittensor chains (including localnet built on the standard subtensor
# image) use the generic substrate SS58 prefix. The verifier compares
# against the configured send_address which is the operator's SS58 string
# in the same format.
_BITTENSOR_SS58_PREFIX = 42


def _decode_dest(raw: Any) -> str:
    """Normalise the Pylon ``dest`` arg to a plain SS58 string.

    Pylon's flattened ``call_args`` carries the destination as one of
    three shapes depending on the upstream decode path:

    - plain SS58 string (``"5..."``)
    - ``{"Id": "5..."}`` dict (substrate-interface ``MultiAddress::Id``)
    - hex-encoded raw account ID (``"0x..."``) - the canonical shape
      Pylon returns for transfer_keep_alive on recent subtensor
      releases; the verifier rehydrates this to SS58 before the
      equality check.

    The verifier compares against a string ``send_address``, so unify
    here. Any unrecognised shape returns an empty string and fails the
    equality check with a clean :class:`PaymentDestinationMismatch`.
    """
    if isinstance(raw, str):
        return _maybe_hex_to_ss58(raw)
    if isinstance(raw, dict):
        inner = raw.get("Id")
        if isinstance(inner, str):
            return _maybe_hex_to_ss58(inner)
    return ""


def _maybe_hex_to_ss58(s: str) -> str:
    """Return ``s`` as SS58. If it is a ``0x``-prefixed hex pubkey,
    encode it; otherwise return the string unchanged.
    """
    if not s.startswith("0x"):
        return s
    try:
        from scalecodec.utils.ss58 import ss58_encode

        return ss58_encode(bytes.fromhex(s[2:]), ss58_format=_BITTENSOR_SS58_PREFIX)
    except Exception:
        return ""
