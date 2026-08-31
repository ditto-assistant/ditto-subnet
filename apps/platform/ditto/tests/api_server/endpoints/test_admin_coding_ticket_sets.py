"""Endpoint coverage for default-off k=3 shadow coding ticket issuance."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_server.dependencies import get_chain_client, get_session
from ditto.api_server.endpoints import admin_coding_ticket_sets as endpoint_module
from ditto.db.models import CodingShadowTicket
from ditto.db.queries.coding_ticket_sets import (
    CodingTicketSetResult,
    CodingTicketSetUnavailableError,
    coding_shadow_ticket_id,
)

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_RUN_ROW_ID = UUID("8ae8ed37-48e4-48fb-9693-717f4098d1ae")
_TICKET_SET_ID = UUID("2e4ff1a8-2ded-47a0-9359-e3534e53e7cb")
_VALIDATORS = tuple("5" + character * 47 for character in "ABC")
_ISSUED_AT = datetime(2026, 8, 31, 10, tzinfo=UTC)


def _payload(*, confirmation: str | None = None) -> dict[str, object]:
    expected = (
        "ISSUE SHADOW CODING TICKET SET "
        f"{_RUN_ROW_ID} {_TICKET_SET_ID} {','.join(_VALIDATORS)}"
    )
    return {
        "run_row_id": str(_RUN_ROW_ID),
        "ticket_set_id": str(_TICKET_SET_ID),
        "validator_hotkeys": list(_VALIDATORS),
        "confirmation": expected if confirmation is None else confirmation,
    }


def _install(
    app: FastAPI,
    maker: async_sessionmaker[AsyncSession],
    *,
    enabled: bool,
) -> object:
    chain = object()
    app.state.config = replace(
        app.state.config,
        admin_api_token=_ADMIN_TOKEN,
        coding_shadow_ticket_set_enabled=enabled,
        coding_shadow_ticket_lease_seconds=900,
    )

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    async def _chain() -> object:
        return chain

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_chain_client] = _chain
    return chain


def _tickets() -> tuple[CodingShadowTicket, CodingShadowTicket, CodingShadowTicket]:
    deadline = _ISSUED_AT + timedelta(minutes=15)
    values = tuple(
        CodingShadowTicket(
            ticket_id=coding_shadow_ticket_id(
                ticket_set_id=_TICKET_SET_ID,
                run_row_id=_RUN_ROW_ID,
                validator_hotkey=hotkey,
            ),
            run_row_id=_RUN_ROW_ID,
            task_count=1,
            validator_hotkey=hotkey,
            certification_row_id=UUID(int=index),
            issued_at=_ISSUED_AT,
            deadline=deadline,
        )
        for index, hotkey in enumerate(_VALIDATORS, start=1)
    )
    return values[0], values[1], values[2]


async def test_ticket_set_is_admin_only_and_default_off(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    _install(app, session_maker, enabled=False)
    issue = AsyncMock()
    monkeypatch.setattr(endpoint_module, "issue_coding_shadow_ticket_set", issue)

    missing_auth = await client.post(
        "/api/v1/admin/coding-shadow/ticket-sets",
        json=_payload(),
    )
    assert missing_auth.status_code == 401

    disabled = await client.post(
        "/api/v1/admin/coding-shadow/ticket-sets",
        headers={"Authorization": f"Bearer {_ADMIN_TOKEN}"},
        json=_payload(),
    )
    assert disabled.status_code == 503
    issue.assert_not_awaited()


async def test_ticket_set_requires_canonical_validators_and_confirmation(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    _install(app, session_maker, enabled=True)
    issue = AsyncMock()
    monkeypatch.setattr(endpoint_module, "issue_coding_shadow_ticket_set", issue)
    headers = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}

    invalid_confirmation = await client.post(
        "/api/v1/admin/coding-shadow/ticket-sets",
        headers=headers,
        json=_payload(confirmation="ISSUE SHADOW CODING TICKET SET"),
    )
    assert invalid_confirmation.status_code == 422

    unsorted = _payload()
    unsorted["validator_hotkeys"] = list(reversed(_VALIDATORS))
    invalid_validators = await client.post(
        "/api/v1/admin/coding-shadow/ticket-sets",
        headers=headers,
        json=unsorted,
    )
    assert invalid_validators.status_code == 422
    issue.assert_not_awaited()


async def test_ticket_set_delegates_exact_k3_without_running_work(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    chain = _install(app, session_maker, enabled=True)
    tickets = _tickets()
    issue = AsyncMock(
        return_value=CodingTicketSetResult(tickets=tickets, idempotent=False)
    )
    monkeypatch.setattr(endpoint_module, "issue_coding_shadow_ticket_set", issue)

    response = await client.post(
        "/api/v1/admin/coding-shadow/ticket-sets",
        headers={"Authorization": f"Bearer {_ADMIN_TOKEN}"},
        json=_payload(),
    )

    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert body["run_row_id"] == str(_RUN_ROW_ID)
    assert body["ticket_set_id"] == str(_TICKET_SET_ID)
    assert [item["validator_hotkey"] for item in body["tickets"]] == list(_VALIDATORS)
    assert body["idempotent"] is False
    assert body["weight_eligible"] is False
    issue.assert_awaited_once()
    assert issue.await_args is not None
    kwargs = issue.await_args.kwargs
    assert kwargs["permit_source"] is chain
    assert kwargs["netuid"] == 118
    assert kwargs["run_row_id"] == _RUN_ROW_ID
    assert kwargs["ticket_set_id"] == _TICKET_SET_ID
    assert kwargs["validator_hotkeys"] == _VALIDATORS
    assert kwargs["policy"].lease_seconds == 900


async def test_ticket_set_maps_permit_unavailability_to_service_unavailable(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    _install(app, session_maker, enabled=True)
    monkeypatch.setattr(
        endpoint_module,
        "issue_coding_shadow_ticket_set",
        AsyncMock(side_effect=CodingTicketSetUnavailableError("permits unavailable")),
    )

    response = await client.post(
        "/api/v1/admin/coding-shadow/ticket-sets",
        headers={"Authorization": f"Bearer {_ADMIN_TOKEN}"},
        json=_payload(),
    )

    assert response.status_code == 503
