"""CLI consent and identity proof boundaries without chain transactions."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import bittensor
import httpx
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
from ditto.miner_cli.errors import ApiResponseError, TransientApiError

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

    def confirm():
        assert target.read_bytes() == TEXT
        output = capsys.readouterr()
        assert str(target) in output.err
        assert "Type exactly" in output.err
        assert not output.out
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


@pytest.mark.parametrize("command", ["link", "unlink"])
def test_github_coldkey_unlock_failure_is_actionable_without_traceback(command, capsys):
    class KeyfileFailure(Exception):
        pass

    class LockedWallet:
        hotkey = KEY

        @property
        def coldkey(self):
            raise KeyfileFailure("private SDK error fixture")

    api = client()
    attempt = uuid4()
    api.start_github_link.return_value = GithubLinkStart(
        netuid=118,
        attempt_id=attempt,
        authorize_url="https://dittobench.ai/fixture",
        expires_in=600,
    )
    api.get_github_link_attempt.return_value = GithubLinkAttemptStatus(
        attempt_id=attempt,
        status="accepted",
        github_user_id=100,
        github_login="miner-fixture",
    )
    api.get_github_link.return_value = SimpleNamespace(
        netuid=118,
        link=SimpleNamespace(
            link_id=uuid4(), github_login="miner-fixture", github_user_id=100
        ),
    )
    with (
        patch.object(link_github, "ApiClient", return_value=api),
        patch.object(
            link_github,
            "load_miner_session",
            return_value={"token": "fixture-session", "hotkey": KEY.ss58_address},
        ),
        patch.object(
            link_github,
            "load_wallet",
            return_value=(
                SimpleNamespace(hotkey_ss58=KEY.ss58_address),
                LockedWallet(),
            ),
        ),
    ):
        assert link_github.run(args(github_command=command, key_kind="coldkey")) == 1
    output = capsys.readouterr()
    assert "cannot load or unlock the coldkey signing key" in output.err
    assert "keyfile and passphrase" in output.err
    assert "private SDK error" not in output.err
    assert not output.out
    api.confirm_github_link.assert_not_called()
    api.unlink_github.assert_not_called()


@pytest.mark.parametrize("failure", ["connect", "stream_timeout"])
def test_cla_transport_failure_is_handled_before_consent_or_signing(
    failure, tmp_path, capsys
):
    class Timeout(httpx.SyncByteStream):
        def __iter__(self):
            yield b"fixture"
            raise httpx.ReadTimeout("fixture timeout")

    requests = []
    status = ClaStatus(netuid=118, mode="off", current=version())

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/text"):
            if failure == "connect":
                raise httpx.ConnectError("fixture connection failure")
            return httpx.Response(200, stream=Timeout())
        return httpx.Response(200, json=status.model_dump(mode="json"))

    api = cla.ApiClient(base_url="http://test")
    api._client.close()
    api._client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://test"
    )
    handle, live = wallet()
    target = tmp_path / "CLA.md"
    with (
        patch.object(cla, "load_wallet", return_value=(handle, live)),
        patch.object(cla, "ApiClient", return_value=api),
        patch("builtins.input", side_effect=AssertionError("must not ask for consent")),
    ):
        assert cla.run(args(output=target)) == 1
    output = capsys.readouterr()
    assert ("unreachable" if failure == "connect" else "timed out") in output.err
    assert not output.out
    assert not target.exists()
    assert len(requests) == 2 and all(request.method == "GET" for request in requests)
    live.hotkey.sign.assert_not_called()


@pytest.mark.parametrize("command", ["link", "status", "unlink"])
def test_github_unavailable_error_exits_before_signing(command, capsys):
    api = client()
    failure = ApiResponseError("Ask the operator to upgrade Platform.")
    api.get_github_link.side_effect = failure
    api.start_github_link.side_effect = failure
    with (
        patch.object(link_github, "ApiClient", return_value=api),
        patch.object(
            link_github,
            "load_miner_session",
            return_value={"token": "fixture-session", "hotkey": KEY.ss58_address},
        ),
        patch.object(link_github, "load_wallet") as load_wallet,
        patch.object(link_github.webbrowser, "open") as open_browser,
    ):
        assert link_github.run(args(github_command=command)) == 1
    output = capsys.readouterr()
    assert "upgrade Platform" in output.err
    assert not output.out
    load_wallet.assert_not_called()
    open_browser.assert_not_called()


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


@pytest.mark.parametrize("stage", ["status", "start", "poll", "confirm", "wallet"])
def test_github_malformed_responses_and_wallet_errors_exit_cleanly(stage, capsys):
    api = client()
    handle, live = wallet()
    attempt = uuid4()
    api.start_github_link.return_value = GithubLinkStart(
        netuid=118,
        attempt_id=attempt,
        authorize_url="https://dittobench.ai/fixture",
        expires_in=600,
    )
    api.get_github_link_attempt.return_value = GithubLinkAttemptStatus(
        attempt_id=attempt,
        status="accepted",
        github_user_id=100,
        github_login="fixture",
    )
    if stage != "wallet":
        with pytest.raises(ValueError) as malformed:
            GithubLinkAttemptStatus.model_validate({})
        method = {
            "status": "get_github_link",
            "start": "start_github_link",
            "poll": "get_github_link_attempt",
            "confirm": "confirm_github_link",
        }[stage]
        getattr(api, method).side_effect = malformed.value
    with (
        patch.object(link_github, "ApiClient", return_value=api),
        patch.object(
            link_github,
            "load_wallet",
            return_value=(handle, live),
            side_effect=OSError("fixture wallet read failed")
            if stage == "wallet"
            else None,
        ),
        patch.object(
            link_github,
            "load_miner_session",
            return_value={
                "token": "fixture-session",
                "hotkey": KEY.ss58_address,
            },
        ),
    ):
        assert (
            link_github.run(
                args(github_command="status" if stage == "status" else "link")
            )
            == 1
        )
    error = capsys.readouterr().err
    assert (
        "fixture wallet read failed" if stage == "wallet" else "validation errors"
    ) in error


@pytest.mark.parametrize("has_link", [True, False])
@pytest.mark.parametrize("skip_confirmation", [True, False])
def test_json_unlink_signs_current_context_for_a_global_link(
    capsys, monkeypatch, has_link, skip_confirmation
):
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
    monkeypatch.setattr("sys.stdin", io.StringIO("UNLINK\n"))
    with (
        patch.object(link_github, "ApiClient", return_value=api),
        patch.object(link_github, "load_wallet", return_value=(handle, live)),
        patch.object(
            link_github,
            "load_miner_session",
            return_value={"token": "fixture-session", "hotkey": KEY.ss58_address},
        ),
    ):
        assert (
            link_github.run(
                args(
                    github_command="unlink",
                    json=True,
                    yes=skip_confirmation,
                    coldkey_name="fixture" if has_link else None,
                    hotkey_name="fixture" if has_link else None,
                )
            )
            == 0
        )
    output = capsys.readouterr()
    assert json.loads(output.out) == {
        "link_id": str(link_id) if has_link else None,
        "revoked": has_link,
    }
    assert ("Type UNLINK" in output.err) == (has_link and not skip_confirmation)
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


@pytest.mark.parametrize("as_json", [True, False])
@pytest.mark.parametrize("transient", [True, False])
def test_github_prints_verified_account_before_loading_private_wallet(
    capsys, monkeypatch, as_json, transient
):
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
    if transient:
        api.get_github_link_attempt.side_effect = [
            TransientApiError("fixture temporary outage"),
            api.get_github_link_attempt.return_value,
        ]
    api.confirm_github_link.return_value.model_dump.return_value = {
        "github_user_id": 100
    }
    monkeypatch.setattr("sys.stdin", io.StringIO("LINK\n"))
    handle, live = wallet()

    def load(**_kwargs):
        printed = capsys.readouterr()
        assert "miner-fixture" in printed.err and "id 100" in printed.err
        assert "Type LINK" in printed.err
        assert not printed.out
        return handle, live

    with (
        patch.object(
            link_github,
            "load_miner_session",
            return_value={"token": "fixture-session", "hotkey": KEY.ss58_address},
        ),
        patch.object(link_github, "load_wallet", side_effect=load),
        patch.object(link_github, "ApiClient", return_value=api),
        patch.object(link_github.time, "sleep"),
    ):
        assert link_github.run(args(yes=False, json=as_json)) == 0
    if as_json:
        assert json.loads(capsys.readouterr().out) == {"github_user_id": 100}
    proof = api.confirm_github_link.call_args.kwargs["body"]
    assert (
        proof.github_user_id == 100
        and proof.github_login == "miner-fixture"
        and proof.hotkey == KEY.ss58_address
    )
    live.hotkey.sign.assert_called_once()
    assert api.get_github_link_attempt.call_count == (2 if transient else 1)


def test_transient_github_polling_obeys_original_deadline(capsys):
    api = client()
    api.start_github_link.return_value = GithubLinkStart(
        netuid=118,
        attempt_id=uuid4(),
        authorize_url="https://dittobench.ai/fixture",
        expires_in=600,
    )
    api.get_github_link_attempt.side_effect = TransientApiError(
        "fixture temporary outage"
    )
    clock = SimpleNamespace(
        monotonic=MagicMock(side_effect=[0.0, 0.0, 0.5, 1.0]), sleep=MagicMock()
    )
    with (
        patch.object(link_github, "ApiClient", return_value=api),
        patch.object(
            link_github,
            "load_miner_session",
            return_value={"token": "fixture", "hotkey": KEY.ss58_address},
        ),
        patch.object(link_github, "load_wallet") as load_wallet,
        patch.object(link_github, "time", clock),
    ):
        assert link_github.run(args(wait=1)) == 1
    assert "timed out" in capsys.readouterr().err
    api.get_github_link_attempt.assert_called_once()
    clock.sleep.assert_called_once_with(0.5)
    load_wallet.assert_not_called()


def test_interactive_json_cla_sign_keeps_prompt_on_stderr(
    tmp_path, capsys, monkeypatch
):
    api = client()
    handle, live = wallet()
    v = version()
    monkeypatch.setattr("sys.stdin", io.StringIO(f"SIGN CLA 1 {v.sha256}\n"))
    with (
        patch.object(cla, "ApiClient", return_value=api),
        patch.object(cla, "load_wallet", return_value=(handle, live)),
    ):
        assert cla.run(args(output=tmp_path / "CLA.md", json=True)) == 0
    output = capsys.readouterr()
    assert "Type exactly" in output.err
    assert json.loads(output.out)["cla_sha256"] == v.sha256


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
