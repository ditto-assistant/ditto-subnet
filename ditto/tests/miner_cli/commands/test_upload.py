"""Unit tests for :mod:`ditto.miner_cli.commands.upload`.

Heavy module-level mocks replace every collaborator (wallet, signing,
api_client, payment) so the orchestrator's control flow is exercised
without any real I/O.

Invariants pinned:

- Happy path: every step runs in order; returns 0; agent_id printed.
- Missing wallet args: exit 1 before any work.
- Pre-flight failure: chain payment never reached.
- /upload/check rejection: payment never submitted.
- Wallet ownership mismatch: confirmation and payment never reached.
- Payment cancelled at prompt: exit 2 before chain call.
- Upload rejection after payment: exit 1 + proof surfaced.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from ditto.api_models import (
    EvalPricingResponse,
    UploadAgentResponse,
    UploadCheckResponse,
)
from ditto.api_models.agent_status import AgentStatus
from ditto.miner_cli.commands.upload import run
from ditto.miner_cli.errors import (
    ApiResponseError,
    PaymentCancelledError,
    PaymentFinalizationTimeoutError,
    PaymentSubmissionError,
    UploadAgentRejectedError,
)
from ditto.miner_cli.models import PaymentReceipt, PreflightCheckResult, PreflightResult

HOTKEY = "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"
DEST = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"


def make_args(tar_path: Path, **overrides) -> argparse.Namespace:
    base = {
        "tar_path": tar_path,
        "name": "alpha",
        "coldkey_name": "miner",
        "hotkey_name": "default",
        "yes": True,  # bypass interactive prompt by default in tests
        "network": "local",
        "verbose": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _patch_api_client(client_mock: MagicMock) -> MagicMock:
    ctor = MagicMock()
    ctor.return_value.__enter__.return_value = client_mock
    ctor.return_value.__exit__.return_value = False
    return ctor


@pytest.fixture(autouse=True)
def _stub_payment_signer_preflight():  # type: ignore[no-untyped-def]
    """Keep orchestration tests offline unless they exercise this gate."""
    with patch("ditto.miner_cli.commands.upload.preflight_payment_signer"):
        yield


def _good_preflight() -> PreflightResult:
    return PreflightResult(
        sha256="ab" * 32,
        file_size_bytes=512,
        checks=(
            PreflightCheckResult(name="file_size", passed=True, detail="ok"),
            PreflightCheckResult(name="gzip_valid", passed=True, detail="ok"),
            PreflightCheckResult(name="tar_opens", passed=True, detail="ok"),
        ),
    )


def _bad_preflight() -> PreflightResult:
    return PreflightResult(
        sha256="ab" * 32,
        file_size_bytes=512,
        checks=(PreflightCheckResult(name="gzip_valid", passed=False, detail="bad"),),
    )


def _ok_check() -> UploadCheckResponse:
    return UploadCheckResponse(ok=True, error_codes=[], messages=[])


def _rejected_check() -> UploadCheckResponse:
    return UploadCheckResponse(
        ok=False,
        error_codes=[1100],
        messages=["signature did not verify"],
    )


def _pricing() -> EvalPricingResponse:
    return EvalPricingResponse(amount_rao=1_500_000_000, send_address=DEST)


def _payment_receipt() -> PaymentReceipt:
    return PaymentReceipt(
        block_hash="0x" + "cd" * 32,
        block_number=42,
        extrinsic_index=3,
    )


def _upload_response() -> UploadAgentResponse:
    return UploadAgentResponse(agent_id=uuid4(), status=AgentStatus.UPLOADED)


class TestUploadHappyPath:
    def test_full_flow_exits_zero_and_prints_agent_id(
        self, good_tar: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        client = MagicMock()
        client.post_upload_check.return_value = _ok_check()
        client.get_eval_pricing.return_value = _pricing()
        client.post_upload_agent.return_value = _upload_response()

        fake_handle = MagicMock(hotkey_ss58=HOTKEY, coldkey_name="miner")

        with (
            patch(
                "ditto.miner_cli.commands.upload.load_wallet",
                return_value=(fake_handle, MagicMock()),
            ) as load_w,
            patch(
                "ditto.miner_cli.commands.upload.run_preflight",
                return_value=_good_preflight(),
            ),
            patch(
                "ditto.miner_cli.commands.upload.sign_upload_payload",
                return_value="cd" * 64,
            ),
            patch(
                "ditto.miner_cli.commands.upload.submit_eval_payment",
                return_value=_payment_receipt(),
            ) as pay,
            patch(
                "ditto.miner_cli.commands.upload.ApiClient",
                _patch_api_client(client),
            ),
        ):
            rc = run(make_args(good_tar))

        out = capsys.readouterr().out
        assert rc == 0
        # Wallet loaded with the names from args.
        load_w.assert_called_once_with(coldkey_name="miner", hotkey_name="default")
        # Pre-check + pricing + payment + upload all called.
        client.post_upload_check.assert_called_once()
        client.get_eval_pricing.assert_called_once()
        pay.assert_called_once()
        client.post_upload_agent.assert_called_once()
        # agent_id printed to stdout (the value returned from upload).
        assert client.post_upload_agent.return_value.agent_id == _peek_uuid_from_stdout(
            client.post_upload_agent.return_value
        )
        assert str(client.post_upload_agent.return_value.agent_id) in out


def _peek_uuid_from_stdout(response):  # type: ignore[no-untyped-def]
    """Helper that the happy-path test compares directly; readability shim."""
    return response.agent_id


class TestUploadFailurePaths:
    def test_missing_wallet_args_exits_one_without_running_anything(
        self, good_tar: Path
    ) -> None:
        with patch("ditto.miner_cli.commands.upload.load_wallet") as load_w:
            rc = run(make_args(good_tar, coldkey_name=None, hotkey_name=None))

        assert rc == 1
        load_w.assert_not_called()

    def test_preflight_fail_exits_one_before_chain_call(self, good_tar: Path) -> None:
        client = MagicMock()
        fake_handle = MagicMock(hotkey_ss58=HOTKEY, coldkey_name="miner")

        with (
            patch(
                "ditto.miner_cli.commands.upload.load_wallet",
                return_value=(fake_handle, MagicMock()),
            ),
            patch(
                "ditto.miner_cli.commands.upload.run_preflight",
                return_value=_bad_preflight(),
            ),
            patch("ditto.miner_cli.commands.upload.submit_eval_payment") as pay,
            patch(
                "ditto.miner_cli.commands.upload.ApiClient",
                _patch_api_client(client),
            ),
        ):
            rc = run(make_args(good_tar))

        assert rc == 1
        pay.assert_not_called()
        client.post_upload_check.assert_not_called()

    def test_check_rejection_exits_one_before_payment(
        self, good_tar: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        client = MagicMock()
        client.post_upload_check.return_value = _rejected_check()

        fake_handle = MagicMock(hotkey_ss58=HOTKEY, coldkey_name="miner")

        with (
            patch(
                "ditto.miner_cli.commands.upload.load_wallet",
                return_value=(fake_handle, MagicMock()),
            ),
            patch(
                "ditto.miner_cli.commands.upload.run_preflight",
                return_value=_good_preflight(),
            ),
            patch(
                "ditto.miner_cli.commands.upload.sign_upload_payload",
                return_value="cd" * 64,
            ),
            patch("ditto.miner_cli.commands.upload.submit_eval_payment") as pay,
            patch(
                "ditto.miner_cli.commands.upload.ApiClient",
                _patch_api_client(client),
            ),
        ):
            rc = run(make_args(good_tar))

        assert rc == 1
        pay.assert_not_called()
        client.get_eval_pricing.assert_not_called()
        # Per-code message surfaced to stderr.
        assert "1100" in capsys.readouterr().err

    def test_signer_mismatch_exits_one_before_pricing_confirm_or_payment(
        self, good_tar: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        client = MagicMock()
        client.post_upload_check.return_value = _ok_check()
        fake_handle = MagicMock(hotkey_ss58=HOTKEY, coldkey_name="poker")
        mismatch = (
            "payment signer mismatch detected before payment: "
            "on-chain owner 5Owner, selected wallet signer 5Signer. "
            "No funds were sent."
        )

        with (
            patch(
                "ditto.miner_cli.commands.upload.load_wallet",
                return_value=(fake_handle, MagicMock()),
            ),
            patch(
                "ditto.miner_cli.commands.upload.run_preflight",
                return_value=_good_preflight(),
            ),
            patch(
                "ditto.miner_cli.commands.upload.sign_upload_payload",
                return_value="cd" * 64,
            ),
            patch(
                "ditto.miner_cli.commands.upload.preflight_payment_signer",
                side_effect=PaymentSubmissionError(mismatch),
            ) as ownership_check,
            patch("ditto.miner_cli.commands.upload.confirm_payment") as confirm,
            patch("ditto.miner_cli.commands.upload.submit_eval_payment") as pay,
            patch(
                "ditto.miner_cli.commands.upload.ApiClient",
                _patch_api_client(client),
            ),
        ):
            rc = run(make_args(good_tar, yes=True))

        assert rc == 1
        assert "No funds were sent" in capsys.readouterr().err
        ownership_check.assert_called_once()
        client.get_eval_pricing.assert_not_called()
        confirm.assert_not_called()
        pay.assert_not_called()

    def test_cancel_at_confirm_exits_two_before_chain_call(
        self, good_tar: Path
    ) -> None:
        client = MagicMock()
        client.post_upload_check.return_value = _ok_check()
        client.get_eval_pricing.return_value = _pricing()

        fake_handle = MagicMock(hotkey_ss58=HOTKEY, coldkey_name="miner")

        with (
            patch(
                "ditto.miner_cli.commands.upload.load_wallet",
                return_value=(fake_handle, MagicMock()),
            ),
            patch(
                "ditto.miner_cli.commands.upload.run_preflight",
                return_value=_good_preflight(),
            ),
            patch(
                "ditto.miner_cli.commands.upload.sign_upload_payload",
                return_value="cd" * 64,
            ),
            patch(
                "ditto.miner_cli.commands.upload.confirm_payment",
                side_effect=PaymentCancelledError("user said n"),
            ),
            patch("ditto.miner_cli.commands.upload.submit_eval_payment") as pay,
            patch(
                "ditto.miner_cli.commands.upload.ApiClient",
                _patch_api_client(client),
            ),
        ):
            rc = run(make_args(good_tar, yes=False))

        assert rc == 2
        pay.assert_not_called()

    def test_upload_rejection_after_payment_surfaces_proof_to_stderr(
        self, good_tar: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        client = MagicMock()
        client.post_upload_check.return_value = _ok_check()
        client.get_eval_pricing.return_value = _pricing()
        client.post_upload_agent.side_effect = UploadAgentRejectedError(
            "replay rejected"
        )

        fake_handle = MagicMock(hotkey_ss58=HOTKEY, coldkey_name="miner")
        receipt = _payment_receipt()

        with (
            patch(
                "ditto.miner_cli.commands.upload.load_wallet",
                return_value=(fake_handle, MagicMock()),
            ),
            patch(
                "ditto.miner_cli.commands.upload.run_preflight",
                return_value=_good_preflight(),
            ),
            patch(
                "ditto.miner_cli.commands.upload.sign_upload_payload",
                return_value="cd" * 64,
            ),
            patch(
                "ditto.miner_cli.commands.upload.submit_eval_payment",
                return_value=receipt,
            ),
            patch(
                "ditto.miner_cli.commands.upload.ApiClient",
                _patch_api_client(client),
            ),
        ):
            rc = run(make_args(good_tar))

        err = capsys.readouterr().err
        assert rc == 1
        # Proof surfaced so the miner can take it to support.
        assert receipt.block_hash in err
        assert str(receipt.block_number) in err
        assert str(receipt.extrinsic_index) in err

    def test_payment_submission_failure_exits_one_no_proof_surfaced(
        self, good_tar: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Chain rejected the extrinsic before it landed in a block; no
        money left the wallet. Exit 1 with stderr message but NO proof
        tuple printed: there is no proof, and printing one would mislead
        the miner into thinking they paid."""
        client = MagicMock()
        client.post_upload_check.return_value = _ok_check()
        client.get_eval_pricing.return_value = _pricing()

        fake_handle = MagicMock(hotkey_ss58=HOTKEY, coldkey_name="miner")

        with (
            patch(
                "ditto.miner_cli.commands.upload.load_wallet",
                return_value=(fake_handle, MagicMock()),
            ),
            patch(
                "ditto.miner_cli.commands.upload.run_preflight",
                return_value=_good_preflight(),
            ),
            patch(
                "ditto.miner_cli.commands.upload.sign_upload_payload",
                return_value="cd" * 64,
            ),
            patch(
                "ditto.miner_cli.commands.upload.submit_eval_payment",
                side_effect=PaymentSubmissionError("insufficient balance"),
            ),
            patch(
                "ditto.miner_cli.commands.upload.ApiClient",
                _patch_api_client(client),
            ),
        ):
            rc = run(make_args(good_tar))

        captured = capsys.readouterr()
        assert rc == 1
        assert "insufficient balance" in captured.err
        # No proof tuple: money never left.
        assert "block_hash:" not in captured.err
        # post_upload_agent must NOT be called after a payment failure.
        client.post_upload_agent.assert_not_called()

    def test_payment_finalization_timeout_exits_one(
        self, good_tar: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Extrinsic was submitted but did not finalise in time. Ambiguous
        whether money left or not. CLI exits 1 with stderr message; user
        can check chain via btcli or wait + retry."""
        client = MagicMock()
        client.post_upload_check.return_value = _ok_check()
        client.get_eval_pricing.return_value = _pricing()

        fake_handle = MagicMock(hotkey_ss58=HOTKEY, coldkey_name="miner")

        with (
            patch(
                "ditto.miner_cli.commands.upload.load_wallet",
                return_value=(fake_handle, MagicMock()),
            ),
            patch(
                "ditto.miner_cli.commands.upload.run_preflight",
                return_value=_good_preflight(),
            ),
            patch(
                "ditto.miner_cli.commands.upload.sign_upload_payload",
                return_value="cd" * 64,
            ),
            patch(
                "ditto.miner_cli.commands.upload.submit_eval_payment",
                side_effect=PaymentFinalizationTimeoutError("60s elapsed"),
            ),
            patch(
                "ditto.miner_cli.commands.upload.ApiClient",
                _patch_api_client(client),
            ),
        ):
            rc = run(make_args(good_tar))

        captured = capsys.readouterr()
        assert rc == 1
        assert (
            "60s" in captured.err
            or "finalise" in captured.err.lower()
            or "timeout" in captured.err.lower()
        )
        client.post_upload_agent.assert_not_called()

    def test_check_rejection_with_multiple_codes_prints_all(
        self, good_tar: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The server returns parallel error_codes + messages arrays so
        every rejection surfaces in one round trip. Each code must
        appear in stderr so the miner sees the full picture, not just
        the first rejection."""
        client = MagicMock()
        client.post_upload_check.return_value = UploadCheckResponse(
            ok=False,
            error_codes=[1100, 1101, 1102],
            messages=["bad sig", "not registered", "too large"],
        )

        fake_handle = MagicMock(hotkey_ss58=HOTKEY, coldkey_name="miner")

        with (
            patch(
                "ditto.miner_cli.commands.upload.load_wallet",
                return_value=(fake_handle, MagicMock()),
            ),
            patch(
                "ditto.miner_cli.commands.upload.run_preflight",
                return_value=_good_preflight(),
            ),
            patch(
                "ditto.miner_cli.commands.upload.sign_upload_payload",
                return_value="cd" * 64,
            ),
            patch("ditto.miner_cli.commands.upload.submit_eval_payment"),
            patch(
                "ditto.miner_cli.commands.upload.ApiClient",
                _patch_api_client(client),
            ),
        ):
            rc = run(make_args(good_tar))

        err = capsys.readouterr().err
        assert rc == 1
        for code in ("1100", "1101", "1102"):
            assert code in err, f"missing rejection code {code} in stderr"
        for msg in ("bad sig", "not registered", "too large"):
            assert msg in err, f"missing rejection message {msg!r} in stderr"

    def test_transport_error_after_payment_surfaces_proof(
        self, good_tar: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """API goes down between payment and /upload/agent: connect-refused
        bubbles up as a generic ApiResponseError (not the specific
        UploadAgentRejectedError). The proof tuple must STILL surface so
        the miner has it for support; money is on chain regardless of
        whether the API rejected or was unreachable."""
        client = MagicMock()
        client.post_upload_check.return_value = _ok_check()
        client.get_eval_pricing.return_value = _pricing()
        # Note: bare ApiResponseError, NOT UploadAgentRejectedError.
        # Simulates the api_client._request connect-refused wrapper raising.
        client.post_upload_agent.side_effect = ApiResponseError(
            "api unreachable at http://localhost:8000: connection refused"
        )

        fake_handle = MagicMock(hotkey_ss58=HOTKEY, coldkey_name="miner")
        receipt = _payment_receipt()

        with (
            patch(
                "ditto.miner_cli.commands.upload.load_wallet",
                return_value=(fake_handle, MagicMock()),
            ),
            patch(
                "ditto.miner_cli.commands.upload.run_preflight",
                return_value=_good_preflight(),
            ),
            patch(
                "ditto.miner_cli.commands.upload.sign_upload_payload",
                return_value="cd" * 64,
            ),
            patch(
                "ditto.miner_cli.commands.upload.submit_eval_payment",
                return_value=receipt,
            ),
            patch(
                "ditto.miner_cli.commands.upload.ApiClient",
                _patch_api_client(client),
            ),
        ):
            rc = run(make_args(good_tar))

        err = capsys.readouterr().err
        assert rc == 1
        # The CRITICAL invariant: proof tuple surfaces even on transport
        # failure post-payment, not just on server-side rejection.
        assert receipt.block_hash in err
        assert str(receipt.block_number) in err
        assert str(receipt.extrinsic_index) in err


class TestUploadWireCorrectness:
    """Pin that values flow through the orchestrator to the right sink.

    Money-flow code: if amount, dest_address, or subtensor_network are
    mis-wired, real TAO lands in the wrong place. These tests assert
    the exact arguments passed to ``submit_eval_payment``."""

    def test_pricing_amount_wired_to_payment_call(self, good_tar: Path) -> None:
        client = MagicMock()
        client.post_upload_check.return_value = _ok_check()
        client.get_eval_pricing.return_value = EvalPricingResponse(
            amount_rao=2_750_000_000, send_address=DEST
        )
        client.post_upload_agent.return_value = _upload_response()

        fake_handle = MagicMock(hotkey_ss58=HOTKEY, coldkey_name="miner")

        with (
            patch(
                "ditto.miner_cli.commands.upload.load_wallet",
                return_value=(fake_handle, MagicMock()),
            ),
            patch(
                "ditto.miner_cli.commands.upload.run_preflight",
                return_value=_good_preflight(),
            ),
            patch(
                "ditto.miner_cli.commands.upload.sign_upload_payload",
                return_value="cd" * 64,
            ),
            patch(
                "ditto.miner_cli.commands.upload.submit_eval_payment",
                return_value=_payment_receipt(),
            ) as pay,
            patch(
                "ditto.miner_cli.commands.upload.ApiClient",
                _patch_api_client(client),
            ),
        ):
            assert run(make_args(good_tar)) == 0

        kwargs = pay.call_args.kwargs
        assert kwargs["amount_rao"] == 2_750_000_000, (
            "amount from /upload/eval-pricing must flow into "
            "submit_eval_payment unchanged; mis-wiring sends wrong fee"
        )

    def test_pricing_send_address_wired_to_payment_call(self, good_tar: Path) -> None:
        custom_dest = "5FCfAonRZgTFrTd9HREEyeJjDpT397KMzizE6T3DvebLFE7n"
        client = MagicMock()
        client.post_upload_check.return_value = _ok_check()
        client.get_eval_pricing.return_value = EvalPricingResponse(
            amount_rao=1_500_000_000, send_address=custom_dest
        )
        client.post_upload_agent.return_value = _upload_response()

        fake_handle = MagicMock(hotkey_ss58=HOTKEY, coldkey_name="miner")

        with (
            patch(
                "ditto.miner_cli.commands.upload.load_wallet",
                return_value=(fake_handle, MagicMock()),
            ),
            patch(
                "ditto.miner_cli.commands.upload.run_preflight",
                return_value=_good_preflight(),
            ),
            patch(
                "ditto.miner_cli.commands.upload.sign_upload_payload",
                return_value="cd" * 64,
            ),
            patch(
                "ditto.miner_cli.commands.upload.submit_eval_payment",
                return_value=_payment_receipt(),
            ) as pay,
            patch(
                "ditto.miner_cli.commands.upload.ApiClient",
                _patch_api_client(client),
            ),
        ):
            assert run(make_args(good_tar)) == 0

        kwargs = pay.call_args.kwargs
        assert kwargs["dest_address"] == custom_dest, (
            "dest_address from /upload/eval-pricing must flow into "
            "submit_eval_payment unchanged; mis-wiring sends TAO to "
            "the wrong recipient"
        )

    @pytest.mark.parametrize(
        ("network_name", "expected_subtensor"),
        [
            ("finney", "finney"),
            ("test", "test"),
            ("local", "local"),
        ],
    )
    def test_network_subtensor_wired_to_payment_call(
        self, good_tar: Path, network_name: str, expected_subtensor: str
    ) -> None:
        """Cross-chain safety: the subtensor identifier from the resolved
        ``NetworkConfig`` must reach ``submit_eval_payment``. If a refactor
        breaks this wire, the miner could pay on the wrong chain entirely."""
        client = MagicMock()
        client.post_upload_check.return_value = _ok_check()
        client.get_eval_pricing.return_value = _pricing()
        client.post_upload_agent.return_value = _upload_response()

        fake_handle = MagicMock(hotkey_ss58=HOTKEY, coldkey_name="miner")

        with (
            patch(
                "ditto.miner_cli.commands.upload.load_wallet",
                return_value=(fake_handle, MagicMock()),
            ),
            patch(
                "ditto.miner_cli.commands.upload.run_preflight",
                return_value=_good_preflight(),
            ),
            patch(
                "ditto.miner_cli.commands.upload.sign_upload_payload",
                return_value="cd" * 64,
            ),
            patch(
                "ditto.miner_cli.commands.upload.submit_eval_payment",
                return_value=_payment_receipt(),
            ) as pay,
            patch(
                "ditto.miner_cli.commands.upload.ApiClient",
                _patch_api_client(client),
            ),
        ):
            assert run(make_args(good_tar, network=network_name)) == 0

        kwargs = pay.call_args.kwargs
        assert kwargs["subtensor_network"] == expected_subtensor


class TestUploadConfirmBypass:
    """``-y`` / ``--yes`` translates to ``confirm_payment(skip=True)``.
    Regression guard: a refactor that drops the wire would silently
    re-introduce interactive prompts in scripted contexts."""

    def test_yes_flag_passes_skip_true_to_confirm(self, good_tar: Path) -> None:
        client = MagicMock()
        client.post_upload_check.return_value = _ok_check()
        client.get_eval_pricing.return_value = _pricing()
        client.post_upload_agent.return_value = _upload_response()

        fake_handle = MagicMock(hotkey_ss58=HOTKEY, coldkey_name="miner")

        with (
            patch(
                "ditto.miner_cli.commands.upload.load_wallet",
                return_value=(fake_handle, MagicMock()),
            ),
            patch(
                "ditto.miner_cli.commands.upload.run_preflight",
                return_value=_good_preflight(),
            ),
            patch(
                "ditto.miner_cli.commands.upload.sign_upload_payload",
                return_value="cd" * 64,
            ),
            patch(
                "ditto.miner_cli.commands.upload.submit_eval_payment",
                return_value=_payment_receipt(),
            ),
            patch(
                "ditto.miner_cli.commands.upload.confirm_payment",
            ) as confirm,
            patch(
                "ditto.miner_cli.commands.upload.ApiClient",
                _patch_api_client(client),
            ),
        ):
            assert run(make_args(good_tar, yes=True)) == 0

        assert confirm.call_args.kwargs["skip"] is True

    def test_no_yes_flag_passes_skip_false_to_confirm(self, good_tar: Path) -> None:
        """Inverse: without --yes the confirm must be called with
        skip=False so the interactive prompt actually fires."""
        client = MagicMock()
        client.post_upload_check.return_value = _ok_check()
        client.get_eval_pricing.return_value = _pricing()
        client.post_upload_agent.return_value = _upload_response()

        fake_handle = MagicMock(hotkey_ss58=HOTKEY, coldkey_name="miner")

        with (
            patch(
                "ditto.miner_cli.commands.upload.load_wallet",
                return_value=(fake_handle, MagicMock()),
            ),
            patch(
                "ditto.miner_cli.commands.upload.run_preflight",
                return_value=_good_preflight(),
            ),
            patch(
                "ditto.miner_cli.commands.upload.sign_upload_payload",
                return_value="cd" * 64,
            ),
            patch(
                "ditto.miner_cli.commands.upload.submit_eval_payment",
                return_value=_payment_receipt(),
            ),
            patch(
                "ditto.miner_cli.commands.upload.confirm_payment",
            ) as confirm,
            patch(
                "ditto.miner_cli.commands.upload.ApiClient",
                _patch_api_client(client),
            ),
        ):
            assert run(make_args(good_tar, yes=False)) == 0

        assert confirm.call_args.kwargs["skip"] is False
