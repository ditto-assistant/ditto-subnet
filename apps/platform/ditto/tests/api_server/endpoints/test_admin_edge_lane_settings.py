"""Contract tests for the Ditto Edge shadow lane settings (SN-481)."""

from collections.abc import AsyncIterator
from dataclasses import replace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.edge_lane import EdgeLaneSettings
from ditto.api_server.dependencies import get_session
from ditto.api_server.edge_lane_settings import settings_from_row
from ditto.db.models import EdgeLaneSettingsRevision as RevisionRow

pytestmark = pytest.mark.asyncio

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}
_URL = "/api/v1/admin/edge-lane-settings"


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_ADMIN_TOKEN)
    app.state.session_maker = maker

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    app.state.edge_lane_settings.invalidate()


def _payload(
    *,
    expected_revision: int = 0,
    confirmation: str = "APPLY EDGE LANE SETTINGS",
    **settings: object,
) -> dict[str, object]:
    return {
        "expected_revision": expected_revision,
        "scope": "*",
        "settings": {"mode": "shadow", **settings},
        "reason": "open the SN-481 shadow lane for team Macs",
        "actor": "operator@example.com",
        "confirmation": confirmation,
    }


async def test_default_is_off_and_never_weight_eligible(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    response = await client.get(_URL, headers=_HEADERS)
    assert response.status_code == 200, response.text
    effective = response.json()["effective"]
    # Deploying the lane must change nothing: it is off until a revision opens it.
    assert effective["settings"]["mode"] == "off"
    assert effective["source"] == "default"
    assert effective["revision"] == 0
    assert effective["weight_eligible"] is False


async def test_a_shadow_revision_is_audited_and_takes_effect(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    applied = await client.post(
        _URL, headers=_HEADERS, json=_payload(replicas=2, challenges_per_window=12)
    )
    assert applied.status_code == 200, applied.text
    body = applied.json()
    assert body["revision"] == 1
    assert body["parent_revision"] == 0
    assert body["settings"]["mode"] == "shadow"
    assert body["settings"]["replicas"] == 2
    assert body["actor"] == "operator@example.com"
    assert len(body["checksum"]) == 64

    effective = (await client.get(_URL, headers=_HEADERS)).json()["effective"]
    assert effective["source"] == "revision"
    assert effective["revision"] == 1
    assert effective["settings"]["challenges_per_window"] == 12
    resolved = await app.state.edge_lane_settings.resolve(session_maker)
    assert resolved.settings.mode == "shadow"
    assert resolved.revision == 1


async def test_writes_need_the_phrase_the_current_revision_and_global_scope(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    wrong_phrase = await client.post(
        _URL, headers=_HEADERS, json=_payload(confirmation="APPLY")
    )
    assert wrong_phrase.status_code == 409
    assert (
        await client.post(_URL, headers=_HEADERS, json=_payload())
    ).status_code == 200
    stale = await client.post(
        _URL, headers=_HEADERS, json=_payload(expected_revision=0)
    )
    assert stale.status_code == 409
    scoped = {**_payload(expected_revision=1), "scope": "node"}
    assert (await client.post(_URL, headers=_HEADERS, json=scoped)).status_code == 422


@pytest.mark.parametrize(
    "settings",
    [
        {"mode": "enforce"},
        {"retrieval_share": 0.7, "revoked_share": 0.5},
        {"lease_seconds": 60, "poll_interval_seconds": 60},
        {"min_challenges": 13, "challenges_per_window": 12},
        {"max_object_bytes": 51 * 1024 * 1024},
    ],
)
async def test_incoherent_policies_are_refused(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    settings: dict[str, object],
) -> None:
    _install(app, session_maker)
    payload = _payload()
    payload["settings"] = {"mode": "shadow", **settings}
    response = await client.post(_URL, headers=_HEADERS, json=payload)
    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reason", "  short   "),
        ("reason", " \t\n" * 4),
        ("actor", "    "),
        ("actor", " " + "a" * 121 + " "),
    ],
)
async def test_padded_audit_text_is_refused_before_the_insert(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    field: str,
    value: str,
) -> None:
    _install(app, session_maker)
    response = await client.post(
        _URL, headers=_HEADERS, json={**_payload(), field: value}
    )
    # A validation error, not a retryable "changed concurrently" conflict.
    assert response.status_code == 422, response.text
    effective = (await client.get(_URL, headers=_HEADERS)).json()["effective"]
    assert effective["revision"] == 0


async def test_audit_text_is_stored_trimmed(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    payload = {**_payload(), "reason": "  open the shadow lane \n", "actor": " ops "}
    applied = await client.post(_URL, headers=_HEADERS, json=payload)
    assert applied.status_code == 200, applied.text
    assert applied.json()["reason"] == "open the shadow lane"
    assert applied.json()["actor"] == "ops"


async def test_admin_token_is_required(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    assert (await client.get(_URL)).status_code in (401, 403)
    assert (await client.post(_URL, json=_payload())).status_code in (401, 403)


async def test_an_unreadable_revision_leaves_the_lane_off() -> None:
    row = RevisionRow(settings={"mode": "enforce", "replicas": 0})
    assert settings_from_row(row) == EdgeLaneSettings()
    assert settings_from_row(row).mode == "off"


async def test_ledger_rows_cannot_be_weight_eligible(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    # The shadow pin: no code path can store a payable edge ledger row.
    async with session_maker() as session:
        await session.execute(
            text(
                "INSERT INTO edge_window_closures (window_index, settings_revision, "
                "node_count) VALUES (1, 1, 1)"
            )
        )
        await session.commit()
        with pytest.raises(
            IntegrityError, match="edge_ledger_windows_shadow_only_check"
        ):
            await session.execute(
                text(
                    "INSERT INTO edge_ledger_windows (window_index, node_id, operator, "
                    "scheduled, passed, failed, missed, refilled, refused, "
                    "revocation_checks, revocation_violations, held_gib_hours, "
                    "availability, eligible, verified_units, hypothetical_share, "
                    "reasons, settings_revision, weight_eligible) VALUES (1, "
                    "'edn_00000000000000000000000000000000', 'op', 6, 6, 0, 0, 0, 0, "
                    "0, 0, 1.0, 1.0, true, 1.0, 1.0, '[]', 1, true)"
                )
            )
