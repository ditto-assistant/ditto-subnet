"""One Ditto OAuth connection, existing miner proof semantics."""

import json
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest

from ditto.api_models.miner_ditto_link import MinerDittoLinkAttemptResponse
from ditto.miner_cli.__main__ import _build_parser
from ditto.miner_cli.commands.link_ditto import run
from ditto.miner_cli.errors import LoginRequiredError, TransientApiError
from ditto.tests.miner_cli.commands.test_link_ditto import (
    HOTKEY,
    SESSION,
    _args,
    _authenticated,
    _client,
    _view,
)


@pytest.mark.parametrize("existing", ["matching", "other-hotkey", "none"])
def test_connect_ditto_retains_proofs_and_only_signs_when_needed(existing):
    attempt_id = uuid4()
    client = _client(attempt_id, statuses=[_authenticated(attempt_id)])
    client.confirm_ditto_link.return_value = MinerDittoLinkAttemptResponse(
        attempt_id=attempt_id,
        status="linked",
        link=_view(),
        github_proof_attempt_id=attempt_id,
    )
    identity = SimpleNamespace(
        github_user_id=123, github_login="miner", hotkey=HOTKEY, status="accepted"
    )
    client.get_github_link_attempt.return_value = identity
    client.get_github_link.return_value.link = (
        identity if existing == "matching" else None
    )
    client.get_github_account_link.return_value.link = (
        SimpleNamespace(hotkey="other") if existing == "other-hotkey" else None
    )
    with (
        patch(
            "ditto.miner_cli.commands.link_ditto.load_miner_session",
            return_value=SESSION,
        ),
        patch("ditto.miner_cli.commands.link_ditto.ApiClient", return_value=client),
        patch("ditto.miner_cli.commands.link_github.run", return_value=0) as sign,
    ):
        assert run(_args(yes=True, expect_github_id=123)) == 0
    if existing == "none":
        sign.assert_called_once()
        assert sign.call_args.args[0].ditto_attempt == attempt_id
        assert sign.call_args.args[0].expect_github_id == 123
    else:
        sign.assert_not_called()
    client.start_github_link.assert_not_called()


@pytest.mark.parametrize("failure", ["none", "transient", "json", "schema", "login"])
def test_json_link_has_one_result_when_optional_proof_runs_or_is_unavailable(
    failure, capsys
):
    attempt_id = uuid4()
    client = _client(attempt_id, statuses=[_authenticated(attempt_id)])
    client.confirm_ditto_link.return_value = MinerDittoLinkAttemptResponse(
        attempt_id=attempt_id,
        status="linked",
        link=_view(),
        github_proof_attempt_id=attempt_id,
    )
    client.get_github_link_attempt.return_value = SimpleNamespace(
        github_user_id=123, github_login="miner", status="accepted"
    )
    if failure != "none":
        client.get_github_link_attempt.side_effect = {
            "transient": TransientApiError("try again"),
            "json": json.JSONDecodeError("truncated response", "{", 1),
            "schema": ValueError("invalid GitHub proof fields"),
            "login": LoginRequiredError("Miner session expired; log in again"),
        }[failure]
    client.get_github_link.return_value.link = None
    client.get_github_account_link.return_value.link = None

    def finish(args):
        assert args.json is False
        print("GitHub proof linked")
        return 0

    with (
        patch(
            "ditto.miner_cli.commands.link_ditto.load_miner_session",
            return_value=SESSION,
        ),
        patch("ditto.miner_cli.commands.link_ditto.ApiClient", return_value=client),
        patch("ditto.miner_cli.commands.link_github.run", side_effect=finish) as sign,
    ):
        assert run(_args(json=True, yes=True, expect_github_id=123)) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["ditto_user_id"] == "ditto-user-1"
    if failure != "none":
        sign.assert_not_called()
        assert "GitHub miner proof is incomplete" in output.err
        assert "GitHub miner proof could not be completed:" in output.err
        if failure == "login":
            assert "Miner session expired; log in again" in output.err
            assert "Run `ditto login` again" in output.err
    else:
        sign.assert_called_once()
        assert "GitHub proof linked" in output.err


@pytest.mark.parametrize("status", ["failed", "expired", "accepted"])
@pytest.mark.parametrize("cli_default_network", [False, True])
def test_ditto_success_survives_an_incomplete_or_racing_proof(
    status, cli_default_network, capsys, monkeypatch
):
    monkeypatch.delenv("DITTO_NETWORK", raising=False)
    args = (
        _build_parser().parse_args(
            ["link-ditto", "--no-browser", "--yes", "--expect-github-id", "123"]
        )
        if cli_default_network
        else _args(yes=True, expect_github_id=123)
    )
    assert args.network == ("finney" if cli_default_network else "local")
    attempt_id = uuid4()
    client = _client(attempt_id, statuses=[_authenticated(attempt_id)])
    client.confirm_ditto_link.return_value = MinerDittoLinkAttemptResponse(
        attempt_id=attempt_id,
        status="linked",
        link=_view(),
        github_proof_attempt_id=attempt_id,
    )
    client.get_github_link_attempt.return_value = SimpleNamespace(
        github_user_id=123, github_login="miner", status=status
    )
    client.get_github_link.return_value.link = None
    client.get_github_account_link.return_value.link = None
    with (
        patch(
            "ditto.miner_cli.commands.link_ditto.load_miner_session",
            return_value=SESSION,
        ),
        patch("ditto.miner_cli.commands.link_ditto.ApiClient", return_value=client),
        patch("ditto.miner_cli.commands.link_github.run", return_value=1) as sign,
    ):
        assert run(args) == 0
    if status == "accepted":
        sign.assert_called_once()
        assert sign.call_args.args[0].expect_github_id == 123
    else:
        sign.assert_not_called()
    err = capsys.readouterr().err
    assert "Your Ditto account is linked" in err
    assert "GitHub miner proof is incomplete" in err
    assert f"ditto --network {args.network} link-ditto" in err
    assert "--expect-github-id 123" in err
    assert "--yes" not in err
    client.start_github_link.assert_not_called()
