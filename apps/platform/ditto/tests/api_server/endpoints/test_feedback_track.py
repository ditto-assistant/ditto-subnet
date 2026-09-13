"""Feedback Track plumbing: operator writes, miner reads via the link, public counts."""

from __future__ import annotations

from dataclasses import replace

import bittensor
import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.tests.api_server.endpoints.test_miner_ditto_link import _install, _sign_in

ADMIN = "feedback-track-admin-token-at-least-32-characters-long"


async def test_contributions_follow_the_active_link_and_never_name_the_account(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    app.state.config = replace(app.state.config, admin_api_token=ADMIN)
    alice = bittensor.Keypair.create_from_uri("//Alice")
    auth = {"authorization": f"Bearer {await _sign_in(client, alice)}"}
    admin = {"authorization": f"Bearer {ADMIN}", "x-admin-actor": "ditto-backend"}

    # Operator write is idempotent on (source, external_ref, kind).
    first = await client.post(
        "/api/v1/feedback-track/contributions",
        headers=admin,
        json={
            "ditto_user_id": "ditto-user-1",
            "external_ref": "FB-12@ditto-user-1",
            "kind": "report",
            "weight": "1.5",
        },
    )
    assert first.status_code == 201, first.text
    assert first.json()["created"] is True
    again = await client.post(
        "/api/v1/feedback-track/contributions",
        headers=admin,
        json={
            "ditto_user_id": "ditto-user-1",
            "external_ref": "FB-12@ditto-user-1",
            "kind": "report",
            "weight": "2",
        },
    )
    assert again.status_code == 200
    assert again.json()["created"] is False
    shipped = await client.post(
        "/api/v1/feedback-track/contributions",
        headers=admin,
        json={
            "ditto_user_id": "ditto-user-1",
            "external_ref": "FB-12@ditto-user-1",
            "kind": "shipped",
        },
    )
    assert shipped.status_code == 201
    # Wrong bearer and unknown kinds are refused.
    assert (
        await client.post(
            "/api/v1/feedback-track/contributions",
            headers={"authorization": "Bearer nope"},
            json={"ditto_user_id": "x", "external_ref": "r", "kind": "report"},
        )
    ).status_code == 401
    assert (
        await client.post(
            "/api/v1/feedback-track/contributions",
            headers=admin,
            json={"ditto_user_id": "x", "external_ref": "r", "kind": "emission_bonus"},
        )
    ).status_code == 422

    # Unlinked hotkey: nothing to show, publicly or privately.
    me = await client.get("/api/v1/me/feedback-track", headers=auth)
    assert me.status_code == 200 and me.json() == {
        "linked": False,
        "ditto_user_id": None,
        "contributions": [],
        "counts": {},
    }
    public = await client.get(f"/api/v1/public/feedback-track/{alice.ss58_address}")
    assert public.status_code == 200
    assert public.json() == {
        "miner_hotkey": alice.ss58_address,
        "linked": False,
        "counts": {},
        "total": 0,
    }

    # Link Alice's hotkey to ditto-user-1 directly (the OIDC flow is covered elsewhere).
    async with session_maker() as session, session.begin():
        from datetime import UTC, datetime

        from ditto.db.queries.miner_ditto_links import upsert_link

        await upsert_link(
            session,
            hotkey=alice.ss58_address,
            ditto_user_id="ditto-user-1",
            ditto_email="miner@example.com",
            miner_coldkey=None,
            linked_via="cli",
            session_id=None,
            now=datetime.now(UTC),
        )
    me = await client.get("/api/v1/me/feedback-track", headers=auth)
    body = me.json()
    assert body["linked"] is True and body["ditto_user_id"] == "ditto-user-1"
    assert sorted(c["kind"] for c in body["contributions"]) == ["report", "shipped"]
    assert body["counts"] == {"report": 1, "shipped": 1}
    public = await client.get(f"/api/v1/public/feedback-track/{alice.ss58_address}")
    assert public.json() == {
        "miner_hotkey": alice.ss58_address,
        "linked": True,
        "counts": {"report": 1, "shipped": 1},
        "total": 2,
    }
    assert "ditto-user-1" not in public.text and "miner@example.com" not in public.text

    # Revoking the link stops attribution without deleting the record.
    assert (
        await client.delete("/api/v1/me/ditto-link", headers=auth)
    ).status_code == 204
    public = await client.get(f"/api/v1/public/feedback-track/{alice.ss58_address}")
    assert public.json()["linked"] is False and public.json()["total"] == 0
    assert (await client.get("/api/v1/me/feedback-track", headers=auth)).json()[
        "linked"
    ] is False


async def test_public_feedback_track_rejects_garbage_hotkeys(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    assert (
        await client.get("/api/v1/public/feedback-track/not-a-hotkey")
    ).status_code == 404
