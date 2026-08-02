"""Unit tests for :mod:`ditto.api_server.endpoints.retrieval`.

These cover endpoint-layer wiring only: the query layer is replaced via
``monkeypatch`` so the tests do not touch a real database. Query-layer
behaviour (latest-by-created_at semantics, status-unfiltered ordering)
is covered separately in :mod:`ditto.tests.db.queries.test_agents`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from ditto.api_server.dependencies import get_session
from ditto.api_server.middleware.error_envelope import (
    ERROR_CODE_AGENT_NOT_FOUND,
    ERROR_CODE_HOTKEY_AGENT_NOT_FOUND,
    ERROR_CODE_VALIDATION,
)

_HOTKEY = "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"


def _override_session_with_dummy(app: FastAPI) -> None:
    """Endpoint queries are monkey-patched, so the session is unused.

    Wire a dummy yielder so the ``Depends(get_session)`` resolves
    without touching a real database.
    """

    async def _dummy_session() -> AsyncIterator[MagicMock]:
        yield MagicMock()

    app.dependency_overrides[get_session] = _dummy_session


class TestAgentByHotkey:
    async def test_404_envelope_when_query_returns_none(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Missing hotkey -> HotkeyAgentNotFoundError -> 404 + code 1201."""
        _override_session_with_dummy(app)

        async def _no_agent(*_args: object, **_kwargs: object) -> None:
            return None

        monkeypatch.setattr(
            "ditto.api_server.endpoints.retrieval.get_latest_agent_by_hotkey",
            _no_agent,
        )

        response = await client.get(
            f"/api/v1/retrieval/agent-by-hotkey?miner_hotkey={_HOTKEY}"
        )
        assert response.status_code == 404
        body = response.json()
        assert body["error_code"] == ERROR_CODE_HOTKEY_AGENT_NOT_FOUND

    async def test_banned_hotkey_surfaces_banned_status(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A hotkey-level ban is reported as ``banned`` even if the latest
        agent's own status is ``scored``."""
        from datetime import UTC, datetime
        from types import SimpleNamespace

        from ditto.db.models import AgentStatus

        _override_session_with_dummy(app)

        agent = SimpleNamespace(
            agent_id=uuid4(),
            miner_hotkey=_HOTKEY,
            name="alpha",
            version=2,
            status=AgentStatus.SCORED,  # the agent itself is fine...
            sha256="ab" * 32,
            created_at=datetime.now(UTC),
            screening_reason="Submission needs a dependency update",
            screening_reason_code="docker-build",
        )

        async def _agent(*_a: object, **_k: object) -> object:
            return agent

        async def _banned(*_a: object, **_k: object) -> bool:
            return True

        monkeypatch.setattr(
            "ditto.api_server.endpoints.retrieval.get_latest_agent_by_hotkey", _agent
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.retrieval.is_hotkey_banned", _banned
        )

        response = await client.get(
            f"/api/v1/retrieval/agent-by-hotkey?miner_hotkey={_HOTKEY}"
        )
        assert response.status_code == 200
        # ...but the miner is banned, so the response says so.
        assert response.json()["status"] == AgentStatus.BANNED.value
        assert response.json()["version"] == 2
        assert response.json()["screening_reason"] == agent.screening_reason

    async def test_malformed_hotkey_returns_422(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
    ) -> None:
        """Regression guard for the ``Query(pattern=_SS58_PATTERN)`` decorator.

        Removing the ``pattern`` constraint in a future refactor would
        silently weaken input validation; this test fires before that
        regression reaches main.
        """
        _override_session_with_dummy(app)

        response = await client.get(
            "/api/v1/retrieval/agent-by-hotkey?miner_hotkey=not-an-ss58"
        )
        assert response.status_code == 422
        assert response.json()["error_code"] == ERROR_CODE_VALIDATION


class TestAgentStatus:
    async def test_returns_miner_visible_screening_reason(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from types import SimpleNamespace

        from ditto.db.models import AgentStatus

        _override_session_with_dummy(app)
        agent = SimpleNamespace(
            agent_id=uuid4(),
            status=AgentStatus.REJECTED,
            screening_reason="Remove the bundled credential and resubmit",
            screening_reason_code="source-safety",
        )

        async def _agent(*_args: object, **_kwargs: object) -> object:
            return agent

        monkeypatch.setattr(
            "ditto.api_server.endpoints.retrieval.get_agent_by_id",
            _agent,
        )

        response = await client.get(f"/api/v1/retrieval/agent/{agent.agent_id}/status")

        assert response.status_code == 200
        assert response.json() == {
            "agent_id": str(agent.agent_id),
            "status": AgentStatus.REJECTED.value,
            "screening_reason": agent.screening_reason,
            "screening_reason_code": agent.screening_reason_code,
        }

    async def test_404_envelope_when_query_returns_none(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Missing agent -> AgentNotFoundError -> 404 + code 1200."""
        _override_session_with_dummy(app)

        async def _no_agent(*_args: object, **_kwargs: object) -> None:
            return None

        monkeypatch.setattr(
            "ditto.api_server.endpoints.retrieval.get_agent_by_id",
            _no_agent,
        )

        agent_id = uuid4()
        response = await client.get(f"/api/v1/retrieval/agent/{agent_id}/status")
        assert response.status_code == 404
        body = response.json()
        assert body["error_code"] == ERROR_CODE_AGENT_NOT_FOUND

    async def test_malformed_uuid_returns_422(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
    ) -> None:
        """Regression guard for the FastAPI ``UUID`` path-param coercion.

        Removing the type hint in a future refactor would degrade the
        404 path into a 500 (no UUID type to coerce against); this test
        catches that.
        """
        _override_session_with_dummy(app)

        response = await client.get("/api/v1/retrieval/agent/not-a-uuid/status")
        assert response.status_code == 422
        assert response.json()["error_code"] == ERROR_CODE_VALIDATION
