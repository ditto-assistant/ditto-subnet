"""Payment-verifier core: chain-side validation of upload-fee extrinsics."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
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
    PaymentProof,
    VerifiedPayment,
)
from ditto.chain.errors import ExtrinsicNotFoundError

if TYPE_CHECKING:
    from typing import Any

    from ditto.api_server.pricing import PriceOracle
    from ditto.chain import ChainClient

logger = logging.getLogger(__name__)

_EXPECTED_CALL_MODULE = "Balances"
_EXPECTED_CALL_FUNCTION = "transfer_keep_alive"


class PaymentVerifier:
    """Verifies a miner-supplied payment proof against the on-chain extrinsic.

    The ``/upload/agent`` orchestrator calls
    :meth:`verify_payment` exactly once per upload attempt. All chain
    I/O is delegated to the injected :class:`ChainClient` +
    :class:`PriceOracle`; the verifier itself owns no resources and is
    safe to share across requests.

    Verification flow (single async path):

    1. Resolve ``block_number`` and require its canonical hash to match the proof.
    2. Fetch the extrinsic via Pylon by ``(block_number, extrinsic_index)``.
    3. Confirm the call is ``Balances.transfer_keep_alive``.
    4. Confirm the chain emitted ``ExtrinsicSuccess`` at the matching index.
    5. Confirm the destination equals the configured upload-payment address.
    6. Confirm the paid ``amount_rao`` equals the admission's TAO-denominated fee.
    7. Confirm the extrinsic signer equals the on-chain coldkey owner of
       the claimed hotkey at the payment block.
    8. Return a :class:`VerifiedPayment` with best-effort USD reporting metadata
       ready for the orchestrator to bind into ``evaluation_payments``.

    Each failure path raises a distinct
    :class:`~ditto.api_server.payment_verifier.errors.PaymentVerifierError`
    subclass so the error envelope can surface a typed 32xx response.

    Usage:
        verifier = create_payment_verifier(chain, oracle, config, address)
        try:
            verified = await verifier.verify_payment(
                proof, hotkey, expected_amount_rao=40_000_000
            )
        except PaymentVerifierError:
            # envelope handler maps to a 402 response with a typed code
            raise
    """

    def __init__(
        self,
        chain: ChainClient,
        oracle: PriceOracle,
        send_address: str,
    ) -> None:
        self._chain = chain
        self._oracle = oracle
        self._send_address = send_address

    async def verify_payment(
        self,
        proof: PaymentProof,
        expected_hotkey: str,
        expected_amount_rao: int,
        legacy_amount_cutoff_at: datetime | None = None,
        expected_send_address: str | None = None,
    ) -> VerifiedPayment:
        """Verify a payment proof end-to-end. See class docstring for flow."""
        # 1. Bind the miner-supplied number/hash pair before combining Pylon's
        # number-keyed extrinsic data with hash-keyed Substrate events/storage.
        canonical_block_hash = (
            await self._chain.get_block_hash(proof.block_number)
        ).lower()
        if canonical_block_hash != proof.block_hash.lower():
            raise PaymentNotFoundOnChain(
                f"block number {proof.block_number} resolves to "
                f"{canonical_block_hash}, not {proof.block_hash}"
            )

        # 2. Pylon: fetch the extrinsic.
        try:
            ext = await self._chain.get_extrinsic(
                proof.block_number, proof.extrinsic_index
            )
        except ExtrinsicNotFoundError as e:
            raise PaymentNotFoundOnChain(
                f"extrinsic at block_number={proof.block_number} "
                f"index={proof.extrinsic_index} not found on chain"
            ) from e

        # 3. Call must be Balances.transfer_keep_alive.
        if (
            ext.call_module != _EXPECTED_CALL_MODULE
            or ext.call_function != _EXPECTED_CALL_FUNCTION
        ):
            raise PaymentCallTypeMismatch(
                f"expected {_EXPECTED_CALL_MODULE}.{_EXPECTED_CALL_FUNCTION}, "
                f"got {ext.call_module}.{ext.call_function}"
            )

        # 4. Substrate event read: confirm success.
        succeeded = await self._chain.check_extrinsic_success(
            canonical_block_hash, proof.extrinsic_index
        )
        if not succeeded:
            raise PaymentExtrinsicFailed(
                f"extrinsic at block_hash={canonical_block_hash} "
                f"index={proof.extrinsic_index} emitted ExtrinsicFailed"
            )

        # 5. Destination address.
        dest = _to_ss58(ext.call_args.get("dest"))
        send_address = expected_send_address or self._send_address
        if dest != send_address:
            raise PaymentDestinationMismatch(
                f"destination {dest!r} does not match configured "
                f"send_address {send_address!r}"
            )

        # 6. Amount must equal the operator-controlled TAO fee reserved before
        # payment. USD never decides admission; it is reporting metadata only.
        try:
            value = int(ext.call_args["value"])
        except (KeyError, TypeError, ValueError) as e:
            raise PaymentCallTypeMismatch(
                f"extrinsic call_args missing or non-integer value: {ext.call_args!r}"
            ) from e
        block_ts_seconds = await self._chain.get_block_timestamp(canonical_block_hash)
        block_ts = datetime.fromtimestamp(block_ts_seconds, tz=UTC)
        legacy_cutoff = (
            legacy_amount_cutoff_at.replace(tzinfo=UTC)
            if legacy_amount_cutoff_at is not None
            and legacy_amount_cutoff_at.tzinfo is None
            else legacy_amount_cutoff_at
        )
        accepted_under_legacy_fee_amnesty = (
            value > 0
            and value != expected_amount_rao
            and legacy_cutoff is not None
            and block_ts <= legacy_cutoff
        )
        if value != expected_amount_rao and not accepted_under_legacy_fee_amnesty:
            raise PaymentAmountMismatch(
                f"paid {value} rao, expected {expected_amount_rao} rao"
            )

        # 7. Signer must equal the on-chain coldkey owner of the hotkey.
        on_chain_coldkey = await self._chain.get_coldkey_for_hotkey(
            expected_hotkey, canonical_block_hash
        )
        if _to_ss58(ext.signer_address) != _to_ss58(on_chain_coldkey):
            raise PaymentSignerMismatch(
                f"extrinsic signer {ext.signer_address!r} does not match "
                f"on-chain coldkey {on_chain_coldkey!r} for hotkey "
                f"{expected_hotkey} at block {canonical_block_hash}"
            )

        try:
            tao_usd_rate = await self._oracle.get_tao_usd()
        except Exception as error:
            logger.warning("TAO/USD reporting price unavailable: %s", error)
            tao_usd_rate = None

        verified = VerifiedPayment(
            block_hash=canonical_block_hash,
            extrinsic_index=proof.extrinsic_index,
            miner_hotkey=expected_hotkey,
            miner_coldkey=on_chain_coldkey,
            amount_rao=value,
            tao_usd_rate=tao_usd_rate,
            dest_address=dest,
            block_timestamp=block_ts,
            accepted_under_legacy_fee_amnesty=accepted_under_legacy_fee_amnesty,
        )
        if accepted_under_legacy_fee_amnesty:
            assert legacy_cutoff is not None
            logger.warning(
                "accepted legacy upload payment hotkey=%s paid_rao=%s "
                "expected_rao=%s block_hash=%s idx=%s cutoff=%s",
                expected_hotkey,
                value,
                expected_amount_rao,
                canonical_block_hash,
                proof.extrinsic_index,
                legacy_cutoff.isoformat(),
            )
        logger.info(
            f"payment verified hotkey={expected_hotkey} amount_rao={value} "
            f"block_hash={canonical_block_hash} idx={proof.extrinsic_index}"
        )
        return verified


def _to_ss58(raw: Any) -> str:
    """Normalise a Pylon address arg (dest or signer) to a plain SS58 string.

    Pylon's flattened ``call_args`` carries the destination as an SS58
    string (``"5..."``), a ``0x``-prefixed 32-byte public-key hex, or a
    ``{"Id": ...}`` wrapper around either -- the substrate-interface
    decode shapes for ``MultiAddress::Id``; which one appears depends on
    the chain's metadata. Unify to SS58 so the equality check is
    encoding-agnostic. Any other shape returns an empty string and fails
    the check with a clean :class:`PaymentDestinationMismatch`.
    """
    if isinstance(raw, dict):
        raw = raw.get("Id")
    if not isinstance(raw, str):
        return ""
    raw = raw.strip()
    if raw.startswith("0x") and len(raw) == 66:
        # 32-byte public-key hex -> SS58 (bittensor ss58 format = 42).
        from scalecodec.utils.ss58 import ss58_encode

        return ss58_encode(bytes.fromhex(raw[2:]), 42)
    return raw
