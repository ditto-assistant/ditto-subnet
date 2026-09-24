"""Synthetic settled-broker tests; no protected cases or provider calls."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from ditto_screener.v13_private_runtime import _ConversationPrivateCaseSession


class FakeRuntime:
    def __init__(self, ledger: dict[str, object]) -> None:
        self.ledger = ledger
        self.stops = 0

    async def stop_private(self) -> dict[str, object]:
        self.stops += 1
        return self.ledger


def _ledger(**changes: object) -> dict[str, object]:
    return {
        "settled": True,
        "requests": 1,
        "chat_dispatches": 1,
        "successful_chat_responses": 1,
        "unmetered": False,
        "failed": False,
        "failure": None,
        **changes,
    }


def _session(runtime: FakeRuntime) -> _ConversationPrivateCaseSession:
    return _ConversationPrivateCaseSession(
        runtime,  # type: ignore[arg-type] - synthetic runtime boundary
        agent_id=uuid4(),
        attempt_id=uuid4(),
        artifact_sha256="a" * 64,
        image_sha256="b" * 64,
        session_id=str(uuid4()),
        case_id=str(uuid4()),
    )


@pytest.mark.asyncio
async def test_private_session_accepts_only_drained_attributed_provider_work() -> None:
    runtime = FakeRuntime(_ledger())
    session = _session(runtime)
    observed = await session.settle()
    assert observed.status == "attributed"
    assert observed.chat_dispatches == observed.successful_responses == 1
    assert observed.attributed_responses == 1
    await session.close()
    assert runtime.stops == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change,expected",
    [
        ({"chat_dispatches": 0, "successful_chat_responses": 0}, "zero_call"),
        ({"successful_chat_responses": 0}, "no_successful_response"),
        ({"failed": True}, "unattributed"),
        ({"unmetered": True}, "unattributed"),
    ],
)
async def test_private_session_does_not_promote_ambiguous_broker_work(
    change: dict[str, object], expected: str
) -> None:
    session = _session(FakeRuntime(_ledger(**change)))
    observed = await session.settle()
    assert observed.status == expected
    assert observed.attributed_responses == 0


@pytest.mark.asyncio
async def test_private_session_requires_terminal_drain() -> None:
    session = _session(FakeRuntime(_ledger(settled=False)))
    with pytest.raises(ValidationError):
        await session.settle()
