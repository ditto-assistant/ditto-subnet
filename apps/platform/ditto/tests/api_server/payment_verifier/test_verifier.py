"""Unit tests for :mod:`ditto.api_server.payment_verifier.verifier`.

Chain + oracle are mocked at the module boundary. Each verifier branch
gets a dedicated test so a regression on any single check fails on a
named test, not a generic happy-path explosion.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ditto.api_server.payment_verifier import (
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
from ditto.api_server.payment_verifier.verifier import _to_ss58
from ditto.chain.errors import ChainConnectionError, ExtrinsicNotFoundError

QUOTE_RAO = 40_000_000

# Alice's well-known account: ss58 <-> 32-byte public key. Some chains' Pylon
# decodes extrinsic addresses as hex pubkeys, so the verifier must normalize.
_ALICE_SS58 = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
_ALICE_PUBKEY = "0xd43593c715fdd31c61141abd04a99fd6822c8558854ccde39a5684e7a56da27d"


class TestToSs58:
    """Address normalization: ss58 passthrough + hex-pubkey -> ss58."""

    def test_ss58_passthrough(self):
        assert _to_ss58(_ALICE_SS58) == _ALICE_SS58

    def test_hex_pubkey_to_ss58(self):
        assert _to_ss58(_ALICE_PUBKEY) == _ALICE_SS58

    def test_dict_id_hex_pubkey_to_ss58(self):
        assert _to_ss58({"Id": _ALICE_PUBKEY}) == _ALICE_SS58

    def test_dict_id_ss58_passthrough(self):
        assert _to_ss58({"Id": _ALICE_SS58}) == _ALICE_SS58

    def test_unparseable_returns_empty(self):
        assert _to_ss58(None) == ""
        assert _to_ss58(12345) == ""


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
    canonical_block_hash: str = "0xblock",
    block_hash_side_effect: Exception | None = None,
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
) -> PaymentVerifier:
    chain = MagicMock()
    if block_hash_side_effect is not None:
        chain.get_block_hash = AsyncMock(side_effect=block_hash_side_effect)
    else:
        chain.get_block_hash = AsyncMock(return_value=canonical_block_hash)
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
        send_address=send_address,
    )


class TestVerifyPaymentHappyPath:
    async def test_happy_path_returns_verified_payment(self):
        verifier = _make_verifier()
        result = await verifier.verify_payment(
            _make_proof(), expected_hotkey="5Hotkey", expected_amount_rao=QUOTE_RAO
        )
        assert isinstance(result, VerifiedPayment)
        assert result.block_hash == "0xblock"
        assert result.extrinsic_index == 7
        assert result.miner_hotkey == "5Hotkey"
        assert result.miner_coldkey == "5Coldkey"
        assert result.amount_rao == QUOTE_RAO
        assert result.tao_usd_rate == Decimal("400")
        assert result.dest_address == "5SendAddress"
        assert result.block_timestamp == datetime.fromtimestamp(1_700_000_000, tz=UTC)

    async def test_accepts_dict_shaped_dest(self):
        """Pylon flattens dest as either str or ``{"Id": "5..."}`` depending
        on SDK decode. Verifier normalises both."""
        ext = _make_extrinsic_info(dest={"Id": "5SendAddress"})
        verifier = _make_verifier(extrinsic_info=ext)
        result = await verifier.verify_payment(
            _make_proof(), expected_hotkey="5Hotkey", expected_amount_rao=QUOTE_RAO
        )
        assert result.dest_address == "5SendAddress"

    async def test_usd_reporting_outage_does_not_reject_payment(self):
        verifier = _make_verifier()
        verifier._oracle.get_tao_usd = AsyncMock(side_effect=RuntimeError("down"))

        result = await verifier.verify_payment(
            _make_proof(),
            expected_hotkey="5Hotkey",
            expected_amount_rao=QUOTE_RAO,
        )

        assert result.amount_rao == QUOTE_RAO
        assert result.tao_usd_rate is None


class TestExtrinsicLookup:
    async def test_block_number_hash_mismatch_is_rejected(self):
        verifier = _make_verifier(canonical_block_hash="0xcanonical")

        with pytest.raises(PaymentNotFoundOnChain):
            await verifier.verify_payment(
                _make_proof(block_hash="0xsupplied"),
                expected_hotkey="5Hotkey",
                expected_amount_rao=QUOTE_RAO,
            )

        verifier._chain.get_extrinsic.assert_not_awaited()

    async def test_block_hash_is_canonicalized_before_storage_reads(self):
        verifier = _make_verifier(canonical_block_hash="0xBLOCK")

        result = await verifier.verify_payment(
            _make_proof(block_hash="0xblock"),
            expected_hotkey="5Hotkey",
            expected_amount_rao=QUOTE_RAO,
        )

        assert result.block_hash == "0xblock"
        verifier._chain.check_extrinsic_success.assert_awaited_once_with("0xblock", 7)
        verifier._chain.get_block_timestamp.assert_awaited_once_with("0xblock")
        verifier._chain.get_coldkey_for_hotkey.assert_awaited_once_with(
            "5Hotkey", "0xblock"
        )

    async def test_not_found_raises_typed(self):
        verifier = _make_verifier(extrinsic_side_effect=ExtrinsicNotFoundError("nope"))
        with pytest.raises(PaymentNotFoundOnChain):
            await verifier.verify_payment(
                _make_proof(), expected_hotkey="5Hotkey", expected_amount_rao=QUOTE_RAO
            )

    async def test_chain_connection_error_propagates(self):
        """ChainConnectionError must NOT be swallowed; envelope handler
        already maps it to 503. Catching it here would lose typed signal."""
        verifier = _make_verifier(
            extrinsic_side_effect=ChainConnectionError("pylon down")
        )
        with pytest.raises(ChainConnectionError):
            await verifier.verify_payment(
                _make_proof(), expected_hotkey="5Hotkey", expected_amount_rao=QUOTE_RAO
            )


class TestCallType:
    async def test_wrong_module_rejected(self):
        ext = _make_extrinsic_info(call_module="System")
        verifier = _make_verifier(extrinsic_info=ext)
        with pytest.raises(PaymentCallTypeMismatch):
            await verifier.verify_payment(
                _make_proof(), expected_hotkey="5Hotkey", expected_amount_rao=QUOTE_RAO
            )

    async def test_wrong_function_rejected(self):
        ext = _make_extrinsic_info(call_function="transfer_allow_death")
        verifier = _make_verifier(extrinsic_info=ext)
        with pytest.raises(PaymentCallTypeMismatch):
            await verifier.verify_payment(
                _make_proof(), expected_hotkey="5Hotkey", expected_amount_rao=QUOTE_RAO
            )

    async def test_missing_value_arg_rejected_as_call_type(self):
        ext = _make_extrinsic_info()
        ext.call_args = {"dest": "5SendAddress"}  # value key missing
        verifier = _make_verifier(extrinsic_info=ext)
        with pytest.raises(PaymentCallTypeMismatch):
            await verifier.verify_payment(
                _make_proof(), expected_hotkey="5Hotkey", expected_amount_rao=QUOTE_RAO
            )


class TestSuccessEvent:
    async def test_failed_extrinsic_rejected(self):
        verifier = _make_verifier(success=False)
        with pytest.raises(PaymentExtrinsicFailed):
            await verifier.verify_payment(
                _make_proof(), expected_hotkey="5Hotkey", expected_amount_rao=QUOTE_RAO
            )


class TestDestination:
    async def test_wrong_dest_rejected(self):
        ext = _make_extrinsic_info(dest="5SomeoneElse")
        verifier = _make_verifier(extrinsic_info=ext)
        with pytest.raises(PaymentDestinationMismatch):
            await verifier.verify_payment(
                _make_proof(), expected_hotkey="5Hotkey", expected_amount_rao=QUOTE_RAO
            )

    async def test_unparseable_dest_rejected_as_mismatch(self):
        ext = _make_extrinsic_info(dest=12345)  # int, not str/dict
        verifier = _make_verifier(extrinsic_info=ext)
        with pytest.raises(PaymentDestinationMismatch):
            await verifier.verify_payment(
                _make_proof(), expected_hotkey="5Hotkey", expected_amount_rao=QUOTE_RAO
            )


class TestAmount:
    @pytest.mark.parametrize(
        "delta_pct",
        [Decimal("-0.5"), Decimal("-0.05"), Decimal("-0.021")],
    )
    async def test_underpayment_rejected(self, delta_pct: Decimal):
        value = int(QUOTE_RAO * (Decimal(1) + delta_pct))
        ext = _make_extrinsic_info(value=value)
        verifier = _make_verifier(extrinsic_info=ext)
        with pytest.raises(PaymentAmountMismatch):
            await verifier.verify_payment(
                _make_proof(), expected_hotkey="5Hotkey", expected_amount_rao=QUOTE_RAO
            )

    @pytest.mark.parametrize(
        "delta_pct",
        [Decimal("0.021"), Decimal("0.05"), Decimal("0.5")],
    )
    async def test_overpayment_rejected(self, delta_pct: Decimal):
        value = int(QUOTE_RAO * (Decimal(1) + delta_pct))
        ext = _make_extrinsic_info(value=value)
        verifier = _make_verifier(extrinsic_info=ext)
        with pytest.raises(PaymentAmountMismatch):
            await verifier.verify_payment(
                _make_proof(), expected_hotkey="5Hotkey", expected_amount_rao=QUOTE_RAO
            )

    async def test_exact_operator_fee_accepts(self):
        ext = _make_extrinsic_info(value=QUOTE_RAO)
        verifier = _make_verifier(extrinsic_info=ext)
        result = await verifier.verify_payment(
            _make_proof(), expected_hotkey="5Hotkey", expected_amount_rao=QUOTE_RAO
        )
        assert result.amount_rao == QUOTE_RAO
        assert result.accepted_under_legacy_fee_amnesty is False

    async def test_pre_cutover_legacy_amount_is_accepted_once_by_policy(self):
        block_time = datetime.fromtimestamp(1_700_000_000, tz=UTC)
        ext = _make_extrinsic_info(value=17_500_000)
        verifier = _make_verifier(extrinsic_info=ext)

        result = await verifier.verify_payment(
            _make_proof(),
            expected_hotkey="5Hotkey",
            expected_amount_rao=QUOTE_RAO,
            legacy_amount_cutoff_at=block_time + timedelta(seconds=1),
        )

        assert result.amount_rao == 17_500_000
        assert result.accepted_under_legacy_fee_amnesty is True

    async def test_post_cutover_wrong_amount_is_rejected(self):
        block_time = datetime.fromtimestamp(1_700_000_000, tz=UTC)
        ext = _make_extrinsic_info(value=17_500_000)
        verifier = _make_verifier(extrinsic_info=ext)

        with pytest.raises(PaymentAmountMismatch):
            await verifier.verify_payment(
                _make_proof(),
                expected_hotkey="5Hotkey",
                expected_amount_rao=QUOTE_RAO,
                legacy_amount_cutoff_at=block_time - timedelta(seconds=1),
            )

    async def test_zero_value_is_never_eligible_for_legacy_amnesty(self):
        block_time = datetime.fromtimestamp(1_700_000_000, tz=UTC)
        ext = _make_extrinsic_info(value=0)
        verifier = _make_verifier(extrinsic_info=ext)

        with pytest.raises(PaymentAmountMismatch):
            await verifier.verify_payment(
                _make_proof(),
                expected_hotkey="5Hotkey",
                expected_amount_rao=QUOTE_RAO,
                legacy_amount_cutoff_at=block_time + timedelta(seconds=1),
            )


class TestSignerOwnership:
    async def test_signer_not_owner_rejected(self):
        ext = _make_extrinsic_info(signer="5Different")
        verifier = _make_verifier(extrinsic_info=ext, coldkey="5Coldkey")
        with pytest.raises(PaymentSignerMismatch):
            await verifier.verify_payment(
                _make_proof(), expected_hotkey="5Hotkey", expected_amount_rao=QUOTE_RAO
            )

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
            await verifier.verify_payment(
                _make_proof(), expected_hotkey="5Hotkey", expected_amount_rao=QUOTE_RAO
            )
