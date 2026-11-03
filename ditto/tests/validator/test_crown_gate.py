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
from typing import Any, cast
from uuid import UUID

import pytest

from ditto.api_models.validator import (
    ConfirmationScoreRecord,
    CrownGateMarker,
    LedgerEntry,
    LedgerResponse,
)
from ditto.validator.config import KOTH_DETHRONE_Z, KOTH_MARGIN
from ditto.validator.weights import (
    CrownGate,
    _champion,
    _tail,
    filter_eligible,
    select_champion,
)
from ditto.validator.worker import _ledger_crown_gate
from ditto_screening_protocol.dueling import DuelingColumnAggregate, DuelingReport

_CASES = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures/crown_gate_cases.json").read_text()
)["cases"]
_T0 = datetime(2026, 10, 29, tzinfo=UTC)
_V16_CASES = [case for case in _CASES if case.get("bench_version", 14) >= 16]
_LEGACY_CASES = [case for case in _CASES if case.get("bench_version", 14) < 16]


def _dueling_report(
    case: dict[str, Any], spec: dict[str, Any], owner: str, season: str
) -> DuelingReport:
    """A fixture report: 35 cases per column at the column's mean score."""
    return DuelingReport(
        season_id=season,
        season_index=1,
        slice_sha256="ab" * 32,
        owner_key=owner,
        datagen_raw=spec["datagen_raw"],
        datagen_gate=1.0,
        column_weight=0.1,
        columns=[
            DuelingColumnAggregate(
                column_id=f"c{index}",
                owner_key=column_owner,
                n=35,
                sum=mean * 35,
                sum_sq=mean * mean * 35,
            )
            for index, (column_owner, mean) in enumerate(
                zip(case["slice_owners"], spec["means"], strict=True)
            )
        ],
        composite_stderr=0.01,
    )


def dueling_ledger(case: dict[str, Any]) -> dict[str, LedgerEntry]:
    """The served v16 ledger rows a ``bench_version >= 16`` case describes.

    Mirrored by the Platform test, which lifts the same rows through
    ``koth_entries_from_ledger``: both folds read identical wire bytes.
    """
    version = case["bench_version"]
    out: dict[str, LedgerEntry] = {}
    for index, spec in enumerate(case["entries"], start=1):
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
            else _dueling_report(
                case, next(iter(spec["dueling_seeds"].values())), owner, season
            ).self_composite()
        )
        out[spec["name"]] = LedgerEntry(
            miner_hotkey=f"hotkey-{spec['name']}",
            agent_id=UUID(int=index),
            composite=composite,
            composite_stderr=spec["stderr"],
            n=350,
            first_seen=_T0 + timedelta(minutes=spec["minutes"]),
            sha256="ab" * 32,
            size_bytes=None,
            score_proofs=[],
            confirmation_composites=None,
            confirmation_seeds=None,
            run_id=f"run-{index}",
            seed=index,
            validator_hotkey="hotkey-validator",
            bench_version=version,
            signature=None,
            status="scored",
            dueling_season_id=spec["season_id"],
            dueling=head,
            confirmation_history=[
                ConfirmationScoreRecord(
                    seed=seed,
                    composite=report.self_composite(),
                    validator_hotkey="hotkey-validator",
                    bench_version=version,
                    dueling=report if head is not None else None,
                )
                for seed, report in sorted(seeds.items())
            ]
            or None,
        )
    return out


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


@pytest.mark.parametrize(
    "case", _LEGACY_CASES, ids=[case["name"] for case in _LEGACY_CASES]
)
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


@pytest.mark.parametrize("case", _V16_CASES, ids=[case["name"] for case in _V16_CASES])
def test_validator_fold_matches_shared_dueling_cases(case: dict[str, Any]) -> None:
    """Bench v16: season isolation, pair-masked dethrone and tail order."""
    entries = dueling_ledger(case)
    names = {entry.agent_id: name for name, entry in entries.items()}
    incumbent = entries[case["incumbent"]].agent_id

    def fold(crown_gate: CrownGate | None) -> str:
        champion = select_champion(
            list(entries.values()),
            margin=KOTH_MARGIN,
            dethrone_z=KOTH_DETHRONE_Z,
            incumbent_agent_id=incumbent,
            crown_gate=crown_gate,
        )
        assert champion is not None
        return names[champion.agent_id]

    assert fold(None) == case["ungated_champion"]
    assert fold(CrownGate(**case["gate"])) == case["gated_champion"]
    if "tail" in case:
        pool = [e for e in filter_eligible(list(entries.values())) if e.composite > 0]
        champion = entries[case["ungated_champion"]]
        assert [names[e.agent_id] for e in _tail(pool, champion, 4)] == case["tail"]


def _ledger(**fields: object) -> LedgerResponse:
    return cast(LedgerResponse, SimpleNamespace(**fields))


def test_only_a_well_formed_served_marker_gates_the_crown() -> None:
    marker = CrownGateMarker(min_dethrone_confirmations=3, champion_standing_rank=3)
    assert _ledger_crown_gate(_ledger(crown_gate=marker)) == CrownGate(3, 3)
    assert _ledger_crown_gate(_ledger(crown_gate=None)) is None
    # An older Platform without the field folds the pre-34 walk.
    assert _ledger_crown_gate(_ledger()) is None
    bogus = SimpleNamespace(min_dethrone_confirmations=1, champion_standing_rank=3)
    assert _ledger_crown_gate(_ledger(crown_gate=bogus)) is None
