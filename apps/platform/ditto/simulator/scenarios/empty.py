"""Baseline scenario: an empty database.

The CLI wipes all simulator tables before every scenario (unless
``--no-wipe``), so this scenario seeds nothing — useful for resetting the
dashboard to its zero state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ditto.simulator.scenarios import ScenarioContext

NAME = "empty"
DESCRIPTION = "Wipe only: reset the dashboard to a zero-state database."


async def apply(_ctx: ScenarioContext) -> None:
    """Seed nothing; the default pre-scenario wipe already emptied the tables."""
