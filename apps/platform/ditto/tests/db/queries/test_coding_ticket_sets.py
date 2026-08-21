"""Tests for atomic k=3 shadow coding ticket sets."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from ditto.db.models import CodingShadowRun, CodingShadowTicket
from ditto.db.queries import coding_ticket_sets
from ditto.db.queries.coding_evaluations import (
    CodingShadowConflictError,
    CodingShadowNotQualifiedError,
)
from ditto.db.queries.coding_ticket_sets import (
    CodingTicketSetPolicy,
    CodingTicketSetUnavailableError,
    coding_shadow_ticket_id,
    issue_coding_shadow_ticket_set,
)

_NOW = datetime(2026, 8, 21, 12, tzinfo=UTC)
_VALIDATORS = tuple("5" + character * 47 for character in "ABC")


class _Nested(AbstractAsyncContextManager[None]):
    def __init__(self) -> None:
        self.entered = False
        self.error_type: type[BaseException] | None = None

    async def __aenter__(self) -> None:
        self.entered = True

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        del exc_value, traceback
        self.error_type = exc_type


class _Session:
    def __init__(self, *, run: object | None, existing: Sequence[object] | None = None):
        self.run = run
        self.existing = list(existing or ())
        self.nested = _Nested()
        self.scalar_calls = 0

    async def get(self, model, identity, **kwargs):
        del identity, kwargs
        return self.run if model is CodingShadowRun else None

    async def scalars(self, statement):
        del statement
        return self.existing

    async def scalar(self, statement):
        del statement
        self.scalar_calls += 1
        return _NOW

    def begin_nested(self) -> _Nested:
        return self.nested


class _PermitSource:
    def __init__(self, validators: tuple[str, ...], *, fail: bool = False) -> None:
        self.validators = validators
        self.fail = fail

    async def get_recent_neurons(self, netuid: int):
        assert netuid == 118
        if self.fail:
            raise TimeoutError("chain unavailable")
        return [
            SimpleNamespace(hotkey=hotkey, validator_permit=True)
            for hotkey in self.validators
        ]


def _ticket(
    *, ticket_id: UUID, run_row_id: UUID, validator_hotkey: str
) -> CodingShadowTicket:
    return CodingShadowTicket(
        ticket_id=ticket_id,
        run_row_id=run_row_id,
        task_count=1,
        validator_hotkey=validator_hotkey,
        certification_row_id=uuid4(),
        issued_at=_NOW,
        deadline=_NOW.replace(hour=13),
    )


def test_ticket_set_policy_is_fixed_to_k3_and_bounded() -> None:
    assert CodingTicketSetPolicy().validator_count == 3
    with pytest.raises(ValueError, match="exactly three"):
        CodingTicketSetPolicy(validator_count=2)
    with pytest.raises(ValueError, match="between 1m and 2h"):
        CodingTicketSetPolicy(lease_seconds=30)


@pytest.mark.parametrize(
    "validators,match",
    [
        (_VALIDATORS[:2], "exactly three"),
        ((_VALIDATORS[0], _VALIDATORS[0], _VALIDATORS[2]), "unique and sorted"),
        (tuple(reversed(_VALIDATORS)), "unique and sorted"),
    ],
)
async def test_ticket_set_rejects_noncanonical_validator_sets(
    validators: tuple[str, ...], match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        await issue_coding_shadow_ticket_set(
            _Session(run=object()),  # type: ignore[arg-type]
            permit_source=_PermitSource(_VALIDATORS),
            netuid=118,
            run_row_id=uuid4(),
            ticket_set_id=uuid4(),
            validator_hotkeys=validators,
        )


async def test_ticket_set_requires_current_chain_permits() -> None:
    with pytest.raises(CodingShadowNotQualifiedError, match="unpermitted"):
        await issue_coding_shadow_ticket_set(
            _Session(run=object()),  # type: ignore[arg-type]
            permit_source=_PermitSource(_VALIDATORS[:2]),
            netuid=118,
            run_row_id=uuid4(),
            ticket_set_id=uuid4(),
            validator_hotkeys=_VALIDATORS,
        )
    with pytest.raises(CodingTicketSetUnavailableError, match="unavailable"):
        await issue_coding_shadow_ticket_set(
            _Session(run=object()),  # type: ignore[arg-type]
            permit_source=_PermitSource(_VALIDATORS, fail=True),
            netuid=118,
            run_row_id=uuid4(),
            ticket_set_id=uuid4(),
            validator_hotkeys=_VALIDATORS,
        )


async def test_ticket_set_issues_deterministic_atomic_k3(monkeypatch) -> None:
    run_row_id = uuid4()
    ticket_set_id = uuid4()
    session = _Session(run=object())
    calls: list[UUID] = []

    async def issue(_session, **kwargs):
        calls.append(kwargs["ticket_id"])
        return SimpleNamespace(
            row=_ticket(
                ticket_id=kwargs["ticket_id"],
                run_row_id=run_row_id,
                validator_hotkey=kwargs["validator_hotkey"],
            ),
            idempotent=False,
        )

    monkeypatch.setattr(coding_ticket_sets, "issue_coding_shadow_ticket", issue)
    result = await issue_coding_shadow_ticket_set(
        session,  # type: ignore[arg-type]
        permit_source=_PermitSource(_VALIDATORS),
        netuid=118,
        run_row_id=run_row_id,
        ticket_set_id=ticket_set_id,
        validator_hotkeys=_VALIDATORS,
    )

    assert calls == [
        coding_shadow_ticket_id(
            ticket_set_id=ticket_set_id,
            run_row_id=run_row_id,
            validator_hotkey=hotkey,
        )
        for hotkey in _VALIDATORS
    ]
    assert len(result.tickets) == 3
    assert result.idempotent is False
    assert result.weight_eligible is False
    assert session.nested.entered
    assert session.nested.error_type is None
    assert session.scalar_calls == 1


async def test_ticket_set_rejects_partial_or_competing_existing_set() -> None:
    run_row_id = uuid4()
    existing = [
        _ticket(ticket_id=uuid4(), run_row_id=run_row_id, validator_hotkey="5A" * 24)
    ]
    with pytest.raises(CodingShadowConflictError, match="partial or different"):
        await issue_coding_shadow_ticket_set(
            _Session(run=object(), existing=existing),  # type: ignore[arg-type]
            permit_source=_PermitSource(_VALIDATORS),
            netuid=118,
            run_row_id=run_row_id,
            ticket_set_id=uuid4(),
            validator_hotkeys=_VALIDATORS,
        )


async def test_ticket_set_exact_replay_is_idempotent(monkeypatch) -> None:
    run_row_id = uuid4()
    ticket_set_id = uuid4()
    existing = [
        _ticket(
            ticket_id=coding_shadow_ticket_id(
                ticket_set_id=ticket_set_id,
                run_row_id=run_row_id,
                validator_hotkey=hotkey,
            ),
            run_row_id=run_row_id,
            validator_hotkey=hotkey,
        )
        for hotkey in _VALIDATORS
    ]
    issue = AsyncMock(
        side_effect=[
            SimpleNamespace(row=ticket, idempotent=True) for ticket in existing
        ]
    )
    monkeypatch.setattr(coding_ticket_sets, "issue_coding_shadow_ticket", issue)

    session = _Session(run=object(), existing=existing)
    result = await issue_coding_shadow_ticket_set(
        session,  # type: ignore[arg-type]
        permit_source=_PermitSource(_VALIDATORS, fail=True),
        netuid=118,
        run_row_id=run_row_id,
        ticket_set_id=ticket_set_id,
        validator_hotkeys=_VALIDATORS,
    )

    assert result.idempotent is True
    assert tuple(result.tickets) == tuple(existing)
    assert session.scalar_calls == 0


async def test_ticket_set_failure_exits_nested_transaction_with_error(
    monkeypatch,
) -> None:
    session = _Session(run=object())
    first = _ticket(
        ticket_id=uuid4(),
        run_row_id=uuid4(),
        validator_hotkey=_VALIDATORS[0],
    )
    issue = AsyncMock(
        side_effect=[
            SimpleNamespace(row=first, idempotent=False),
            RuntimeError("second ticket failed"),
        ]
    )
    monkeypatch.setattr(coding_ticket_sets, "issue_coding_shadow_ticket", issue)

    with pytest.raises(RuntimeError, match="second ticket failed"):
        await issue_coding_shadow_ticket_set(
            session,  # type: ignore[arg-type]
            permit_source=_PermitSource(_VALIDATORS),
            netuid=118,
            run_row_id=uuid4(),
            ticket_set_id=uuid4(),
            validator_hotkeys=_VALIDATORS,
        )
    assert session.nested.error_type is RuntimeError
