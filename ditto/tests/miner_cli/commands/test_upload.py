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
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch
from uuid import uuid4

import pytest

from ditto.api_models import (
    EvalPricingResponse,
    UploadAgentResponse,
    UploadCheckResponse,
)
from ditto.api_models.agent_status import AgentStatus
from ditto.miner_cli.commands.upload import (
    _offer_owner_link,
    _post_upload_with_retries,
    run,
)
from ditto.miner_cli.errors import (
    ApiResponseError,
    PaymentAmountMismatchError,
    PaymentCancelledError,
    PaymentFinalizationTimeoutError,
    PaymentRecoveryExpiredError,
    PaymentSubmissionError,
    PreCheckRejectedError,
    RegistrationCancelledError,
    SubmissionCooldownError,
    TransientApiError,
    UploadAgentRejectedError,
)
from ditto.miner_cli.models import (
    PaymentReceipt,
    PendingUploadPayment,
    PreflightCheckResult,
    PreflightResult,
    RegistrationQuote,
)
from ditto.miner_cli.preferences import AgentWalletIdentity

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
    with (
        patch("ditto.miner_cli.commands.upload.preflight_payment_signer"),
        patch("ditto.miner_cli.commands.upload.save_agent_name", return_value=True),
        patch("ditto.miner_cli.commands.upload.save_agent_wallet", return_value=True),
        patch(
            "ditto.miner_cli.commands.upload.load_previous_agent_wallet",
            return_value=None,
        ),
        patch(
            "ditto.miner_cli.commands.upload.load_pending_payment",
            return_value=None,
        ),
        patch(
            "ditto.miner_cli.commands.upload.load_pending_payments_for_hotkey",
            return_value=(),
        ),
        patch(
            "ditto.miner_cli.commands.upload.save_pending_payment",
            return_value=True,
        ),
        patch(
            "ditto.miner_cli.commands.upload.clear_pending_payment",
            return_value=True,
        ),
    ):
        yield


class TestOwnerLinkOffer:
    def test_interactive_acceptance_submits_existing_attestation_contract(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        args = make_args(Path("agent.tgz"), network="finney", yes=False)
        previous = AgentWalletIdentity(
            coldkey_name="old-cold",
            hotkey_name="old-hot",
            hotkey_ss58="5OldHotkey",
        )
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
        with patch(
            "ditto.miner_cli.commands.attest._run_attest", return_value=0
        ) as submit:
            _offer_owner_link(
                args=args,
                agent_name="alpha",
                previous_wallet=previous,
                current_hotkey=HOTKEY,
                network_api_url="https://platform.example",
            )

        attest_args = submit.call_args.args[0]
        assert attest_args.coldkey_name == "miner"
        assert attest_args.hotkey_name == "default"
        assert attest_args.other_coldkey_name == "old-cold"
        assert attest_args.other_hotkey_name == "old-hot"
        assert attest_args.key_kind == "hotkey"
        assert attest_args.other_key_kind == "hotkey"
        assert submit.call_args.kwargs == {
            "network_api_url": "https://platform.example",
            "emit_result": False,
        }

    def test_noninteractive_upload_prints_manual_command_without_signing(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        args = make_args(Path("agent.tgz"), network="finney")
        previous = AgentWalletIdentity("old-cold", "old-hot", "5OldHotkey")
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        with patch("ditto.miner_cli.commands.attest._run_attest") as submit:
            _offer_owner_link(
                args=args,
                agent_name="alpha",
                previous_wallet=previous,
                current_hotkey=HOTKEY,
                network_api_url="https://platform.example",
            )

        submit.assert_not_called()
        err = capsys.readouterr().err
        assert "previously uploaded with hotkey 5OldHotkey" in err
        assert "--other-coldkey old-cold" in err


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
    return UploadCheckResponse(
        ok=True,
        error_codes=[],
        messages=[],
        admission_token=uuid4(),
        admission_expires_at=datetime(2026, 7, 24, 20, 0, tzinfo=UTC),
        cooldown_seconds=3600,
    )


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
    return UploadAgentResponse(agent_id=uuid4(), version=2, status=AgentStatus.UPLOADED)


def _reusable_credit_response() -> UploadAgentResponse:
    """What the platform returns for a byte-identical resubmission.

    HTTP 200 carrying the EXISTING agent, with the payment banked rather than
    spent. Structurally indistinguishable from a fresh success except for
    ``payment_disposition``.
    """
    existing = uuid4()
    return UploadAgentResponse(
        agent_id=existing,
        version=2,
        status=AgentStatus.SCORED,
        payment_disposition="reusable_credit",
        credit_for_agent_id=existing,
    )


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

        captured = capsys.readouterr()
        out = captured.out
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
        assert "alpha · submission v2" in captured.err
        assert "saved as this hotkey's local default" in captured.err

    def test_reuses_saved_name_when_name_flag_is_omitted(
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
            ),
            patch(
                "ditto.miner_cli.commands.upload.load_agent_name",
                return_value="remembered-agent",
            ) as load_name,
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
                "ditto.miner_cli.commands.upload.ApiClient",
                _patch_api_client(client),
            ),
        ):
            assert run(make_args(good_tar, name=None)) == 0

        load_name.assert_called_once_with(network="local", hotkey=HOTKEY)
        assert client.post_upload_agent.call_args.kwargs["name"] == "remembered-agent"
        assert "using saved agent name: remembered-agent" in capsys.readouterr().err

    def test_reuses_finalized_payment_without_new_transfer(
        self, good_tar: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        client = MagicMock()
        client.post_upload_check.return_value = _ok_check().model_copy(
            update={"payment_required": False}
        )
        client.post_upload_agent.return_value = _upload_response()
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
            patch("ditto.miner_cli.commands.upload.submit_eval_payment") as pay,
            patch(
                "ditto.miner_cli.commands.upload.ApiClient",
                _patch_api_client(client),
            ),
        ):
            rc = run(
                make_args(
                    good_tar,
                    payment_block_hash=receipt.block_hash,
                    payment_block_number=receipt.block_number,
                    payment_extrinsic_index=receipt.extrinsic_index,
                )
            )

        assert rc == 0
        pay.assert_not_called()
        client.get_eval_pricing.assert_not_called()
        assert client.post_upload_agent.call_args.kwargs["payment"] == receipt
        assert "no new transfer" in capsys.readouterr().err

    def test_identical_rerun_auto_reuses_locally_saved_payment(
        self, good_tar: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        client = MagicMock()
        client.post_upload_check.return_value = _ok_check().model_copy(
            update={"payment_required": False}
        )
        client.post_upload_agent.return_value = _upload_response()
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
                "ditto.miner_cli.commands.upload.load_pending_payment",
                return_value=receipt,
            ) as load_pending,
            patch("ditto.miner_cli.commands.upload.submit_eval_payment") as pay,
            patch(
                "ditto.miner_cli.commands.upload.ApiClient",
                _patch_api_client(client),
            ),
        ):
            rc = run(make_args(good_tar))

        assert rc == 0
        load_pending.assert_called_once_with(
            network="local", hotkey=HOTKEY, name="alpha", sha256="ab" * 32
        )
        pay.assert_not_called()
        client.get_eval_pricing.assert_not_called()
        assert client.post_upload_agent.call_args.kwargs["payment"] == receipt
        assert "found a saved finalized payment" in capsys.readouterr().err

    def test_saved_hotkey_credit_is_applied_to_a_new_archive(
        self, good_tar: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        client = MagicMock()
        client.post_upload_check.return_value = _ok_check().model_copy(
            update={"payment_required": False}
        )
        client.post_upload_agent.return_value = _upload_response()
        fake_handle = MagicMock(hotkey_ss58=HOTKEY, coldkey_name="miner")
        receipt = _payment_receipt()
        previous = PendingUploadPayment(
            network="local",
            hotkey=HOTKEY,
            name="old-agent",
            sha256="ef" * 32,
            payment=receipt,
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
                "ditto.miner_cli.commands.upload.load_pending_payments_for_hotkey",
                return_value=(previous,),
            ) as discover,
            patch(
                "ditto.miner_cli.commands.upload.save_pending_payment",
                return_value=True,
            ) as save_pending,
            patch(
                "ditto.miner_cli.commands.upload.clear_pending_payment",
                return_value=True,
            ) as clear_pending,
            patch("ditto.miner_cli.commands.upload.submit_eval_payment") as pay,
            patch(
                "ditto.miner_cli.commands.upload.ApiClient",
                _patch_api_client(client),
            ),
        ):
            assert run(make_args(good_tar)) == 0

        discover.assert_called_once_with(network="local", hotkey=HOTKEY)
        pay.assert_not_called()
        client.get_eval_pricing.assert_not_called()
        check = client.post_upload_check.call_args.args[0]
        assert check.payment_block_hash == receipt.block_hash
        assert check.payment_block_number == receipt.block_number
        assert check.payment_extrinsic_index == receipt.extrinsic_index
        assert client.post_upload_agent.call_args.kwargs["payment"] == receipt
        save_pending.assert_called_once_with(
            network="local",
            hotkey=HOTKEY,
            name="alpha",
            sha256="ab" * 32,
            payment=receipt,
        )
        clear_pending.assert_any_call(
            network="local",
            hotkey=HOTKEY,
            name="old-agent",
            sha256="ef" * 32,
            payment=receipt,
        )
        assert "applying it to alpha and the new archive" in capsys.readouterr().err

    def test_new_payment_is_saved_before_upload_attempt(self, good_tar: Path) -> None:
        client = MagicMock()
        client.post_upload_check.return_value = _ok_check()
        client.get_eval_pricing.return_value = _pricing()
        client.post_upload_agent.side_effect = UploadAgentRejectedError("rejected")
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
                "ditto.miner_cli.commands.upload.save_pending_payment",
                return_value=True,
            ) as save_pending,
            patch(
                "ditto.miner_cli.commands.upload.ApiClient",
                _patch_api_client(client),
            ),
        ):
            assert run(make_args(good_tar)) == 1

        save_pending.assert_called_once_with(
            network="local",
            hotkey=HOTKEY,
            name="alpha",
            sha256="ab" * 32,
            payment=receipt,
        )

    def test_transient_upload_failure_retries_same_payment(
        self, good_tar: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        client = MagicMock()
        client.post_upload_check.return_value = _ok_check()
        client.get_eval_pricing.return_value = _pricing()
        client.post_upload_agent.side_effect = [
            TransientApiError("gateway unavailable"),
            _upload_response(),
        ]
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
            patch("ditto.miner_cli.commands.upload.time.sleep") as sleep,
            patch(
                "ditto.miner_cli.commands.upload.ApiClient",
                _patch_api_client(client),
            ),
        ):
            rc = run(make_args(good_tar))

        assert rc == 0
        assert client.post_upload_agent.call_count == 2
        assert {
            call.kwargs["payment"] for call in client.post_upload_agent.call_args_list
        } == {receipt}
        sleep.assert_called_once_with(2.0)
        assert "retrying in 2s" in capsys.readouterr().err

    def test_submission_cooldown_skips_short_retry_loop(self, good_tar: Path) -> None:
        client = MagicMock()
        client.post_upload_agent.side_effect = SubmissionCooldownError(
            "owner coldkey may submit again later", retry_after_seconds=1800
        )

        with (
            patch("ditto.miner_cli.commands.upload.time.sleep") as sleep,
            pytest.raises(SubmissionCooldownError),
        ):
            _post_upload_with_retries(
                client=client,
                tar_path=good_tar,
                hotkey=HOTKEY,
                sha256="ab" * 32,
                name="alpha",
                signature="cd" * 64,
                payment=_payment_receipt(),
                admission_token=uuid4(),
            )

        client.post_upload_agent.assert_called_once()
        sleep.assert_not_called()


def _peek_uuid_from_stdout(response):  # type: ignore[no-untyped-def]
    """Helper that the happy-path test compares directly; readability shim."""
    return response.agent_id


class TestUploadFailurePaths:
    def test_saved_mismatch_never_sends_again_without_explicit_authorization(
        self,
        good_tar: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        client = MagicMock()
        client.post_upload_check.side_effect = PaymentAmountMismatchError(
            "payment amount mismatch"
        )
        fake_handle = MagicMock(hotkey_ss58=HOTKEY, coldkey_name="miner")
        receipt = _payment_receipt()
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)

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
                "ditto.miner_cli.commands.upload.load_pending_payment",
                return_value=receipt,
            ),
            patch("ditto.miner_cli.commands.upload.clear_pending_payment") as clear,
            patch("ditto.miner_cli.commands.upload.submit_eval_payment") as pay,
            patch(
                "ditto.miner_cli.commands.upload.ApiClient",
                _patch_api_client(client),
            ),
        ):
            rc = run(make_args(good_tar))

        assert rc == 1
        assert client.post_upload_check.call_count == 1
        pay.assert_not_called()
        clear.assert_not_called()
        err = capsys.readouterr().err
        assert "No second transfer has been sent" in err
        assert "--pay-again" in err

    def test_interactive_replacement_prompt_defaults_to_no(
        self,
        good_tar: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = MagicMock()
        client.post_upload_check.side_effect = PaymentAmountMismatchError(
            "payment amount mismatch"
        )
        fake_handle = MagicMock(hotkey_ss58=HOTKEY, coldkey_name="miner")
        receipt = _payment_receipt()
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda _prompt: "")

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
                "ditto.miner_cli.commands.upload.load_pending_payment",
                return_value=receipt,
            ),
            patch("ditto.miner_cli.commands.upload.clear_pending_payment") as clear,
            patch("ditto.miner_cli.commands.upload.submit_eval_payment") as pay,
            patch(
                "ditto.miner_cli.commands.upload.ApiClient",
                _patch_api_client(client),
            ),
        ):
            rc = run(make_args(good_tar, yes=False))

        assert rc == 2
        clear.assert_not_called()
        pay.assert_not_called()

    def test_pay_again_rechecks_then_uses_reservation_bound_tao_fee(
        self, good_tar: Path
    ) -> None:
        client = MagicMock()
        bound_check = _ok_check().model_copy(
            update={
                "payment_amount_rao": 40_000_000,
                "payment_send_address": DEST,
            }
        )
        client.post_upload_check.side_effect = [
            PaymentAmountMismatchError("payment amount mismatch"),
            bound_check,
        ]
        client.post_upload_agent.return_value = _upload_response()
        fake_handle = MagicMock(hotkey_ss58=HOTKEY, coldkey_name="miner")
        rejected_receipt = _payment_receipt()
        replacement_receipt = PaymentReceipt(
            block_hash="0x" + "ef" * 32,
            block_number=43,
            extrinsic_index=1,
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
                "ditto.miner_cli.commands.upload.load_pending_payment",
                return_value=rejected_receipt,
            ),
            patch(
                "ditto.miner_cli.commands.upload.clear_pending_payment",
                return_value=True,
            ) as clear,
            patch(
                "ditto.miner_cli.commands.upload.submit_eval_payment",
                return_value=replacement_receipt,
            ) as pay,
            patch(
                "ditto.miner_cli.commands.upload.ApiClient",
                _patch_api_client(client),
            ),
        ):
            rc = run(make_args(good_tar, pay_again=True))

        assert rc == 0
        assert client.post_upload_check.call_count == 2
        first_check = client.post_upload_check.call_args_list[0].args[0]
        second_check = client.post_upload_check.call_args_list[1].args[0]
        assert first_check.payment_block_hash == rejected_receipt.block_hash
        assert second_check.payment_block_hash is None
        clear.assert_any_call(
            network="local",
            hotkey=HOTKEY,
            name="alpha",
            sha256="ab" * 32,
            payment=rejected_receipt,
        )
        client.get_eval_pricing.assert_not_called()
        pay.assert_called_once_with(
            live_wallet=ANY,
            subtensor_network="local",
            amount_rao=40_000_000,
            dest_address=DEST,
            chain_endpoint=None,
        )

    def test_replacement_check_must_succeed_before_saved_proof_is_retired(
        self, good_tar: Path
    ) -> None:
        client = MagicMock()
        client.post_upload_check.side_effect = [
            PaymentRecoveryExpiredError("payment recovery window expired"),
            _rejected_check(),
        ]
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
                "ditto.miner_cli.commands.upload.load_pending_payment",
                return_value=receipt,
            ),
            patch("ditto.miner_cli.commands.upload.clear_pending_payment") as clear,
            patch("ditto.miner_cli.commands.upload.submit_eval_payment") as pay,
            patch(
                "ditto.miner_cli.commands.upload.ApiClient",
                _patch_api_client(client),
            ),
        ):
            rc = run(make_args(good_tar, pay_again=True))

        assert rc == 1
        clear.assert_not_called()
        pay.assert_not_called()

    def test_pay_again_does_not_override_other_precheck_failures(
        self, good_tar: Path
    ) -> None:
        client = MagicMock()
        client.post_upload_check.side_effect = PreCheckRejectedError(
            "destination mismatch"
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
                "ditto.miner_cli.commands.upload.load_pending_payment",
                return_value=receipt,
            ),
            patch("ditto.miner_cli.commands.upload.clear_pending_payment") as clear,
            patch("ditto.miner_cli.commands.upload.submit_eval_payment") as pay,
            patch(
                "ditto.miner_cli.commands.upload.ApiClient",
                _patch_api_client(client),
            ),
        ):
            rc = run(make_args(good_tar, pay_again=True))

        assert rc == 1
        assert client.post_upload_check.call_count == 1
        clear.assert_not_called()
        pay.assert_not_called()

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

    def test_missing_admission_token_fails_closed_before_payment(
        self, good_tar: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        client = MagicMock()
        client.post_upload_check.return_value = UploadCheckResponse(
            ok=True, error_codes=[], messages=[]
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
        assert "no payment was sent" in capsys.readouterr().err.lower()

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
        the miner can retry without paying again; money is on chain regardless
        of whether the API rejected or was unreachable."""
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
        assert "using either the same archive or a replacement" in err
        assert "will not send another transfer" in err
        assert "--payment-block-hash" in err


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


class TestPaymentDisposition:
    """The platform's three payment dispositions must read differently.

    ``reusable_credit`` means no new evaluation was bought. Reporting it with
    the ordinary success line tells a miner they purchased a run they did not,
    which is exactly what happened while these fields were missing from this
    repo's copy of ``UploadAgentResponse``.
    """

    def _run(
        self, good_tar: Path, response: UploadAgentResponse, **arg_overrides
    ) -> tuple[int, MagicMock]:
        client = MagicMock()
        client.post_upload_check.return_value = _ok_check()
        client.get_eval_pricing.return_value = _pricing()
        client.post_upload_agent.return_value = response
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
                "ditto.miner_cli.commands.upload.ApiClient",
                _patch_api_client(client),
            ),
        ):
            rc = run(make_args(good_tar, **arg_overrides))
        return rc, client

    def test_reusable_credit_is_not_reported_as_a_new_submission(
        self, good_tar: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        response = _reusable_credit_response()
        rc, _ = self._run(good_tar, response)
        err = capsys.readouterr().err

        assert rc == 0
        # The ordinary success line must NOT appear: nothing new was submitted.
        assert "upload succeeded" not in err
        assert "submission v2" not in err
        assert "byte-identical" in err
        assert "NOT spent" in err
        assert "reusable credit" in err
        # Names the flag that actually buys another seed, and the existing agent.
        assert "--allow-identical-rescore" in err
        assert str(response.credit_for_agent_id) in err

    def test_credit_consumed_says_no_transfer_was_sent(
        self, good_tar: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, _ = self._run(
            good_tar,
            UploadAgentResponse(
                agent_id=uuid4(),
                version=3,
                status=AgentStatus.UPLOADED,
                payment_disposition="credit_consumed",
            ),
        )
        err = capsys.readouterr().err

        assert rc == 0
        assert "upload succeeded" in err
        assert "funded by your reusable credit" in err

    def test_ordinary_success_is_unchanged(
        self, good_tar: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, _ = self._run(good_tar, _upload_response())
        err = capsys.readouterr().err

        assert rc == 0
        assert "upload succeeded: alpha \u00b7 submission v2" in err
        assert "reusable credit" not in err

    def test_allow_identical_rescore_reaches_both_platform_calls(
        self, good_tar: Path
    ) -> None:
        """The flag is useless unless it rides BOTH the check and the upload.

        ``/upload/check`` refuses the duplicate before payment; ``/upload/agent``
        decides credit-vs-purchase after it. Sending it to only one of the two
        either blocks the miner pre-payment or banks a credit they explicitly
        declined.
        """
        _, client = self._run(
            good_tar, _upload_response(), allow_identical_rescore=True
        )

        check_body = client.post_upload_check.call_args.args[0]
        assert check_body.allow_identical_rescore is True
        upload_kwargs = client.post_upload_agent.call_args.kwargs
        assert upload_kwargs["allow_identical_rescore"] is True

    def test_flag_defaults_off_so_a_duplicate_cannot_spend_tao(
        self, good_tar: Path
    ) -> None:
        _, client = self._run(good_tar, _upload_response())

        check_body = client.post_upload_check.call_args.args[0]
        assert check_body.allow_identical_rescore is False
        upload_kwargs = client.post_upload_agent.call_args.kwargs
        assert upload_kwargs["allow_identical_rescore"] is False


def _unregistered_check() -> UploadCheckResponse:
    return UploadCheckResponse(
        ok=False,
        error_codes=[1101],
        messages=["hotkey is not registered on netuid 118"],
    )


def _multi_rejected_check() -> UploadCheckResponse:
    return UploadCheckResponse(
        ok=False,
        error_codes=[1100, 1101],
        messages=[
            "signature did not verify",
            "hotkey is not registered on netuid 118",
        ],
    )


def _quote(recycle_rao: int = 500_000, balance_rao: int = 12_402_100_000):
    return RegistrationQuote(
        netuid=118,
        hotkey_ss58=HOTKEY,
        coldkey_name="miner",
        coldkey_ss58=DEST,
        recycle_rao=recycle_rao,
        balance_rao=balance_rao,
    )


class TestInlineRegistration:
    """The 1101 pre-check rejection is resolvable without leaving the CLI.

    The pre-check runs before any TAO moves, so a hotkey that is merely
    unregistered is the one rejection the CLI can fix on the miner's
    behalf. Everything here pins that it fixes ONLY that case, and only
    against a live cost the miner has seen.
    """

    def _run(
        self,
        good_tar: Path,
        monkeypatch,
        *,
        checks: list[UploadCheckResponse],
        isatty: bool = True,
        **arg_overrides,
    ):
        monkeypatch.setenv("NETUID", "118")
        client = MagicMock()
        client.post_upload_check.side_effect = checks
        client.get_eval_pricing.return_value = _pricing()
        client.post_upload_agent.return_value = _upload_response()
        fake_handle = MagicMock(hotkey_ss58=HOTKEY, coldkey_name="miner")

        stdin = MagicMock()
        stdin.isatty.return_value = isatty

        with (
            patch("ditto.miner_cli.commands.upload.sys.stdin", stdin),
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
                "ditto.miner_cli.commands.upload.ApiClient",
                _patch_api_client(client),
            ),
            patch(
                "ditto.miner_cli.commands.upload.quote_registration",
                return_value=arg_overrides.pop("quote", _quote()),
            ) as quote,
            patch(
                "ditto.miner_cli.commands.upload.submit_registration",
                return_value=412,
            ) as register,
            patch(
                "ditto.miner_cli.commands.upload.confirm_registration",
                side_effect=arg_overrides.pop("confirm_side_effect", None),
            ) as confirm,
        ):
            rc = run(make_args(good_tar, **arg_overrides))
        return rc, client, quote, register, confirm

    def test_registers_then_continues_to_upload(
        self, good_tar: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, client, quote, register, _ = self._run(
            good_tar, monkeypatch, checks=[_unregistered_check(), _ok_check()]
        )

        assert rc == 0
        quote.assert_called_once()
        assert quote.call_args.kwargs["netuid"] == 118
        register.assert_called_once()
        # The amount the miner confirmed is the ceiling carried into submission.
        assert register.call_args.kwargs["confirmed_recycle_rao"] == 500_000
        # Re-checked after registering, then the upload proceeded.
        assert client.post_upload_check.call_count == 2
        client.post_upload_agent.assert_called_once()
        err = capsys.readouterr().err
        assert "registered on netuid 118: uid 412" in err
        assert "continuing upload" in err

    def test_never_registers_when_another_rejection_also_blocks(
        self, good_tar: Path, monkeypatch
    ) -> None:
        rc, client, quote, register, _ = self._run(
            good_tar, monkeypatch, checks=[_multi_rejected_check()]
        )

        assert rc == 1
        quote.assert_not_called()
        register.assert_not_called()
        assert client.post_upload_check.call_count == 1

    def test_no_register_flag_keeps_the_old_dead_end(
        self, good_tar: Path, monkeypatch
    ) -> None:
        rc, _, quote, register, _ = self._run(
            good_tar,
            monkeypatch,
            checks=[_unregistered_check()],
            no_register=True,
        )

        assert rc == 1
        quote.assert_not_called()
        register.assert_not_called()

    def test_non_interactive_without_register_flag_refuses_to_burn(
        self, good_tar: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, _, quote, register, _ = self._run(
            good_tar,
            monkeypatch,
            checks=[_unregistered_check()],
            isatty=False,
        )

        assert rc == 1
        quote.assert_called_once()
        register.assert_not_called()
        err = capsys.readouterr().err
        assert "--register" in err
        assert "btcli subnets register --netuid 118" in err

    def test_register_flag_pre_authorizes_non_interactive_run(
        self, good_tar: Path, monkeypatch
    ) -> None:
        rc, _, _, register, confirm = self._run(
            good_tar,
            monkeypatch,
            checks=[_unregistered_check(), _ok_check()],
            isatty=False,
            register=True,
        )

        assert rc == 0
        register.assert_called_once()
        assert confirm.call_args.kwargs["skip"] is True

    def test_yes_alone_does_not_skip_the_registration_prompt(
        self, good_tar: Path, monkeypatch
    ) -> None:
        """``--yes`` covers the eval fee only; the recycle cost is separate."""
        rc, _, _, _, confirm = self._run(
            good_tar,
            monkeypatch,
            checks=[_unregistered_check(), _ok_check()],
            yes=True,
        )

        assert rc == 0
        assert confirm.call_args.kwargs["skip"] is False

    def test_declined_prompt_exits_two_without_registering(
        self, good_tar: Path, monkeypatch
    ) -> None:
        rc, _, _, register, _ = self._run(
            good_tar,
            monkeypatch,
            checks=[_unregistered_check()],
            confirm_side_effect=RegistrationCancelledError(
                "registration cancelled (response='n')"
            ),
        )

        assert rc == 2
        register.assert_not_called()

    def test_unaffordable_quote_reports_shortfall_and_does_not_submit(
        self, good_tar: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, _, _, register, confirm = self._run(
            good_tar,
            monkeypatch,
            checks=[_unregistered_check()],
            quote=_quote(recycle_rao=5_000_000_000, balance_rao=1_000_000_000),
        )

        assert rc == 1
        confirm.assert_not_called()
        register.assert_not_called()
        assert "4000000000 rao short" in capsys.readouterr().err

    def test_registration_that_does_not_unblock_still_fails(
        self, good_tar: Path, monkeypatch
    ) -> None:
        """A second rejection after registering is real and must not be eaten."""
        rc, client, _, register, _ = self._run(
            good_tar,
            monkeypatch,
            checks=[_unregistered_check(), _rejected_check()],
        )

        assert rc == 1
        register.assert_called_once()
        assert client.post_upload_check.call_count == 2
        client.post_upload_agent.assert_not_called()

    def test_declining_by_flag_still_prints_the_btcli_command(
        self, good_tar: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, _, _, _, _ = self._run(
            good_tar,
            monkeypatch,
            checks=[_unregistered_check()],
            no_register=True,
        )

        assert rc == 1
        assert (
            "btcli subnets register --netuid 118 --wallet-name miner "
            "--hotkey default" in capsys.readouterr().err
        )

    def test_multi_code_rejection_still_prints_the_btcli_command(
        self, good_tar: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, _, _, _, _ = self._run(
            good_tar, monkeypatch, checks=[_multi_rejected_check()]
        )

        assert rc == 1
        assert "btcli subnets register --netuid 118" in capsys.readouterr().err
