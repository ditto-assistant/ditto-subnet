"""Unit tests for :mod:`ditto.miner_cli.commands.attest`.

Every collaborator (wallet loading, the API client) is replaced with a mock so
the orchestrator's control flow is exercised without touching disk, the chain,
or the network.

Invariants pinned:

- Happy path: both wallets load, both halves are signed over one shared nonce
  and issued_at, the body reaches the API, exit 0, attestation_id on stdout.
- ``--print-only``: the signed body lands on stdout and the API is never
  called.
- Missing wallet flags: exit 1 before any wallet is loaded.
- Old and new hotkey resolving to the same address: exit 1, nothing submitted.
- A declined confirmation: exit 1, nothing submitted.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from ditto.api_models import HotkeyAttestationResponse
from ditto.miner_cli.commands.attest import run
from ditto.miner_cli.errors import AttestationCancelledError, AttestationRejectedError

OLD_HOTKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
NEW_HOTKEY = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"


def make_args(**overrides) -> argparse.Namespace:
    base = {
        "old_coldkey_name": "old-miner",
        "old_hotkey_name": "default",
        "coldkey_name": "miner",
        "hotkey_name": "default",
        "netuid": 118,
        "yes": True,  # bypass the interactive prompt by default in tests
        "print_only": False,
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


def _wallets() -> MagicMock:
    """``load_wallet`` side effect returning old then new (handle, wallet)."""
    old_handle = MagicMock(hotkey_ss58=OLD_HOTKEY, coldkey_name="old-miner")
    new_handle = MagicMock(hotkey_ss58=NEW_HOTKEY, coldkey_name="miner")
    old_wallet = MagicMock()
    old_wallet.hotkey.sign.return_value = b"\xaa" * 64
    new_wallet = MagicMock()
    new_wallet.hotkey.sign.return_value = b"\xbb" * 64
    return MagicMock(side_effect=[(old_handle, old_wallet), (new_handle, new_wallet)])


def _attestation_response() -> HotkeyAttestationResponse:
    return HotkeyAttestationResponse(
        attestation_id=uuid4(),
        netuid=118,
        old_hotkey=OLD_HOTKEY,
        new_hotkey=NEW_HOTKEY,
        created_at=datetime(2026, 7, 26, 15, 0, tzinfo=UTC),
    )


class TestAttestHappyPath:
    def test_submits_and_prints_attestation_id(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        client = MagicMock()
        response = _attestation_response()
        client.post_hotkey_attestation.return_value = response

        with (
            patch("ditto.miner_cli.commands.attest.load_wallet", _wallets()),
            patch(
                "ditto.miner_cli.commands.attest.ApiClient", _patch_api_client(client)
            ),
        ):
            rc = run(make_args())

        assert rc == 0
        out = capsys.readouterr().out
        assert str(response.attestation_id) in out
        client.post_hotkey_attestation.assert_called_once()

    def test_both_halves_share_one_nonce_and_issued_at(self) -> None:
        """The platform rejects a pair whose halves bind different tuples, so
        the command must sign both over exactly one mint."""
        client = MagicMock()
        client.post_hotkey_attestation.return_value = _attestation_response()

        with (
            patch("ditto.miner_cli.commands.attest.load_wallet", _wallets()),
            patch(
                "ditto.miner_cli.commands.attest.ApiClient", _patch_api_client(client)
            ),
            patch(
                "ditto.miner_cli.commands.attest.sign_attestation",
                return_value="aa" * 64,
            ) as sign_attest,
            patch(
                "ditto.miner_cli.commands.attest.sign_acceptance",
                return_value="bb" * 64,
            ) as sign_accept,
        ):
            rc = run(make_args())

        assert rc == 0
        attest_kwargs = sign_attest.call_args.kwargs
        accept_kwargs = sign_accept.call_args.kwargs
        assert attest_kwargs["nonce"] == accept_kwargs["nonce"]
        assert attest_kwargs["issued_at"] == accept_kwargs["issued_at"]
        assert attest_kwargs["old_hotkey"] == OLD_HOTKEY
        assert attest_kwargs["new_hotkey"] == NEW_HOTKEY
        assert attest_kwargs["netuid"] == 118

        body = client.post_hotkey_attestation.call_args.args[0]
        assert body.attestation_signature == "aa" * 64
        assert body.acceptance_signature == "bb" * 64
        assert body.nonce == attest_kwargs["nonce"]

    def test_netuid_flag_reaches_the_signed_payload(self) -> None:
        client = MagicMock()
        client.post_hotkey_attestation.return_value = _attestation_response()

        with (
            patch("ditto.miner_cli.commands.attest.load_wallet", _wallets()),
            patch(
                "ditto.miner_cli.commands.attest.ApiClient", _patch_api_client(client)
            ),
        ):
            rc = run(make_args(netuid=42))

        assert rc == 0
        body = client.post_hotkey_attestation.call_args.args[0]
        assert body.netuid == 42


class TestPrintOnly:
    def test_prints_the_body_and_never_calls_the_api(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ctor = _patch_api_client(MagicMock())

        with (
            patch("ditto.miner_cli.commands.attest.load_wallet", _wallets()),
            patch("ditto.miner_cli.commands.attest.ApiClient", ctor),
        ):
            rc = run(make_args(print_only=True))

        assert rc == 0
        ctor.assert_not_called()

        captured = capsys.readouterr()
        body = json.loads(captured.out)
        assert body["netuid"] == 118
        assert body["old_hotkey"] == OLD_HOTKEY
        assert body["new_hotkey"] == NEW_HOTKEY
        assert len(body["attestation_signature"]) == 128
        assert len(body["acceptance_signature"]) == 128
        assert "nothing was submitted" in captured.err

    def test_print_only_does_not_prompt(self) -> None:
        """Nothing is sent, so there is nothing to confirm; a prompt here
        would break the non-interactive air-gapped workflow."""
        with (
            patch("ditto.miner_cli.commands.attest.load_wallet", _wallets()),
            patch("ditto.miner_cli.commands.attest.confirm_attestation") as confirm,
        ):
            rc = run(make_args(print_only=True, yes=False))

        assert rc == 0
        confirm.assert_not_called()


class TestAttestFailurePaths:
    @pytest.mark.parametrize(
        "missing",
        [
            {"old_coldkey_name": None},
            {"old_hotkey_name": None},
            {"coldkey_name": None},
            {"hotkey_name": None},
        ],
    )
    def test_missing_wallet_flags_exit_one_before_loading(
        self, missing, capsys: pytest.CaptureFixture[str]
    ) -> None:
        loader = _wallets()
        with patch("ditto.miner_cli.commands.attest.load_wallet", loader):
            rc = run(make_args(**missing))

        assert rc == 1
        loader.assert_not_called()
        assert "required" in capsys.readouterr().err

    def test_same_hotkey_twice_exits_one_without_submitting(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        same = MagicMock(hotkey_ss58=OLD_HOTKEY, coldkey_name="miner")
        loader = MagicMock(side_effect=[(same, MagicMock()), (same, MagicMock())])
        ctor = _patch_api_client(MagicMock())

        with (
            patch("ditto.miner_cli.commands.attest.load_wallet", loader),
            patch("ditto.miner_cli.commands.attest.ApiClient", ctor),
        ):
            rc = run(make_args())

        assert rc == 1
        ctor.assert_not_called()
        assert "cannot succeed itself" in capsys.readouterr().err

    def test_declined_confirmation_exits_one_without_submitting(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ctor = _patch_api_client(MagicMock())

        with (
            patch("ditto.miner_cli.commands.attest.load_wallet", _wallets()),
            patch("ditto.miner_cli.commands.attest.ApiClient", ctor),
            patch(
                "ditto.miner_cli.commands.attest.confirm_attestation",
                side_effect=AttestationCancelledError("attestation cancelled"),
            ),
        ):
            rc = run(make_args(yes=False))

        assert rc == 1
        ctor.assert_not_called()
        assert "cancelled" in capsys.readouterr().err

    def test_api_rejection_exits_one(self, capsys: pytest.CaptureFixture[str]) -> None:
        client = MagicMock()
        client.post_hotkey_attestation.side_effect = AttestationRejectedError(
            "hotkey-attestation failed: HTTP 400 code=1300 expired"
        )

        with (
            patch("ditto.miner_cli.commands.attest.load_wallet", _wallets()),
            patch(
                "ditto.miner_cli.commands.attest.ApiClient", _patch_api_client(client)
            ),
        ):
            rc = run(make_args())

        assert rc == 1
        assert "expired" in capsys.readouterr().err
