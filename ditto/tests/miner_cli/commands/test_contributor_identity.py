"""CLI consent and identity proof boundaries without chain transactions."""

from __future__ import annotations

import argparse
import hashlib
import json
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
    github_unlink_message,
)
from ditto.miner_cli.__main__ import _build_parser, main
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


@pytest.mark.parametrize("after_unlink", [False, True])
def test_unlink_signing_options_work_before_and_after_subcommand(after_unlink):
    options = [
        "--coldkey",
        "W",
        "--hotkey",
        "H",
        "--key-kind",
        "coldkey",
        "--yes",
        "--json",
    ]
    argv = (
        ["link-github", "unlink", *options]
        if after_unlink
        else ["link-github", *options, "unlink"]
    )
    parsed = _build_parser().parse_args(argv)
    assert parsed.github_command == "unlink"
    assert parsed.json is True
    assert (parsed.coldkey_name, parsed.hotkey_name, parsed.key_kind, parsed.yes) == (
        "W",
        "H",
        "coldkey",
        True,
    )


@pytest.mark.parametrize("has_link", [True, False])
def test_json_unlink_signs_current_context_for_a_global_link(capsys, has_link):
    api = client()
    handle, live = wallet()
    link_id = uuid4()
    api.get_github_link.return_value = SimpleNamespace(
        netuid=119,
        link=SimpleNamespace(
            netuid=118, link_id=link_id, github_login="fixture", github_user_id=100
        )
        if has_link
        else None,
    )
    with (
        patch.object(link_github, "ApiClient", return_value=api),
        patch.object(link_github, "load_wallet", return_value=(handle, live)),
        patch.object(
            link_github,
            "load_miner_session",
            return_value={"token": "fixture-session", "hotkey": KEY.ss58_address},
        ),
    ):
        assert link_github.run(args(github_command="unlink", json=True)) == 0
    assert json.loads(capsys.readouterr().out) == {
        "link_id": str(link_id) if has_link else None,
        "revoked": has_link,
    }
    if has_link:
        body = api.unlink_github.call_args.kwargs["body"]
        assert body.netuid == 119 and body.link_id == link_id
        assert KEY.verify(
            github_unlink_message(
                netuid=body.netuid,
                hotkey=body.hotkey,
                link_id=body.link_id,
                nonce=body.nonce,
                issued_at=body.issued_at,
                key_kind=body.proof.key_kind,
                signer=body.proof.signer,
            ),
            bytes.fromhex(body.proof.signature),
        )
    else:
        api.unlink_github.assert_not_called()
        live.hotkey.sign.assert_not_called()


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


@pytest.mark.parametrize("stage", ["status", "sign"])
def test_malformed_successful_cla_responses_fail_cleanly(tmp_path, capsys, stage):
    api = client()
    handle, live = wallet()
    if stage == "status":
        api.get_cla_status.side_effect = ValueError("malformed fixture response")
    else:
        api.sign_cla.side_effect = ValueError("malformed fixture response")
    v = version()
    with (
        patch.object(cla, "ApiClient", return_value=api),
        patch.object(cla, "load_wallet", return_value=(handle, live)),
    ):
        assert (
            cla.run(
                args(output=tmp_path / "CLA.md", confirmation=f"SIGN CLA 1 {v.sha256}")
            )
            == 1
        )
    assert "malformed fixture response" in capsys.readouterr().err


def test_cla_confirmation_interrupt_exits_130_without_signing(tmp_path, capsys):
    api = client()
    handle, live = wallet()
    with (
        patch.object(cla, "ApiClient", return_value=api),
        patch.object(cla, "load_wallet", return_value=(handle, live)),
        patch("builtins.input", side_effect=KeyboardInterrupt),
    ):
        assert (
            main(
                [
                    "cla",
                    "sign",
                    "--coldkey",
                    "fixture",
                    "--hotkey",
                    "fixture",
                    "--output",
                    str(tmp_path / "CLA.md"),
                ]
            )
            == 130
        )
    api.sign_cla.assert_not_called()
    live.hotkey.sign.assert_not_called()
    assert "interrupted" in capsys.readouterr().err


def test_github_link_completed_by_another_client_returns_without_signing():
    api = client()
    attempt = uuid4()
    api.start_github_link.return_value = GithubLinkStart(
        netuid=118,
        attempt_id=attempt,
        authorize_url="https://dittobench.ai/fixture#launch=fixture",
        expires_in=600,
    )
    api.get_github_link_attempt.return_value = GithubLinkAttemptStatus(
        attempt_id=attempt, status="linked"
    )
    api.get_github_link.return_value = SimpleNamespace(
        link=SimpleNamespace(
            link_attempt_id=attempt,
            hotkey=KEY.ss58_address,
            github_login="fixture",
            github_user_id=100,
            linked_at=datetime.now(UTC),
        )
    )
    with (
        patch.object(link_github, "ApiClient", return_value=api),
        patch.object(
            link_github,
            "load_miner_session",
            return_value={"token": "fixture", "hotkey": KEY.ss58_address},
        ),
        patch.object(link_github, "load_wallet") as load,
        patch.object(
            link_github.time, "sleep", side_effect=AssertionError("must not wait")
        ),
    ):
        assert link_github.run(args()) == 0
    load.assert_not_called()
    api.confirm_github_link.assert_not_called()


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
