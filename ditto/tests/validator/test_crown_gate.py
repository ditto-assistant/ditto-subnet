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
from ditto_screening_protocol.dueling import (
    DuelingColumnAggregate,
    DuelingReport,
    DuelingWeighting,
    is_held_out_bench_version,
)

_CASES = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures/crown_gate_cases.json").read_text()
)["cases"]
_T0 = datetime(2026, 10, 29, tzinfo=UTC)
_V16_CASES = [case for case in _CASES if case.get("bench_version", 14) >= 16]
_LEGACY_CASES = [case for case in _CASES if case.get("bench_version", 14) < 16]


def _dueling_report(
    case: dict[str, Any], spec: dict[str, Any], owner: str, season: str
) -> DuelingReport:
    """A fixture report: 35 cases per column at the column's mean score,
    weighted for the case's bench version (w = 0.15 and the qualifier from
    v19)."""
    version = case["bench_version"]
    weighting = DuelingWeighting.for_version(version, spec["datagen_raw"])
    return DuelingReport(
        season_id=season,
        season_index=1,
        slice_sha256="ab" * 32,
        owner_key=owner,
        datagen_raw=spec["datagen_raw"],
        datagen_gate=1.0,
        column_weight=weighting.column_weight,
        datagen_qualifier=(
            weighting.qualifier if is_held_out_bench_version(version) else None
        ),
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


# --- Protocol-35 title duel ---------------------------------------------------

_DUEL_CASES = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures/crown_gate_cases.json").read_text()
)["duel_cases"]


def _season_report(
    case: dict[str, Any], spec: dict[str, Any], owner: str, season: str, index: int
) -> DuelingReport:
    return _dueling_report(case, spec, owner, season).model_copy(
        update={"season_index": index}
    )


def duel_ledger(case: dict[str, Any]) -> dict[str, LedgerEntry]:
    """The served rows of a ``duel_cases`` case, mirrored by the Platform test:
    the current season as ``confirmation_history``, every listed season as
    ``duel_history``."""
    version = case["bench_version"]
    out: dict[str, LedgerEntry] = {}
    for position, spec in enumerate(case["entries"], start=1):
        owner, current = spec["owner_key"], spec["season_id"]
        head = _season_report(
            case,
            spec["dueling"],
            owner,
            current,
            spec["duel_seasons"].get(current, {"index": 1})["index"],
        )
        records = [
            ConfirmationScoreRecord(
                seed=int(seed),
                composite=report.self_composite(),
                validator_hotkey="hotkey-validator",
                bench_version=version,
                dueling=report,
            )
            for season, block in sorted(spec["duel_seasons"].items())
            for seed, value in sorted(block["seeds"].items())
            for report in [_season_report(case, value, owner, season, block["index"])]
        ]
        out[spec["name"]] = LedgerEntry(
            miner_hotkey=f"hotkey-{spec['name']}",
            agent_id=UUID(int=position),
            composite=head.self_composite(),
            composite_stderr=spec["stderr"],
            n=350,
            first_seen=_T0 + timedelta(minutes=spec["minutes"]),
            sha256="ab" * 32,
            size_bytes=None,
            score_proofs=[],
            confirmation_composites=None,
            confirmation_seeds=None,
            run_id=f"run-{position}",
            seed=position,
            validator_hotkey="hotkey-validator",
            bench_version=version,
            signature=None,
            status="scored",
            dueling_season_id=current,
            dueling=head,
            confirmation_history=[
                record
                for record in records
                if record.dueling is not None and record.dueling.season_id == current
            ]
            or None,
            duel_history=records or None,
        )
    return out


@pytest.mark.parametrize(
    "case", _DUEL_CASES, ids=[case["name"] for case in _DUEL_CASES]
)
def test_validator_fold_matches_shared_title_duel_cases(case: dict[str, Any]) -> None:
    entries = duel_ledger(case)
    names = {entry.agent_id: name for name, entry in entries.items()}
    incumbent = entries[case["incumbent"]].agent_id

    def fold(crown_gate: CrownGate) -> str:
        champion = select_champion(
            list(entries.values()),
            margin=KOTH_MARGIN,
            dethrone_z=KOTH_DETHRONE_Z,
            incumbent_agent_id=incumbent,
            crown_gate=crown_gate,
        )
        assert champion is not None
        return names[champion.agent_id]

    gate = CrownGate(**case["gate"])
    assert fold(gate) == case["gate_champion"]
    duel = CrownGate(**case["gate"], duel_window=case["duel_window"])
    assert fold(duel) == case["duel_champion"]


def test_a_served_duel_window_rides_the_gate_and_reads_absent_when_bogus() -> None:
    marker = CrownGateMarker(
        min_dethrone_confirmations=3, champion_standing_rank=3, duel_window_pairs=15
    )
    assert _ledger_crown_gate(_ledger(crown_gate=marker)) == CrownGate(3, 3, 15)
    bogus = SimpleNamespace(
        min_dethrone_confirmations=3, champion_standing_rank=3, duel_window_pairs=99
    )
    assert _ledger_crown_gate(_ledger(crown_gate=bogus)) == CrownGate(3, 3)


def test_the_duel_is_inert_below_bench_16() -> None:
    """A served duel window never touches a pre-dueling comparison."""
    case = next(c for c in _CASES if c.get("bench_version", 14) < 16)
    entries = _entries(case)
    incumbent = entries[case["incumbent"]].agent_id
    gate = CrownGate(**case["gate"])
    plain = _champion(
        list(entries.values()),
        KOTH_MARGIN,
        KOTH_DETHRONE_Z,
        incumbent_agent_id=incumbent,
        crown_gate=gate,
    )
    dueled = _champion(
        list(entries.values()),
        KOTH_MARGIN,
        KOTH_DETHRONE_Z,
        incumbent_agent_id=incumbent,
        crown_gate=CrownGate(gate.min_shared_seeds, gate.standing_rank, 15),
    )
    assert plain.agent_id == dueled.agent_id


def test_a_season_scoped_quorum_seed_must_re_derive() -> None:
    """Protocol 35: a season seed is a CRN seed bound to its season anchor and
    is never accepted unverified."""
    from ditto.validator.crn import crn_seed
    from ditto.validator.worker import _season_seed_matches

    anchor = UUID(int=7)
    block_hash = "0x" + "ab" * 32
    seed = crn_seed([str(anchor)], version=16, k=1, block_hash=block_hash)
    job = SimpleNamespace(
        seed=seed,
        seed_scope="season",
        seed_anchor_agent_id=anchor,
        seed_index=1,
        dataset_seed_block_hash=block_hash,
        bench_version=16,
    )
    assert _season_seed_matches(job)
    for change in (
        {"seed": seed + 1},
        {"seed_index": 2},
        {"seed_anchor_agent_id": UUID(int=8)},
        {"dataset_seed_block_hash": None},
        {"seed_index": None},
    ):
        assert not _season_seed_matches(SimpleNamespace(**{**vars(job), **change}))
