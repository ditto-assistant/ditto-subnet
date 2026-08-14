"""Contract, auth, validation, concurrency, and hot-swap tests for the
operator-controlled cap on concurrent benchmark slots per validator.

The last class is the operational one: a new revision written through the admin
endpoint must land on the NEXT ``resolve()`` of the SAME running app, with no
restart -- that is what makes this setting a kill switch rather than a release.
"""

from __future__ import annotations

import socket
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ditto.api_models.validator_slot_settings import CEILING_DISABLED
from ditto.api_server.dependencies import get_session
from ditto.api_server.validator_slot_settings import (
    DEFAULT_SETTINGS,
    HostResourceSample,
    ValidatorSlotSettingsResolver,
    allowed_slot_count,
    blocked_resources,
    settings_from_row,
    throttled_resources,
)
from ditto.db.models import ValidatorSlotSettingsRevision
from ditto.tests.pgharness import Dsn

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_ADMIN_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}
_URL = "/api/v1/admin/validator-slot-settings"
_ALICE = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
_BOB = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"


def _closed_port() -> int:
    """A localhost port with nothing listening, so a connect is *refused*.

    Bind-then-close rather than a hardcoded number: a hardcoded port that
    something happens to be listening on would turn a refused connection into
    a hang or a protocol error, and quietly stop testing the outage shape.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def settings_maker(
    session_maker: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    """Alias onto the root real-Postgres fixture in ``ditto/tests/conftest.py``.

    Aliasing rather than renaming keeps every test signature in this file
    untouched, so the diff cannot change what is asserted.
    """
    return session_maker


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_ADMIN_TOKEN)
    app.state.session_maker = maker

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    app.state.validator_slot_settings.invalidate()


def _settings(**overrides: Any) -> dict[str, Any]:
    """A COMPLETE policy body, as backroom is required to send.

    Every key is spelled out on purpose. Backroom parses this object with a
    ``z.object``, which strips keys it does not declare, so a body that omits a
    field is exactly the shape that silently resets that field to its default --
    see :class:`~ditto.api_models.validator_slot_settings.ValidatorSlotSettings`.
    """
    base: dict[str, Any] = {
        "max_concurrent_slots": 4,
        "disk_percent_ceiling": 90,
        "memory_percent_ceiling": 90,
        "cpu_percent_ceiling": CEILING_DISABLED,
        "resource_block_percent_ceiling": 95,
        "paused_validator_hotkeys": [],
    }
    base.update(overrides)
    return base


def _disk(percent: int | None) -> HostResourceSample:
    return HostResourceSample(disk_percent=percent)


def _payload(
    *, expected: int = 0, confirmation: str | None = None, **settings: Any
) -> dict[str, Any]:
    body = _settings(**settings)
    return {
        "scope": "*",
        "expected_revision": expected,
        "settings": body,
        "reason": "ramp concurrent benchmark slots for the canary fleet",
        "actor": "backroom:test",
        "confirmation": confirmation
        if confirmation is not None
        else f"APPLY VALIDATOR SLOT CAP {body['max_concurrent_slots']}",
    }


class TestAuth:
    async def test_get_requires_admin_token(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        assert (await client.get(_URL)).status_code == 401

    async def test_post_requires_admin_token(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        assert (await client.post(_URL, json=_payload())).status_code == 401

    async def test_bad_token_is_rejected(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        bad = {"Authorization": "Bearer not-the-admin-token-but-long-enough-x"}
        assert (await client.get(_URL, headers=bad)).status_code == 401


class TestDefaultsAndRoundTrip:
    async def test_empty_reports_conservative_default(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """No revision ever written: cap 2, not the advertised maximum of 8."""
        _install(app, settings_maker)
        body = (await client.get(_URL, headers=_ADMIN_HEADERS)).json()
        assert body["current"] == []
        assert body["history"] == []
        assert body["default"] == {
            "max_concurrent_slots": 2,
            "disk_percent_ceiling": 90,
            "memory_percent_ceiling": 90,
            "cpu_percent_ceiling": 0,
            "resource_block_percent_ceiling": 95,
            "paused_validator_hotkeys": [],
        }
        assert body["effective"]["source"] == "default"
        assert body["effective"]["revision"] == 0
        assert body["effective"]["checksum"] == ""
        assert body["effective"]["settings"]["max_concurrent_slots"] == 2
        assert body["effective"]["hard_slot_ceiling"] == 8
        assert body["effective"]["disk_restricted_slots"] == 1

    async def test_apply_then_get_reflects_revision(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        created = await client.post(_URL, headers=_ADMIN_HEADERS, json=_payload())
        assert created.status_code == 200, created.text
        first = created.json()
        assert first["revision"] == 1
        assert first["parent_revision"] == 0
        assert first["scope"] == "*"
        assert first["actor"] == "backroom:test"
        assert len(first["checksum"]) == 64

        body = (await client.get(_URL, headers=_ADMIN_HEADERS)).json()
        assert len(body["current"]) == 1
        assert body["current"][0]["revision"] == 1
        assert body["effective"]["source"] == "revision"
        assert body["effective"]["revision"] == 1
        assert body["effective"]["settings"]["max_concurrent_slots"] == 4

    async def test_both_knobs_settable_independently_in_one_revision(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        resp = await client.post(
            _URL,
            headers=_ADMIN_HEADERS,
            json=_payload(
                max_concurrent_slots=6,
                disk_percent_ceiling=75,
                memory_percent_ceiling=85,
                cpu_percent_ceiling=95,
                resource_block_percent_ceiling=100,
            ),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["settings"] == {
            "max_concurrent_slots": 6,
            "disk_percent_ceiling": 75,
            "memory_percent_ceiling": 85,
            "cpu_percent_ceiling": 95,
            "resource_block_percent_ceiling": 100,
            "paused_validator_hotkeys": [],
        }

    async def test_an_omitted_ceiling_falls_back_to_its_shipped_default(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Pins the backroom ``z.object`` hazard, in the safe direction.

        Backroom strips keys its schema does not declare, so a stale backroom
        sends a body missing the new ceilings. The platform must not 422 that
        write (which would make the policy unwritable) and must not invent a
        permissive value: it falls back to the shipped default, which is the
        conservative one. The matching backroom schema change is what stops the
        reset from happening at all.
        """
        _install(app, settings_maker)
        resp = await client.post(
            _URL,
            headers=_ADMIN_HEADERS,
            json={
                "scope": "*",
                "expected_revision": 0,
                "settings": {"max_concurrent_slots": 3, "disk_percent_ceiling": 90},
                "reason": "a backroom that predates the resource ceilings",
                "actor": "backroom:test",
                "confirmation": "APPLY VALIDATOR SLOT CAP 3",
            },
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["settings"] == {
            "max_concurrent_slots": 3,
            "disk_percent_ceiling": 90,
            "memory_percent_ceiling": 90,
            "cpu_percent_ceiling": 0,
            "resource_block_percent_ceiling": 95,
            "paused_validator_hotkeys": [],
        }

    async def test_kill_switch_to_one_slot(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        resp = await client.post(
            _URL, headers=_ADMIN_HEADERS, json=_payload(max_concurrent_slots=1)
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["settings"]["max_concurrent_slots"] == 1


class TestConfirmationAndConcurrency:
    async def test_pause_and_resume_one_exact_validator(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        paused = await client.post(
            _URL,
            headers=_ADMIN_HEADERS,
            json=_payload(
                max_concurrent_slots=2,
                paused_validator_hotkeys=[_ALICE],
                confirmation=f"PAUSE VALIDATOR {_ALICE}",
            ),
        )
        assert paused.status_code == 200, paused.text
        assert paused.json()["settings"]["paused_validator_hotkeys"] == [_ALICE]

        resumed = await client.post(
            _URL,
            headers=_ADMIN_HEADERS,
            json=_payload(
                expected=1,
                max_concurrent_slots=2,
                paused_validator_hotkeys=[],
                confirmation=f"RESUME VALIDATOR {_ALICE}",
            ),
        )
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["settings"]["paused_validator_hotkeys"] == []

    @pytest.mark.parametrize(
        ("hotkeys", "confirmation"),
        [
            ([_ALICE], f"PAUSE VALIDATOR {_BOB}"),
            (sorted([_ALICE, _BOB]), f"PAUSE VALIDATOR {_ALICE}"),
        ],
    )
    async def test_pause_confirmation_and_single_action_are_fail_closed(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
        hotkeys: list[str],
        confirmation: str,
    ) -> None:
        _install(app, settings_maker)
        response = await client.post(
            _URL,
            headers=_ADMIN_HEADERS,
            json=_payload(
                max_concurrent_slots=2,
                paused_validator_hotkeys=hotkeys,
                confirmation=confirmation,
            ),
        )
        assert response.status_code == 409, response.text
        async with settings_maker() as session:
            rows = (await session.scalars(select(ValidatorSlotSettingsRevision))).all()
        assert rows == []

    async def test_pause_cannot_be_mixed_with_capacity_change(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        response = await client.post(
            _URL,
            headers=_ADMIN_HEADERS,
            json=_payload(
                max_concurrent_slots=7,
                paused_validator_hotkeys=[_ALICE],
                confirmation=f"PAUSE VALIDATOR {_ALICE}",
            ),
        )
        assert response.status_code == 409, response.text

    async def test_legacy_writer_cannot_drop_an_active_pause(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        assert (
            await client.post(
                _URL,
                headers=_ADMIN_HEADERS,
                json=_payload(
                    max_concurrent_slots=2,
                    paused_validator_hotkeys=[_ALICE],
                    confirmation=f"PAUSE VALIDATOR {_ALICE}",
                ),
            )
        ).status_code == 200
        legacy = _payload(expected=1, max_concurrent_slots=5)
        legacy["settings"].pop("paused_validator_hotkeys")
        response = await client.post(_URL, headers=_ADMIN_HEADERS, json=legacy)
        assert response.status_code == 409, response.text
        assert "paused_validator_hotkeys" in response.json()["message"]
        assert (
            await app.state.validator_slot_settings.resolve(settings_maker)
        ).paused_validator_hotkeys == [_ALICE]

    async def test_wrong_confirmation_is_409(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        resp = await client.post(
            _URL,
            headers=_ADMIN_HEADERS,
            json=_payload(confirmation="APPLY VALIDATOR SLOT CAP"),
        )
        assert resp.status_code == 409
        assert "confirmation" in resp.json()["message"]

    async def test_confirmation_must_name_the_applied_cap(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Typing the previous cap while raising the number must not apply."""
        _install(app, settings_maker)
        resp = await client.post(
            _URL,
            headers=_ADMIN_HEADERS,
            json=_payload(
                max_concurrent_slots=8, confirmation="APPLY VALIDATOR SLOT CAP 2"
            ),
        )
        assert resp.status_code == 409
        async with settings_maker() as session:
            rows = (await session.scalars(select(ValidatorSlotSettingsRevision))).all()
        assert rows == []

    async def test_stale_expected_revision_is_409(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        assert (
            await client.post(_URL, headers=_ADMIN_HEADERS, json=_payload())
        ).status_code == 200
        # A second operator still believes revision 0 is current.
        stale = await client.post(_URL, headers=_ADMIN_HEADERS, json=_payload())
        assert stale.status_code == 409
        assert "refresh" in stale.json()["message"]

    async def test_racing_writer_on_same_parent_is_409(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """The (scope, parent_revision) key turns a race into an IntegrityError,
        which the endpoint reports as 409 rather than clobbering."""
        _install(app, settings_maker)
        await client.post(_URL, headers=_ADMIN_HEADERS, json=_payload())
        # Insert a competing child of revision 1 out-of-band, then have the API
        # try to append its own child of 1 with a matching expected_revision.
        async with settings_maker() as session, session.begin():
            session.add(
                ValidatorSlotSettingsRevision(
                    parent_revision=1,
                    scope="*",
                    settings={"max_concurrent_slots": 3, "disk_percent_ceiling": 90},
                    checksum="a" * 64,
                    reason="a competing out-of-band revision",
                    actor="other-operator",
                )
            )
        raced = await client.post(
            _URL, headers=_ADMIN_HEADERS, json=_payload(expected=1)
        )
        assert raced.status_code == 409


class TestValidation:
    async def test_non_global_scope_rejected(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        payload = _payload()
        payload["scope"] = "validator-7"
        resp = await client.post(_URL, headers=_ADMIN_HEADERS, json=payload)
        assert resp.status_code == 422

    @pytest.mark.parametrize(
        "overrides",
        [
            {"max_concurrent_slots": 0},  # below 1
            {"max_concurrent_slots": 9},  # above the protocol ceiling of 8
            {"max_concurrent_slots": -1},
            {"disk_percent_ceiling": 49},  # below 50
            {"disk_percent_ceiling": 101},  # above 100
            {"disk_percent_ceiling": 87},  # off the heartbeat's 5% grid
            {"memory_percent_ceiling": 49},
            {"memory_percent_ceiling": 101},
            {"memory_percent_ceiling": 87},
            {"cpu_percent_ceiling": 49},
            {"cpu_percent_ceiling": 101},
            {"cpu_percent_ceiling": 87},
            {"resource_block_percent_ceiling": 49},
            {"resource_block_percent_ceiling": 101},
            {"resource_block_percent_ceiling": 87},
            # A hard stop below the throttle makes the throttle unreachable.
            {"disk_percent_ceiling": 95, "resource_block_percent_ceiling": 90},
            {"memory_percent_ceiling": 95, "resource_block_percent_ceiling": 90},
        ],
    )
    async def test_out_of_range_values_rejected(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
        overrides: dict[str, Any],
    ) -> None:
        _install(app, settings_maker)
        resp = await client.post(
            _URL, headers=_ADMIN_HEADERS, json=_payload(**overrides)
        )
        assert resp.status_code == 422, resp.text

    @pytest.mark.parametrize(
        "overrides",
        [
            {"max_concurrent_slots": "4"},  # strict: no string coercion
            {"disk_percent_ceiling": 90.0},  # strict: no float coercion
            {"memory_percent_ceiling": "90"},
            {"resource_block_percent_ceiling": 95.0},
        ],
    )
    async def test_strict_types_rejected(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
        overrides: dict[str, Any],
    ) -> None:
        _install(app, settings_maker)
        resp = await client.post(
            _URL, headers=_ADMIN_HEADERS, json=_payload(**overrides)
        )
        assert resp.status_code == 422, resp.text

    async def test_unknown_setting_key_rejected(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        resp = await client.post(
            _URL, headers=_ADMIN_HEADERS, json=_payload(max_slots_typo=4)
        )
        assert resp.status_code == 422

    async def test_short_reason_rejected(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        payload = _payload()
        payload["reason"] = "nope"
        resp = await client.post(_URL, headers=_ADMIN_HEADERS, json=payload)
        assert resp.status_code == 422

    @pytest.mark.parametrize(
        "hotkeys",
        [
            [_ALICE, _BOB],
            [_ALICE, _ALICE],
            ["not-an-ss58-hotkey"],
        ],
    )
    async def test_paused_hotkeys_must_be_canonical_ss58_values(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
        hotkeys: list[str],
    ) -> None:
        _install(app, settings_maker)
        response = await client.post(
            _URL,
            headers=_ADMIN_HEADERS,
            json=_payload(paused_validator_hotkeys=hotkeys),
        )
        assert response.status_code == 422, response.text


class TestCeilingsCanBeDisabled:
    """Zero is the documented "do not gate on this" value, not an out-of-range one."""

    @pytest.mark.parametrize(
        "overrides",
        [
            {"cpu_percent_ceiling": 0},
            {"memory_percent_ceiling": 0},
            {"disk_percent_ceiling": 0},
            {"resource_block_percent_ceiling": 0},
            {
                "disk_percent_ceiling": 0,
                "memory_percent_ceiling": 0,
                "cpu_percent_ceiling": 0,
                "resource_block_percent_ceiling": 0,
            },
        ],
    )
    async def test_zero_is_accepted(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
        overrides: dict[str, Any],
    ) -> None:
        _install(app, settings_maker)
        resp = await client.post(
            _URL, headers=_ADMIN_HEADERS, json=_payload(**overrides)
        )
        assert resp.status_code == 200, resp.text


class TestAppendOnlyHistory:
    async def test_history_accumulates_and_chains(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        await client.post(
            _URL, headers=_ADMIN_HEADERS, json=_payload(max_concurrent_slots=3)
        )
        second = await client.post(
            _URL,
            headers=_ADMIN_HEADERS,
            json=_payload(expected=1, max_concurrent_slots=5),
        )
        assert second.status_code == 200, second.text
        assert second.json()["revision"] == 2
        assert second.json()["parent_revision"] == 1
        # A rollback is itself an append, never an edit of revision 1.
        third = await client.post(
            _URL,
            headers=_ADMIN_HEADERS,
            json=_payload(expected=2, max_concurrent_slots=1),
        )
        assert third.status_code == 200, third.text

        body = (await client.get(_URL, headers=_ADMIN_HEADERS)).json()
        assert [row["revision"] for row in body["history"]] == [3, 2, 1]
        assert [row["settings"]["max_concurrent_slots"] for row in body["history"]] == [
            1,
            5,
            3,
        ]
        # The superseded revisions are untouched: same values, same checksums.
        assert body["history"][2]["settings"]["max_concurrent_slots"] == 3
        assert body["effective"]["revision"] == 3
        assert body["effective"]["settings"]["max_concurrent_slots"] == 1


class TestResolverFailClosed:
    async def test_no_session_maker_returns_default(self) -> None:
        resolver = ValidatorSlotSettingsResolver(ttl_seconds=0)
        assert await resolver.resolve(None) == DEFAULT_SETTINGS
        assert DEFAULT_SETTINGS.max_concurrent_slots == 2

    async def test_database_error_holds_the_default_cap(
        self, postgres_admin_dsn: Dsn
    ) -> None:
        """A DB outage must never uncap the fleet.

        Keeps the original shape -- a reachable database in which the table
        does not exist -- but on real Postgres: the harness's admin database
        is migrated by nobody, so the read raises
        ``ProgrammingError(UndefinedTableError)``.

        The *unreachable* database is the sibling case below; it is the shape
        that used to escape the guard.
        """
        engine = create_async_engine(postgres_admin_dsn.sqlalchemy)
        maker = async_sessionmaker(engine)
        resolver = ValidatorSlotSettingsResolver(ttl_seconds=0)
        try:
            assert await resolver.resolve(maker) == DEFAULT_SETTINGS
        finally:
            await engine.dispose()

    async def test_refused_connection_holds_the_default_cap(
        self, postgres_admin_dsn: Dsn
    ) -> None:
        """Postgres being *down* is the outage the guard most has to survive.

        asyncpg raises a bare ``OSError`` that SQLAlchemy does not wrap -- so
        this escaped the resolver's original ``except SQLAlchemyError`` arm
        entirely and propagated out of ``resolve()``, 500ing the ticket-issue
        path instead of holding the cap that #433 promises. Pinned here
        because it is the exact shape a type-enumerated guard gets wrong.

        The precondition asserts ``OSError`` and *not*-``SQLAlchemyError``,
        which is the whole load-bearing claim, rather than the narrower
        ``ConnectionRefusedError``. Which ``OSError`` subclass surfaces
        depends on the host's IP stack: against an IPv4-only host asyncio
        raises ``ConnectionRefusedError``, but against a dual-stack
        ``localhost`` it fans out to ``::1`` and ``127.0.0.1``, gets two
        refusals with different messages, and combines them into a plain
        ``OSError("Multiple exceptions: ...")``. Pinning the subclass made
        this test pass on a developer's IPv4 DSN and fail on CI's dual-stack
        one -- and it also understates the bug, since even an
        ``except ConnectionRefusedError`` guard would have missed that shape.
        """
        dead = replace(postgres_admin_dsn, port=_closed_port())
        engine = create_async_engine(dead.sqlalchemy)
        maker = async_sessionmaker(engine)
        resolver = ValidatorSlotSettingsResolver(ttl_seconds=0)
        try:
            with pytest.raises(OSError) as refused:
                async with maker() as session:
                    await session.execute(select(ValidatorSlotSettingsRevision))
            # The point of the fix: SQLAlchemy never wrapped this, so an
            # `except SQLAlchemyError` arm could not have seen it.
            assert not isinstance(refused.value, SQLAlchemyError)
            assert await resolver.resolve(maker) == DEFAULT_SETTINGS
            assert DEFAULT_SETTINGS.max_concurrent_slots == 2
            # An outage is never cached: the next issue re-reads rather than
            # pinning the default for a full TTL after Postgres returns.
            assert resolver._cache is None
        finally:
            await engine.dispose()

    async def test_unparseable_row_falls_back_to_default(
        self, settings_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        async with settings_maker() as session, session.begin():
            session.add(
                ValidatorSlotSettingsRevision(
                    parent_revision=0,
                    scope="*",
                    settings={"max_concurrent_slots": 99},  # drifted / hand-edited
                    checksum="b" * 64,
                    reason="a row written by a future schema",
                    actor="drift",
                )
            )
        async with settings_maker() as session:
            row = (await session.scalars(select(ValidatorSlotSettingsRevision))).one()
        assert settings_from_row(row) == DEFAULT_SETTINGS

        resolver = ValidatorSlotSettingsResolver(ttl_seconds=0)
        assert await resolver.resolve(settings_maker) == DEFAULT_SETTINGS


class TestSlotArithmetic:
    def test_cap_only_narrows_what_is_advertised(self) -> None:
        settings = DEFAULT_SETTINGS.model_copy(update={"max_concurrent_slots": 4})
        assert allowed_slot_count(settings, advertised_slots=8, sample=_disk(10)) == 4
        assert allowed_slot_count(settings, advertised_slots=2, sample=_disk(10)) == 2
        assert allowed_slot_count(settings, advertised_slots=0, sample=_disk(10)) == 0

    def test_disk_ceiling_holds_a_full_host_to_one_slot(self) -> None:
        settings = DEFAULT_SETTINGS.model_copy(update={"max_concurrent_slots": 8})
        assert allowed_slot_count(settings, advertised_slots=8, sample=_disk(85)) == 8
        assert allowed_slot_count(settings, advertised_slots=8, sample=_disk(90)) == 1
        # Past the shared hard stop it is not one slot, it is none.
        assert allowed_slot_count(settings, advertised_slots=8, sample=_disk(95)) == 0
        # Missing telemetry is not evidence of a full disk.
        assert allowed_slot_count(settings, advertised_slots=8, sample=_disk(None)) == 8
        assert not throttled_resources(settings, _disk(None))
        assert not blocked_resources(settings, _disk(None))
        assert throttled_resources(settings, _disk(100)) == ("disk",)
        assert blocked_resources(settings, _disk(100)) == ("disk",)


class TestHotSwap:
    async def test_new_revision_lands_on_next_resolve_without_restart(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """The operational contract: the running app's resolver picks up the new
        cap on its NEXT read, with no redeploy and no restart."""
        _install(app, settings_maker)
        resolver: ValidatorSlotSettingsResolver = app.state.validator_slot_settings

        # Before any revision: the conservative default governs.
        assert (await resolver.resolve(settings_maker)).max_concurrent_slots == 2

        # Operator ramps to 6 from backroom.
        applied = await client.post(
            _URL, headers=_ADMIN_HEADERS, json=_payload(max_concurrent_slots=6)
        )
        assert applied.status_code == 200, applied.text

        # SAME app object, SAME resolver instance -- no restart.
        assert (await resolver.resolve(settings_maker)).max_concurrent_slots == 6

        # And the kill switch lands just as fast.
        killed = await client.post(
            _URL,
            headers=_ADMIN_HEADERS,
            json=_payload(expected=1, max_concurrent_slots=1),
        )
        assert killed.status_code == 200, killed.text
        assert (await resolver.resolve(settings_maker)).max_concurrent_slots == 1

    async def test_disk_ceiling_change_lands_on_next_resolve(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        resolver: ValidatorSlotSettingsResolver = app.state.validator_slot_settings
        applied = await client.post(
            _URL,
            headers=_ADMIN_HEADERS,
            json=_payload(max_concurrent_slots=4, disk_percent_ceiling=70),
        )
        assert applied.status_code == 200, applied.text
        settings = await resolver.resolve(settings_maker)
        assert settings.disk_percent_ceiling == 70
        assert allowed_slot_count(settings, advertised_slots=4, sample=_disk(75)) == 1

    async def test_cached_read_is_bounded_by_the_ttl(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A resolver that did NOT see the write (another worker) still converges
        once its TTL window lapses; a long TTL is what it converges within."""
        _install(app, settings_maker)
        other_worker = ValidatorSlotSettingsResolver(ttl_seconds=3600)
        assert (await other_worker.resolve(settings_maker)).max_concurrent_slots == 2

        applied = await client.post(
            _URL, headers=_ADMIN_HEADERS, json=_payload(max_concurrent_slots=7)
        )
        assert applied.status_code == 200, applied.text
        # Still serving its cached value inside the window.
        assert (await other_worker.resolve(settings_maker)).max_concurrent_slots == 2
        # Its own TTL lapsing (simulated here by invalidation) converges it.
        other_worker.invalidate()
        assert (await other_worker.resolve(settings_maker)).max_concurrent_slots == 7
