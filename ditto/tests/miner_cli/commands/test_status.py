"""Unit tests for :mod:`ditto.miner_cli.commands.status`."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from ditto.api_models.retrieval import AgentResponse, AgentStatusResponse
from ditto.db.models import AgentStatus
from ditto.miner_cli.commands.status import run
from ditto.miner_cli.errors import AgentNotFoundError, HotkeyAgentNotFoundError

HOTKEY = "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"


def make_args(**overrides: object) -> argparse.Namespace:
    base = {
        "agent_id": None,
        "coldkey_name": None,
        "hotkey_name": None,
        "json": False,
        "network": "local",
        "verbose": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _patch_api_client(client_mock: MagicMock) -> MagicMock:
    """Patch ApiClient constructor + return value as a context manager."""
    ctor_mock = MagicMock()
    ctor_mock.return_value.__enter__.return_value = client_mock
    ctor_mock.return_value.__exit__.return_value = False
    return ctor_mock


class TestStatusByAgentId:
    def test_happy_path_prints_text(self, capsys: pytest.CaptureFixture[str]) -> None:
        agent_id = uuid4()
        client = MagicMock()
        client.get_agent_status.return_value = AgentStatusResponse(
            agent_id=agent_id,
            status=AgentStatus.SCREENING,
        )

        with patch(
            "ditto.miner_cli.commands.status.ApiClient", _patch_api_client(client)
        ):
            exit_code = run(make_args(agent_id=agent_id))

        out = capsys.readouterr().out
        assert exit_code == 0
        assert str(agent_id) in out
        assert "screening" in out
        client.get_agent_status.assert_called_once_with(agent_id=agent_id)

    def test_json_flag_emits_parseable_json(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        agent_id = uuid4()
        client = MagicMock()
        client.get_agent_status.return_value = AgentStatusResponse(
            agent_id=agent_id,
            status=AgentStatus.UPLOADED,
        )

        with patch(
            "ditto.miner_cli.commands.status.ApiClient", _patch_api_client(client)
        ):
            run(make_args(agent_id=agent_id, json=True))

        # Output must be valid JSON on a single line.
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload == {"agent_id": str(agent_id), "status": "uploaded"}

    def test_404_returns_exit_code_3(self, capsys: pytest.CaptureFixture[str]) -> None:
        agent_id = uuid4()
        client = MagicMock()
        client.get_agent_status.side_effect = AgentNotFoundError("not found")

        with patch(
            "ditto.miner_cli.commands.status.ApiClient", _patch_api_client(client)
        ):
            exit_code = run(make_args(agent_id=agent_id))

        assert exit_code == 3
        assert capsys.readouterr().err


class TestStatusByHotkey:
    def test_resolves_via_wallet_when_no_agent_id_given(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        client = MagicMock()
        client.get_agent_by_hotkey.return_value = AgentResponse(
            agent_id=uuid4(),
            miner_hotkey=HOTKEY,
            name="alpha",
            status=AgentStatus.UPLOADED,
            sha256="ab" * 32,
            created_at=datetime(2026, 6, 16, 12, 0, tzinfo=UTC),
        )

        fake_handle = MagicMock(hotkey_ss58=HOTKEY)
        with (
            patch(
                "ditto.miner_cli.commands.status.load_wallet",
                return_value=(fake_handle, MagicMock()),
            ),
            patch(
                "ditto.miner_cli.commands.status.ApiClient",
                _patch_api_client(client),
            ),
        ):
            exit_code = run(make_args(coldkey_name="miner", hotkey_name="default"))

        out = capsys.readouterr().out
        assert exit_code == 0
        assert HOTKEY in out
        client.get_agent_by_hotkey.assert_called_once_with(miner_hotkey=HOTKEY)

    def test_missing_hotkey_fields_exits_one_with_help_message(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch(
            "ditto.miner_cli.commands.status.ApiClient",
            _patch_api_client(MagicMock()),
        ):
            exit_code = run(make_args())

        err = capsys.readouterr().err
        assert exit_code == 1
        assert "DITTO_COLDKEY_NAME" in err or "coldkey" in err

    def test_404_returns_exit_code_3(self, capsys: pytest.CaptureFixture[str]) -> None:
        client = MagicMock()
        client.get_agent_by_hotkey.side_effect = HotkeyAgentNotFoundError("none")

        fake_handle = MagicMock(hotkey_ss58=HOTKEY)
        with (
            patch(
                "ditto.miner_cli.commands.status.load_wallet",
                return_value=(fake_handle, MagicMock()),
            ),
            patch(
                "ditto.miner_cli.commands.status.ApiClient",
                _patch_api_client(client),
            ),
        ):
            exit_code = run(make_args(coldkey_name="miner", hotkey_name="default"))

        assert exit_code == 3
        assert capsys.readouterr().err
