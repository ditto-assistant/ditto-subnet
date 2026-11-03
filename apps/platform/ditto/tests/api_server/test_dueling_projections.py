"""Bench v16: database-sourced KOTH projections fold like the pinned ledger.

The retest lane (``_current_koth_entries``) and the public board
(``_public_koth_emissions``) build their entries from ledger rows and stored
confirmation records instead of the served ``LedgerEntry`` list. Both must
pair-mask and season-isolate exactly as the pinned projection and the
validator do, so they fold the shared ``crown_gate_cases.json`` v16 cases to
the same King and tail.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from ditto.api_server.endpoints import public as public_endpoint
from ditto.api_server.endpoints import validator as validator_endpoint
from ditto.api_server.koth import CrownGate, project_koth
from ditto.db.queries.scores import LedgerRow
from ditto.tests.api_server.test_crown_gate import _T0, _V16_CASES, _dueling_report

_BASE58 = "ABCDEFGHJKLMNPQRSTUVWXYZ"


@dataclass(frozen=True)
class _DuelingLedgerRow(LedgerRow):
    """A ledger row carrying the representative score's dueling columns."""

    dueling_season_id: str | None = None
    dueling: dict | None = None


def _hotkey(index: int) -> str:
    return "5" + "F" * 46 + _BASE58[index]


def _case_rows(
    case: dict[str, Any],
) -> tuple[
    list[LedgerRow],
    dict[UUID, dict[int, float]],
    dict[UUID, list[SimpleNamespace]],
    dict[UUID, str],
]:
    """Ledger rows, per-seed medians and raw dueling records for one case."""
    version = case["bench_version"]
    rows: list[LedgerRow] = []
    by_seed: dict[UUID, dict[int, float]] = {}
    history: dict[UUID, list[SimpleNamespace]] = {}
    names: dict[UUID, str] = {}
    for index, spec in enumerate(case["entries"], start=1):
        agent_id = UUID(int=index)
        names[agent_id] = spec["name"]
        owner = spec["owner_key"]
        season = spec["season_id"] or "s16-unknown"
        seeds = {
            int(seed): _dueling_report(case, value, owner, season)
            for seed, value in spec.get("dueling_seeds", {}).items()
        }
        head = (
            _dueling_report(case, spec["dueling"], owner, season)
            if spec["dueling"] is not None
            else None
        )
        composite = (
            head.self_composite()
            if head is not None
            else next(iter(seeds.values())).self_composite()
        )
        rows.append(
            _DuelingLedgerRow(
                miner_hotkey=_hotkey(index),
                agent_id=agent_id,
                composite=composite,
                tool_mean=composite,
                memory_mean=composite,
                first_seen=_T0 + timedelta(minutes=spec["minutes"]),
                size_bytes=None,
                sha256="ab" * 32,
                run_id=f"run-{index}",
                seed=index,
                validator_hotkey="hotkey-validator",
                signature=None,
                status="scored",
                emission_owner_root=owner,
                bench_version=version,
                n=350,
                eligible=True,
                details={},
                stored_composite_stderr=spec["stderr"],
                dueling_season_id=spec["season_id"],
                dueling=None if head is None else head.model_dump(mode="json"),
            )
        )
        by_seed[agent_id] = {
            seed: report.self_composite() for seed, report in sorted(seeds.items())
        }
        history[agent_id] = [
            SimpleNamespace(
                seed=seed,
                bench_version=version,
                dueling=None if head is None else report.model_dump(mode="json"),
            )
            for seed, report in sorted(seeds.items())
        ]
    return rows, by_seed, history, names


@pytest.mark.parametrize("case", _V16_CASES, ids=[case["name"] for case in _V16_CASES])
def test_public_board_folds_the_shared_dueling_cases(case: dict[str, Any]) -> None:
    rows, by_seed, history, names = _case_rows(case)

    def board(gate: CrownGate | None) -> Any:
        emissions = public_endpoint._public_koth_emissions(
            rows,
            stderrs={row.agent_id: row.stored_composite_stderr for row in rows},
            confirmation_by_seed=by_seed,
            dueling_history=history,
            anchor_version=case["bench_version"],
            crown_gate=gate,
        )
        assert emissions is not None
        return emissions

    assert names[board(None).champion_agent_id] == case["ungated_champion"]
    assert (
        names[board(CrownGate(**case["gate"])).champion_agent_id]
        == case["gated_champion"]
    )
    if "tail" in case:
        recipients = [names[recipient.agent_id] for recipient in board(None).recipients]
        assert recipients[1:] == case["tail"]


@pytest.mark.parametrize("case", _V16_CASES, ids=[case["name"] for case in _V16_CASES])
async def test_retest_lane_folds_the_shared_dueling_cases(
    case: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    rows, by_seed, history, names = _case_rows(case)
    for name, value in {
        "list_eligible_ledger": rows,
        "quorum_composites": {},
        "confirmation_composites_by_seed": by_seed,
        "confirmation_history_by_agent": history,
        "resolve_efficiency_adjustments": ({}, {}, {}),
        "reign_seed_planning": validator_endpoint.LEGACY_PLANNING,
    }.items():
        monkeypatch.setattr(validator_endpoint, name, AsyncMock(return_value=value))

    snapshot = await validator_endpoint._current_koth_entries(
        AsyncMock(), canonical_version=case["bench_version"]
    )
    entries = list(snapshot.folded_entries)
    incumbent = next(
        entry.agent_id
        for entry in entries
        if names[entry.agent_id] == case["incumbent"]
    )

    def fold(gate: CrownGate | None) -> str:
        projection = project_koth(
            entries, incumbent_agent_id=incumbent, crown_gate=gate
        )
        assert projection is not None
        return names[projection.champion.agent_id]

    assert fold(None) == case["ungated_champion"]
    assert fold(CrownGate(**case["gate"])) == case["gated_champion"]
    if "tail" in case:
        projection = project_koth(entries, incumbent_agent_id=incumbent)
        assert projection is not None
        assert [names[entry.agent_id] for entry in projection.tail] == case["tail"]
