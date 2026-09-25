"""Revisioned, audited, publicly inspectable miner submission pricing (#472).

Runs against the real migrated Postgres, whose chain seeds revision 1
(cooldown 3600 s, fee 40,000,000 rao, ``fixed_tao``).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.submission_settings import (
    MAX_SUBMISSION_FEE_RAO,
    MIN_SUBMISSION_FEE_RAO,
    format_rao_as_tao,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.pricing.errors import UnsupportedFeeDenominationError
from ditto.db.models import SubmissionSettingsRevision, UploadAdmissionReservation
from ditto.db.queries.submission_settings import (
    UPLOAD_ADMISSION_TTL,
    effective_submission_settings,
    require_supported_fee_denomination,
    reserve_upload_admission,
)

pytestmark = pytest.mark.asyncio

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}
_SETTINGS = "/api/v1/admin/submission-settings"
_PREVIEW = "/api/v1/admin/submission-settings/preview"
_PUBLIC = "/api/v1/public/submission-fee"
_PAYMENT_ADDRESS = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
_GENESIS_FEE = 40_000_000


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_ADMIN_TOKEN)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


def _payload(
    *,
    expected: int,
    fee_amount_rao: int,
    cooldown_seconds: int = 3600,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "expected_revision": expected,
        "cooldown_seconds": cooldown_seconds,
        "fee_amount_rao": fee_amount_rao,
        "reason": "measured platform cost and spam pressure",
        "actor": "operator@example.com",
        "confirmation": (
            f"SET SUBMISSION COOLDOWN {cooldown_seconds} SECONDS "
            f"FEE {fee_amount_rao} RAO"
        ),
        **extra,
    }


async def _apply(client: httpx.AsyncClient, **kwargs: Any) -> dict[str, Any]:
    response = await client.post(_SETTINGS, headers=_HEADERS, json=_payload(**kwargs))
    assert response.status_code == 200, response.text
    return response.json()


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


async def _revision_rows(
    maker: async_sessionmaker[AsyncSession],
) -> list[tuple[int, int, int, str]]:
    async with maker() as session:
        rows = await session.execute(
            select(
                SubmissionSettingsRevision.revision,
                SubmissionSettingsRevision.cooldown_seconds,
                SubmissionSettingsRevision.fee_amount_rao,
                SubmissionSettingsRevision.fee_denomination,
            ).order_by(SubmissionSettingsRevision.revision)
        )
        return [tuple(row) for row in rows.all()]


# --- Defaults: no operator action reproduces current behaviour --------------


async def test_seeded_policy_is_the_current_fixed_tao_fee(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    assert await _revision_rows(session_maker) == [(1, 3600, _GENESIS_FEE, "fixed_tao")]
    async with session_maker() as session:
        effective = await effective_submission_settings(
            session, default_payment_address=_PAYMENT_ADDRESS
        )
    assert (
        effective.revision,
        effective.cooldown_seconds,
        effective.fee_amount_rao,
    ) == (
        1,
        3600,
        _GENESIS_FEE,
    )


# --- Rounding: rao is an integer; TAO renders exactly -----------------------


@pytest.mark.parametrize(
    ("rao", "tao"),
    [
        (37_271_710, "0.037271710"),
        (40_000_000, "0.040000000"),
        (1, "0.000000001"),
        (999_999_999, "0.999999999"),
        (1_000_000_000, "1.000000000"),
        (10_000_000_001, "10.000000001"),
        (0, "0.000000000"),
    ],
)
async def test_rao_renders_as_exact_nine_decimal_tao(rao: int, tao: str) -> None:
    assert format_rao_as_tao(rao) == tao


async def test_fractional_or_float_fee_is_rejected_not_rounded(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    for bad in (40_000_000.5, 40_000_000.0, "40000000", "0.04"):
        body = _payload(expected=1, fee_amount_rao=40_000_000)
        body["fee_amount_rao"] = bad
        response = await client.post(_SETTINGS, headers=_HEADERS, json=body)
        assert response.status_code == 422, (bad, response.text)
    assert len(await _revision_rows(session_maker)) == 1


# --- Bounds ------------------------------------------------------------------


@pytest.mark.parametrize(
    "fee",
    [0, MIN_SUBMISSION_FEE_RAO - 1, MAX_SUBMISSION_FEE_RAO + 1, 1_000_000_000_000],
)
async def test_fee_outside_safe_bounds_is_rejected_and_changes_nothing(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    fee: int,
) -> None:
    _install(app, session_maker)
    response = await client.post(
        _SETTINGS, headers=_HEADERS, json=_payload(expected=1, fee_amount_rao=fee)
    )
    assert response.status_code == 422
    preview = await client.get(
        _PREVIEW,
        headers=_HEADERS,
        params={
            "expected_revision": 1,
            "cooldown_seconds": 3600,
            "fee_amount_rao": fee,
        },
    )
    assert preview.status_code == 422
    assert len(await _revision_rows(session_maker)) == 1


async def test_fee_at_each_bound_is_accepted(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    low = await _apply(client, expected=1, fee_amount_rao=MIN_SUBMISSION_FEE_RAO)
    high = await _apply(
        client, expected=low["revision"], fee_amount_rao=MAX_SUBMISSION_FEE_RAO
    )
    assert high["fee_amount_tao"] == "10.000000000"
    assert low["fee_amount_tao"] == "0.001000000"


# --- Denomination --------------------------------------------------------------


@pytest.mark.parametrize("denomination", ["usd_indexed", "usd", "FIXED_TAO", ""])
async def test_only_the_fixed_tao_denomination_can_be_applied(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    denomination: str,
) -> None:
    _install(app, session_maker)
    response = await client.post(
        _SETTINGS,
        headers=_HEADERS,
        json=_payload(
            expected=1, fee_amount_rao=5_000_000, fee_denomination=denomination
        ),
    )
    assert response.status_code == 422
    assert await _revision_rows(session_maker) == [(1, 3600, _GENESIS_FEE, "fixed_tao")]


async def test_explicit_fixed_tao_denomination_is_recorded(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    applied = await _apply(
        client, expected=1, fee_amount_rao=37_271_710, fee_denomination="fixed_tao"
    )
    assert applied["fee_denomination"] == "fixed_tao"
    assert (await _revision_rows(session_maker))[-1] == (
        applied["revision"],
        3600,
        37_271_710,
        "fixed_tao",
    )


async def test_a_usd_denominated_revision_is_never_quoted_as_tao() -> None:
    """A newer writer's USD target must fail closed, not become a rao fee."""
    row = SubmissionSettingsRevision(
        revision=9,
        parent_revision=8,
        cooldown_seconds=3600,
        fee_amount_rao=5_000_000_000,
        fee_denomination="usd_indexed",
        reason="five dollars, not five TAO",
        actor="future-platform",
    )
    with pytest.raises(UnsupportedFeeDenominationError):
        require_supported_fee_denomination(row)


async def test_database_refuses_an_unreviewed_denomination(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    from sqlalchemy.exc import IntegrityError

    async with session_maker() as session:
        session.add(
            SubmissionSettingsRevision(
                parent_revision=1,
                cooldown_seconds=3600,
                fee_amount_rao=5_000_000,
                fee_denomination="usd_indexed",
                reason="bypass the API validation",
                actor="test",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
    assert len(await _revision_rows(session_maker)) == 1


# --- Stale and concurrent writes ---------------------------------------------


async def test_stale_revision_returns_409_and_changes_nothing(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    first = await _apply(client, expected=1, fee_amount_rao=50_000_000)
    before = await _revision_rows(session_maker)

    stale = await client.post(
        _SETTINGS,
        headers=_HEADERS,
        json=_payload(expected=1, fee_amount_rao=60_000_000),
    )

    assert stale.status_code == 409
    assert "refresh before applying" in stale.json()["message"]
    assert await _revision_rows(session_maker) == before
    current = (await client.get(_SETTINGS, headers=_HEADERS)).json()["current"]
    assert current["revision"] == first["revision"]
    assert current["fee_amount_rao"] == 50_000_000


async def test_concurrent_writes_against_one_revision_admit_exactly_one(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    fees = [45_000_000, 55_000_000, 65_000_000, 75_000_000]
    responses = await asyncio.gather(
        *(
            client.post(
                _SETTINGS,
                headers=_HEADERS,
                json=_payload(expected=1, fee_amount_rao=fee),
            )
            for fee in fees
        )
    )
    statuses = sorted(response.status_code for response in responses)
    assert statuses == [200, 409, 409, 409]
    winner = next(r.json() for r in responses if r.status_code == 200)
    rows = await _revision_rows(session_maker)
    assert len(rows) == 2
    assert rows[-1][2] == winner["fee_amount_rao"]
    assert winner["parent_revision"] == 1


# --- Audit and rollback ----------------------------------------------------------


async def test_history_audits_old_and_new_values_and_rollback_is_a_new_revision(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    raised = await _apply(client, expected=1, fee_amount_rao=100_000_000)
    rollback = await _apply(
        client, expected=raised["revision"], fee_amount_rao=_GENESIS_FEE
    )

    assert rollback["revision"] > raised["revision"] > 1
    assert rollback["previous_fee_amount_rao"] == 100_000_000
    assert rollback["fee_amount_rao"] == _GENESIS_FEE

    control = (await client.get(_SETTINGS, headers=_HEADERS)).json()
    assert control["quote_lifetime_seconds"] == int(
        UPLOAD_ADMISSION_TTL.total_seconds()
    )
    assert control["bounds"]["min_fee_amount_rao"] == MIN_SUBMISSION_FEE_RAO
    assert control["bounds"]["max_fee_amount_rao"] == MAX_SUBMISSION_FEE_RAO
    history = control["history"]
    assert [row["revision"] for row in history] == [
        rollback["revision"],
        raised["revision"],
        1,
    ]
    newest, middle, genesis = history
    assert (newest["previous_fee_amount_rao"], newest["fee_amount_rao"]) == (
        100_000_000,
        _GENESIS_FEE,
    )
    assert (middle["previous_fee_amount_rao"], middle["fee_amount_rao"]) == (
        _GENESIS_FEE,
        100_000_000,
    )
    assert genesis["previous_fee_amount_rao"] is None
    for row in history:
        assert row["fee_denomination"] == "fixed_tao"
        assert row["fee_amount_tao"] == format_rao_as_tao(row["fee_amount_rao"])
        assert row["created_at"] is not None
    assert newest["actor"] == "operator@example.com"
    assert newest["reason"] == "measured platform cost and spam pressure"
    assert newest["previous_cooldown_seconds"] == 3600


# --- Preview -----------------------------------------------------------------


async def test_preview_reports_the_diff_confirmation_and_in_flight_quotes(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    now = datetime.now(UTC)
    async with session_maker() as session, session.begin():
        settings = await effective_submission_settings(
            session, default_payment_address=_PAYMENT_ADDRESS
        )
        for index in range(2):
            await reserve_upload_admission(
                session,
                miner_coldkey=f"coldkey-{index}",
                miner_hotkey=f"hotkey-{index}",
                sha256=str(index) * 64,
                settings=settings,
                now=now - timedelta(minutes=index),
            )
        # An expired reservation is not an in-flight quote.
        await reserve_upload_admission(
            session,
            miner_coldkey="coldkey-expired",
            miner_hotkey="hotkey-expired",
            sha256="e" * 64,
            settings=settings,
            now=now - UPLOAD_ADMISSION_TTL - timedelta(minutes=1),
        )
    before = await _revision_rows(session_maker)

    response = await client.get(
        _PREVIEW,
        headers=_HEADERS,
        params={
            "expected_revision": 1,
            "cooldown_seconds": 3600,
            "fee_amount_rao": 37_271_710,
        },
    )

    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["current"]["revision"] == 1
    assert preview["proposed"] == {
        "cooldown_seconds": 3600,
        "fee_amount_rao": 37_271_710,
        "fee_amount_tao": "0.037271710",
        "fee_denomination": "fixed_tao",
    }
    assert preview["stale"] is False
    assert preview["fee_changed"] is True
    assert preview["cooldown_changed"] is False
    assert preview["fee_change_ratio"] == "0.9318"
    assert preview["applicable"] is True
    assert preview["required_confirmation"] == (
        "SET SUBMISSION COOLDOWN 3600 SECONDS FEE 37271710 RAO"
    )
    assert preview["in_flight_quotes"] == 2
    assert preview["in_flight_quotes_at_other_fees"] == 2
    assert preview["in_flight_quotes_expire_by"] is not None
    # Previewing is read-only and is not an audited mutation.
    assert await _revision_rows(session_maker) == before

    applied = await client.post(
        _SETTINGS,
        headers=_HEADERS,
        json={
            **_payload(expected=1, fee_amount_rao=37_271_710),
            "confirmation": preview["required_confirmation"],
        },
    )
    assert applied.status_code == 200, applied.text


async def test_preview_flags_a_stale_or_no_op_proposal(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    await _apply(client, expected=1, fee_amount_rao=50_000_000)

    stale = (
        await client.get(
            _PREVIEW,
            headers=_HEADERS,
            params={
                "expected_revision": 1,
                "cooldown_seconds": 3600,
                "fee_amount_rao": 60_000_000,
            },
        )
    ).json()
    assert stale["stale"] is True
    assert stale["applicable"] is False

    current = (await client.get(_SETTINGS, headers=_HEADERS)).json()["current"]
    unchanged = (
        await client.get(
            _PREVIEW,
            headers=_HEADERS,
            params={
                "expected_revision": current["revision"],
                "cooldown_seconds": 3600,
                "fee_amount_rao": 50_000_000,
            },
        )
    ).json()
    assert unchanged["stale"] is False
    assert unchanged["fee_changed"] is False
    assert unchanged["fee_change_ratio"] is None
    assert unchanged["applicable"] is False


async def test_preview_requires_admin(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    response = await client.get(
        _PREVIEW,
        params={
            "expected_revision": 1,
            "cooldown_seconds": 3600,
            "fee_amount_rao": 50_000_000,
        },
    )
    assert response.status_code == 401


# --- In-flight quote binding ----------------------------------------------------


async def test_issued_quote_survives_a_policy_change_only_for_its_lifetime(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    issued_at = datetime.now(UTC)
    async with session_maker() as session, session.begin():
        settings = await effective_submission_settings(
            session, default_payment_address=_PAYMENT_ADDRESS
        )
        quoted = await reserve_upload_admission(
            session,
            miner_coldkey="coldkey-inflight",
            miner_hotkey="hotkey-inflight",
            sha256="f" * 64,
            settings=settings,
            now=issued_at,
        )
    assert quoted.fee_amount_rao == _GENESIS_FEE

    changed = await _apply(client, expected=1, fee_amount_rao=90_000_000)

    async with session_maker() as session, session.begin():
        settings = await effective_submission_settings(
            session, default_payment_address=_PAYMENT_ADDRESS
        )
        assert settings.revision == changed["revision"]
        # The same bound upload keeps the fee it was quoted at.
        again = await reserve_upload_admission(
            session,
            miner_coldkey="coldkey-inflight",
            miner_hotkey="hotkey-inflight",
            sha256="f" * 64,
            settings=settings,
            now=issued_at + timedelta(hours=1),
        )
        # A new quote is priced under the new revision.
        fresh = await reserve_upload_admission(
            session,
            miner_coldkey="coldkey-new",
            miner_hotkey="hotkey-new",
            sha256="c" * 64,
            settings=settings,
            now=issued_at + timedelta(hours=1),
        )
    assert again.token == quoted.token
    assert again.fee_amount_rao == _GENESIS_FEE
    assert fresh.fee_amount_rao == 90_000_000

    async with session_maker() as session:
        bound = await session.get(UploadAdmissionReservation, "coldkey-inflight")
        assert bound is not None
        assert bound.settings_revision == 1
        new = await session.get(UploadAdmissionReservation, "coldkey-new")
        assert new is not None
        assert new.settings_revision == changed["revision"]

    # After its lifetime the old quote grants nothing: re-reserving replaces it
    # at the current fee.
    async with session_maker() as session, session.begin():
        settings = await effective_submission_settings(
            session, default_payment_address=_PAYMENT_ADDRESS
        )
        expired = await reserve_upload_admission(
            session,
            miner_coldkey="coldkey-inflight",
            miner_hotkey="hotkey-inflight",
            sha256="f" * 64,
            settings=settings,
            now=issued_at + UPLOAD_ADMISSION_TTL + timedelta(seconds=1),
        )
    assert expired.token != quoted.token
    assert expired.fee_amount_rao == 90_000_000


# --- Public projection ---------------------------------------------------------


async def test_public_fee_starts_at_the_seeded_revision(
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    app: FastAPI,
) -> None:
    _install(app, session_maker)
    response = await client.get(_PUBLIC)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"].startswith("public")
    body = response.json()
    assert body["policy_revision"] == 1
    assert body["fee_revision"] == 1
    assert body["fee_denomination"] == "fixed_tao"
    assert body["fee_amount_rao"] == _GENESIS_FEE
    assert body["fee_amount_tao"] == "0.040000000"
    assert body["quote_lifetime_seconds"] == int(UPLOAD_ADMISSION_TTL.total_seconds())
    assert [row["revision"] for row in body["history"]] == [1]
    assert body["history"][0]["previous_fee_amount_rao"] is None
    assert body["history_truncated"] is False


async def test_public_history_shows_every_fee_change_and_no_operator_identity(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    raised = await _apply(client, expected=1, fee_amount_rao=37_271_710)
    cooldown_only = await _apply(
        client,
        expected=raised["revision"],
        fee_amount_rao=37_271_710,
        cooldown_seconds=1800,
    )
    rolled_back = await _apply(
        client,
        expected=cooldown_only["revision"],
        fee_amount_rao=_GENESIS_FEE,
        cooldown_seconds=1800,
    )

    response = await client.get(_PUBLIC)
    body = response.json()

    assert body["policy_revision"] == rolled_back["revision"]
    assert body["fee_revision"] == rolled_back["revision"]
    assert body["fee_amount_rao"] == _GENESIS_FEE
    assert body["fee_effective_at"] == body["history"][0]["effective_at"]
    # The cooldown-only revision is not a fee change.
    assert [row["revision"] for row in body["history"]] == [
        rolled_back["revision"],
        raised["revision"],
        1,
    ]
    assert body["history"][0]["previous_fee_amount_tao"] == "0.037271710"
    assert body["history"][1]["fee_amount_tao"] == "0.037271710"
    assert body["history"][1]["previous_fee_amount_rao"] == _GENESIS_FEE
    serialized = json.dumps(body)
    for private in (
        "operator@example.com",
        "measured platform cost",
        "actor",
        "reason",
    ):
        assert private not in serialized

    truncated = (await client.get(_PUBLIC, params={"limit": 1})).json()
    assert [row["revision"] for row in truncated["history"]] == [
        rolled_back["revision"]
    ]
    assert truncated["history_truncated"] is True


async def test_public_fee_effective_time_ignores_cooldown_only_revisions(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    raised = await _apply(client, expected=1, fee_amount_rao=60_000_000)
    cooldown_only = await _apply(
        client,
        expected=raised["revision"],
        fee_amount_rao=60_000_000,
        cooldown_seconds=7200,
    )

    body = (await client.get(_PUBLIC)).json()

    assert body["policy_revision"] == cooldown_only["revision"]
    assert body["fee_revision"] == raised["revision"]
    assert _instant(body["fee_effective_at"]) == _instant(raised["created_at"])


async def test_public_fee_reflects_a_change_without_a_deploy(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    before = (await client.get("/api/v1/upload/eval-pricing")).json()
    await _apply(client, expected=1, fee_amount_rao=37_271_710)
    after = (await client.get("/api/v1/upload/eval-pricing")).json()
    public = (await client.get(_PUBLIC)).json()
    assert before["amount_rao"] == _GENESIS_FEE
    assert after["amount_rao"] == 37_271_710 == public["fee_amount_rao"]


# --- Fail closed on an unreviewed denomination ------------------------------


@pytest.mark.parametrize("path", [_PUBLIC, _SETTINGS, "/api/v1/upload/eval-pricing"])
async def test_unreviewed_denomination_is_refused_not_relabelled(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    """A USD target must never be published or quoted as a fixed TAO fee."""
    _install(app, session_maker)
    usd = SubmissionSettingsRevision(
        revision=2,
        parent_revision=1,
        cooldown_seconds=3600,
        fee_amount_rao=5_000_000,
        fee_denomination="usd_indexed",
        reason="five dollar target from a newer writer",
        actor="future-platform",
        created_at=datetime.now(UTC),
    )

    async def _history(
        _session: AsyncSession, **_kwargs: object
    ) -> list[tuple[SubmissionSettingsRevision, None]]:
        return [(usd, None)]

    async def _latest(_session: AsyncSession) -> SubmissionSettingsRevision:
        return usd

    for module in (
        "ditto.api_server.endpoints.public_submission_fee",
        "ditto.api_server.endpoints.admin_submission_settings",
    ):
        monkeypatch.setattr(f"{module}.submission_settings_history", _history)
    monkeypatch.setattr(
        "ditto.db.queries.submission_settings.latest_submission_settings", _latest
    )

    response = await client.get(path, headers=_HEADERS)

    assert response.status_code == 503, response.text
    assert response.json()["error_code"] == 3100
    assert "5000000" not in response.text
