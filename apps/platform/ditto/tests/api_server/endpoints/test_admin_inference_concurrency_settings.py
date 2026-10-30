"""Contract tests for the hosted-embedding concurrency board."""

from collections.abc import AsyncIterator
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)

from ditto.api_models.inference_concurrency_settings import (
    InferenceConcurrencySettings,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.inference_concurrency_settings import (
    InferenceConcurrencySettingsResolver,
)

pytestmark = pytest.mark.asyncio

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}
_URL = "/api/v1/admin/inference-concurrency-settings"
_CONFIRMATION = "APPLY INFERENCE CONCURRENCY SETTINGS"


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
    app.state.inference_concurrency_settings = InferenceConcurrencySettingsResolver()

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


def _settings(**overrides: object) -> dict[str, object]:
    """A COMPLETE policy body; the board stores the whole object."""
    settings: dict[str, object] = {
        "chat_request_budget": 8192,
        "chat_token_budget": 25_000_000,
        "chat_per_ticket_concurrency": 16,
        "chat_per_validator_concurrency": 48,
        "chat_global_concurrency": 96,
        "embedding_per_ticket_concurrency": 12,
        "embedding_per_validator_concurrency": 48,
        "embedding_global_concurrency": 96,
        "chat_per_ticket_requests_per_minute": 1920,
        "chat_per_validator_requests_per_minute": 7680,
        "chat_global_requests_per_minute": 23040,
        "embedding_per_ticket_requests_per_minute": 10_000,
        "embedding_per_validator_requests_per_minute": 40_000,
        "embedding_global_requests_per_minute": 100_000,
    }
    settings.update(overrides)
    return settings


def _payload(
    *,
    expected_revision: int = 0,
    confirmation: str = _CONFIRMATION,
    settings: dict[str, object] | None = None,
    **setting_overrides: object,
) -> dict[str, object]:
    return {
        "scope": "*",
        "expected_revision": expected_revision,
        "settings": settings
        if settings is not None
        else _settings(**setting_overrides),
        "reason": "widen the hosted v7 embedding lane; ollama rationale is gone",
        "actor": "backroom:test",
        "confirmation": confirmation,
    }


class TestAuth:
    async def test_read_requires_the_admin_token(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        assert (await client.get(_URL)).status_code == 401


class TestWrite:
    async def test_apply_then_read_back(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        created = await client.post(
            _URL, json=_payload(embedding_per_ticket_concurrency=24), headers=_HEADERS
        )
        assert created.status_code == 200, created.text
        assert created.json()["revision"] == 1

        body = (await client.get(_URL, headers=_HEADERS)).json()
        assert body["effective"]["source"] == "revision"
        assert body["effective"]["settings"]["embedding_per_ticket_concurrency"] == 24
        assert len(body["history"]) == 1

    async def test_legacy_write_preserves_current_v10_runtime_policy(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        runtime = {
            "case_concurrency": 4,
            "relay_delay_fingerprint_mode": "shadow",
            "relay_delay_fingerprint_min_ms": 40,
            "relay_delay_fingerprint_max_ms": 160,
            # A non-default cap proves a legacy write carries it forward.
            "sandbox_memory_limit_mib": 8_192,
        }
        decision_lane = {
            **InferenceConcurrencySettings().decision_lane.model_dump(mode="json"),
            "enabled": True,
            "model": "typesafe/jev-1.14",
        }
        first = await client.post(
            _URL,
            json=_payload(benchmark_runtime=runtime, decision_lane=decision_lane),
            headers=_HEADERS,
        )
        assert first.status_code == 200, first.text

        # Simulate an already-deployed Backroom that knows only the original
        # eight fields. It may change those fields, but it must not reset the
        # newer runtime controls during a rolling upgrade.
        legacy = await client.post(
            _URL,
            json=_payload(
                expected_revision=1,
                embedding_per_ticket_concurrency=10,
            ),
            headers=_HEADERS,
        )
        assert legacy.status_code == 200, legacy.text
        assert legacy.json()["settings"]["benchmark_runtime"] == runtime
        assert legacy.json()["settings"]["decision_lane"] == decision_lane

    async def test_cost_ceiling_round_trips_with_actor_and_reason(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        created = await client.post(
            _URL,
            json=_payload(
                run_cost_ceiling_microusd=5_000_000,
                cost_ceiling_mode="shadow",
                case_cost_report_microusd=50_000,
            ),
            headers=_HEADERS,
        )
        assert created.status_code == 200, created.text
        revision = created.json()
        assert revision["actor"] == "backroom:test"
        assert revision["reason"].startswith("widen the hosted")
        assert revision["settings"]["run_cost_ceiling_microusd"] == 5_000_000
        assert revision["settings"]["cost_ceiling_mode"] == "shadow"
        assert revision["settings"]["case_cost_report_microusd"] == 50_000
        body = (await client.get(_URL, headers=_HEADERS)).json()
        assert body["effective"]["settings"]["cost_ceiling_mode"] == "shadow"
        assert body["default"]["cost_ceiling_mode"] == "off"
        assert body["default"]["run_cost_ceiling_microusd"] is None

    async def test_cost_ceiling_bounds_are_enforced_on_write(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        for overrides in (
            {"run_cost_ceiling_microusd": 0},
            {"cost_ceiling_mode": "enforce"},  # a live mode with no ceiling
            {"run_cost_ceiling_microusd": 10, "cost_ceiling_mode": "panic"},
        ):
            response = await client.post(
                _URL, json=_payload(**overrides), headers=_HEADERS
            )
            assert response.status_code == 422, (overrides, response.text)

    async def test_legacy_write_preserves_the_cost_ceiling(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        first = await client.post(
            _URL,
            json=_payload(
                run_cost_ceiling_microusd=5_000_000, cost_ceiling_mode="enforce"
            ),
            headers=_HEADERS,
        )
        assert first.status_code == 200, first.text
        # A Backroom that predates #3271 sends neither field; the live ceiling
        # must survive its write instead of resetting to off.
        legacy = await client.post(
            _URL,
            json=_payload(expected_revision=1, embedding_per_ticket_concurrency=10),
            headers=_HEADERS,
        )
        assert legacy.status_code == 200, legacy.text
        settings = legacy.json()["settings"]
        assert settings["run_cost_ceiling_microusd"] == 5_000_000
        assert settings["cost_ceiling_mode"] == "enforce"
        assert settings["embedding_per_ticket_concurrency"] == 10

    async def test_wrong_confirmation_is_refused(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        response = await client.post(
            _URL, json=_payload(confirmation="yes"), headers=_HEADERS
        )
        assert response.status_code == 409

    async def test_stale_expected_revision_is_refused(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        assert (await client.post(_URL, json=_payload(), headers=_HEADERS)).status_code
        stale = await client.post(
            _URL, json=_payload(expected_revision=0), headers=_HEADERS
        )
        assert stale.status_code == 409

    async def test_write_invalidates_the_admission_cache(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A backroom write must land on the next admission, not in five seconds.

        The resolver is TTL-cached so the admission path does not pay a SELECT
        per embedding. Without this invalidation an operator pulling the brake
        would watch nothing happen and pull it again.
        """
        _install(app, settings_maker)
        resolver = app.state.inference_concurrency_settings
        assert (
            await resolver.resolve(settings_maker)
        ).embedding_per_ticket_concurrency == 12
        await client.post(
            _URL, json=_payload(embedding_per_ticket_concurrency=2), headers=_HEADERS
        )
        resolved = await resolver.resolve(settings_maker)
        assert resolved.embedding_per_ticket_concurrency == 2


class TestDecisionLaneRead:
    async def test_reports_switch_pin_and_ledger_usage(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        settings_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, settings_maker)
        url = f"{_URL}/decision-lane"
        dark = (await client.get(url, headers=_HEADERS)).json()
        assert dark["state"] == "disabled"
        assert dark["settings"]["enabled"] is False
        assert dark["upstream"] == "https://openrouter.ai/api/v1/systemone"
        assert [w["requests"] for w in dark["usage"]] == [0, 0, 0]

        agent, grant = uuid4(), uuid4()
        async with settings_maker() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO agents (agent_id, miner_hotkey, name, sha256) "
                    "VALUES (:a, 'miner', 'decision-agent', repeat('d', 64))"
                ),
                {"a": agent},
            )
            await session.execute(
                text(
                    "INSERT INTO validator_tickets (agent_id, validator_hotkey, "
                    "slot_id, status, deadline, bench_version, attempt_count) "
                    "VALUES (:a, 'validator', 'slot-0', 'issued', "
                    "now() + interval '1 hour', 9, 1)"
                ),
                {"a": agent},
            )
            await session.execute(
                text(
                    "INSERT INTO inference_grants (grant_id, agent_id, bench_version, "
                    "validator_hotkey, slot_id, ticket_deadline, status, generation, "
                    "allowed_models, request_budget, token_budget, expires_at, "
                    "decision_model, decision_input_microusd_per_mtok, "
                    "decision_request_budget, decision_token_budget) "
                    "VALUES (:g, :a, 9, 'validator', 'slot-0', "
                    "now() + interval '1 hour', 'active', 1, '[\"m\"]'::jsonb, 10, "
                    "10, now() + interval '1 hour', 'typesafe/jev-1.14', 42000, 10, "
                    "1000)"
                ),
                {"g": grant, "a": agent},
            )
            for code, tokens, cost in (
                (None, 476, 20),
                ("decision_model_mismatch", 0, 0),
            ):
                await session.execute(
                    text(
                        "INSERT INTO inference_requests (grant_id, nonce, generation, "
                        "request_kind, model, reserved_tokens, max_chargeable_tokens, "
                        "started_at, status, prompt_tokens, completion_tokens, "
                        "cost_microusd, timed_out, terminal_error_code) VALUES "
                        "(:g, :n, 1, 'decision', 'typesafe/jev-1.14', 1, 2000, now(), "
                        ":s, :t, 0, :c, false, :code)"
                    ),
                    {
                        "g": grant,
                        "n": uuid4(),
                        "s": "completed" if code is None else "failed",
                        "t": tokens,
                        "c": cost,
                        "code": code,
                    },
                )

        body = (await client.get(url, headers=_HEADERS)).json()
        hour = body["usage"][0]
        assert hour["window_seconds"] == 3600
        assert (hour["requests"], hour["completed"], hour["model_mismatches"]) == (
            2,
            1,
            1,
        )
        assert (hour["input_tokens"], hour["ledger_cost_microusd"]) == (476, 20)
        assert hour["provider_failures"] == 0
        assert body["live_grants_with_lane"] == 1
        assert body["terminal_error_codes_24h"] == [
            {"terminal_error_code": "decision_model_mismatch", "requests": 1}
        ]
