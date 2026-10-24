"""Round panel sizing when a reference agent is also a ranked agent."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from ditto.api_models.generator import GeneratorCompetitionSettings
from ditto.api_server import generator_competition
from ditto.api_server.generator_rounds import select_round_panel


async def test_a_ranked_reference_frees_its_ranked_seat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ranked_ids = [str(uuid4()) for _ in range(4)]

    async def _select_panel(_session: Any, *, size: int) -> list[dict[str, Any]]:
        return [
            {"agent_id": agent_id, "owner_key": f"o{i}", "champion": i == 0}
            for i, agent_id in enumerate(ranked_ids[:size])
        ]

    monkeypatch.setattr(generator_competition, "select_panel", _select_panel)
    session = SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(
                status="live", screened_image_sha256="ab" * 32, miner_hotkey="5Ref"
            )
        )
    )
    # The champion is also the configured reference agent.
    settings = GeneratorCompetitionSettings(
        panel_size=3,
        min_panel=4,
        min_solver_owners=2,
        reference_agent_ids=[UUID(ranked_ids[0])],
    )

    panel, skipped = await select_round_panel(session, settings)  # type: ignore[arg-type]

    assert skipped == []
    assert [slot["agent_id"] for slot in panel] == [*ranked_ids[1:4], ranked_ids[0]]
    assert len(panel) == settings.panel_size + 1 >= settings.min_panel
    reference = panel[-1]
    assert reference["reference"] and reference["champion"]
    assert reference["owner_key"] == f"reference:{ranked_ids[0]}"
