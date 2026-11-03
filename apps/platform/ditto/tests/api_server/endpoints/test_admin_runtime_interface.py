"""The runtime upgrade report is admin-only, read-only and fails explicitly."""

from collections.abc import AsyncIterator
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_server.dependencies import get_session
from ditto.chain.errors import ChainConnectionError
from ditto.chain.runtime_interface_report import (
    BlockNotFinalized,
    RuntimeInterfaceReport,
    SurfaceVerdict,
    TreasurySignerView,
)

_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_TOKEN}"}
_URL = "/api/v1/admin/runtime-interface"

REPORT = RuntimeInterfaceReport(
    block=9305810,
    block_hash="0x" + "11" * 32,
    runtime_code_hash="0x" + "ab" * 32,
    spec_version=479,
    surfaces=(
        SurfaceVerdict("source_emission", "interface", "d" * 64, "v478", ()),
        SurfaceVerdict(
            "treasury_receipt",
            None,
            "e" * 64,
            "v478",
            ("storage:SubtensorModule.Uids",),
        ),
    ),
    treasury_signer=TreasurySignerView(
        approved_runtime_code_hash="0x" + "7e" * 32,
        signs_on_this_runtime=False,
        call_baseline_release="v478",
        changed_calls=("SubtensorModule.set_reject_staking_hotkey_growth",),
    ),
)


@pytest.fixture
def chain(app: FastAPI, session_maker: async_sessionmaker[AsyncSession]) -> AsyncMock:
    app.state.config = replace(app.state.config, admin_api_token=_TOKEN)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    reader = AsyncMock(return_value=REPORT)
    app.state.chain = SimpleNamespace(get_runtime_interface_report=reader)
    return reader


async def test_report_round_trips_and_is_not_cached(
    client: httpx.AsyncClient, chain: AsyncMock
) -> None:
    response = await client.get(_URL, headers=_HEADERS, params={"block": 9305810})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["spec_version"] == 479
    assert [s["admitted_by"] for s in body["surfaces"]] == ["interface", None]
    assert body["surfaces"][1]["changed_entries"] == ["storage:SubtensorModule.Uids"]
    assert body["treasury_signer"] == {
        "approved_runtime_code_hash": "0x" + "7e" * 32,
        "signs_on_this_runtime": False,
        "call_baseline_release": "v478",
        "changed_calls": ["SubtensorModule.set_reject_staking_hotkey_growth"],
        "changed_calls_truncated": False,
        "calls_error": None,
    }
    chain.assert_awaited_once_with(9305810)


async def test_requires_admin(client: httpx.AsyncClient, chain: AsyncMock) -> None:
    response = await client.get(_URL)
    assert response.status_code == 401
    chain.assert_not_awaited()


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (BlockNotFinalized("block is not finalized"), 422),
        (ValueError("malformed runtime metadata: KeyError"), 503),
        (ChainConnectionError("archive down"), 503),
    ],
)
async def test_failures_are_explicit(
    client: httpx.AsyncClient, chain: AsyncMock, error: Exception, status: int
) -> None:
    chain.side_effect = error
    response = await client.get(_URL, headers=_HEADERS)
    assert response.status_code == status, response.text
    assert "archive down" not in response.text
