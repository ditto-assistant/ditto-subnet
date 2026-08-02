"""Contract and concurrency tests for public source-release settings."""

from collections.abc import AsyncIterator
from dataclasses import replace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)

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


def _payload(hours: int, expected: int = 0) -> dict[str, object]:
    return {
        "expected_revision": expected,
        "embargo_hours": hours,
        "reason": f"stage public source release at {hours} hours",
        "actor": "operator@example.com",
        "confirmation": f"SET SOURCE EMBARGO {hours} HOURS",
    }


async def test_defaults_to_48_then_shortens_with_audited_revisions(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    initial = await client.get(
        "/api/v1/admin/artifact-release-settings", headers=_HEADERS
    )
    assert initial.status_code == 200
    # The 48-hour default is served from the migration-seeded audit chain, not
    # from the built-in fallback: the first artifact-release migration seeds a
    # row and the table is append-only, so `revision 0 / actor "platform"` is
    # a state production is never in.
    baseline = initial.json()
    assert baseline["current"]["embargo_hours"] == 48
    assert baseline["current"]["actor"] == "migration"
    assert baseline["current"]["created_at"] is not None
    head = baseline["current"]["revision"]
    seeded_hours = [row["embargo_hours"] for row in baseline["history"]]

    twelve = await client.post(
        "/api/v1/admin/artifact-release-settings",
        headers=_HEADERS,
        json=_payload(12, expected=head),
    )
    assert twelve.status_code == 200, twelve.text
    revision = twelve.json()["revision"]

    six = await client.post(
        "/api/v1/admin/artifact-release-settings",
        headers=_HEADERS,
        json=_payload(6, expected=revision),
    )
    assert six.status_code == 200, six.text
    current = await client.get(
        "/api/v1/admin/artifact-release-settings", headers=_HEADERS
    )
    assert current.json()["current"]["embargo_hours"] == 6
    assert [row["embargo_hours"] for row in current.json()["history"]] == [
        6,
        12,
        *seeded_hours,
    ]


async def test_lengthens_shortens_and_rejects_stale_or_wrong_confirmation(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    head = (
        await client.get("/api/v1/admin/artifact-release-settings", headers=_HEADERS)
    ).json()["current"]["revision"]
    first = await client.post(
        "/api/v1/admin/artifact-release-settings",
        headers=_HEADERS,
        json=_payload(12, expected=head),
    )
    assert first.status_code == 200, first.text
    revision = first.json()["revision"]

    # Lengthening is now allowed, up to the one-year ceiling.
    increase = await client.post(
        "/api/v1/admin/artifact-release-settings",
        headers=_HEADERS,
        json=_payload(24, expected=revision),
    )
    assert increase.status_code == 200, increase.text
    revision = increase.json()["revision"]

    # 48 is the default, not a limit: a window past it is an ordinary write.
    beyond_default = await client.post(
        "/api/v1/admin/artifact-release-settings",
        headers=_HEADERS,
        json=_payload(168, expected=revision),
    )
    assert beyond_default.status_code == 200, beyond_default.text
    assert beyond_default.json()["embargo_hours"] == 168

    # A month, still an ordinary write.
    month = await client.post(
        "/api/v1/admin/artifact-release-settings",
        headers=_HEADERS,
        json=_payload(720, expected=beyond_default.json()["revision"]),
    )
    assert month.status_code == 200, month.text
    assert month.json()["embargo_hours"] == 720

    # A year: one of the four options under discussion, so the range has to
    # reach it. A ceiling that stopped at 30 days would mean another migration
    # and another deploy the moment that option was chosen.
    ceiling = await client.post(
        "/api/v1/admin/artifact-release-settings",
        headers=_HEADERS,
        json=_payload(8760, expected=month.json()["revision"]),
    )
    assert ceiling.status_code == 200, ceiling.text
    assert ceiling.json()["embargo_hours"] == 8760

    # One hour past the ceiling is rejected by the request contract. Past a
    # year the honest value is `never`, which has its own representation.
    over = await client.post(
        "/api/v1/admin/artifact-release-settings",
        headers=_HEADERS,
        json=_payload(8761, expected=ceiling.json()["revision"]),
    )
    assert over.status_code == 422

    # And one hour below the floor, so widening the ceiling did not quietly
    # loosen the other end of the range.
    under = await client.post(
        "/api/v1/admin/artifact-release-settings",
        headers=_HEADERS,
        json=_payload(5, expected=ceiling.json()["revision"]),
    )
    assert under.status_code == 422

    stale = await client.post(
        "/api/v1/admin/artifact-release-settings",
        headers=_HEADERS,
        json=_payload(6, expected=0),
    )
    assert stale.status_code == 409
    assert "refresh before applying" in stale.text

    wrong = _payload(6, expected=ceiling.json()["revision"])
    wrong["confirmation"] = "SET SOURCE EMBARGO 12 HOURS"
    confirmation = await client.post(
        "/api/v1/admin/artifact-release-settings",
        headers=_HEADERS,
        json=wrong,
    )
    assert confirmation.status_code == 409
    assert "must be exactly" in confirmation.text


def _never(expected: int) -> dict[str, object]:
    return {
        "expected_revision": expected,
        "disclosure": "never",
        # Required and in range even under `never`. It is retained, not used,
        # so returning to `public` restores an agreed window rather than
        # forcing one to be invented during the reversal.
        "embargo_hours": 48,
        "reason": "subnet policy: source is not published",
        "actor": "operator@example.com",
        "confirmation": "SET SOURCE DISCLOSURE NEVER",
    }


async def test_never_is_a_setting_the_board_can_hold_and_come_back_from(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The four options Peyton is choosing between must all be expressible.

    Short window, long window and a year are hours. `never` is not any number
    of hours, so it is a second field on the same revision -- one decision,
    one write, one confirmation, one audit row.
    """
    _install(app, session_maker)
    head = (
        await client.get("/api/v1/admin/artifact-release-settings", headers=_HEADERS)
    ).json()["current"]
    # The board starts where the subnet already is, and this change does not
    # move it: shipping the mechanism must not ship a policy.
    assert head["disclosure"] == "public"
    assert head["embargo_hours"] == 48

    applied = await client.post(
        "/api/v1/admin/artifact-release-settings",
        headers=_HEADERS,
        json=_never(head["revision"]),
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["disclosure"] == "never"
    assert applied.json()["embargo_hours"] == 48

    current = (
        await client.get("/api/v1/admin/artifact-release-settings", headers=_HEADERS)
    ).json()
    assert current["current"]["disclosure"] == "never"

    # Reversible, and the reversal is an ordinary audited revision rather than
    # a special case: the audit trail is the whole guarantee here.
    back = await client.post(
        "/api/v1/admin/artifact-release-settings",
        headers=_HEADERS,
        json={
            "expected_revision": applied.json()["revision"],
            "disclosure": "public",
            "embargo_hours": 72,
            "reason": "subnet agreed a three-day window instead",
            "actor": "peyton@omniaura.ai",
            "confirmation": "SET SOURCE EMBARGO 72 HOURS",
        },
    )
    assert back.status_code == 200, back.text
    assert back.json()["disclosure"] == "public"

    history = (
        await client.get("/api/v1/admin/artifact-release-settings", headers=_HEADERS)
    ).json()["history"]
    assert [row["disclosure"] for row in history[:2]] == ["public", "never"]
    assert history[0]["actor"] == "peyton@omniaura.ai"
    assert history[0]["reason"] == "subnet agreed a three-day window instead"


async def test_never_needs_its_own_phrase_and_rejects_an_unknown_policy(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    head = (
        await client.get("/api/v1/admin/artifact-release-settings", headers=_HEADERS)
    ).json()["current"]["revision"]

    # The hours phrase must not apply a `never` policy. These two are one
    # keystroke apart in intent and a world apart in effect.
    borrowed = {**_never(head), "confirmation": "SET SOURCE EMBARGO 48 HOURS"}
    assert (
        await client.post(
            "/api/v1/admin/artifact-release-settings",
            headers=_HEADERS,
            json=borrowed,
        )
    ).status_code == 409

    # And a policy the enum does not know is a 422, not a silently stored
    # string that the release gate would later read as "not public".
    unknown = {**_never(head), "disclosure": "private"}
    assert (
        await client.post(
            "/api/v1/admin/artifact-release-settings",
            headers=_HEADERS,
            json=unknown,
        )
    ).status_code == 422


async def test_omitting_disclosure_keeps_the_write_public(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The strip hazard, from the platform side.

    Backroom boards submit whole policies and `z.object` drops what it does not
    declare, so a console that never learned about `disclosure` would omit it
    on every embargo save. The safe direction for that omission is `public` --
    the status quo, and a visible one -- rather than inheriting `never` and
    silently keeping the subnet dark after an unrelated window change.
    """
    _install(app, session_maker)
    head = (
        await client.get("/api/v1/admin/artifact-release-settings", headers=_HEADERS)
    ).json()["current"]["revision"]
    applied = await client.post(
        "/api/v1/admin/artifact-release-settings",
        headers=_HEADERS,
        json=_never(head),
    )
    assert applied.json()["disclosure"] == "never"

    legacy = await client.post(
        "/api/v1/admin/artifact-release-settings",
        headers=_HEADERS,
        json=_payload(12, expected=applied.json()["revision"]),
    )
    assert legacy.status_code == 200, legacy.text
    assert legacy.json()["disclosure"] == "public"
