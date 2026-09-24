"""Synthetic settled-broker tests; no protected cases or provider calls."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from ditto_screener.v13_private_adapter import PrivateExecutionUnavailable
from ditto_screener.v13_private_runtime import (
    ConversationPrivateCaseSessionFactory,
    _ConversationPrivateCaseSession,
)


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


def test_private_aggregate_budget_is_partitioned_across_all_case_sides() -> None:
    factory = ConversationPrivateCaseSessionFactory(
        config=None,  # type: ignore[arg-type] - no runtime is started
        provider_key="synthetic",
        resolver=None,
        max_cost_microusd=20_000_000,
    )
    factory.configure_budget(60)
    assert factory._max_cases == 240
    assert factory._case_budget_microusd is not None
    assert factory._case_budget_microusd * factory._max_cases <= 20_000_000
    with pytest.raises(PrivateExecutionUnavailable):
        factory.configure_budget(60)


@pytest.mark.asyncio
async def test_private_budget_refuses_extra_case_before_image_lookup() -> None:
    factory = ConversationPrivateCaseSessionFactory(
        config=None,  # type: ignore[arg-type] - no runtime is started
        provider_key="synthetic",
        resolver=object(),  # type: ignore[arg-type] - must not be called
        max_cost_microusd=20_000_000,
    )
    factory.configure_budget(60)
    factory._cases_started = factory._max_cases
    with pytest.raises(PrivateExecutionUnavailable):
        await factory.open_case(
            role="target",
            agent_id=uuid4(),
            attempt_id=uuid4(),
            artifact_sha256="a" * 64,
            image_sha256="b" * 64,
            session_id=str(uuid4()),
            case_id=str(uuid4()),
        )
