from __future__ import annotations

import argparse
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch
from uuid import uuid4

from ditto.api_models.miner_logs import MinerHarnessLogAttempt, MinerHarnessLogsResponse
from ditto.miner_cli.commands.logs import run, sanitize_harness_output
from ditto.miner_cli.errors import AgentNotFoundError, LoginRequiredError

HOTKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"


def _args(**overrides: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "agent_id": uuid4(),
        "json": False,
        "network": "local",
        "chain_endpoint": None,
        "verbose": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_sanitize_strips_ansi_and_escapes_control_bytes() -> None:
    raw = "ok\x1b[31mred\x1b[0m\x07bell"
    assert sanitize_harness_output(raw) == "okred\\x07bell"
    assert "\x1b" not in sanitize_harness_output(raw)
    assert "\x07" not in sanitize_harness_output(raw)


def test_logs_requires_a_saved_session(capsys) -> None:
    with patch("ditto.miner_cli.commands.logs.load_miner_session", return_value=None):
        assert run(_args()) == 1
    assert "not signed in" in capsys.readouterr().err


def test_logs_prints_stale_attempt_without_negative_runtime(capsys) -> None:
    agent_id = uuid4()
    issued = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
    failed = datetime(2026, 8, 18, 11, 0, tzinfo=UTC)
    response = MinerHarnessLogsResponse(
        agent_id=agent_id,
        miner_hotkey=HOTKEY,
        agent_status="evaluating",
        attempts=[
            MinerHarnessLogAttempt(
                validator_hotkey=HOTKEY,
                bench_version=12,
                status="issued",
                attempt_count=2,
                issued_at=issued,
                deadline=issued,
                failed_at=failed,
                failure_reason="scoring_error",
                failure_detail=None,
                container_log_tail="\x1b[31mpanic\x1b[0m",
                log_tail_attempt=1,
                stale=True,
            )
        ],
    )
    client = MagicMock()
    client.get_harness_logs.return_value = response
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    with (
        patch(
            "ditto.miner_cli.commands.logs.load_miner_session",
            return_value={"token": "ditto_ms_abc", "hotkey": HOTKEY},
        ),
        patch("ditto.miner_cli.commands.logs.ApiClient", return_value=client),
    ):
        assert run(_args(agent_id=agent_id)) == 0
    out = capsys.readouterr().out
    assert "stale" in out
    assert "attempt 2" in out
    assert "s in)" not in out
    assert "panic" in out
    assert "\x1b" not in out


def test_logs_maps_expired_session(capsys) -> None:
    client = MagicMock()
    client.get_harness_logs.side_effect = LoginRequiredError("expired")
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    with (
        patch(
            "ditto.miner_cli.commands.logs.load_miner_session",
            return_value={"token": "ditto_ms_abc", "hotkey": HOTKEY},
        ),
        patch("ditto.miner_cli.commands.logs.ApiClient", return_value=client),
    ):
        assert run(_args()) == 1
    assert "session expired" in capsys.readouterr().err


def test_logs_maps_unknown_agent(capsys) -> None:
    client = MagicMock()
    client.get_harness_logs.side_effect = AgentNotFoundError("missing")
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    with (
        patch(
            "ditto.miner_cli.commands.logs.load_miner_session",
            return_value={"token": "ditto_ms_abc", "hotkey": HOTKEY},
        ),
        patch("ditto.miner_cli.commands.logs.ApiClient", return_value=client),
    ):
        assert run(_args()) == 3
    assert "not found" in capsys.readouterr().err
