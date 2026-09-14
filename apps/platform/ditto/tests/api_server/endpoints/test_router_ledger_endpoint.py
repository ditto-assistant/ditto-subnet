"""Unit tests for ``GET /scoring/router-ledger``.

The shadow router-track relay reuses the exact signed proof-of-possession the
memory ledger uses, so the auth cases mirror ``test_scoring`` (imported rather
than duplicated). What is unique here is the shadow contract: with no scorer
feed wired the endpoint serves an empty ledger, and whatever a feed returns is
defensively clamped so every relayed entry folds to zero emission
(``weight_eligible=False``, ``combined_score=0``) while the real measurement
rides ``shadow_composite`` untouched.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.router_ledger import (
    RouterHarness,
    RouterHarnessResult,
    RouterLedgerEntry,
    RouterLedgerResponse,
)
from ditto.api_server.middleware.error_envelope import ERROR_CODE_VALIDATOR_AUTH
from ditto.tests.api_server.endpoints.test_scoring import (
    _AUTH_HEADER,
    _install_chain,
    _install_db,
    _ledger_headers,
)

_ROUTER_LEDGER_PATH = "/api/v1/scoring/router-ledger"


def _weight_bearing_entry(*, shadow_composite: float) -> RouterLedgerEntry:
    """An entry deliberately shaped as weight-bearing, to prove the clamp.

    ``weight_eligible=True`` and ``combined_score>0`` are exactly what the relay
    must never pass through to a folding validator; the clamp has to override
    both regardless of what the (trusted) scorer returned.
    """
    return RouterLedgerEntry(
        miner_hotkey="5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm",
        agent_id=uuid4(),
        router_contract_version=1,
        weight_eligible=True,
        combined_score=0.91,
        shadow_composite=shadow_composite,
        harnesses=(
            RouterHarnessResult(
                harness=RouterHarness.CLAUDE_CODE,
                operational=True,
                floor_pass=True,
                efficiency=shadow_composite,
                upstream_token_cost_micros=12_345,
            ),
        ),
        first_seen=datetime.now(UTC),
    )


class _StubReader:
    """Minimal ``router_ledger_reader`` stand-in: returns or raises on read."""

    def __init__(
        self,
        *,
        ledger: RouterLedgerResponse | None = None,
        error: Exception | None = None,
    ) -> None:
        self._ledger = ledger
        self._error = error

    async def read(self) -> RouterLedgerResponse:
        if self._error is not None:
            raise self._error
        assert self._ledger is not None
        return self._ledger


class TestRouterLedgerEndpoint:
    async def test_no_reader_serves_empty_shadow_ledger(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        # No scorer feed wired: factory leaves router_ledger_reader unset.
        assert getattr(app.state, "router_ledger_reader", None) is None
        resp = await client.get(_ROUTER_LEDGER_PATH, headers=_ledger_headers())
        assert resp.status_code == 200
        assert resp.headers["Cache-Control"] == "no-store"
        body = resp.json()
        assert body["entries"] == []
        assert body["count"] == 0

    async def test_reader_ledger_is_clamped_to_shadow(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        app.state.router_ledger_reader = _StubReader(
            ledger=RouterLedgerResponse(
                entries=[_weight_bearing_entry(shadow_composite=0.42)],
                count=1,
            )
        )
        resp = await client.get(_ROUTER_LEDGER_PATH, headers=_ledger_headers())
        assert resp.status_code == 200
        (entry,) = resp.json()["entries"]
        # The shadow invariant is forced on the way out, whatever the feed said.
        assert entry["weight_eligible"] is False
        assert entry["combined_score"] == 0.0
        # The real measurement is preserved for the dashboard.
        assert entry["shadow_composite"] == pytest.approx(0.42)

    async def test_reader_failure_serves_empty_not_error(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        app.state.router_ledger_reader = _StubReader(
            error=RuntimeError("scorer unreachable")
        )
        resp = await client.get(_ROUTER_LEDGER_PATH, headers=_ledger_headers())
        # A router-track read failure must never fail the validator's fold.
        assert resp.status_code == 200
        assert resp.json()["entries"] == []

    async def test_missing_auth_returns_401(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        resp = await client.get(_ROUTER_LEDGER_PATH)
        assert resp.status_code == 401
        assert resp.json()["error_code"] == ERROR_CODE_VALIDATOR_AUTH

    async def test_unsigned_identity_returns_401(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        resp = await client.get(_ROUTER_LEDGER_PATH, headers=_AUTH_HEADER)
        assert resp.status_code == 401
        assert resp.json()["error_code"] == ERROR_CODE_VALIDATOR_AUTH

    async def test_replayed_nonce_returns_409(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        headers = _ledger_headers()
        first = await client.get(_ROUTER_LEDGER_PATH, headers=headers)
        replay = await client.get(_ROUTER_LEDGER_PATH, headers=headers)
        assert first.status_code == 200
        assert replay.status_code == 409

    async def test_unpermitted_validator_returns_401(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app, permitted=False)
        resp = await client.get(_ROUTER_LEDGER_PATH, headers=_ledger_headers())
        assert resp.status_code == 401
        assert resp.json()["error_code"] == ERROR_CODE_VALIDATOR_AUTH
