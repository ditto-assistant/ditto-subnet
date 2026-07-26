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

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.retrieval import AgentResponse, AgentStatusResponse

_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "retrieval"


def _load_fixture(name: str) -> dict[str, object]:
    return json.loads((_FIXTURES_DIR / name).read_text())


class TestAgentResponse:
    def test_parses_v1_fixture(self) -> None:
        parsed = AgentResponse.model_validate(_load_fixture("agent_response_v1.json"))
        assert parsed.agent_id == UUID("550e8400-e29b-41d4-a716-446655440000")
        assert parsed.miner_hotkey == "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"
        assert parsed.name == "alpha-agent"
        assert parsed.version == 1
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

    def test_stable_polling_shape(self) -> None:
        """Polling carries the miner-visible screening reason and its code.

        ``screening_reason`` is prose for a human; ``screening_reason_code`` is
        the stable token a miner's tooling can branch on. The platform has
        served both since it added the code; this copy carried only the prose,
        so pydantic's default ``extra='ignore'`` dropped the code on every poll
        and no client could act on it.
        """
        parsed = AgentStatusResponse.model_validate(
            _load_fixture("agent_status_response_v1.json")
        )
        dumped = parsed.model_dump()
        assert set(dumped.keys()) == {
            "agent_id",
            "status",
            "screening_reason",
            "screening_reason_code",
        }
        assert dumped["screening_reason"] is None
        assert dumped["screening_reason_code"] is None
