from __future__ import annotations

import argparse
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch
from uuid import uuid4

from ditto.api_models.miner_ditto_link import (
    MinerDittoLinkAttemptResponse,
    MinerDittoLinkResponse,
    MinerDittoLinkStartResponse,
    MinerDittoLinkView,
)
from ditto.miner_cli.commands.link_ditto import run
from ditto.miner_cli.errors import LoginRequiredError

HOTKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
SESSION = {"token": "ditto_ms_abc", "hotkey": HOTKEY, "scopes": ["read", "profile"]}


def _args(**overrides: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "link_command": "link",
        "no_browser": True,
        "wait_seconds": 30,
        "json": False,
        "yes": False,
        "network": "local",
        "chain_endpoint": None,
        "verbose": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _view() -> MinerDittoLinkView:
    now = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
    return MinerDittoLinkView(
        miner_hotkey=HOTKEY,
        ditto_user_id="ditto-user-1",
        ditto_email="miner@example.com",
        linked_via="cli",
        created_at=now,
        updated_at=now,
    )


def _authenticated(attempt_id, *, who: str = "miner@example.com"):
    return MinerDittoLinkAttemptResponse(
        attempt_id=attempt_id,
        status="authenticated",
        ditto_user_id="ditto-user-1",
        ditto_email=who,
        miner_hotkey=HOTKEY,
    )


def _client(attempt_id, *, statuses):
    client = MagicMock()
    client.__enter__.return_value = client
    client.start_ditto_link.return_value = MinerDittoLinkStartResponse(
        attempt_id=attempt_id,
        authorize_url="https://api.heyditto.ai/authorize?client_id=dittobench&state=s",
        expires_in=600,
    )
    client.get_ditto_link_attempt.side_effect = statuses
    client.confirm_ditto_link.return_value = MinerDittoLinkAttemptResponse(
        attempt_id=attempt_id, status="linked", link=_view()
    )
    return client


def test_link_requires_a_saved_session(capsys) -> None:
    with patch(
        "ditto.miner_cli.commands.link_ditto.load_miner_session", return_value=None
    ):
        assert run(_args()) == 1
    assert "not signed in" in capsys.readouterr().err


def test_link_prints_the_consent_url_then_confirms_the_pairing(capsys) -> None:
    attempt_id = uuid4()
    client = _client(
        attempt_id,
        statuses=[
            MinerDittoLinkAttemptResponse(attempt_id=attempt_id, status="pending"),
            _authenticated(attempt_id),
        ],
    )
    with (
        patch(
            "ditto.miner_cli.commands.link_ditto.load_miner_session",
            return_value=SESSION,
        ),
        patch("ditto.miner_cli.commands.link_ditto.ApiClient", return_value=client),
        patch("ditto.miner_cli.commands.link_ditto.time.sleep"),
        patch("ditto.miner_cli.commands.link_ditto.webbrowser.open") as opened,
        patch("builtins.input", return_value="y"),
        patch("sys.stdin.isatty", return_value=True),
    ):
        assert run(_args()) == 0
    out = capsys.readouterr().out
    assert "https://api.heyditto.ai/authorize?client_id=dittobench" in out
    assert "Ditto signed in as miner@example.com" in out
    assert "linked to Ditto account miner@example.com" in out
    opened.assert_not_called()
    # The CLI only ever sends the saved session token; it never names a user,
    # and the link is written only after the confirmation.
    kwargs = client.start_ditto_link.call_args.kwargs
    assert kwargs["token"] == "ditto_ms_abc"
    assert kwargs["body"].client == "cli"
    client.confirm_ditto_link.assert_called_once_with(
        token="ditto_ms_abc", attempt_id=attempt_id
    )


def test_link_refuses_to_pair_without_confirmation(capsys) -> None:
    attempt_id = uuid4()
    client = _client(
        attempt_id,
        statuses=[_authenticated(attempt_id, who="someone-else@example.com")],
    )
    with (
        patch(
            "ditto.miner_cli.commands.link_ditto.load_miner_session",
            return_value=SESSION,
        ),
        patch("ditto.miner_cli.commands.link_ditto.ApiClient", return_value=client),
        patch("builtins.input", return_value="n"),
        patch("sys.stdin.isatty", return_value=True),
    ):
        assert run(_args()) == 1
    client.confirm_ditto_link.assert_not_called()
    assert "not linked" in capsys.readouterr().err

    # A non-interactive terminal without --yes never links either.
    client = _client(attempt_id, statuses=[_authenticated(attempt_id)])
    with (
        patch(
            "ditto.miner_cli.commands.link_ditto.load_miner_session",
            return_value=SESSION,
        ),
        patch("ditto.miner_cli.commands.link_ditto.ApiClient", return_value=client),
        patch("sys.stdin.isatty", return_value=False),
    ):
        assert run(_args()) == 1
    client.confirm_ditto_link.assert_not_called()

    # --yes confirms without a prompt.
    client = _client(attempt_id, statuses=[_authenticated(attempt_id)])
    with (
        patch(
            "ditto.miner_cli.commands.link_ditto.load_miner_session",
            return_value=SESSION,
        ),
        patch("ditto.miner_cli.commands.link_ditto.ApiClient", return_value=client),
        patch("sys.stdin.isatty", return_value=False),
    ):
        assert run(_args(yes=True)) == 0
    client.confirm_ditto_link.assert_called_once()


def test_link_reports_a_declined_consent(capsys) -> None:
    attempt_id = uuid4()
    client = _client(
        attempt_id,
        statuses=[
            MinerDittoLinkAttemptResponse(
                attempt_id=attempt_id, status="failed", error="user declined"
            )
        ],
    )
    with (
        patch(
            "ditto.miner_cli.commands.link_ditto.load_miner_session",
            return_value=SESSION,
        ),
        patch("ditto.miner_cli.commands.link_ditto.ApiClient", return_value=client),
    ):
        assert run(_args()) == 1
    assert "link failed: user declined" in capsys.readouterr().err


def test_status_and_unlink(capsys) -> None:
    client = MagicMock()
    client.__enter__.return_value = client
    client.get_ditto_link.return_value = MinerDittoLinkResponse(
        enabled=True, link=_view()
    )
    with (
        patch(
            "ditto.miner_cli.commands.link_ditto.load_miner_session",
            return_value=SESSION,
        ),
        patch("ditto.miner_cli.commands.link_ditto.ApiClient", return_value=client),
    ):
        assert run(_args(link_command="status", json=True)) == 0
        assert run(_args(link_command="unlink")) == 0
    out = capsys.readouterr().out
    assert '"ditto_user_id": "ditto-user-1"' in out
    assert "unlinked the Ditto account" in out
    client.delete_ditto_link.assert_called_once_with(token="ditto_ms_abc")


def test_expired_session_asks_for_login_again(capsys) -> None:
    client = MagicMock()
    client.__enter__.return_value = client
    client.get_ditto_link.side_effect = LoginRequiredError(
        "miner session is invalid or expired"
    )
    with (
        patch(
            "ditto.miner_cli.commands.link_ditto.load_miner_session",
            return_value=SESSION,
        ),
        patch("ditto.miner_cli.commands.link_ditto.ApiClient", return_value=client),
    ):
        assert run(_args(link_command="status")) == 1
    assert "run `ditto login` again" in capsys.readouterr().err
