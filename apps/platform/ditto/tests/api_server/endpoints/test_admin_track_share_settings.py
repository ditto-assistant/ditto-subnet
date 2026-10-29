"""Contract tests for the governed per-track emission split."""

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.track_share_settings import (
    TrackShareSettings,
    track_shares_confirmation,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.track_share_settings import (
    settings_from_row,
)
from ditto.db.models import ValidatorHeartbeat

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}
_URL = "/api/v1/admin/track-share-settings"
_ACTIVATION = {"memory": 9000, "generator": 1000}


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_ADMIN_TOKEN)
    app.state.session_maker = maker

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    app.state.track_share_settings.invalidate()


def _payload(
    shares: dict[str, int] | None,
    *,
    expected_revision: int = 0,
    confirmation: str | None = None,
) -> dict[str, object]:
    return {
        "expected_revision": expected_revision,
        "scope": "*",
        "settings": {"track_shares_bps": shares},
        "reason": "owner-approved generator activation",
        "actor": "operator@example.com",
        "confirmation": confirmation
        if confirmation is not None
        else track_shares_confirmation(shares),
    }


async def _heartbeat(maker: async_sessionmaker[AsyncSession], protocol: int) -> None:
    now = datetime.now(UTC)
    async with maker() as session, session.begin():
        session.add(
            ValidatorHeartbeat(
                validator_hotkey="5" + "V" * 47,
                software_version="0.55.0",
                protocol_version=protocol,
                code_digest="ab" * 32,
                state="idle",
                reported_at=now,
                seen_at=now,
                signature="cd" * 64,
            )
        )


@pytest.mark.parametrize(
    "shares",
    [
        {},
        {"generator": 1000},
        {"memory": 9000, "router": 1000},
        {"memory": 9500, "generator": 1000},
        {"memory": 9000, "generator": 1000, "bogus": 0},
        {"memory": 9000, "generator": True},
        {"memory": 0},
    ],
)
def test_incoherent_splits_are_refused(shares: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        TrackShareSettings.model_validate({"track_shares_bps": shares})


def test_shadow_tracks_may_be_named_at_zero_and_order_is_canonical() -> None:
    settings = TrackShareSettings.model_validate(
        {"track_shares_bps": {"generator": 1000, "router": 0, "memory": 9000}}
    )
    assert list(settings.track_shares_bps or {}) == ["memory", "router", "generator"]


async def test_default_is_unset_and_a_split_is_audited(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    initial = await client.get(_URL, headers=_HEADERS)
    assert initial.status_code == 200, initial.text
    effective = initial.json()["effective"]
    assert effective["settings"] == {"track_shares_bps": None}
    assert effective["source"] == "default"
    assert effective["in_force_bps"] == {"memory": 10000}
    assert effective["unallocated_bps"] == 0
    assert effective["served"] is False
    assert effective["required_protocol"] == 33
    assert effective["generator_mode"] == "off"

    wrong = await client.post(
        _URL,
        headers=_HEADERS,
        json=_payload(_ACTIVATION, confirmation="APPLY TRACK SHARES memory=9500"),
    )
    assert wrong.status_code == 409
    assert "APPLY TRACK SHARES memory=9000 generator=1000" in wrong.text
    stale = await client.post(
        _URL, headers=_HEADERS, json=_payload(_ACTIVATION, expected_revision=4)
    )
    assert stale.status_code == 409
    unpaid = await client.post(
        _URL, headers=_HEADERS, json=_payload({"memory": 9000, "router": 1000})
    )
    assert unpaid.status_code == 422

    applied = await client.post(_URL, headers=_HEADERS, json=_payload(_ACTIVATION))
    assert applied.status_code == 200, applied.text
    assert applied.json()["revision"] == 1
    assert applied.json()["settings"]["track_shares_bps"] == _ACTIVATION

    # Configured, but no protocol-33 weight setter yet: not served.
    await _heartbeat(session_maker, 32)
    held = (await client.get(_URL, headers=_HEADERS)).json()["effective"]
    assert held["served"] is False
    assert held["in_force_bps"] == {"memory": 10000}
    assert held["confirmation_hint"] == "APPLY TRACK SHARES memory=9000 generator=1000"

    async with session_maker() as session, session.begin():
        heartbeat = await session.get(ValidatorHeartbeat, "5" + "V" * 47)
        assert heartbeat is not None
        heartbeat.protocol_version = 33
    live = (await client.get(_URL, headers=_HEADERS)).json()
    assert live["effective"]["served"] is True
    assert live["effective"]["in_force_bps"] == _ACTIVATION
    assert [row["revision"] for row in live["history"]] == [1]

    cleared = await client.post(
        _URL, headers=_HEADERS, json=_payload(None, expected_revision=1)
    )
    assert cleared.status_code == 200, cleared.text
    after = (await client.get(_URL, headers=_HEADERS)).json()["effective"]
    assert after["settings"]["track_shares_bps"] is None
    assert after["in_force_bps"] == {"memory": 10000}


async def test_write_requires_admin(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    assert (await client.get(_URL)).status_code == 401
    assert (await client.post(_URL, json=_payload(_ACTIVATION))).status_code == 401


async def test_a_malformed_revision_degrades_to_unset() -> None:
    row = SimpleNamespace(revision=7, settings={"track_shares_bps": {"generator": 1}})
    assert settings_from_row(row).track_shares_bps is None  # type: ignore[arg-type]
    assert settings_from_row(None).track_shares_bps is None


async def test_unreadable_fleet_readiness_reports_the_split_as_unknown(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy.exc import OperationalError

    import ditto.api_server.endpoints.admin_track_share_settings as endpoint

    _install(app, session_maker)
    applied = await client.post(_URL, headers=_HEADERS, json=_payload(_ACTIVATION))
    assert applied.status_code == 200, applied.text

    async def _boom(*_args: object, **_kwargs: object) -> bool:
        raise OperationalError("SELECT ...", {}, Exception("db down"))

    monkeypatch.setattr(endpoint, "live_weight_setter_fleet_supports_protocol", _boom)
    effective = (await client.get(_URL, headers=_HEADERS)).json()["effective"]
    assert effective["served"] is None
    assert effective["in_force_bps"] is None
    assert effective["unallocated_bps"] is None
