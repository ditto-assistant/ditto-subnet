"""Unit tests for :mod:`ditto.miner_cli.payment`.

The bittensor SDK is mocked at the module boundary per TESTING-STRATEGY
§145. The real Subtensor connects over the network; a unit test must
not. We mock :func:`bittensor.Subtensor` so the function under test
exercises real flow without doing real chain I/O.

Invariants pinned:

- Subtensor is constructed with the requested network identifier
- ``transfer`` is called with rao-converted amount + correct dest
- Successful response with extrinsic_receipt → PaymentReceipt with
  proof tuple populated
- Connection failure / transfer error → PaymentSubmissionError
- TimeoutError → PaymentFinalizationTimeoutError
- Missing block_hash on success → PaymentSubmissionError (defensive)
- Block hash normalization: bare hex gets ``0x`` prefix
"""

from __future__ import annotations

from unittest.mock import MagicMock

import bittensor
import pytest

from ditto.miner_cli.errors import (
    PaymentFinalizationTimeoutError,
    PaymentSubmissionError,
)
from ditto.miner_cli.models import PaymentReceipt
from ditto.miner_cli.payment import submit_eval_payment


def make_extrinsic_receipt(
    *,
    block_hash: str = "0x" + "ab" * 32,
    block_number: int = 42,
    extrinsic_idx: int = 3,
) -> MagicMock:
    receipt = MagicMock()
    receipt.block_hash = block_hash
    receipt.block_number = block_number
    receipt.extrinsic_idx = extrinsic_idx
    return receipt


def make_response(
    *,
    success: bool = True,
    extrinsic_receipt: MagicMock | None = None,
    message: str = "ok",
) -> MagicMock:
    response = MagicMock()
    response.success = success
    response.message = message
    response.extrinsic_receipt = extrinsic_receipt or make_extrinsic_receipt()
    return response


class TestSubmitEvalPayment:
    HOTKEY_DEST = "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"

    def test_happy_path_returns_payment_receipt(self, monkeypatch) -> None:
        captured: dict = {}

        fake_subtensor = MagicMock()
        fake_subtensor.transfer = MagicMock(return_value=make_response())

        def fake_ctor(*, network: str) -> MagicMock:
            captured["network"] = network
            return fake_subtensor

        monkeypatch.setattr(bittensor, "Subtensor", fake_ctor)

        result = submit_eval_payment(
            live_wallet=MagicMock(),
            subtensor_network="finney",
            amount_rao=1_500_000_000,
            dest_address=self.HOTKEY_DEST,
        )

        # Subtensor constructed with requested network.
        assert captured["network"] == "finney"

        # transfer() called with rao-converted Balance + correct dest.
        call = fake_subtensor.transfer.call_args
        assert call.kwargs["destination_ss58"] == self.HOTKEY_DEST
        assert call.kwargs["amount"].rao == 1_500_000_000
        assert call.kwargs["wait_for_finalization"] is True

        # Result populated from extrinsic_receipt.
        assert isinstance(result, PaymentReceipt)
        assert result.block_hash == "0x" + "ab" * 32
        assert result.block_number == 42
        assert result.extrinsic_index == 3

    def test_subtensor_construction_failure_raises_submission_error(
        self, monkeypatch
    ) -> None:
        def fake_ctor(**_kwargs: object) -> MagicMock:
            raise RuntimeError("no route to host")

        monkeypatch.setattr(bittensor, "Subtensor", fake_ctor)

        with pytest.raises(PaymentSubmissionError) as e:
            submit_eval_payment(
                live_wallet=MagicMock(),
                subtensor_network="local",
                amount_rao=1,
                dest_address=self.HOTKEY_DEST,
            )

        assert "local" in str(e.value)

    def test_transfer_exception_raises_submission_error(self, monkeypatch) -> None:
        fake_subtensor = MagicMock()
        fake_subtensor.transfer = MagicMock(
            side_effect=RuntimeError("insufficient balance")
        )

        monkeypatch.setattr(bittensor, "Subtensor", lambda **_kwargs: fake_subtensor)

        with pytest.raises(PaymentSubmissionError) as e:
            submit_eval_payment(
                live_wallet=MagicMock(),
                subtensor_network="finney",
                amount_rao=1,
                dest_address=self.HOTKEY_DEST,
            )

        assert "insufficient balance" in str(e.value)

    def test_timeout_raises_finalization_timeout_error(self, monkeypatch) -> None:
        fake_subtensor = MagicMock()
        fake_subtensor.transfer = MagicMock(side_effect=TimeoutError("12s"))

        monkeypatch.setattr(bittensor, "Subtensor", lambda **_kwargs: fake_subtensor)

        with pytest.raises(PaymentFinalizationTimeoutError):
            submit_eval_payment(
                live_wallet=MagicMock(),
                subtensor_network="finney",
                amount_rao=1,
                dest_address=self.HOTKEY_DEST,
            )

    def test_success_false_response_raises_submission_error(self, monkeypatch) -> None:
        """Defensive: raise_error=True should already raise; if a
        success=False slips through, we still bail rather than
        returning a half-populated receipt."""
        fake_subtensor = MagicMock()
        fake_subtensor.transfer = MagicMock(
            return_value=make_response(success=False, message="rejected")
        )

        monkeypatch.setattr(bittensor, "Subtensor", lambda **_kwargs: fake_subtensor)

        with pytest.raises(PaymentSubmissionError) as e:
            submit_eval_payment(
                live_wallet=MagicMock(),
                subtensor_network="finney",
                amount_rao=1,
                dest_address=self.HOTKEY_DEST,
            )

        assert "rejected" in str(e.value)

    def test_missing_extrinsic_receipt_raises_submission_error(
        self, monkeypatch
    ) -> None:
        response = MagicMock()
        response.success = True
        response.extrinsic_receipt = None

        fake_subtensor = MagicMock()
        fake_subtensor.transfer = MagicMock(return_value=response)
        monkeypatch.setattr(bittensor, "Subtensor", lambda **_kwargs: fake_subtensor)

        with pytest.raises(PaymentSubmissionError):
            submit_eval_payment(
                live_wallet=MagicMock(),
                subtensor_network="finney",
                amount_rao=1,
                dest_address=self.HOTKEY_DEST,
            )

    def test_block_hash_without_0x_prefix_gets_normalized(self, monkeypatch) -> None:
        """Different substrate SDK versions return block_hash with or
        without the 0x prefix; server regex requires it."""
        receipt = make_extrinsic_receipt(block_hash="ab" * 32)
        response = make_response(extrinsic_receipt=receipt)
        fake_subtensor = MagicMock()
        fake_subtensor.transfer = MagicMock(return_value=response)
        monkeypatch.setattr(bittensor, "Subtensor", lambda **_kwargs: fake_subtensor)

        result = submit_eval_payment(
            live_wallet=MagicMock(),
            subtensor_network="finney",
            amount_rao=1,
            dest_address=self.HOTKEY_DEST,
        )

        assert result.block_hash == "0x" + "ab" * 32

    def test_empty_block_hash_raises_submission_error(self, monkeypatch) -> None:
        receipt = make_extrinsic_receipt(block_hash="")
        response = make_response(extrinsic_receipt=receipt)
        fake_subtensor = MagicMock()
        fake_subtensor.transfer = MagicMock(return_value=response)
        monkeypatch.setattr(bittensor, "Subtensor", lambda **_kwargs: fake_subtensor)

        with pytest.raises(PaymentSubmissionError) as e:
            submit_eval_payment(
                live_wallet=MagicMock(),
                subtensor_network="finney",
                amount_rao=1,
                dest_address=self.HOTKEY_DEST,
            )

        assert "block_hash" in str(e.value)
