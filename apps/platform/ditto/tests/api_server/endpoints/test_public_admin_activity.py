"""Real-Postgres audit coverage, privacy, failure and pagination regressions."""

from dataclasses import replace
from typing import Annotated
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import Depends, HTTPException
from fastapi.routing import APIRoute
from sqlalchemy import func, select

from ditto.api_server.admin_activity import public_details
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.models import AdminActivity

TOKEN = "audit-test-admin-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "X-Admin-Actor": "private@example.com"}


def test_projection_rejects_private_and_unknown_fields():
    agent = str(uuid4())
    details = public_details(
        "/api/v1/admin/screener-review-settings",
        {
            "agent_id": agent,
            "actor": "secret@example.com",
            "reason": "secret",
            "token": "secret",
            "settings": {"mode": "shadow", "api_key": "secret"},
        },
    )
    assert details == {"agent_id": agent, "settings": {"mode": "shadow"}}
    assert public_details("/admin/future", {"settings": {"token": "secret"}}) == {}
    assert public_details("/admin/future", {"agent_id": "secret"}) == {}


def _install(app, maker):
    app.state.config = replace(app.state.config, admin_api_token=TOKEN)
    app.state.session_maker = maker
    app.state.admin_activity_session_maker = maker

    async def session():
        async with maker() as value:
            yield value

    app.dependency_overrides[get_session] = session

    @app.post("/api/v1/admin/activity-test/{agent_id}")
    async def mutation(
        agent_id: str, payload: dict, _admin: Annotated[None, Depends(require_admin)]
    ):
        if payload.get("fail"):
            raise HTTPException(409, "private failure detail")
        return {"token": "response-secret", "agent_id": agent_id}


@pytest.mark.asyncio
async def test_success_failure_auth_privacy_and_cursor(app, client, session_maker):
    _install(app, session_maker)
    agent = str(uuid4())
    url = f"/api/v1/admin/activity-test/{agent}"
    assert (await client.post(url, json={})).status_code == 401
    assert (
        await client.post(url, headers=HEADERS, json={"secret": "hidden"})
    ).status_code == 200
    assert (
        await client.post(url, headers=HEADERS, json={"fail": True})
    ).status_code == 409
    response = await client.get(
        "/api/v1/public/admin-activity", params={"q": agent, "limit": 1}
    )
    assert response.status_code == 200
    page = response.json()
    assert page["items"][0]["status"] == "failed"
    assert page["items"][0]["details"] == {"agent_id": agent}
    assert page["next_before"] is not None
    older = (
        await client.get(
            "/api/v1/public/admin-activity",
            params={"q": agent, "before": page["next_before"]},
        )
    ).json()
    assert [item["status"] for item in older["items"]] == ["succeeded"]
    assert older["next_before"] is None
    assert not any(
        word in response.text
        for word in ("private@", "hidden", "response-secret", "failure detail")
    )
    assert (
        await client.get("/api/v1/public/admin-activity?q=private@example.com")
    ).json()["items"] == []
    async with session_maker() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AdminActivity)
                .where(AdminActivity.source == "request")
            )
            == 2
        )
        assert (
            await session.scalar(
                select(AdminActivity.actor)
                .where(AdminActivity.source == "request")
                .limit(1)
            )
            == "private@example.com"
        )


@pytest.mark.asyncio
async def test_write_fails_closed_without_audit_database(app, client, session_maker):
    _install(app, session_maker)
    with patch.object(
        app.state,
        "admin_activity_session_maker",
        side_effect=RuntimeError("db offline"),
    ):
        response = await client.post(
            f"/api/v1/admin/activity-test/{uuid4()}", headers=HEADERS, json={}
        )
    assert response.status_code == 500
    async with session_maker() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AdminActivity)
                .where(AdminActivity.source == "request")
            )
            == 0
        )


@pytest.mark.asyncio
async def test_missing_completion_is_unknown_and_not_searchable_by_private_data(
    app, client, session_maker
):
    _install(app, session_maker)
    async with session_maker() as session:
        session.add(
            AdminActivity(
                action="/api/v1/admin/unknown",
                method="POST",
                actor="secret@example.com",
                details={"settings": {"secret": "hidden"}},
                source="request",
            )
        )
        await session.commit()
    page = (await client.get("/api/v1/public/admin-activity?status=unknown")).json()
    assert page["items"][0]["status"] == "unknown"
    assert page["items"][0]["details"] == {}
    assert (await client.get("/api/v1/public/admin-activity?q=hidden")).json()[
        "items"
    ] == []
    assert (
        await client.get("/api/v1/public/admin-activity?limit=101")
    ).status_code == 422


def test_every_admin_mutation_crosses_audit_auth_boundary(app):
    def guarded(dependant):
        return dependant.call is require_admin or any(
            guarded(child) for child in dependant.dependencies
        )

    routes = [
        route
        for route in app.routes
        if isinstance(route, APIRoute)
        and route.path.startswith("/api/v1/admin/")
        and route.methods & {"POST", "PUT", "PATCH", "DELETE"}
    ]
    assert len(routes) > 50
    assert [route.path for route in routes if not guarded(route.dependant)] == []


@pytest.mark.asyncio
async def test_lost_completion_retains_intent_without_encouraging_retry(
    app, client, session_maker
):
    _install(app, session_maker)
    agent = str(uuid4())
    with patch(
        "ditto.api_server.admin_activity.AdminActivityOutcome",
        side_effect=RuntimeError("completion store offline"),
    ):
        response = await client.post(
            f"/api/v1/admin/activity-test/{agent}", headers=HEADERS, json={}
        )
    assert response.status_code == 200
    page = (
        await client.get("/api/v1/public/admin-activity", params={"q": agent})
    ).json()
    assert len(page["items"]) == 1
    assert page["items"][0]["status"] == "unknown"
    assert page["items"][0]["http_status"] is None


@pytest.mark.asyncio
async def test_history_is_retained_with_original_timestamp(app, client, session_maker):
    _install(app, session_maker)
    page = (await client.get("/api/v1/public/admin-activity?status=recorded")).json()
    assert page["items"]
    assert all(item["http_status"] is None for item in page["items"])
    assert all(item["method"] == "HISTORY" for item in page["items"])
    async with session_maker() as session:
        row = await session.get(AdminActivity, page["items"][0]["id"])
        assert (
            row.recorded_at.isoformat().replace("+00:00", "Z")
            == page["items"][0]["recorded_at"]
        )
