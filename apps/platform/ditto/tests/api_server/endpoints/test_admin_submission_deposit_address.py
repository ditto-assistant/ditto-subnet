"""Contract tests for the Backroom submission deposit address control."""

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_server.dependencies import get_session
from ditto.db.models import UploadAdmissionReservation

pytestmark = pytest.mark.asyncio

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}
_OLD_ADDRESS = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
_NEW_ADDRESS = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(
        app.state.config,
        admin_api_token=_ADMIN_TOKEN,
        upload_payment_address=_OLD_ADDRESS,
    )

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


def _payload(*, expected_revision: int = 0) -> dict[str, object]:
    return {
        "expected_revision": expected_revision,
        "payment_address": _NEW_ADDRESS,
        "reason": "move submission earnings to the treasury coldkey",
        "actor": "operator@omniaura.ai",
        "confirmation": f"SET SUBMISSION DEPOSIT ADDRESS {_NEW_ADDRESS}",
    }


async def test_defaults_to_boot_address_and_appends_audited_revision(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)

    initial = await client.get(
        "/api/v1/admin/submission-deposit-address", headers=_HEADERS
    )
    assert initial.status_code == 200
    assert initial.json()["current"] == {
        "revision": 0,
        "parent_revision": 0,
        "payment_address": _OLD_ADDRESS,
        "reason": "Boot-configured submission deposit address",
        "actor": "platform",
        "created_at": None,
    }

    updated = await client.post(
        "/api/v1/admin/submission-deposit-address",
        headers=_HEADERS,
        json=_payload(),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["current"]["payment_address"] == _NEW_ADDRESS
    assert updated.json()["current"]["actor"] == "operator@omniaura.ai"

    quote = await client.get("/api/v1/upload/eval-pricing")
    assert quote.status_code == 200, quote.text
    assert quote.json()["send_address"] == _NEW_ADDRESS

    accounting = await client.get("/api/v1/admin/miner-fees", headers=_HEADERS)
    assert accounting.status_code == 200, accounting.text
    assert accounting.json()["payment_address"] == _NEW_ADDRESS


async def test_rotation_snapshots_preexisting_reservations(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    async with session_maker() as session, session.begin():
        session.add(
            UploadAdmissionReservation(
                miner_coldkey="5Coldkey",
                token=UUID("11111111-1111-4111-8111-111111111111"),
                miner_hotkey="5Hotkey",
                sha256="a" * 64,
                settings_revision=1,
                cooldown_seconds=3600,
                fee_amount_rao=40_000_000,
                payment_send_address=None,
                expires_at=datetime(2027, 8, 13, 12, 0, tzinfo=UTC),
            )
        )

    response = await client.post(
        "/api/v1/admin/submission-deposit-address",
        headers=_HEADERS,
        json=_payload(),
    )
    assert response.status_code == 200, response.text

    async with session_maker() as session:
        reservation = await session.get(UploadAdmissionReservation, "5Coldkey")
        assert reservation is not None
        assert reservation.payment_send_address == _OLD_ADDRESS


async def test_rejects_stale_revision_wrong_confirmation_and_invalid_address(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    first = await client.post(
        "/api/v1/admin/submission-deposit-address",
        headers=_HEADERS,
        json=_payload(),
    )
    assert first.status_code == 200

    stale = await client.post(
        "/api/v1/admin/submission-deposit-address",
        headers=_HEADERS,
        json=_payload(expected_revision=0),
    )
    assert stale.status_code == 409

    wrong_confirmation = _payload(expected_revision=first.json()["current"]["revision"])
    wrong_confirmation["confirmation"] = "SET SUBMISSION DEPOSIT ADDRESS wrong"
    wrong = await client.post(
        "/api/v1/admin/submission-deposit-address",
        headers=_HEADERS,
        json=wrong_confirmation,
    )
    assert wrong.status_code == 409

    invalid = _payload(expected_revision=first.json()["current"]["revision"])
    invalid["payment_address"] = _NEW_ADDRESS[:-1] + "Y"
    malformed = await client.post(
        "/api/v1/admin/submission-deposit-address",
        headers=_HEADERS,
        json=invalid,
    )
    assert malformed.status_code == 422
