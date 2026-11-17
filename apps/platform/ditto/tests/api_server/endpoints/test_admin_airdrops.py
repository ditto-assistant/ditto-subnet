"""Contract tests for Backroom airdrop runs (SN-490).

Every execution here goes through a simulated or test-only executor. Nothing
in this module, or in the shipped runtime, can move funds or credits.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.airdrop import AIRDROP_MAX_RECIPIENTS, AIRDROP_MAX_TOTAL
from ditto.api_server.airdrop import AirdropSettlement, default_executors
from ditto.api_server.dependencies import get_session
from ditto.db.models import (
    AdminActivity,
    AirdropRecipient,
    AirdropRun,
    MinerDittoLink,
)

pytestmark = pytest.mark.asyncio

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_ACTOR = "operator@omniaura.ai"
_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}", "X-Admin-Actor": _ACTOR}
_ALICE = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
_BOB = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
_CHARLIE = "5FLSigC9HGRKVhB9FiEo4Y3koPsNmBmLJbpXg2mp1hXcS59Y"
_DAVE = "5DAAnrj7VHTznn2AWBemMuyBwZWs6FNFjdyVXUeYum3PTXFy"
_ALPHA = 10**9


@dataclass
class _CountingExecutor:
    """Test executor: records every settle call and returns scripted outcomes."""

    name: str = "counting"
    live: bool = False
    calls: list[str] = field(default_factory=list)
    outcomes: dict[str, AirdropSettlement] = field(default_factory=dict)
    raises: set[str] = field(default_factory=set)
    delay: float = 0.0

    async def settle(self, *, run_id, asset, recipient_key, payee, amount):
        del run_id, asset, amount
        self.calls.append(payee)
        if self.delay:
            await asyncio.sleep(self.delay)
        if payee in self.raises:
            raise TimeoutError("broadcast outcome unknown")
        return self.outcomes.get(
            payee, AirdropSettlement("settled", receipt=f"test:{recipient_key[:8]}")
        )


@dataclass
class _Funding:
    name: str = "test-funding"
    balance: int | None = 0

    async def available(self, asset):
        del asset
        return self.balance


def _install(
    app: FastAPI,
    maker: async_sessionmaker[AsyncSession],
    *,
    executors: dict[tuple[str, str], Any] | None = None,
    funding: dict[str, Any] | None = None,
) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_ADMIN_TOKEN)
    app.state.session_maker = maker
    if executors is not None:
        app.state.airdrop_executors = executors
    if funding is not None:
        app.state.airdrop_funding = funding

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


def _request(
    *,
    key: str = "airdrop-test-0001",
    asset: str = "alpha",
    mode: str = "rehearsal",
    recipients: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "asset": asset,
        "mode": mode,
        "idempotency_key": key,
        "reason": "thank early SN118 miners",
        "recipients": recipients
        if recipients is not None
        else [
            {"kind": "wallet", "address": _ALICE, "amount": _ALPHA},
            {"kind": "wallet", "address": _BOB, "amount": _ALPHA // 2},
        ],
    }


async def _preview(client: httpx.AsyncClient, body: dict[str, Any]) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/admin/airdrops/preview", headers=_HEADERS, json=body
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _execute(
    client: httpx.AsyncClient, body: dict[str, Any], preview: dict[str, Any]
) -> httpx.Response:
    return await client.post(
        "/api/v1/admin/airdrops/execute",
        headers=_HEADERS,
        json={
            **body,
            "dry_run": False,
            "confirmation_digest": preview["confirmation_digest"],
            "confirmation": preview["confirmation"],
        },
    )


async def _run(client: httpx.AsyncClient, body: dict[str, Any]) -> httpx.Response:
    """Preview, then execute exactly what the preview showed."""
    return await _execute(client, body, await _preview(client, body))


async def _count(maker: async_sessionmaker[AsyncSession], model: type) -> int:
    async with maker() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def test_preview_totals_recipients_cap_and_balance_without_writing(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)

    preview = await _preview(client, _request())

    assert preview["dry_run"] is True
    assert preview["spending_authority"] == "none"
    assert preview["unit"] == "rao"
    assert preview["executor"] == "simulated"
    assert preview["recipient_count"] == 2
    assert preview["total_amount"] == _ALPHA + _ALPHA // 2
    assert preview["cap"] == {
        "max_recipients": AIRDROP_MAX_RECIPIENTS,
        "max_total_amount": AIRDROP_MAX_TOTAL["alpha"],
        "recipient_count": 2,
        "total_amount": _ALPHA + _ALPHA // 2,
        "within_cap": True,
    }
    assert preview["balance"]["status"] == "unavailable"
    assert preview["blockers"] == []
    assert preview["ready"] is True
    assert preview["confirmation"] == "AIRDROP 1.5 ALPHA TO 2 RECIPIENTS AS REHEARSAL"
    assert [row["payee"] for row in preview["recipients"]] == [_ALICE, _BOB]
    keys = {row["recipient_key"] for row in preview["recipients"]}
    assert len(keys) == 2 and all(len(key) == 64 for key in keys)
    assert await _count(session_maker, AirdropRun) == 0


async def test_execute_defaults_to_dry_run(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)

    response = await client.post(
        "/api/v1/admin/airdrops/execute", headers=_HEADERS, json=_request()
    )

    assert response.status_code == 200, response.text
    assert response.json()["dry_run"] is True
    assert response.json()["run"] is None
    assert response.json()["preview"]["ready"] is True
    assert await _count(session_maker, AirdropRun) == 0


async def test_rehearsal_settles_audits_and_retry_never_pays_twice(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    executor = _CountingExecutor()
    _install(app, session_maker, executors={("alpha", "rehearsal"): executor})
    body = _request()
    preview = await _preview(client, body)

    first = await _execute(client, body, preview)

    assert first.status_code == 200, first.text
    run = first.json()["run"]
    assert first.json()["dry_run"] is False
    assert run["status"] == "settled"
    assert run["resumed"] is False
    assert run["mode"] == "rehearsal"
    assert run["actor"] == _ACTOR
    assert run["settled_amount"] == _ALPHA + _ALPHA // 2
    assert run["counts"] == {"settled": 2}
    assert run["completed_at"] is not None
    assert [row["status"] for row in run["recipients"]] == ["settled", "settled"]
    assert all(row["receipt"].startswith("test:") for row in run["recipients"])
    assert executor.calls == [_ALICE, _BOB]

    retry = await _execute(client, body, preview)

    assert retry.status_code == 200, retry.text
    assert retry.json()["run"]["run_id"] == run["run_id"]
    assert retry.json()["run"]["resumed"] is True
    assert executor.calls == [_ALICE, _BOB]
    assert await _count(session_maker, AirdropRun) == 1
    assert await _count(session_maker, AirdropRecipient) == 2

    async with session_maker() as session:
        audited = (
            await session.scalars(
                select(AdminActivity).where(
                    AdminActivity.action == "/api/v1/admin/airdrops/execute"
                )
            )
        ).all()
    assert [row.actor for row in audited] == [_ACTOR, _ACTOR]
    # Recipients and amounts never reach the public audit projection.
    assert all(row.details == {} for row in audited)

    report = await client.get(
        f"/api/v1/admin/airdrops/{run['run_id']}", headers=_HEADERS
    )
    assert report.status_code == 200
    assert report.json()["recipients"] == run["recipients"]


async def test_concurrent_executes_of_one_key_send_each_recipient_once(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    executor = _CountingExecutor(delay=0.05)
    _install(app, session_maker, executors={("alpha", "rehearsal"): executor})
    body = _request()
    preview = await _preview(client, body)

    responses = await asyncio.gather(
        *(_execute(client, body, preview) for _ in range(3))
    )

    assert all(response.status_code == 200 for response in responses)
    assert len({response.json()["run"]["run_id"] for response in responses}) == 1
    assert sorted(executor.calls) == sorted([_ALICE, _BOB])
    assert await _count(session_maker, AirdropRun) == 1


async def test_same_key_with_different_contents_is_refused(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    executor = _CountingExecutor()
    _install(app, session_maker, executors={("alpha", "rehearsal"): executor})
    body = _request()
    assert (await _run(client, body)).status_code == 200

    changed = _request(recipients=[{"kind": "wallet", "address": _ALICE, "amount": 7}])
    preview = await _preview(client, changed)

    assert preview["ready"] is False
    assert preview["existing_run_matches"] is False
    assert any("idempotency key already names run" in b for b in preview["blockers"])
    refused = await _execute(client, changed, preview)
    assert refused.status_code == 409
    assert executor.calls == [_ALICE, _BOB]


async def test_digest_phrase_and_actor_are_required(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    body = _request()
    preview = await _preview(client, body)

    stale = await client.post(
        "/api/v1/admin/airdrops/execute",
        headers=_HEADERS,
        json={
            **body,
            "dry_run": False,
            "confirmation_digest": "0" * 64,
            "confirmation": preview["confirmation"],
        },
    )
    wrong_phrase = await client.post(
        "/api/v1/admin/airdrops/execute",
        headers=_HEADERS,
        json={
            **body,
            "dry_run": False,
            "confirmation_digest": preview["confirmation_digest"],
            "confirmation": "AIRDROP EVERYTHING",
        },
    )
    no_actor = await client.post(
        "/api/v1/admin/airdrops/execute",
        headers={"Authorization": _HEADERS["Authorization"]},
        json={
            **body,
            "dry_run": False,
            "confirmation_digest": preview["confirmation_digest"],
            "confirmation": preview["confirmation"],
        },
    )

    assert stale.status_code == 409
    assert wrong_phrase.status_code == 422
    assert preview["confirmation"] in wrong_phrase.text
    assert no_actor.status_code == 422
    assert await _count(session_maker, AirdropRun) == 0


async def test_refused_rows_block_execution(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    body = _request(
        recipients=[
            {"kind": "wallet", "address": _ALICE, "amount": 1},
            {"kind": "wallet", "address": _ALICE, "amount": 2},
            {"kind": "wallet", "address": "not-an-address", "amount": 3},
            {"kind": "account", "address": _BOB, "amount": 4},
        ]
    )

    preview = await _preview(client, body)

    assert [row["refused"] for row in preview["recipients"]] == [
        None,
        "duplicate recipient; combine the amounts into one row",
        "not a valid SS58 address",
        "alpha pays wallet recipients only",
    ]
    assert preview["recipient_count"] == 1
    assert preview["refused_count"] == 3
    assert preview["total_amount"] == 1
    assert preview["ready"] is False
    assert (await _execute(client, body, preview)).status_code == 409
    assert await _count(session_maker, AirdropRun) == 0


async def test_per_run_caps_block_execution(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    over_total = _request(
        recipients=[
            {"kind": "wallet", "address": _ALICE, "amount": AIRDROP_MAX_TOTAL["alpha"]},
            {"kind": "wallet", "address": _BOB, "amount": 1},
        ]
    )
    too_many = _request(
        recipients=[
            {"kind": "wallet", "address": _ALICE, "amount": 1}
            for _ in range(AIRDROP_MAX_RECIPIENTS + 1)
        ]
    )

    total_preview = await _preview(client, over_total)
    many_preview = await _preview(client, too_many)

    assert total_preview["cap"]["within_cap"] is False
    assert any("exceeds the per-run cap" in b for b in total_preview["blockers"])
    assert (await _execute(client, over_total, total_preview)).status_code == 409
    assert many_preview["cap"]["within_cap"] is False
    assert any(
        f"exceed the per-run cap of {AIRDROP_MAX_RECIPIENTS}" in b
        for b in many_preview["blockers"]
    )
    assert await _count(session_maker, AirdropRun) == 0


async def test_live_mode_is_refused_without_a_live_executor(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    # A rehearsal executor registered under "live" must not be accepted either.
    _install(
        app,
        session_maker,
        executors={**default_executors(), ("alpha", "live"): _CountingExecutor()},
    )
    body = _request(mode="live")

    preview = await _preview(client, body)

    assert preview["executor"] is None
    assert preview["confirmation"] == "AIRDROP 1.5 ALPHA TO 2 RECIPIENTS"
    assert any("no live executor" in b for b in preview["blockers"])
    assert (await _execute(client, body, preview)).status_code == 409
    listing = await client.get("/api/v1/admin/airdrops", headers=_HEADERS)
    assert {
        row["asset"]: row["live_executor"] for row in listing.json()["policy"]["assets"]
    } == {
        "alpha": None,
        "ditto_credits": None,
    }
    assert await _count(session_maker, AirdropRun) == 0


async def test_live_runs_require_a_sufficient_funding_balance(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    live = _CountingExecutor(name="test-live", live=True)
    funding = _Funding(balance=_ALPHA)
    _install(
        app,
        session_maker,
        executors={("alpha", "live"): live},
        funding={"alpha": funding},
    )
    body = _request(mode="live")

    short = await _preview(client, body)

    assert short["balance"] == {
        "status": "insufficient",
        "source": "test-funding",
        "available": _ALPHA,
        "required": _ALPHA + _ALPHA // 2,
        "detail": f"test-funding is short by {_ALPHA // 2}.",
    }
    assert "live funding balance is insufficient" in short["blockers"]
    assert (await _execute(client, body, short)).status_code == 409

    funding.balance = 2 * _ALPHA
    enough = await _preview(client, body)
    executed = await _execute(client, body, enough)

    assert executed.status_code == 200, executed.text
    assert executed.json()["run"]["mode"] == "live"
    assert executed.json()["run"]["executor"] == "test-live"
    assert live.calls == [_ALICE, _BOB]


async def test_failed_and_uncertain_rows_are_reported_and_never_resent(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    executor = _CountingExecutor(
        outcomes={_BOB: AirdropSettlement("failed", error="destination refused")},
        raises={_CHARLIE},
    )
    _install(app, session_maker, executors={("alpha", "rehearsal"): executor})
    body = _request(
        recipients=[
            {"kind": "wallet", "address": _ALICE, "amount": 1},
            {"kind": "wallet", "address": _BOB, "amount": 2},
            {"kind": "wallet", "address": _CHARLIE, "amount": 3},
        ]
    )
    preview = await _preview(client, body)

    first = (await _execute(client, body, preview)).json()["run"]
    retry = (await _execute(client, body, preview)).json()["run"]

    assert first["status"] == "needs_attention"
    assert first["counts"] == {"failed": 1, "settled": 1, "uncertain": 1}
    assert [row["status"] for row in first["recipients"]] == [
        "settled",
        "failed",
        "uncertain",
    ]
    assert first["recipients"][1]["error"] == "destination refused"
    assert "broadcast outcome unknown" in first["recipients"][2]["error"]
    assert first["settled_amount"] == 1
    assert retry["resumed"] is True
    assert executor.calls == [_ALICE, _BOB, _CHARLIE]


async def test_ditto_credit_accounts_resolve_through_verified_links(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    async with session_maker() as session, session.begin():
        session.add_all(
            [
                MinerDittoLink(
                    miner_hotkey=_ALICE, ditto_user_id="ditto-user-1", linked_via="cli"
                ),
                MinerDittoLink(
                    miner_hotkey=_BOB, ditto_user_id="ditto-user-1", linked_via="cli"
                ),
                MinerDittoLink(
                    miner_hotkey=_CHARLIE,
                    ditto_user_id="ditto-user-2",
                    linked_via="dashboard",
                ),
            ]
        )
    async with session_maker() as session, session.begin():
        await session.execute(
            text(
                "UPDATE miner_ditto_links SET revoked_at = now() "
                "WHERE miner_hotkey = :h"
            ),
            {"h": _CHARLIE},
        )
    body = _request(
        asset="ditto_credits",
        recipients=[
            {"kind": "account", "address": _ALICE, "amount": 5 * 10**9},
            {"kind": "account", "address": _BOB, "amount": 5 * 10**9},
            {"kind": "account", "address": _CHARLIE, "amount": 5 * 10**9},
            {"kind": "account", "address": _DAVE, "amount": 5 * 10**9},
            {"kind": "wallet", "address": _DAVE, "amount": 5 * 10**9},
        ],
    )

    preview = await _preview(client, body)

    assert preview["unit"] == "credit"
    assert [(row["payee"], row["refused"]) for row in preview["recipients"]] == [
        ("ditto-user-1", None),
        (None, "another row already pays this Ditto account"),
        (None, "hotkey has no active Sign in with Ditto link"),
        (None, "hotkey has no active Sign in with Ditto link"),
        (None, "ditto_credits pays account recipients only"),
    ]
    assert preview["ready"] is False

    single = _request(
        asset="ditto_credits",
        recipients=[{"kind": "account", "address": _ALICE, "amount": 5 * 10**9}],
    )
    ready = await _preview(client, single)
    assert (
        ready["confirmation"]
        == "AIRDROP 5000000000 DITTO CREDITS TO 1 RECIPIENT AS REHEARSAL"
    )
    executed = await _execute(client, single, ready)
    assert executed.status_code == 200, executed.text
    assert executed.json()["run"]["recipients"][0]["payee"] == "ditto-user-1"
    assert executed.json()["run"]["recipients"][0]["receipt"].startswith("rehearsal:")


async def test_prior_payments_under_other_keys_are_surfaced(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    body = _request()
    assert (await _run(client, body)).status_code == 200

    again = await _preview(client, _request(key="airdrop-test-0002"))

    assert [row["prior_amount"] for row in again["recipients"]] == [
        _ALPHA,
        _ALPHA // 2,
    ]
    assert again["warnings"] == [
        "2 recipient(s) already received or are being paid 1.5 ALPHA under other "
        "run keys; if this is a retry, resume the original key instead"
    ]
    assert again["ready"] is True
    same_key = await _preview(client, body)
    assert [row["prior_amount"] for row in same_key["recipients"]] == [0, 0]
    assert same_key["warnings"] == []
    assert same_key["existing_run_matches"] is True


async def test_list_filters_runs_and_reports_policy(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    for key in ("airdrop-test-0001", "airdrop-test-0002"):
        body = _request(key=key)
        assert (await _run(client, body)).status_code == 200

    listing = await client.get("/api/v1/admin/airdrops", headers=_HEADERS)
    filtered = await client.get(
        "/api/v1/admin/airdrops",
        headers=_HEADERS,
        params={"idempotency_key": "airdrop-test-0001"},
    )

    assert listing.status_code == 200
    assert listing.headers["cache-control"] == "no-store"
    assert [run["idempotency_key"] for run in listing.json()["runs"]] == [
        "airdrop-test-0002",
        "airdrop-test-0001",
    ]
    assert listing.json()["policy"]["max_recipients"] == AIRDROP_MAX_RECIPIENTS
    assert {
        row["asset"]: (row["rehearsal_executor"], row["max_total_amount"])
        for row in listing.json()["policy"]["assets"]
    } == {
        "alpha": ("simulated", AIRDROP_MAX_TOTAL["alpha"]),
        "ditto_credits": ("simulated", AIRDROP_MAX_TOTAL["ditto_credits"]),
    }
    assert [run["idempotency_key"] for run in filtered.json()["runs"]] == [
        "airdrop-test-0001"
    ]
    missing = await client.get(
        "/api/v1/admin/airdrops/00000000-0000-0000-0000-000000000000",
        headers=_HEADERS,
    )
    assert missing.status_code == 404


async def test_admin_bearer_is_required(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)

    response = await client.post("/api/v1/admin/airdrops/preview", json=_request())

    assert response.status_code == 401


async def test_ledger_rows_are_append_only(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    body = _request()
    run_id = UUID((await _run(client, body)).json()["run"]["run_id"])

    statements = [
        (
            "UPDATE airdrop_recipients SET status = 'pending', attempted_at = NULL "
            "WHERE run_id = :id"
        ),
        "UPDATE airdrop_recipients SET amount = amount + 1 WHERE run_id = :id",
        "UPDATE airdrop_recipients SET receipt = 'forged' WHERE run_id = :id",
        "DELETE FROM airdrop_recipients WHERE run_id = :id",
        "UPDATE airdrop_runs SET total_amount = 1 WHERE run_id = :id",
        "UPDATE airdrop_runs SET completed_at = now() WHERE run_id = :id",
        "DELETE FROM airdrop_runs WHERE run_id = :id",
    ]
    for statement in statements:
        async with session_maker() as session:
            with pytest.raises(DBAPIError):
                async with session.begin():
                    await session.execute(text(statement), {"id": run_id})


async def test_malformed_requests_are_422(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    body = _request()

    padded = await client.post(
        "/api/v1/admin/airdrops/preview",
        headers=_HEADERS,
        json={**body, "reason": "        x"},
    )
    too_large = await client.post(
        "/api/v1/admin/airdrops/preview",
        headers=_HEADERS,
        json=_request(
            recipients=[
                {
                    "kind": "wallet",
                    "address": _ALICE,
                    "amount": max(AIRDROP_MAX_TOTAL.values()) + 1,
                }
            ]
        ),
    )
    no_digest = await client.post(
        "/api/v1/admin/airdrops/execute",
        headers=_HEADERS,
        json={**body, "dry_run": False},
    )

    assert padded.status_code == 422
    assert too_large.status_code == 422
    assert no_digest.status_code == 422
    assert await _count(session_maker, AirdropRun) == 0


async def test_resume_needs_funding_only_for_pending_rows(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    live = _CountingExecutor(name="test-live", live=True)
    funding = _Funding(balance=2 * _ALPHA)
    _install(
        app,
        session_maker,
        executors={("alpha", "live"): live},
        funding={"alpha": funding},
    )
    body = _request(mode="live")
    assert (await _run(client, body)).status_code == 200

    funding.balance = 0
    resumed = await _preview(client, body)

    assert resumed["balance"]["required"] == 0
    assert resumed["balance"]["status"] == "sufficient"
    assert resumed["ready"] is True
    assert (await _execute(client, body, resumed)).json()["run"]["resumed"] is True
    assert live.calls == [_ALICE, _BOB]


async def test_stale_dispatching_rows_need_attention_and_are_never_resent(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    executor = _CountingExecutor()
    _install(app, session_maker, executors={("alpha", "rehearsal"): executor})
    body = _request()
    preview = await _preview(client, body)
    run_id = UUID("22222222-3333-4444-8555-666666666666")
    # The state a crash between claiming Alice's row and recording its
    # outcome leaves behind: Alice dispatching an hour ago, Bob still pending.
    async with session_maker() as session, session.begin():
        session.add(
            AirdropRun(
                run_id=run_id,
                idempotency_key=body["idempotency_key"],
                asset="alpha",
                mode="rehearsal",
                executor="counting",
                digest=preview["confirmation_digest"],
                reason=body["reason"],
                actor=_ACTOR,
                recipient_count=2,
                total_amount=preview["total_amount"],
            )
        )
        await session.flush()
        for row in preview["recipients"]:
            claimed = row["address"] == _ALICE
            session.add(
                AirdropRecipient(
                    run_id=run_id,
                    position=row["position"],
                    recipient_key=row["recipient_key"],
                    kind=row["kind"],
                    address=row["address"],
                    payee=row["payee"],
                    amount=row["amount"],
                    status="dispatching" if claimed else "pending",
                    attempted_at=(
                        text("now() - interval '1 hour'") if claimed else None
                    ),
                )
            )

    stale = (
        await client.get(f"/api/v1/admin/airdrops/{run_id}", headers=_HEADERS)
    ).json()
    resumed = (await _execute(client, body, preview)).json()["run"]

    assert stale["status"] == "needs_attention"
    assert stale["stale_dispatching"] == 1
    assert resumed["resumed"] is True
    assert executor.calls == [_BOB]
    assert [row["status"] for row in resumed["recipients"]] == [
        "dispatching",
        "settled",
    ]
    assert resumed["status"] == "needs_attention"
    assert resumed["completed_at"] is None
