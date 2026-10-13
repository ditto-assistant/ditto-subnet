"""CLI consent and identity proof boundaries without chain transactions."""

from __future__ import annotations

import argparse
import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import bittensor
import pytest

from ditto.api_models.contributor_identity import (
    ClaSignatureView,
    ClaStatus,
    ClaVersionView,
    GithubLinkAttemptStatus,
    GithubLinkStart,
    cla_message,
)
from ditto.miner_cli.__main__ import _build_parser
from ditto.miner_cli.commands import cla, link_github

TEXT = b"TEST FIXTURE ONLY. NOT A LEGAL AGREEMENT.\n"
KEY = bittensor.Keypair.create_from_uri("//Alice")


def args(**over):
    base = {
        "network": "local",
        "coldkey_name": "fixture",
        "hotkey_name": "fixture",
        "cla_command": "sign",
        "json": False,
        "output": None,
        "version": None,
        "confirmation": None,
        "github_command": "link",
        "no_browser": True,
        "wait": 10,
        "yes": True,
        "key_kind": "hotkey",
    }
    base.update(over)
    return argparse.Namespace(**base)


def wallet():
    actual = MagicMock()
    actual.hotkey.ss58_address = KEY.ss58_address
    actual.hotkey.sign.side_effect = KEY.sign
    return SimpleNamespace(hotkey_ss58=KEY.ss58_address), actual


def version():
    return ClaVersionView(
        version=1,
        sha256=hashlib.sha256(TEXT).hexdigest(),
        published_at=datetime.now(UTC),
        effective_at=datetime.now(UTC),
    )


def client():
    api = MagicMock()
    api.__enter__.return_value = api
    v = version()
    api.get_cla_status.return_value = ClaStatus(netuid=118, mode="off", current=v)
    api.get_cla_text.return_value = TEXT

    def receipt(body):
        assert KEY.verify(
            cla_message(
                netuid=body.netuid,
                cla_version=body.cla_version,
                cla_sha256=body.cla_sha256,
                hotkey=body.hotkey,
                nonce=body.nonce,
                issued_at=body.issued_at,
            ),
            bytes.fromhex(body.signature),
        )
        return ClaSignatureView(
            signature_id=uuid4(),
            netuid=118,
            hotkey=body.hotkey,
            coldkey=KEY.ss58_address,
            cla_version=body.cla_version,
            cla_sha256=body.cla_sha256,
            nonce=body.nonce,
            issued_at=body.issued_at,
            signature=body.signature,
            signed_at=datetime.now(UTC),
            owner_block=123,
            owner_block_hash="0x" + "ab" * 32,
        )

    api.sign_cla.side_effect = receipt
    return api


def test_cla_signs_exact_verified_bytes_after_explicit_typed_confirmation(
    tmp_path, capsys
):
    api = client()
    handle, live = wallet()
    target = tmp_path / "CLA.md"
    v = api.get_cla_status.return_value.current

    def confirm(_prompt):
        assert target.read_bytes() == TEXT
        assert str(target) in capsys.readouterr().err
        live.hotkey.sign.assert_not_called()
        return f"SIGN CLA {v.version} {v.sha256}"

    with (
        patch.object(cla, "load_wallet", return_value=(handle, live)),
        patch.object(cla, "ApiClient", return_value=api),
        patch("builtins.input", side_effect=confirm),
    ):
        assert cla.run(args(output=target)) == 0
    live.hotkey.sign.assert_called_once()
    api.sign_cla.assert_called_once()


def test_github_omitted_wallet_names_without_tty_never_prompt(capsys):
    api = client()
    with (
        patch.object(link_github, "ApiClient", return_value=api),
        patch.object(
            link_github,
            "load_miner_session",
            return_value={"token": "fixture-session", "hotkey": KEY.ss58_address},
        ),
        patch("sys.stdin.isatty", return_value=False),
        patch(
            "builtins.input", side_effect=AssertionError("must not prompt")
        ) as prompt,
    ):
        assert (
            link_github.run(args(coldkey_name=None, hotkey_name=None, yes=False)) == 1
        )
    assert "wallet name and hotkey are required" in capsys.readouterr().err
    prompt.assert_not_called()
    api.start_github_link.assert_not_called()


@pytest.mark.parametrize("case", ["digest", "confirmation", "missing"])
def test_cla_refuses_to_sign_unverified_or_unconfirmed_text(tmp_path, case):
    api = client()
    handle, live = wallet()
    if case == "digest":
        api.get_cla_text.return_value = b"different bytes"
    if case == "missing":
        api.get_cla_status.return_value = ClaStatus(netuid=118, mode="off")
    with (
        patch.object(cla, "load_wallet", return_value=(handle, live)),
        patch.object(cla, "ApiClient", return_value=api),
    ):
        assert cla.run(args(output=tmp_path / "CLA.md", confirmation="yes")) == 1
    live.hotkey.sign.assert_not_called()
    api.sign_cla.assert_not_called()


def test_github_prints_verified_account_before_loading_private_wallet(capsys):
    api = MagicMock()
    api.__enter__.return_value = api
    attempt = uuid4()
    api.start_github_link.return_value = GithubLinkStart(
        netuid=118,
        attempt_id=attempt,
        authorize_url="https://dittobench.ai/api/v1/miner-auth/github/authorize/fixture?launch=opaque",
        expires_in=600,
    )
    api.get_github_link_attempt.return_value = GithubLinkAttemptStatus(
        attempt_id=attempt,
        status="accepted",
        github_user_id=100,
        github_login="miner-fixture",
    )
    handle, live = wallet()

    def load(**_kwargs):
        printed = capsys.readouterr().err
        assert "miner-fixture" in printed and "id 100" in printed
        return handle, live

    with (
        patch.object(
            link_github,
            "load_miner_session",
            return_value={"token": "fixture-session", "hotkey": KEY.ss58_address},
        ),
        patch.object(link_github, "load_wallet", side_effect=load),
        patch.object(link_github, "ApiClient", return_value=api),
    ):
        assert link_github.run(args()) == 0
    proof = api.confirm_github_link.call_args.kwargs["body"]
    assert (
        proof.github_user_id == 100
        and proof.github_login == "miner-fixture"
        and proof.hotkey == KEY.ss58_address
    )
    live.hotkey.sign.assert_called_once()


def test_parser_exposes_commands_without_implicit_cla_acceptance():
    parser = _build_parser()
    assert parser.parse_args(["cla", "status"]).cla_command == "status"
    assert parser.parse_args(["link-github", "status"]).github_command == "status"
    assert parser.parse_args(["link-github", "unlink"]).github_command == "unlink"
    with pytest.raises(SystemExit):
        parser.parse_args(["cla", "sign", "--yes"])


def test_unsigned_enforce_upload_stops_before_archive_or_payment(tmp_path):
    from ditto.miner_cli.commands import upload
    from ditto.tests.miner_cli.commands.test_upload import make_args

    api = client()
    api.get_cla_status.return_value = ClaStatus(
        netuid=118, mode="enforce", current=version(), is_current=False
    )
    handle, live = wallet()
    with (
        patch.object(upload, "load_wallet", return_value=(handle, live)),
        patch.object(upload, "ApiClient", return_value=api),
        patch.object(upload, "run_preflight") as archive,
        patch.object(upload, "submit_eval_payment") as payment,
    ):
        assert upload.run(make_args(tmp_path / "unpackaged.tgz")) == 1
    archive.assert_not_called()
    payment.assert_not_called()
