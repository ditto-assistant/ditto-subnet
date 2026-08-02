"""Contract tests for platform-owned submission cooldown settings."""

from collections.abc import AsyncIterator
from dataclasses import replace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_server.dependencies import get_session

pytestmark = pytest.mark.asyncio

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_ADMIN_TOKEN)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


# Migration f4b8c2d91a70 seeds one revision (revision=1, parent_revision=0,
# cooldown_seconds=3600, actor="migration"), so the first operator write on a
# real database expects revision 1. The suite previously defaulted to 0 because
# `Base.metadata.create_all` left the table empty -- a baseline that only
# reaches `effective_submission_settings`'s `latest is None` fallback, which
# production has not been able to hit since that migration landed.
def _payload(
    seconds: int, expected: int = 1, fee_amount_rao: int = 40_000_000
) -> dict[str, object]:
    return {
        "expected_revision": expected,
        "cooldown_seconds": seconds,
        "fee_amount_rao": fee_amount_rao,
        "reason": f"set miner submission cooldown to {seconds} seconds",
        "actor": "operator@example.com",
        "confirmation": (
            f"SET SUBMISSION COOLDOWN {seconds} SECONDS FEE {fee_amount_rao} RAO"
        ),
    }


async def test_defaults_to_one_hour_and_appends_audited_revision(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    initial = await client.get("/api/v1/admin/submission-settings", headers=_HEADERS)
    assert initial.status_code == 200
    assert initial.json()["current"]["cooldown_seconds"] == 3600
    assert initial.json()["current"]["fee_amount_rao"] == 40_000_000

    updated = await client.post(
        "/api/v1/admin/submission-settings",
        headers=_HEADERS,
        json=_payload(1800, fee_amount_rao=50_000_000),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["cooldown_seconds"] == 1800
    assert updated.json()["fee_amount_rao"] == 50_000_000
    assert updated.json()["actor"] == "operator@example.com"


async def test_rejects_stale_revision_wrong_confirmation_and_unsafe_bounds(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    first = await client.post(
        "/api/v1/admin/submission-settings",
        headers=_HEADERS,
        json=_payload(1800),
    )
    assert first.status_code == 200

    stale = await client.post(
        "/api/v1/admin/submission-settings",
        headers=_HEADERS,
        json=_payload(1200, expected=0),
    )
    assert stale.status_code == 409

    wrong = _payload(1200, expected=first.json()["revision"])
    wrong["confirmation"] = "SET SUBMISSION COOLDOWN 3600 SECONDS"
    confirmation = await client.post(
        "/api/v1/admin/submission-settings", headers=_HEADERS, json=wrong
    )
    assert confirmation.status_code == 409

    too_short = await client.post(
        "/api/v1/admin/submission-settings",
        headers=_HEADERS,
        json=_payload(59, expected=first.json()["revision"]),
    )
    assert too_short.status_code == 422
