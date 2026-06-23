"""Unit tests for :mod:`ditto.api_server.payment_verifier.verifier`.

Chain + oracle are mocked at the module boundary. Each verifier branch
gets a dedicated test so a regression on any single check fails on a
named test, not a generic happy-path explosion.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ditto.api_server.payment_verifier import (
    PAYMENT_DRIFT_TOLERANCE,
    PaymentAmountMismatch,
    PaymentCallTypeMismatch,
    PaymentDestinationMismatch,
    PaymentExtrinsicFailed,
    PaymentNotFoundOnChain,
    PaymentProof,
    PaymentSignerMismatch,
    PaymentVerifier,
    VerifiedPayment,
)
from ditto.api_server.pricing import PricingConfig
from ditto.chain.errors import ChainConnectionError, ExtrinsicNotFoundError

# At fee_usd=5, fee_buffer=1.4, price=400 USD/TAO:
#   fee_tao = 5 * 1.4 / 400 = 0.0175 TAO
#   quote_rao = 17_500_000
QUOTE_RAO = 17_500_000


def _make_pricing_config(**overrides: Any) -> PricingConfig:
    defaults: dict[str, Any] = {
        "fee_usd": Decimal("5"),
        "fee_buffer": Decimal("1.4"),
        "cache_ttl_seconds": 3600,
        "max_stale_seconds": 86400,
        "coingecko_timeout_seconds": 5.0,
        "override_tao_usd": None,
    }
    defaults.update(overrides)
    return PricingConfig(**defaults)


def _make_proof(**overrides: Any) -> PaymentProof:
    defaults: dict[str, Any] = {
        "block_hash": "0xblock",
        "block_number": 100,
        "extrinsic_index": 7,
    }
    defaults.update(overrides)
    return PaymentProof(**defaults)


def _make_extrinsic_info(
    *,
    call_module: str = "Balances",
    call_function: str = "transfer_keep_alive",
    dest: Any = "5SendAddress",
    value: int = QUOTE_RAO,
    signer: str = "5Coldkey",
) -> MagicMock:
    """Mirror :class:`ditto.chain.ExtrinsicInfo` shape (only the fields the
    verifier reads). Returns a MagicMock so we don't depend on the real
    frozen dataclass constructor for this fixture."""
    info = MagicMock()
    info.call_module = call_module
    info.call_function = call_function
    info.call_args = {"dest": dest, "value": value}
    info.signer_address = signer
    return info


def _make_verifier(
    *,
    extrinsic_info: MagicMock | None = None,
    extrinsic_side_effect: Exception | None = None,
    success: bool = True,
    success_side_effect: Exception | None = None,
    coldkey: str = "5Coldkey",
    coldkey_side_effect: Exception | None = None,
    block_timestamp: int = 1_700_000_000,
    timestamp_side_effect: Exception | None = None,
    price_usd: Decimal = Decimal("400"),
    send_address: str = "5SendAddress",
    pricing_config: PricingConfig | None = None,
) -> PaymentVerifier:
    chain = MagicMock()
    if extrinsic_side_effect is not None:
        chain.get_extrinsic = AsyncMock(side_effect=extrinsic_side_effect)
    else:
        chain.get_extrinsic = AsyncMock(
            return_value=extrinsic_info or _make_extrinsic_info()
        )
    if success_side_effect is not None:
        chain.check_extrinsic_success = AsyncMock(side_effect=success_side_effect)
    else:
        chain.check_extrinsic_success = AsyncMock(return_value=success)
    if coldkey_side_effect is not None:
        chain.get_coldkey_for_hotkey = AsyncMock(side_effect=coldkey_side_effect)
    else:
        chain.get_coldkey_for_hotkey = AsyncMock(return_value=coldkey)
    if timestamp_side_effect is not None:
        chain.get_block_timestamp = AsyncMock(side_effect=timestamp_side_effect)
    else:
        chain.get_block_timestamp = AsyncMock(return_value=block_timestamp)

    oracle = MagicMock()
    oracle.get_tao_usd = AsyncMock(return_value=price_usd)

    return PaymentVerifier(
        chain=chain,
        oracle=oracle,
        pricing_config=pricing_config or _make_pricing_config(),
        send_address=send_address,
    )


class TestVerifyPaymentHappyPath:
    async def test_happy_path_returns_verified_payment(self):
        verifier = _make_verifier()
        result = await verifier.verify_payment(_make_proof(), expected_hotkey="5Hotkey")
        assert isinstance(result, VerifiedPayment)
        assert result.block_hash == "0xblock"
        assert result.extrinsic_index == 7
        assert result.miner_hotkey == "5Hotkey"
        assert result.miner_coldkey == "5Coldkey"
        assert result.amount_rao == QUOTE_RAO
        assert result.dest_address == "5SendAddress"
        assert result.block_timestamp == datetime.fromtimestamp(1_700_000_000, tz=UTC)

    async def test_accepts_dict_shaped_dest(self):
        """Pylon flattens dest as either str or ``{"Id": "5..."}`` depending
        on SDK decode. Verifier normalises both."""
        ext = _make_extrinsic_info(dest={"Id": "5SendAddress"})
        verifier = _make_verifier(extrinsic_info=ext)
        result = await verifier.verify_payment(_make_proof(), expected_hotkey="5Hotkey")
        assert result.dest_address == "5SendAddress"


class TestExtrinsicLookup:
    async def test_not_found_raises_typed(self):
        verifier = _make_verifier(extrinsic_side_effect=ExtrinsicNotFoundError("nope"))
        with pytest.raises(PaymentNotFoundOnChain):
            await verifier.verify_payment(_make_proof(), expected_hotkey="5Hotkey")

    async def test_chain_connection_error_propagates(self):
        """ChainConnectionError must NOT be swallowed; envelope handler
        already maps it to 503. Catching it here would lose typed signal."""
        verifier = _make_verifier(
            extrinsic_side_effect=ChainConnectionError("pylon down")
        )
        with pytest.raises(ChainConnectionError):
            await verifier.verify_payment(_make_proof(), expected_hotkey="5Hotkey")


class TestCallType:
    async def test_wrong_module_rejected(self):
        ext = _make_extrinsic_info(call_module="System")
        verifier = _make_verifier(extrinsic_info=ext)
        with pytest.raises(PaymentCallTypeMismatch):
            await verifier.verify_payment(_make_proof(), expected_hotkey="5Hotkey")

    async def test_wrong_function_rejected(self):
        ext = _make_extrinsic_info(call_function="transfer_allow_death")
        verifier = _make_verifier(extrinsic_info=ext)
        with pytest.raises(PaymentCallTypeMismatch):
            await verifier.verify_payment(_make_proof(), expected_hotkey="5Hotkey")

    async def test_missing_value_arg_rejected_as_call_type(self):
        ext = _make_extrinsic_info()
        ext.call_args = {"dest": "5SendAddress"}  # value key missing
        verifier = _make_verifier(extrinsic_info=ext)
        with pytest.raises(PaymentCallTypeMismatch):
            await verifier.verify_payment(_make_proof(), expected_hotkey="5Hotkey")


class TestSuccessEvent:
    async def test_failed_extrinsic_rejected(self):
        verifier = _make_verifier(success=False)
        with pytest.raises(PaymentExtrinsicFailed):
            await verifier.verify_payment(_make_proof(), expected_hotkey="5Hotkey")


class TestDestination:
    # Real SS58 + its matching hex pubkey. The verifier must decode the
    # hex form to SS58 before comparing. Without this normalisation,
    # the Pylon-returned dest (hex on recent subtensor releases) never
    # matches the configured SS58 send_address and every upload
    # post-payment fails with PaymentDestinationMismatch.
    _REAL_SS58 = "5GNfk6UnxmxtsC8a7p556DR2RMQRj8KfCYuN7DDLMxYxQ9GD"
    _REAL_HEX = "0xbea43ca9f879e54d833afeab197db4cbdd399297bc87f0914c90139de670fa6f"

    async def test_wrong_dest_rejected(self):
        ext = _make_extrinsic_info(dest="5SomeoneElse")
        verifier = _make_verifier(extrinsic_info=ext)
        with pytest.raises(PaymentDestinationMismatch):
            await verifier.verify_payment(_make_proof(), expected_hotkey="5Hotkey")

    async def test_unparseable_dest_rejected_as_mismatch(self):
        ext = _make_extrinsic_info(dest=12345)  # int, not str/dict
        verifier = _make_verifier(extrinsic_info=ext)
        with pytest.raises(PaymentDestinationMismatch):
            await verifier.verify_payment(_make_proof(), expected_hotkey="5Hotkey")

    async def test_hex_pubkey_dest_decodes_to_ss58(self):
        """Pylon returns dest as a 0x-prefixed raw account ID on recent
        subtensor versions; verifier must encode it back to SS58 before
        the equality check.
        """
        ext = _make_extrinsic_info(dest=self._REAL_HEX)
        verifier = _make_verifier(extrinsic_info=ext, send_address=self._REAL_SS58)
        result = await verifier.verify_payment(_make_proof(), expected_hotkey="5Hotkey")
        assert result.dest_address == self._REAL_SS58

    async def test_dict_with_hex_inner_decodes_to_ss58(self):
        """If Pylon wraps the hex form in {'Id': '0x...'}, same
        normalisation must fire.
        """
        ext = _make_extrinsic_info(dest={"Id": self._REAL_HEX})
        verifier = _make_verifier(extrinsic_info=ext, send_address=self._REAL_SS58)
        result = await verifier.verify_payment(_make_proof(), expected_hotkey="5Hotkey")
        assert result.dest_address == self._REAL_SS58

    async def test_malformed_hex_dest_rejected_as_mismatch(self):
        """Hex with odd characters / wrong length must fail the equality
        check cleanly, not raise an unhandled exception."""
        ext = _make_extrinsic_info(dest="0xnotvalidhex")
        verifier = _make_verifier(extrinsic_info=ext)
        with pytest.raises(PaymentDestinationMismatch):
            await verifier.verify_payment(_make_proof(), expected_hotkey="5Hotkey")


class TestAmountBand:
    @pytest.mark.parametrize(
        "delta_pct",
        [Decimal("-0.5"), Decimal("-0.05"), Decimal("-0.021")],
    )
    async def test_below_lower_band_rejected(self, delta_pct: Decimal):
        value = int(QUOTE_RAO * (Decimal(1) + delta_pct))
        ext = _make_extrinsic_info(value=value)
        verifier = _make_verifier(extrinsic_info=ext)
        with pytest.raises(PaymentAmountMismatch):
            await verifier.verify_payment(_make_proof(), expected_hotkey="5Hotkey")

    @pytest.mark.parametrize(
        "delta_pct",
        [Decimal("0.021"), Decimal("0.05"), Decimal("0.5")],
    )
    async def test_above_upper_band_rejected(self, delta_pct: Decimal):
        value = int(QUOTE_RAO * (Decimal(1) + delta_pct))
        ext = _make_extrinsic_info(value=value)
        verifier = _make_verifier(extrinsic_info=ext)
        with pytest.raises(PaymentAmountMismatch):
            await verifier.verify_payment(_make_proof(), expected_hotkey="5Hotkey")

    async def test_exact_lower_band_edge_accepts(self):
        lower = int(QUOTE_RAO * (Decimal(1) - PAYMENT_DRIFT_TOLERANCE))
        ext = _make_extrinsic_info(value=lower)
        verifier = _make_verifier(extrinsic_info=ext)
        result = await verifier.verify_payment(_make_proof(), expected_hotkey="5Hotkey")
        assert result.amount_rao == lower

    async def test_exact_upper_band_edge_accepts(self):
        upper = int(QUOTE_RAO * (Decimal(1) + PAYMENT_DRIFT_TOLERANCE))
        ext = _make_extrinsic_info(value=upper)
        verifier = _make_verifier(extrinsic_info=ext)
        result = await verifier.verify_payment(_make_proof(), expected_hotkey="5Hotkey")
        assert result.amount_rao == upper


class TestSignerOwnership:
    async def test_signer_not_owner_rejected(self):
        ext = _make_extrinsic_info(signer="5Different")
        verifier = _make_verifier(extrinsic_info=ext, coldkey="5Coldkey")
        with pytest.raises(PaymentSignerMismatch):
            await verifier.verify_payment(_make_proof(), expected_hotkey="5Hotkey")

    async def test_owner_lookup_not_found_propagates(self):
        """If the hotkey was not registered at the payment block, the
        chain layer raises ExtrinsicNotFoundError. That propagates rather
        than being silently converted, because the envelope handler maps
        it via the chain-error path (no specific 32xx for "hotkey not
        registered at payment block"; a tighter type can land later if
        observed in practice)."""
        verifier = _make_verifier(
            coldkey_side_effect=ExtrinsicNotFoundError("no owner")
        )
        with pytest.raises(ExtrinsicNotFoundError):
            await verifier.verify_payment(_make_proof(), expected_hotkey="5Hotkey")
