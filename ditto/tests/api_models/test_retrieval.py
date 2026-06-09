"""Contract tests for :mod:`ditto.api_models.retrieval`.

Each test parses a versioned fixture JSON file as the published wire
contract. A future field rename or new required field would have to
update the fixture too, which surfaces the change to any downstream
consumer that depends on the same fixtures.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from ditto.api_models.retrieval import AgentResponse, AgentStatusResponse
from ditto.db.models import AgentStatus

_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "retrieval"


def _load_fixture(name: str) -> dict[str, object]:
    return json.loads((_FIXTURES_DIR / name).read_text())


class TestAgentResponse:
    def test_parses_v1_fixture(self) -> None:
        parsed = AgentResponse.model_validate(_load_fixture("agent_response_v1.json"))
        assert parsed.agent_id == UUID("550e8400-e29b-41d4-a716-446655440000")
        assert parsed.miner_hotkey == "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"
        assert parsed.name == "alpha-agent"
        assert parsed.status == AgentStatus.UPLOADED
        assert parsed.sha256 == "deadbeef" * 8
        assert parsed.created_at == datetime(2026, 6, 8, 12, 0, tzinfo=UTC)

    def test_excludes_ip_address(self) -> None:
        """Privacy-by-design: ``ip_address`` is never serialised.

        The column exists on the ORM model and is captured at upload,
        but the wire shape must never echo it. A column drop PR removes
        the underlying field; until then this guards the wire boundary.
        """
        parsed = AgentResponse.model_validate(_load_fixture("agent_response_v1.json"))
        dumped = parsed.model_dump()
        assert "ip_address" not in dumped


class TestAgentStatusResponse:
    def test_parses_v1_fixture(self) -> None:
        parsed = AgentStatusResponse.model_validate(
            _load_fixture("agent_status_response_v1.json")
        )
        assert parsed.agent_id == UUID("550e8400-e29b-41d4-a716-446655440000")
        assert parsed.status == AgentStatus.SCREENING

    def test_minimal_shape(self) -> None:
        """Polling endpoint: only two fields. Any extra field is a
        regression on the wire-size contract."""
        parsed = AgentStatusResponse.model_validate(
            _load_fixture("agent_status_response_v1.json")
        )
        dumped = parsed.model_dump()
        assert set(dumped.keys()) == {"agent_id", "status"}
