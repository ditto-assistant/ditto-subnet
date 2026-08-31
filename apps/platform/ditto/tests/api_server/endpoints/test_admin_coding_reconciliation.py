"""Endpoint coverage for the default-off shadow coding reconciliation caller."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_server.dependencies import get_chain_client, get_session
from ditto.api_server.endpoints import (
    admin_coding_reconciliation as endpoint_module,
)
from ditto.db.queries.coding_reconciliation import (
    CodingReconciliationResult,
    CodingReconciliationState,
)

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_AGENT_ID = UUID("8ae8ed37-48e4-48fb-9693-717f4098d1ae")
_BENCH_VERSION = 12
_CORPUS_RELEASE_ID = "private-coding-corpus-v1"
_CODING_RUN_ID = "shadow-reconciliation-001"


def _payload(*, confirmation: str | None = None) -> dict[str, object]:
    expected = (
        "RECONCILE SHADOW CODING "
        f"{_AGENT_ID} {_BENCH_VERSION} "
        f"{_CORPUS_RELEASE_ID} {_CODING_RUN_ID}"
    )
    return {
        "agent_id": str(_AGENT_ID),
        "bench_version": _BENCH_VERSION,
        "corpus_release_id": _CORPUS_RELEASE_ID,
        "coding_run_id": _CODING_RUN_ID,
        "confirmation": expected if confirmation is None else confirmation,
    }


def _install(
    app: FastAPI,
    maker: async_sessionmaker[AsyncSession],
    *,
    enabled: bool,
    catalog_source: object | None,
) -> object:
    chain = object()
    app.state.config = replace(
        app.state.config,
        admin_api_token=_ADMIN_TOKEN,
        coding_shadow_reconciliation_enabled=enabled,
        coding_shadow_reconciliation_selection_delay_blocks=23,
    )
    app.state.coding_private_catalog_source = catalog_source

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    async def _chain() -> object:
        return chain

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_chain_client] = _chain
    return chain


async def test_reconciliation_is_admin_only_and_default_off(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    _install(app, session_maker, enabled=False, catalog_source=None)
    reconcile = AsyncMock()
    monkeypatch.setattr(endpoint_module, "reconcile_shadow_coding_run", reconcile)

    missing_auth = await client.post(
        "/api/v1/admin/coding-shadow/reconcile",
        json=_payload(),
    )
    assert missing_auth.status_code == 401

    disabled = await client.post(
        "/api/v1/admin/coding-shadow/reconcile",
        headers={"Authorization": f"Bearer {_ADMIN_TOKEN}"},
        json=_payload(),
    )
    assert disabled.status_code == 503
    reconcile.assert_not_awaited()


async def test_reconciliation_requires_exact_confirmation_and_catalog(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    _install(app, session_maker, enabled=True, catalog_source=None)
    reconcile = AsyncMock()
    monkeypatch.setattr(endpoint_module, "reconcile_shadow_coding_run", reconcile)
    headers = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}

    invalid = await client.post(
        "/api/v1/admin/coding-shadow/reconcile",
        headers=headers,
        json=_payload(confirmation="RECONCILE SHADOW CODING"),
    )
    assert invalid.status_code == 422
    reconcile.assert_not_awaited()

    unavailable = await client.post(
        "/api/v1/admin/coding-shadow/reconcile",
        headers=headers,
        json=_payload(),
    )
    assert unavailable.status_code == 503
    reconcile.assert_not_awaited()


async def test_reconciliation_delegates_one_named_artifact_without_tickets(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    catalog_source = object()
    chain = _install(
        app,
        session_maker,
        enabled=True,
        catalog_source=catalog_source,
    )
    assignment_row_id = uuid4()
    run_row_id = uuid4()
    reconcile = AsyncMock(
        return_value=CodingReconciliationResult(
            state=CodingReconciliationState.ISSUED,
            assignment_row_id=assignment_row_id,
            selection_block_number=1_234,
            run_row_id=run_row_id,
            assignment_idempotent=False,
            issuance_idempotent=False,
        )
    )
    monkeypatch.setattr(endpoint_module, "reconcile_shadow_coding_run", reconcile)

    response = await client.post(
        "/api/v1/admin/coding-shadow/reconcile",
        headers={"Authorization": f"Bearer {_ADMIN_TOKEN}"},
        json=_payload(),
    )

    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {
        "state": "issued",
        "assignment_row_id": str(assignment_row_id),
        "selection_block_number": 1_234,
        "run_row_id": str(run_row_id),
        "assignment_idempotent": False,
        "issuance_idempotent": False,
        "weight_eligible": False,
    }
    reconcile.assert_awaited_once()
    assert reconcile.await_args is not None
    kwargs = reconcile.await_args.kwargs
    assert kwargs["finalized_source"] is chain
    assert kwargs["catalog_source"] is catalog_source
    assert kwargs["agent_id"] == _AGENT_ID
    assert kwargs["bench_version"] == _BENCH_VERSION
    assert kwargs["coding_run_id"] == _CODING_RUN_ID
    assert kwargs["corpus_release_id"] == _CORPUS_RELEASE_ID
    assert kwargs["policy"].selection_delay_blocks == 23
