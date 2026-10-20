"""Contract tests for the scoring lease clock board (#1156)."""

from collections.abc import AsyncIterator
from dataclasses import replace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.scoring_lease_settings import (
    MAX_SCORING_TICKET_TTL_MINUTES,
    MIN_SCORING_TICKET_TTL_MINUTES,
    scoring_lease_confirmation,
)
from ditto.api_server.admin_activity import public_details
from ditto.api_server.dependencies import get_session
from ditto.api_server.scoring_lease_settings import (
    ScoringLeaseSettingsResolver,
    resolve_scoring_ticket_ttl,
)
from ditto.db.models import ScoringLeaseSettingsRevision

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}
_URL = "/api/v1/admin/scoring-lease-settings"


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_ADMIN_TOKEN)
    app.state.session_maker = maker
    app.state.scoring_lease_settings = ScoringLeaseSettingsResolver()

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


def _payload(
    minutes: int = 150,
    *,
    expected_revision: int = 0,
    confirmation: str | None = None,
    settings: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "scope": "*",
        "expected_revision": expected_revision,
        "settings": (
            settings
            if settings is not None
            else {"scoring_ticket_ttl_minutes": minutes}
        ),
        "reason": "v11 completions fit well inside 150 minutes",
        "actor": "backroom:test",
        "confirmation": (
            confirmation
            if confirmation is not None
            else scoring_lease_confirmation(minutes)
        ),
    }


async def _revision_count(maker: async_sessionmaker[AsyncSession]) -> int:
    async with maker() as session:
        return int(
            await session.scalar(
                select(func.count()).select_from(ScoringLeaseSettingsRevision)
            )
            or 0
        )


class TestAuth:
    async def test_read_and_write_require_the_admin_token(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        assert (await client.get(_URL)).status_code == 401
        assert (await client.post(_URL, json=_payload())).status_code == 401
        assert await _revision_count(session_maker) == 0


class TestRead:
    async def test_corrupt_current_and_history_remain_readable(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        for revision, minutes in enumerate((150, 200)):
            response = await client.post(
                _URL,
                headers=_HEADERS,
                json=_payload(minutes, expected_revision=revision),
            )
            assert response.status_code == 200, response.text

        async with session_maker() as session:
            rows = (await session.scalars(select(ScoringLeaseSettingsRevision))).all()
            checksums = {row.revision: row.checksum for row in rows}
            for row in rows:
                row.settings = {"scoring_ticket_ttl_minutes": 1}
            await session.commit()

        response = await client.get(_URL, headers=_HEADERS)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["effective"]["settings"] == {"scoring_ticket_ttl_minutes": 180}
        assert body["effective"]["revision"] == 2
        assert body["effective"]["source"] == "default"
        assert body["effective"]["settings_valid"] is False
        assert [row["revision"] for row in body["history"]] == [2, 1]
        for row in body["current"] + body["history"]:
            assert row["settings"] == {"scoring_ticket_ttl_minutes": 180}
            assert row["settings_valid"] is False
            assert row["checksum"] == checksums[row["revision"]]
            assert row["actor"] == "backroom:test"

        # Reading the fallback never repairs or overwrites the audit records.
        async with session_maker() as session:
            rows = (await session.scalars(select(ScoringLeaseSettingsRevision))).all()
            assert all(
                row.settings == {"scoring_ticket_ttl_minutes": 1} for row in rows
            )

    async def test_empty_board_reports_the_180_minute_default(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        body = (await client.get(_URL, headers=_HEADERS)).json()
        assert body["current"] == []
        assert body["history"] == []
        assert body["default"] == {"scoring_ticket_ttl_minutes": 180}
        effective = body["effective"]
        assert effective["source"] == "default"
        assert effective["settings_valid"] is True
        assert effective["revision"] == 0
        assert effective["checksum"] == ""
        assert effective["settings"] == {"scoring_ticket_ttl_minutes": 180}
        assert effective["min_scoring_ticket_ttl_minutes"] == (
            MIN_SCORING_TICKET_TTL_MINUTES
        )
        assert effective["max_scoring_ticket_ttl_minutes"] == (
            MAX_SCORING_TICKET_TTL_MINUTES
        )
        assert effective["max_age_seconds"] == 5.0


class TestWrite:
    async def test_apply_then_read_back_with_history(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        first = await client.post(_URL, headers=_HEADERS, json=_payload(150))
        assert first.status_code == 200, first.text
        assert first.json()["revision"] == 1
        assert first.json()["parent_revision"] == 0
        second = await client.post(
            _URL, headers=_HEADERS, json=_payload(200, expected_revision=1)
        )
        assert second.status_code == 200, second.text

        body = (await client.get(_URL, headers=_HEADERS)).json()
        assert body["effective"]["source"] == "revision"
        assert body["effective"]["settings_valid"] is True
        assert body["effective"]["revision"] == 2
        assert body["effective"]["settings"] == {"scoring_ticket_ttl_minutes": 200}
        assert len(body["effective"]["checksum"]) == 64
        assert [row["revision"] for row in body["history"]] == [2, 1]
        assert body["history"][1]["actor"] == "backroom:test"
        assert all(row["settings_valid"] for row in body["history"])
        assert body["history"][1]["reason"].startswith("v11 completions")

    async def test_extra_json_is_ignored(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        payload = _payload(120)
        payload["unexpected"] = True
        payload["settings"] = {"scoring_ticket_ttl_minutes": 120, "future_clock": 9}
        response = await client.post(_URL, headers=_HEADERS, json=payload)
        assert response.status_code == 200, response.text
        assert response.json()["settings"] == {"scoring_ticket_ttl_minutes": 120}

    async def test_wrong_confirmation_is_refused(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        response = await client.post(
            _URL,
            headers=_HEADERS,
            json=_payload(150, confirmation=scoring_lease_confirmation(180)),
        )
        assert response.status_code == 409
        assert "APPLY SCORING TICKET TTL 150 MINUTES" in response.text
        assert await _revision_count(session_maker) == 0

    async def test_stale_expected_revision_is_refused(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        assert (
            await client.post(_URL, headers=_HEADERS, json=_payload(150))
        ).status_code == 200
        stale = await client.post(_URL, headers=_HEADERS, json=_payload(120))
        assert stale.status_code == 409
        assert "expected 0, current 1" in stale.text
        assert await _revision_count(session_maker) == 1

    async def test_partial_policy_is_refused(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        response = await client.post(
            _URL, headers=_HEADERS, json=_payload(150, settings={})
        )
        assert response.status_code == 422
        assert await _revision_count(session_maker) == 0

    @pytest.mark.parametrize(
        "minutes",
        [MIN_SCORING_TICKET_TTL_MINUTES - 1, MAX_SCORING_TICKET_TTL_MINUTES + 1],
    )
    async def test_out_of_bounds_ttl_is_refused(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        minutes: int,
    ) -> None:
        _install(app, session_maker)
        response = await client.post(_URL, headers=_HEADERS, json=_payload(minutes))
        assert response.status_code == 422
        assert await _revision_count(session_maker) == 0

    async def test_non_global_scope_is_refused(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        payload = _payload(150)
        payload["scope"] = "validator"
        response = await client.post(_URL, headers=_HEADERS, json=payload)
        assert response.status_code == 422
        assert await _revision_count(session_maker) == 0

    async def test_write_invalidates_the_issuance_cache(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        app.state.scoring_lease_settings = ScoringLeaseSettingsResolver(
            ttl_seconds=3600
        )
        before = await resolve_scoring_ticket_ttl(app.state)
        assert before.total_seconds() == 180 * 60
        response = await client.post(_URL, headers=_HEADERS, json=_payload(90))
        assert response.status_code == 200, response.text
        after = await resolve_scoring_ticket_ttl(app.state)
        assert after.total_seconds() == 90 * 60


class TestWhitespaceAuditFields:
    """Blank audit fields are invalid input, never a revision conflict.

    A whitespace-padded reason or actor used to pass the raw-length check, fail
    the table's trimmed-length constraint on insert, and come back as the
    "changed concurrently" 409 -- advice no refresh can satisfy.
    """

    @pytest.mark.parametrize(
        ("field", "value", "message"),
        [
            ("reason", " " * 12, "reason"),
            ("reason", "   short   ", "reason"),
            ("actor", "    ", "actor"),
            ("confirmation", "   ", "confirmation"),
        ],
    )
    async def test_blank_audit_field_is_a_clear_422_before_the_revision_check(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        field: str,
        value: str,
        message: str,
    ) -> None:
        _install(app, session_maker)
        # A stale expected revision too: the audit-field refusal must win, so
        # the operator is never told to refresh for an input problem.
        assert (
            await client.post(_URL, headers=_HEADERS, json=_payload(150))
        ).status_code == 200
        payload = _payload(120)
        payload[field] = value
        response = await client.post(_URL, headers=_HEADERS, json=payload)
        assert response.status_code == 422, response.text
        body = response.text
        assert message in body
        assert "changed" not in body
        assert await _revision_count(session_maker) == 1

    async def test_padded_audit_fields_are_trimmed_and_accepted(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        payload = _payload(150)
        payload["reason"] = "   v11 completions fit inside 150 minutes   "
        payload["actor"] = "  backroom:test  "
        response = await client.post(_URL, headers=_HEADERS, json=payload)
        assert response.status_code == 200, response.text
        assert response.json()["reason"] == "v11 completions fit inside 150 minutes"
        assert response.json()["actor"] == "backroom:test"


class TestAuditProjection:
    def test_public_activity_publishes_only_the_ttl(self) -> None:
        details = public_details(
            "/api/v1/admin/scoring-lease-settings",
            {
                "expected_revision": 3,
                "settings": {"scoring_ticket_ttl_minutes": 150, "secret": "x"},
                "reason": "private operator reason",
            },
        )
        assert details == {
            "expected_revision": 3,
            "settings": {"scoring_ticket_ttl_minutes": 150},
        }
