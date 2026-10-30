"""Protocol-34 crown gate in the Platform projection.

Folds the same ``ditto/tests/fixtures/crown_gate_cases.json`` cases as the
validator's ``ditto/tests/validator/test_crown_gate.py``: one fixture, two folds,
so the board's King is the King the fleet pays.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from ditto.api_server.koth import (
    CrownGate,
    KothEntry,
    crown_standings,
    project_koth,
)
from ditto.api_server.ledger_pin import served_crown_gate

_FIXTURE = (
    Path(__file__).resolve().parents[5] / "ditto/tests/fixtures/crown_gate_cases.json"
)
_CASES = json.loads(_FIXTURE.read_text())["cases"]
_T0 = datetime(2026, 10, 29, tzinfo=UTC)


def _entries(case: dict[str, Any]) -> dict[str, KothEntry]:
    out: dict[str, KothEntry] = {}
    for index, spec in enumerate(case["entries"], start=1):
        seeds = sorted((int(seed), value) for seed, value in spec.get("seeds", {}).items())
        out[spec["name"]] = KothEntry(
            miner_hotkey=f"hotkey-{spec['name']}",
            agent_id=UUID(int=index),
            composite=spec["composite"],
            composite_stderr=spec["stderr"],
            first_seen=_T0 + timedelta(minutes=spec["minutes"]),
            raw_rank=index,
            bench_version=14,
            confirmation_seeds=tuple(seed for seed, _ in seeds) if seeds else None,
            confirmation_composites=tuple(value for _, value in seeds)
            if seeds
            else None,
        )
    return out


@pytest.mark.parametrize("case", _CASES, ids=[case["name"] for case in _CASES])
def test_projection_matches_shared_crown_gate_cases(case: dict[str, Any]) -> None:
    entries = _entries(case)
    names = {entry.agent_id: name for name, entry in entries.items()}
    incumbent = entries[case["incumbent"]].agent_id

    def fold(gate: CrownGate | None) -> str:
        projection = project_koth(
            list(entries.values()), incumbent_agent_id=incumbent, crown_gate=gate
        )
        assert projection is not None
        return names[projection.champion.agent_id]

    assert fold(None) == case["ungated_champion"]
    assert fold(CrownGate(**case["gate"])) == case["gated_champion"]


def test_standings_explain_what_each_challenger_needs() -> None:
    case = next(c for c in _CASES if c["name"].startswith("two shared seeds"))
    entries = list(_entries(case).values())
    gate = CrownGate(**case["gate"])
    projection = project_koth(
        entries, incumbent_agent_id=entries[0].agent_id, crown_gate=gate
    )
    standings = crown_standings(entries, projection, crown_gate=gate)

    assert standings is not None
    (challenger,) = standings.challengers
    # The challenger leads the King on official order and on its two shared
    # seeds, but two of three confirmations cannot move the crown.
    assert challenger.official_rank == 1
    assert standings.king_official_rank == 2
    assert challenger.shared_seeds == 2
    assert challenger.decision.method == "paired"
    assert challenger.decision.challenger_lead > challenger.decision.required_lead
    assert challenger.decision.gate_blocked
    assert not challenger.decision.dethrones


def test_pins_replay_only_a_well_formed_gate() -> None:
    served = {"crown_gate": {"min_dethrone_confirmations": 3, "champion_standing_rank": 3}}
    assert served_crown_gate(served) == CrownGate(3, 3)
    assert served_crown_gate({}) is None
    bad = {"crown_gate": {"min_dethrone_confirmations": 1, "champion_standing_rank": 3}}
    assert served_crown_gate(bad) is None
