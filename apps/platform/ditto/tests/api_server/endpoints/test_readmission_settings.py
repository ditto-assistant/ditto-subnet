"""The readmission announcement setting and its read-only exposure (SN-485).

``readmission_announced_at`` is null by default, which keeps readmission
inactive. It carries forward on every later revision unless an operator names
it, and the admin settings view and the public memory terms both report the
computed activation state with every reason it is inactive.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.submission_settings import (
    readmission_announcement_confirmation,
)
from ditto.api_server.dependencies import get_session

pytestmark = pytest.mark.asyncio

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}
_URL = "/api/v1/admin/submission-settings"


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_ADMIN_TOKEN)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


async def _current(client: httpx.AsyncClient) -> dict[str, object]:
    resp = await client.get(_URL, headers=_HEADERS)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _write(
    revision: int,
    *,
    fee: int,
    announcement: object = ...,
    confirmation: str | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {
        "expected_revision": revision,
        "cooldown_seconds": 3600,
        "fee_amount_rao": fee,
        "reason": "record the readmission fee announcement",
        "actor": "operator@example.com",
    }
    phrase = f"SET SUBMISSION COOLDOWN 3600 SECONDS FEE {fee} RAO"
    if announcement is None:
        body["clear_readmission_announcement"] = True
        phrase += readmission_announcement_confirmation(None)
    elif announcement is not ...:
        body["readmission_announced_at"] = announcement
        phrase += readmission_announcement_confirmation(
            datetime.fromisoformat(str(announcement))
        )
    body["confirmation"] = confirmation if confirmation is not None else phrase
    return body


async def test_default_is_unset_and_inactive(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    view = await _current(client)
    current = view["current"]
    assert isinstance(current, dict)
    assert current["readmission_announced_at"] is None
    readmission = view["readmission"]
    assert isinstance(readmission, dict)
    assert readmission["active"] is False
    assert readmission["inactive_reasons"][0] == "announcement_unset"
    assert readmission["announced_at"] is None
    assert readmission["effective_at"] is None
    assert readmission["min_bench_version"] == 19
    assert readmission["notice_period_seconds"] == 7 * 86400
    # The price is the upload fee, never a setting of its own.
    assert readmission["fee_amount_rao"] == current["fee_amount_rao"]
    assert readmission["fee_source"] == "upload_default"


async def test_announcement_needs_its_phrase_and_carries_forward(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    revision = (await _current(client))["current"]["revision"]  # type: ignore[index]
    announced = datetime.now(UTC).replace(microsecond=0)
    stamp = announced.isoformat()

    without_phrase = await client.post(
        _URL,
        headers=_HEADERS,
        json=_write(
            revision,
            fee=75_000_000,
            announcement=stamp,
            confirmation="SET SUBMISSION COOLDOWN 3600 SECONDS FEE 75000000 RAO",
        ),
    )
    assert without_phrase.status_code == 409
    assert "READMISSION ANNOUNCED" in without_phrase.text

    recorded = await client.post(
        _URL,
        headers=_HEADERS,
        json=_write(revision, fee=75_000_000, announcement=stamp),
    )
    assert recorded.status_code == 200, recorded.text
    assert datetime.fromisoformat(recorded.json()["readmission_announced_at"]) == (
        announced
    )

    view = await _current(client)
    readmission = view["readmission"]
    assert isinstance(readmission, dict)
    assert readmission["active"] is False
    assert "announcement_unset" not in readmission["inactive_reasons"]
    assert "notice_period" in readmission["inactive_reasons"]
    assert datetime.fromisoformat(readmission["effective_at"]) == (
        announced + timedelta(days=7)
    )

    # A write that sends the whole model with a null announcement keeps it.
    nulled = _write(revision + 1, fee=75_000_000)
    nulled["readmission_announced_at"] = None
    kept = await client.post(_URL, headers=_HEADERS, json=nulled)
    assert kept.status_code == 200, kept.text
    assert datetime.fromisoformat(kept.json()["readmission_announced_at"]) == (
        announced
    )
    revision += 1

    # A later cooldown/fee write that never names the field keeps it, and
    # the legacy phrase still works for such a write.
    later = await client.post(
        _URL,
        headers=_HEADERS,
        json=_write(revision + 1, fee=80_000_000),
    )
    assert later.status_code == 200, later.text
    assert datetime.fromisoformat(later.json()["readmission_announced_at"]) == (
        announced
    )
    readmission = (await _current(client))["readmission"]
    assert isinstance(readmission, dict)
    assert readmission["fee_amount_rao"] == 80_000_000

    cleared = await client.post(
        _URL,
        headers=_HEADERS,
        json=_write(revision + 2, fee=80_000_000, announcement=None),
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["readmission_announced_at"] is None
    readmission = (await _current(client))["readmission"]
    assert isinstance(readmission, dict)
    assert readmission["inactive_reasons"][0] == "announcement_unset"


@pytest.mark.parametrize("case", ["backdated", "too_far_ahead", "naive"])
async def test_announcement_outside_the_window_is_refused(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    case: str,
) -> None:
    now = datetime.now(UTC)
    announcement = {
        "backdated": (now - timedelta(days=2)).isoformat(),
        "too_far_ahead": (now + timedelta(days=45)).isoformat(),
        "naive": now.replace(tzinfo=None).isoformat(),
    }[case]
    _install(app, session_maker)
    revision = (await _current(client))["current"]["revision"]  # type: ignore[index]
    resp = await client.post(
        _URL,
        headers=_HEADERS,
        json=_write(
            revision,
            fee=75_000_000,
            announcement=announcement,
            confirmation="ignored: the window is checked first",
        ),
    )
    assert resp.status_code == 422, resp.text
    assert (await _current(client))["current"]["readmission_announced_at"] is None  # type: ignore[index]


async def test_public_memory_terms_report_the_readmission_state(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    resp = await client.get("/api/v1/public/submission-terms")
    assert resp.status_code == 200, resp.text
    terms = {row["competition"]: row for row in resp.json()["competitions"]}
    readmission = terms["memory"]["readmission"]
    assert readmission["activation"]["active"] is False
    assert readmission["activation"]["inactive_reasons"][0] == "announcement_unset"
    assert (
        readmission["activation"]["fee_amount_rao"]
        == (terms["memory"]["fee"]["amount_rao"])
    )
    rules = " ".join(readmission["rules"])
    assert "upload fee" in rules
    assert "30 days" in rules and "never refunds" in rules
    assert "always free" in rules and "re-tests" in rules
    assert terms["generator"]["readmission"] is None
    # Unannounced: the cost basis is not measured at all.
    assert readmission["cost_basis"] is None


async def test_setting_and_clearing_together_is_refused(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    revision = (await _current(client))["current"]["revision"]  # type: ignore[index]
    body = _write(revision, fee=75_000_000, announcement=datetime.now(UTC).isoformat())
    body["clear_readmission_announcement"] = True
    resp = await client.post(_URL, headers=_HEADERS, json=body)
    assert resp.status_code == 422, resp.text


async def test_terms_survive_a_failed_cost_basis_measurement(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once announced, a failed cost-basis query still serves the terms."""
    from unittest.mock import AsyncMock

    from sqlalchemy.exc import OperationalError

    from ditto.api_server.endpoints import public_submission_terms

    _install(app, session_maker)
    revision = (await _current(client))["current"]["revision"]  # type: ignore[index]
    stamp = datetime.now(UTC).replace(microsecond=0).isoformat()
    recorded = await client.post(
        _URL,
        headers=_HEADERS,
        json=_write(revision, fee=75_000_000, announcement=stamp),
    )
    assert recorded.status_code == 200, recorded.text
    monkeypatch.setattr(
        public_submission_terms,
        "readmission_cost_basis",
        AsyncMock(side_effect=OperationalError("select", {}, Exception("down"))),
    )
    resp = await client.get("/api/v1/public/submission-terms/memory")
    assert resp.status_code == 200, resp.text
    readmission = resp.json()["readmission"]
    assert readmission["cost_basis"] is None
    assert readmission["activation"]["announced_at"] is not None
