"""Protocol-34 crown gate: shared-seed evidence and official standing.

The cases live in ``ditto/tests/fixtures/crown_gate_cases.json`` and are folded
here by the validator and by the Platform projection in
``apps/platform/ditto/tests/api_server/test_crown_gate.py``, so the board's
King and the fleet's King cannot drift apart.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from ditto.api_models.validator import CrownGateMarker
from ditto.validator.config import KOTH_DETHRONE_Z, KOTH_MARGIN
from ditto.validator.weights import CrownGate, _champion
from ditto.validator.worker import _ledger_crown_gate

_CASES = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures/crown_gate_cases.json").read_text()
)["cases"]
_T0 = datetime(2026, 10, 29, tzinfo=UTC)


def _entries(case: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for index, spec in enumerate(case["entries"], start=1):
        entry = SimpleNamespace(
            miner_hotkey=f"hotkey-{spec['name']}",
            agent_id=UUID(int=index),
            composite=spec["composite"],
            composite_stderr=spec["stderr"],
            first_seen=_T0 + timedelta(minutes=spec["minutes"]),
            bench_version=14,
        )
        seeds = spec.get("seeds")
        if seeds:
            ordered = sorted((int(seed), value) for seed, value in seeds.items())
            entry.confirmation_seeds = [seed for seed, _ in ordered]
            entry.confirmation_composites = [value for _, value in ordered]
        out[spec["name"]] = entry
    return out


@pytest.mark.parametrize("case", _CASES, ids=[case["name"] for case in _CASES])
def test_validator_fold_matches_shared_crown_gate_cases(case: dict[str, Any]) -> None:
    entries = _entries(case)
    names = {entry.agent_id: name for name, entry in entries.items()}
    incumbent = entries[case["incumbent"]].agent_id
    gate = CrownGate(**case["gate"])

    def fold(crown_gate: CrownGate | None) -> str:
        champion = _champion(
            list(entries.values()),
            KOTH_MARGIN,
            KOTH_DETHRONE_Z,
            incumbent_agent_id=incumbent,
            crown_gate=crown_gate,
        )
        return names[champion.agent_id]

    assert fold(None) == case["ungated_champion"]
    assert fold(gate) == case["gated_champion"]


def test_only_a_well_formed_served_marker_gates_the_crown() -> None:
    marker = CrownGateMarker(min_dethrone_confirmations=3, champion_standing_rank=3)
    assert _ledger_crown_gate(SimpleNamespace(crown_gate=marker)) == CrownGate(3, 3)
    assert _ledger_crown_gate(SimpleNamespace(crown_gate=None)) is None
    # An older Platform without the field folds the pre-34 walk.
    assert _ledger_crown_gate(SimpleNamespace()) is None
    bogus = SimpleNamespace(min_dethrone_confirmations=1, champion_standing_rank=3)
    assert _ledger_crown_gate(SimpleNamespace(crown_gate=bogus)) is None
