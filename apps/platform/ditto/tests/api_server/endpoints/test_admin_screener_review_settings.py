"""Contract and concurrency tests for screener review settings."""

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)

from ditto.api_server.dependencies import get_session
from ditto.db.models import ScreenerHeartbeat

pytestmark = pytest.mark.asyncio

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_ADMIN_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}
_SCREENER_HEADERS = {
    "Authorization": "Bearer test-screener-token-at-least-32-characters",
    "X-Screener-Hotkey": "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
}


@pytest.fixture
def settings_maker(
    session_maker: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    """Local alias for the root Postgres ``session_maker``."""
    return session_maker


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_ADMIN_TOKEN)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


def _payload(scope: str, mode: str, expected: int = 0) -> dict[str, object]:
    return {
        "scope": scope,
        "expected_revision": expected,
        "settings": {
            "mode": mode,
            "l2_model": "moonshotai/kimi-k3",
            "l2_fallback_models": ["z-ai/glm-5.2", "openai/gpt-5.6-sol"],
            "l3_enabled": True,
            "l3_model": "openai/gpt-5.6-sol",
            "timeout_seconds": 900,
            "max_steps": 18,
            "max_input_tokens": 425_000,
            "max_output_tokens": 20_000,
            "max_completion_tokens": 2_400,
            "max_cost_usd": 2,
            "critic_reasoning_effort": "medium",
            "cache_ttl_seconds": 604_800,
            "audit_retention_days": 30,
        },
        "reason": f"exercise {mode} settings safely",
        "actor": "backroom:test",
        "confirmation": f"APPLY SCREENER REVIEW {scope} {mode.upper()}",
    }


async def test_builtin_off_then_global_shadow_and_instance_override(
    app: FastAPI,
    client: httpx.AsyncClient,
    settings_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, settings_maker)
    denied = await client.get(
        "/api/v1/screener/review-settings?instance_id=ditto-screener-prod"
    )
    assert denied.status_code == 401

    initial = await client.get(
        "/api/v1/screener/review-settings?instance_id=ditto-screener-prod",
        headers=_SCREENER_HEADERS,
    )
    assert initial.status_code == 200
    assert initial.json()["revision"] == 0
    assert initial.json()["settings"]["mode"] == "off"
    assert initial.headers["etag"]

    global_write = await client.post(
        "/api/v1/admin/screener-review-settings",
        headers=_ADMIN_HEADERS,
        json=_payload("*", "shadow"),
    )
    assert global_write.status_code == 200, global_write.text
    global_revision = global_write.json()["revision"]

    fleet = await client.get(
        "/api/v1/screener/review-settings?instance_id=ditto-screener-fleet-abc",
        headers=_SCREENER_HEADERS,
    )
    assert fleet.json()["revision"] == global_revision
    assert fleet.json()["scope"] == "*"
    assert fleet.json()["settings"]["mode"] == "shadow"

    override = await client.post(
        "/api/v1/admin/screener-review-settings",
        headers=_ADMIN_HEADERS,
        json=_payload("ditto-screener-prod", "off"),
    )
    assert override.status_code == 200, override.text
    pet = await client.get(
        "/api/v1/screener/review-settings?instance_id=ditto-screener-prod",
        headers=_SCREENER_HEADERS,
    )
    assert pet.json()["revision"] == override.json()["revision"]
    assert pet.json()["scope"] == "ditto-screener-prod"
    assert pet.json()["settings"]["mode"] == "off"

    inherit = await client.post(
        "/api/v1/admin/screener-review-settings",
        headers=_ADMIN_HEADERS,
        json=_payload(
            "ditto-screener-prod",
            "inherit",
            expected=override.json()["revision"],
        ),
    )
    assert inherit.status_code == 200, inherit.text
    inherited = await client.get(
        "/api/v1/screener/review-settings?instance_id=ditto-screener-prod",
        headers=_SCREENER_HEADERS,
    )
    assert inherited.json()["revision"] == global_revision
    assert inherited.json()["scope"] == "*"
    assert inherited.json()["settings"]["mode"] == "shadow"


async def test_enforce_is_activatable_but_global_inherit_is_not(
    app: FastAPI,
    client: httpx.AsyncClient,
    settings_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, settings_maker)
    enforce = await client.post(
        "/api/v1/admin/screener-review-settings",
        headers=_ADMIN_HEADERS,
        json=_payload("*", "enforce"),
    )
    assert enforce.status_code == 200

    inherit = await client.post(
        "/api/v1/admin/screener-review-settings",
        headers=_ADMIN_HEADERS,
        json=_payload("*", "inherit", expected=enforce.json()["revision"]),
    )
    assert inherit.status_code == 409
    assert "exact worker scope" in inherit.text


async def test_l3_can_be_disabled_without_disabling_l2(
    app: FastAPI,
    client: httpx.AsyncClient,
    settings_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, settings_maker)
    payload = _payload("*", "enforce")
    assert isinstance(payload["settings"], dict)
    payload["settings"]["l3_enabled"] = False

    written = await client.post(
        "/api/v1/admin/screener-review-settings",
        headers=_ADMIN_HEADERS,
        json=payload,
    )
    assert written.status_code == 200, written.text
    effective = await client.get(
        "/api/v1/screener/review-settings?instance_id=ditto-screener-prod",
        headers=_SCREENER_HEADERS,
    )
    assert effective.status_code == 200
    assert effective.json()["settings"]["mode"] == "enforce"
    assert effective.json()["settings"]["l3_enabled"] is False


async def test_stale_parent_and_duplicate_model_chain_are_rejected(
    app: FastAPI,
    client: httpx.AsyncClient,
    settings_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, settings_maker)
    first = await client.post(
        "/api/v1/admin/screener-review-settings",
        headers=_ADMIN_HEADERS,
        json=_payload("ditto-screener-prod", "shadow"),
    )
    assert first.status_code == 200

    stale = await client.post(
        "/api/v1/admin/screener-review-settings",
        headers=_ADMIN_HEADERS,
        json=_payload("ditto-screener-prod", "off", expected=0),
    )
    assert stale.status_code == 409

    invalid = _payload("*", "shadow")
    invalid["settings"] = {
        "mode": "shadow",
        "l2_model": "moonshotai/kimi-k3",
        "l2_fallback_models": ["moonshotai/kimi-k3"],
    }
    duplicate = await client.post(
        "/api/v1/admin/screener-review-settings",
        headers=_ADMIN_HEADERS,
        json=invalid,
    )
    assert duplicate.status_code == 422


async def test_admin_read_is_authenticated_and_history_is_append_only(
    app: FastAPI,
    client: httpx.AsyncClient,
    settings_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, settings_maker)
    now = datetime.now(UTC)
    async with settings_maker() as session, session.begin():
        session.add(
            ScreenerHeartbeat(
                screener_hotkey=_SCREENER_HEADERS["X-Screener-Hotkey"],
                instance_id="ditto-screener-prod",
                software_version="0.14.1",
                protocol_version=4,
                policy_version=9,
                state="polling",
                first_seen_at=now,
                reported_at=now,
                seen_at=now,
                signature="ab" * 64,
                system_metrics={
                    "system_metrics": None,
                    "screening_progress": None,
                    "review_settings": {
                        "revision": 0,
                        "scope": "bootstrap",
                        "mode": "off",
                        "checksum": "cd" * 32,
                        "source": "bootstrap",
                    },
                },
            )
        )
    denied = await client.get("/api/v1/admin/screener-review-settings")
    assert denied.status_code == 401
    first = await client.post(
        "/api/v1/admin/screener-review-settings",
        headers=_ADMIN_HEADERS,
        json=_payload("*", "off"),
    )
    second = await client.post(
        "/api/v1/admin/screener-review-settings",
        headers=_ADMIN_HEADERS,
        json=_payload("*", "shadow", expected=first.json()["revision"]),
    )
    assert second.status_code == 200
    state = await client.get(
        "/api/v1/admin/screener-review-settings", headers=_ADMIN_HEADERS
    )
    assert state.status_code == 200
    assert len(state.json()["current"]) == 1
    assert state.json()["applied_instances"][0]["instance_id"] == (
        "ditto-screener-prod"
    )
    assert state.json()["applied_instances"][0]["source"] == "bootstrap"
    assert state.json()["applied_instances"][0]["fresh"] is True
    assert state.json()["applied_instances"][0]["matches_effective"] is False
    assert state.json()["applied_instances"][0]["expected_scope"] == "*"
    assert [item["revision"] for item in state.json()["history"]] == [
        second.json()["revision"],
        first.json()["revision"],
    ]
