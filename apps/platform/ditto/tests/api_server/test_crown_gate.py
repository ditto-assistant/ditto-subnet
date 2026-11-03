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
from pydantic import ValidationError

from ditto.api_models.continual_retest_settings import (
    MAX_MIN_DETHRONE_CONFIRMATIONS,
    ContinualRetestSettings,
)
from ditto.api_models.validator import ConfirmationScoreRecord, LedgerEntry
from ditto.api_server.koth import (
    TOP5_MAX_CONFIRMATION_SEEDS,
    CrownGate,
    KothEntry,
    crown_standings,
    koth_entries_from_ledger,
    project_koth,
)
from ditto.api_server.ledger_pin import served_crown_gate
from ditto_screening_protocol.dueling import DuelingColumnAggregate, DuelingReport

_FIXTURE = (
    Path(__file__).resolve().parents[5] / "ditto/tests/fixtures/crown_gate_cases.json"
)
_CASES = json.loads(_FIXTURE.read_text())["cases"]
_T0 = datetime(2026, 10, 29, tzinfo=UTC)
_V16_CASES = [case for case in _CASES if case.get("bench_version", 14) >= 16]
_LEGACY_CASES = [case for case in _CASES if case.get("bench_version", 14) < 16]


def _dueling_report(
    case: dict[str, Any], spec: dict[str, Any], owner: str, season: str
) -> DuelingReport:
    """Mirror of the validator test's fixture report builder."""
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


def _dueling_ledger(case: dict[str, Any]) -> list[LedgerEntry]:
    """The same served v16 rows ``ditto/tests/validator/test_crown_gate.py``
    folds, lifted here exactly as the epoch pin lifts them."""
    version = case["bench_version"]
    out: list[LedgerEntry] = []
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
        out.append(
            LedgerEntry(
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
        )
    return out


def _entries(case: dict[str, Any]) -> dict[str, KothEntry]:
    out: dict[str, KothEntry] = {}
    for index, spec in enumerate(case["entries"], start=1):
        seeds = sorted(
            (int(seed), value) for seed, value in spec.get("seeds", {}).items()
        )
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


@pytest.mark.parametrize(
    "case", _LEGACY_CASES, ids=[case["name"] for case in _LEGACY_CASES]
)
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


@pytest.mark.parametrize("case", _V16_CASES, ids=[case["name"] for case in _V16_CASES])
def test_projection_matches_shared_dueling_cases(case: dict[str, Any]) -> None:
    """Bench v16: the pinned projection crowns the King the fleet pays."""
    entries = koth_entries_from_ledger(_dueling_ledger(case))
    names = {
        entry.agent_id: spec["name"]
        for entry, spec in zip(entries, case["entries"], strict=True)
    }
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
    served = {
        "crown_gate": {"min_dethrone_confirmations": 3, "champion_standing_rank": 3}
    }
    assert served_crown_gate(served) == CrownGate(3, 3)
    assert served_crown_gate({}) is None
    bad = {"crown_gate": {"min_dethrone_confirmations": 1, "champion_standing_rank": 3}}
    assert served_crown_gate(bad) is None
    # Replay parses against the validator's 15-seed fold ceiling, exactly as
    # ``ditto.validator.worker._ledger_crown_gate`` does, not the work cap.
    deep = {
        "crown_gate": {"min_dethrone_confirmations": 12, "champion_standing_rank": 3}
    }
    assert served_crown_gate(deep) == CrownGate(12, 3)
    past = {
        "crown_gate": {"min_dethrone_confirmations": 16, "champion_standing_rank": 3}
    }
    assert served_crown_gate(past) is None


def test_operators_cannot_require_more_confirmations_than_the_work_cap() -> None:
    assert MAX_MIN_DETHRONE_CONFIRMATIONS == TOP5_MAX_CONFIRMATION_SEEDS == 10
    ContinualRetestSettings(min_dethrone_confirmations=10)
    with pytest.raises(ValidationError):
        ContinualRetestSettings(min_dethrone_confirmations=11)
