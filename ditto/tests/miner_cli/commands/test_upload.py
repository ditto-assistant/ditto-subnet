"""Unit tests for :mod:`ditto.miner_cli.commands.upload`.

Heavy module-level mocks replace every collaborator (wallet, signing,
api_client, payment) so the orchestrator's control flow is exercised
without any real I/O.

Invariants pinned:

- Happy path: every step runs in order; returns 0; agent_id printed.
- Missing wallet args: exit 1 before any work.
- Pre-flight failure: chain payment never reached.
- /upload/check rejection: payment never submitted.
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
from ditto.db.models import AgentStatus
from ditto.miner_cli.commands.upload import run
from ditto.miner_cli.errors import (
    PaymentCancelledError,
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
