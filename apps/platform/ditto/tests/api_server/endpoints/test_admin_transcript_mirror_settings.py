"""The public transcript mirror is an audited setting and defaults off."""

from __future__ import annotations

from dataclasses import replace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.tests.api_server.endpoints.test_public import _install_db

_TOKEN = "test-admin-token-at-least-32-characters"


@pytest.mark.asyncio
async def test_enable_appends_an_audit_revision(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install_db(app, session_maker)
    app.state.config = replace(app.state.config, admin_api_token=_TOKEN)
    headers = {"Authorization": f"Bearer {_TOKEN}"}
    current = await client.get(
        "/api/v1/admin/transcript-mirror-settings", headers=headers
    )
    assert current.status_code == 200, current.text
    body = current.json()
    assert body["current"]["enabled"] is False
    assert body["current"]["actor"] == "migration"

    denied = await client.post(
        "/api/v1/admin/transcript-mirror-settings",
        headers=headers,
        json={
            "expected_revision": body["current"]["revision"],
            "enabled": True,
            "reason": "Operator enabled the quorum transcript mirror",
            "actor": "operator",
            "confirmation": "DISABLE TRANSCRIPT MIRROR",
        },
    )
    assert denied.status_code == 409

    enabled = await client.post(
        "/api/v1/admin/transcript-mirror-settings",
        headers=headers,
        json={
            "expected_revision": body["current"]["revision"],
            "enabled": True,
            "reason": "Operator enabled the quorum transcript mirror",
            "actor": "operator",
            "confirmation": "ENABLE TRANSCRIPT MIRROR",
        },
    )
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["enabled"] is True
    assert enabled.json()["parent_revision"] == body["current"]["revision"]
    assert enabled.json()["actor"] == "operator"
