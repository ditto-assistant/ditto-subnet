"""Unit tests for :mod:`ditto.miner_cli.commands.attest`.

Every collaborator (wallet loading, the API client) is replaced with a mock so
the orchestrator's control flow is exercised without touching disk, the chain,
or the network.

Invariants pinned:

- Happy path: both wallets load, both halves are signed over one shared nonce
  and issued_at, the body reaches the API, exit 0, attestation_id on stdout.
- Canonical ordering: whichever wallet lands on ``lo`` signs the ``lo`` half,
  no matter which flag pair carried it.
- ``--key-kind coldkey`` signs with the coldkey and names it as the signer.
- ``--print-only``: the signed body lands on stdout and the API is never
  called.
- Missing wallet flags: exit 1 before any wallet is loaded.
- Both wallets resolving to the same hotkey: exit 1, nothing submitted.
- A declined confirmation: exit 1, nothing submitted.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from ditto.api_models import OwnerLinkResponse
from ditto.miner_cli.commands.attest import run
from ditto.miner_cli.errors import AttestationCancelledError, AttestationRejectedError

# Sorted lexicographically, 5F... < 5G..., so THIS side is the "hi" endpoint
# under the default flag assignment below.
HOTKEY_THIS = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
HOTKEY_OTHER = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
COLDKEY_THIS = "5DAAnrj7VHTznn2AWBemMuyBwZWs6FNFjdyVXUeYum3PTXFy"
COLDKEY_OTHER = "5FLSigC9HGRKVhB9FiEo4Y3koPsNmBmLJbpXg2mp1hXcS59Y"

HOTKEY_LO = HOTKEY_OTHER
HOTKEY_HI = HOTKEY_THIS


def make_args(**overrides) -> argparse.Namespace:
    base = {
        "coldkey_name": "miner",
        "hotkey_name": "default",
        "other_coldkey_name": "old-miner",
        "other_hotkey_name": "default",
        "key_kind": "hotkey",
        "other_key_kind": "hotkey",
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


def _wallet(hotkey_ss58: str, coldkey_ss58: str, sig_byte: bytes) -> MagicMock:
    """A live-wallet mock whose hotkey and coldkey sign distinguishably."""
    wallet = MagicMock()
    wallet.hotkey.ss58_address = hotkey_ss58
    wallet.hotkey.sign.return_value = sig_byte * 64
    wallet.coldkeypub.ss58_address = coldkey_ss58
    wallet.coldkey.ss58_address = coldkey_ss58
    wallet.coldkey.sign.return_value = bytes([sig_byte[0] ^ 0xFF]) * 64
    return wallet


def _wallet_pair() -> tuple[MagicMock, MagicMock, MagicMock]:
    """Return ``(load_wallet_mock, this_wallet, other_wallet)``.

    The loader yields THIS side then OTHER side, matching the order
    :func:`run` loads them in.
    """
    this_handle = MagicMock(hotkey_ss58=HOTKEY_THIS, coldkey_name="miner")
    other_handle = MagicMock(hotkey_ss58=HOTKEY_OTHER, coldkey_name="old-miner")
    this_wallet = _wallet(HOTKEY_THIS, COLDKEY_THIS, b"\xaa")
    other_wallet = _wallet(HOTKEY_OTHER, COLDKEY_OTHER, b"\xbb")
    loader = MagicMock(
        side_effect=[(this_handle, this_wallet), (other_handle, other_wallet)]
    )
    return loader, this_wallet, other_wallet


def _wallets() -> MagicMock:
    """``load_wallet`` side effect returning THIS then OTHER (handle, wallet)."""
    return _wallet_pair()[0]


def _owner_link_response(grade: str = "hotkey-hotkey") -> OwnerLinkResponse:
    return OwnerLinkResponse(
        attestation_id=uuid4(),
        netuid=118,
        hotkey_lo=HOTKEY_LO,
        hotkey_hi=HOTKEY_HI,
        evidence_grade=grade,  # type: ignore[arg-type]
        created_at=datetime(2026, 7, 26, 15, 0, tzinfo=UTC),
    )


class TestAttestHappyPath:
    def test_submits_and_prints_attestation_id(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        client = MagicMock()
        response = _owner_link_response()
        client.post_owner_link.return_value = response

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
        client.post_owner_link.assert_called_once()

    def test_both_halves_share_one_nonce_and_issued_at(self) -> None:
        """The platform rejects a pair whose halves bind different tuples, so
        the command must sign both over exactly one mint."""
        client = MagicMock()
        client.post_owner_link.return_value = _owner_link_response()

        with (
            patch("ditto.miner_cli.commands.attest.load_wallet", _wallets()),
            patch(
                "ditto.miner_cli.commands.attest.ApiClient", _patch_api_client(client)
            ),
            patch(
                "ditto.miner_cli.commands.attest.sign_link_half",
                side_effect=["aa" * 64, "bb" * 64],
            ) as sign_half,
        ):
            rc = run(make_args())

        assert rc == 0
        first, second = [call.kwargs for call in sign_half.call_args_list]
        assert first["nonce"] == second["nonce"]
        assert first["issued_at"] == second["issued_at"]
        assert first["netuid"] == second["netuid"] == 118
        assert first["hotkey_lo"] == second["hotkey_lo"] == HOTKEY_LO
        assert first["hotkey_hi"] == second["hotkey_hi"] == HOTKEY_HI

        body = client.post_owner_link.call_args.args[0]
        assert body.nonce == first["nonce"]
        assert body.proof_a.signature == "aa" * 64
        assert body.proof_b.signature == "bb" * 64

    def test_canonical_ordering_decides_each_side(self) -> None:
        """THIS side's hotkey sorts higher, so it must sign the ``hi`` half
        even though it came in on the primary flag pair."""
        client = MagicMock()
        client.post_owner_link.return_value = _owner_link_response()

        with (
            patch("ditto.miner_cli.commands.attest.load_wallet", _wallets()),
            patch(
                "ditto.miner_cli.commands.attest.ApiClient", _patch_api_client(client)
            ),
            patch(
                "ditto.miner_cli.commands.attest.sign_link_half",
                side_effect=["aa" * 64, "bb" * 64],
            ) as sign_half,
        ):
            rc = run(make_args())

        assert rc == 0
        this_call, other_call = [call.kwargs for call in sign_half.call_args_list]
        assert this_call["side"] == "hi"
        assert other_call["side"] == "lo"

    def test_hotkeys_travel_in_flag_order_with_matching_proofs(self) -> None:
        """The server sorts the pair itself; the CLI sends ``hotkey_a`` /
        ``hotkey_b`` as given and each proof next to its own hotkey."""
        client = MagicMock()
        client.post_owner_link.return_value = _owner_link_response()

        with (
            patch("ditto.miner_cli.commands.attest.load_wallet", _wallets()),
            patch(
                "ditto.miner_cli.commands.attest.ApiClient", _patch_api_client(client)
            ),
        ):
            rc = run(make_args())

        assert rc == 0
        body = client.post_owner_link.call_args.args[0]
        assert body.hotkey_a == HOTKEY_THIS
        assert body.hotkey_b == HOTKEY_OTHER
        assert body.proof_a.signer == HOTKEY_THIS
        assert body.proof_b.signer == HOTKEY_OTHER
        assert body.proof_a.key_kind == "hotkey"
        assert body.proof_b.key_kind == "hotkey"

    def test_netuid_flag_reaches_the_signed_payload(self) -> None:
        client = MagicMock()
        client.post_owner_link.return_value = _owner_link_response()

        with (
            patch("ditto.miner_cli.commands.attest.load_wallet", _wallets()),
            patch(
                "ditto.miner_cli.commands.attest.ApiClient", _patch_api_client(client)
            ),
        ):
            rc = run(make_args(netuid=42))

        assert rc == 0
        body = client.post_owner_link.call_args.args[0]
        assert body.netuid == 42


class TestKeyKinds:
    def test_coldkey_kind_names_the_coldkey_as_the_signer(self) -> None:
        """The recovery path: prove THIS side with the coldkey that owns the
        hotkey. The proof carries the coldkey's SS58, not the hotkey's."""
        client = MagicMock()
        client.post_owner_link.return_value = _owner_link_response("mixed")

        with (
            patch("ditto.miner_cli.commands.attest.load_wallet", _wallets()),
            patch(
                "ditto.miner_cli.commands.attest.ApiClient", _patch_api_client(client)
            ),
        ):
            rc = run(make_args(key_kind="coldkey"))

        assert rc == 0
        body = client.post_owner_link.call_args.args[0]
        assert body.proof_a.key_kind == "coldkey"
        assert body.proof_a.signer == COLDKEY_THIS
        # The other side is untouched by this flag.
        assert body.proof_b.key_kind == "hotkey"
        assert body.proof_b.signer == HOTKEY_OTHER

    def test_each_side_chooses_its_kind_independently(self) -> None:
        client = MagicMock()
        client.post_owner_link.return_value = _owner_link_response("coldkey-coldkey")

        with (
            patch("ditto.miner_cli.commands.attest.load_wallet", _wallets()),
            patch(
                "ditto.miner_cli.commands.attest.ApiClient", _patch_api_client(client)
            ),
        ):
            rc = run(make_args(key_kind="coldkey", other_key_kind="coldkey"))

        assert rc == 0
        body = client.post_owner_link.call_args.args[0]
        assert body.proof_a.signer == COLDKEY_THIS
        assert body.proof_b.signer == COLDKEY_OTHER

    def test_coldkey_kind_signs_with_the_coldkey_not_the_hotkey(self) -> None:
        """No funds move, but the right key must sign: a coldkey proof that
        was actually produced by the hotkey would never verify."""
        loader, this_wallet, _ = _wallet_pair()
        client = MagicMock()
        client.post_owner_link.return_value = _owner_link_response("mixed")

        with (
            patch("ditto.miner_cli.commands.attest.load_wallet", loader),
            patch(
                "ditto.miner_cli.commands.attest.ApiClient", _patch_api_client(client)
            ),
        ):
            rc = run(make_args(key_kind="coldkey"))

        assert rc == 0
        this_wallet.coldkey.sign.assert_called_once()
        this_wallet.hotkey.sign.assert_not_called()

    def test_the_signed_bytes_name_the_side_and_kind(self) -> None:
        """End-to-end through the real message builder: what the coldkey half
        signs must carry ``:hi:coldkey:<coldkey>``."""
        loader, this_wallet, _ = _wallet_pair()
        client = MagicMock()
        client.post_owner_link.return_value = _owner_link_response("mixed")

        with (
            patch("ditto.miner_cli.commands.attest.load_wallet", loader),
            patch(
                "ditto.miner_cli.commands.attest.ApiClient", _patch_api_client(client)
            ),
        ):
            rc = run(make_args(key_kind="coldkey"))

        assert rc == 0
        payload = this_wallet.coldkey.sign.call_args.args[0].decode()
        assert payload.startswith("ditto-owner-link:v1:118:")
        assert payload.endswith(f":hi:coldkey:{COLDKEY_THIS}")


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
        assert body["hotkey_a"] == HOTKEY_THIS
        assert body["hotkey_b"] == HOTKEY_OTHER
        assert len(body["proof_a"]["signature"]) == 128
        assert len(body["proof_b"]["signature"]) == 128
        assert "nothing was submitted" in captured.err
        assert "does NOT transfer any TAO" in captured.err

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
            {"coldkey_name": None},
            {"hotkey_name": None},
            {"other_coldkey_name": None},
            {"other_hotkey_name": None},
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
        same = MagicMock(hotkey_ss58=HOTKEY_THIS, coldkey_name="miner")
        loader = MagicMock(side_effect=[(same, MagicMock()), (same, MagicMock())])
        ctor = _patch_api_client(MagicMock())

        with (
            patch("ditto.miner_cli.commands.attest.load_wallet", loader),
            patch("ditto.miner_cli.commands.attest.ApiClient", ctor),
        ):
            rc = run(make_args())

        assert rc == 1
        ctor.assert_not_called()
        assert "cannot be linked to itself" in capsys.readouterr().err

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
        client.post_owner_link.side_effect = AttestationRejectedError(
            "owner-link failed: HTTP 400 code=1300 expired"
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
