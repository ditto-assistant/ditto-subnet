from __future__ import annotations

import argparse
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest

from ditto.api_models.miner_screening_retry import (
    MinerScreeningRetryResponse,
    MinerScreeningRetryStatus,
)
from ditto.miner_cli.__main__ import _build_parser as build_parser
from ditto.miner_cli.commands.retry_screening import EXIT_NOT_ELIGIBLE, run
from ditto.miner_cli.errors import (
    AgentNotFoundError,
    ScreeningRetryRejectedError,
)

_MODULE = "ditto.miner_cli.commands.retry_screening"
_SHA = "ab" * 32
_NOTICE = "A retry starts a new full screening. It costs no TAO."


def _args(**overrides: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "agent_id": uuid4(),
        "check": False,
        "yes": False,
        "json": False,
        "network": "local",
        "chain_endpoint": None,
        "verbose": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _status(agent_id: UUID, attempt_id: UUID, **overrides: object):
    base: dict[str, object] = {
        "agent_id": agent_id,
        "agent_status": "screening_failed",
        "artifact_sha256": _SHA,
        "latest_attempt_id": attempt_id,
        "latest_attempt_status": "failed",
        "reason_code": "source-review-model-tool-call-invalid",
        "public_reason": "Screening was interrupted; manual retry required.",
        "eligible": True,
        "blocker": None,
        "already_authorized": False,
        "retries_used": 0,
        "retries_allowed": 1,
        "cost_notice": _NOTICE,
    }
    base.update(overrides)
    return MinerScreeningRetryStatus.model_validate(base)


def _result(agent_id: UUID, attempt_id: UUID, **overrides: object):
    base: dict[str, object] = {
        "override_id": uuid4(),
        "agent_id": agent_id,
        "attempt_id": attempt_id,
        "agent_status": "screening_failed",
        "created_at": datetime(2026, 11, 6, tzinfo=UTC),
        "idempotent": False,
        "authorized_by_operator": False,
        "retries_used": 1,
        "retries_allowed": 1,
    }
    base.update(overrides)
    return MinerScreeningRetryResponse.model_validate(base)


def _client(status, result=None, error=None) -> MagicMock:
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    if isinstance(status, Exception):
        client.get_screening_retry_status.side_effect = status
    else:
        client.get_screening_retry_status.return_value = status
    if error is not None:
        client.retry_screening.side_effect = error
    else:
        client.retry_screening.return_value = result
    return client


def _run(args: argparse.Namespace, client: MagicMock, *, answer: str | None = None):
    patches = [
        patch(
            f"{_MODULE}.load_miner_session",
            return_value={"token": "ditto_ms_abc"},
        ),
        patch(f"{_MODULE}.ApiClient", return_value=client),
    ]
    if answer is not None:
        patches.append(
            patch("ditto.miner_cli.confirm.prompt_input", return_value=answer)
        )
    for item in patches:
        item.start()
    try:
        return run(args)
    finally:
        for item in reversed(patches):
            item.stop()


def test_parser_registers_retry_screening() -> None:
    agent_id = uuid4()
    parsed = build_parser().parse_args(["retry-screening", str(agent_id), "--yes"])
    assert parsed.agent_id == agent_id
    assert parsed.yes is True
    assert parsed.func is run


def test_requires_saved_session(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(f"{_MODULE}.load_miner_session", return_value=None):
        assert run(_args()) == 1
    assert "--scopes read,upload" in capsys.readouterr().err


def test_confirmed_retry_echoes_exact_guards(
    capsys: pytest.CaptureFixture[str],
) -> None:
    agent_id, attempt_id = uuid4(), uuid4()
    client = _client(_status(agent_id, attempt_id), _result(agent_id, attempt_id))

    assert _run(_args(agent_id=agent_id), client, answer="y") == 0

    body = client.retry_screening.call_args.kwargs["body"]
    assert body.expected_attempt_id == attempt_id
    assert body.expected_sha256 == _SHA
    assert body.acknowledge_cost is True
    out = capsys.readouterr().out
    assert _NOTICE in out
    assert "0 of 1" in out
    assert "retry authorized" in out


def test_declined_prompt_sends_nothing() -> None:
    agent_id, attempt_id = uuid4(), uuid4()
    client = _client(_status(agent_id, attempt_id))

    assert _run(_args(agent_id=agent_id), client, answer="n") == 1
    client.retry_screening.assert_not_called()


def test_check_never_writes(capsys: pytest.CaptureFixture[str]) -> None:
    agent_id, attempt_id = uuid4(), uuid4()
    client = _client(_status(agent_id, attempt_id))

    assert _run(_args(agent_id=agent_id, check=True), client) == 0
    client.retry_screening.assert_not_called()
    assert "run without --check" in capsys.readouterr().out


def test_ineligible_explains_blocker_without_writing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    agent_id, attempt_id = uuid4(), uuid4()
    client = _client(
        _status(agent_id, attempt_id, eligible=False, blocker="budget_exhausted")
    )

    assert _run(_args(agent_id=agent_id, yes=True), client) == EXIT_NOT_ELIGIBLE
    client.retry_screening.assert_not_called()
    assert "ask an operator" in capsys.readouterr().err


def test_already_queued_reports_without_prompt_or_write(
    capsys: pytest.CaptureFixture[str],
) -> None:
    agent_id, attempt_id = uuid4(), uuid4()
    client = _client(
        _status(agent_id, attempt_id, eligible=False, already_authorized=True)
    )

    # No prompt answer patched: a prompt here would read real stdin and fail.
    assert _run(_args(agent_id=agent_id), client) == 0
    client.retry_screening.assert_not_called()
    assert "already queued" in capsys.readouterr().out


def test_idempotent_post_names_the_operator_grant(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The status read raced an operator grant: the POST replays it.
    agent_id, attempt_id = uuid4(), uuid4()
    client = _client(
        _status(agent_id, attempt_id),
        _result(agent_id, attempt_id, idempotent=True, authorized_by_operator=True),
    )

    assert _run(_args(agent_id=agent_id, yes=True), client) == 0
    assert "already queued by an operator" in capsys.readouterr().out


def test_missing_upload_scope_names_the_fix(
    capsys: pytest.CaptureFixture[str],
) -> None:
    agent_id, attempt_id = uuid4(), uuid4()
    client = _client(
        _status(agent_id, attempt_id),
        error=ScreeningRetryRejectedError(
            "screening-retry failed: HTTP 403 code=? "
            "miner session is missing the upload scope"
        ),
    )

    assert _run(_args(agent_id=agent_id, yes=True), client) == EXIT_NOT_ELIGIBLE
    assert "ditto login --scopes read,upload" in capsys.readouterr().err


def test_unknown_agent_exits_3() -> None:
    client = _client(AgentNotFoundError("no agent"))
    assert _run(_args(), client) == 3
