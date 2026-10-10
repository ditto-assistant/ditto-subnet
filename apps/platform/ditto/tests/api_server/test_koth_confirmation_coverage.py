"""Full-window crown decisions stay deferred, explainable, and schedulable."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.continual_retest_settings import ContinualRetestSettings
from ditto.api_models.validator import ConfirmationScoreRecord, LedgerEntry
from ditto.api_server.endpoints.public import _public_koth_emissions
from ditto.api_server.endpoints.validator import (
    _current_retest_cohort,
    _KothLaneSnapshot,
)
from ditto.api_server.koth import (
    KothEntry,
    _dethrone_decision,
    koth_entries_from_ledger,
    project_koth,
)
from ditto.db.queries.scores import LedgerRow

_T0 = datetime(2026, 10, 3, tzinfo=UTC)


def _entry(marker: int, score: float, *, seeds: tuple[int, ...] | None) -> KothEntry:
    return KothEntry(
        miner_hotkey="5" + str(marker) * 47,
        agent_id=UUID(int=marker),
        composite=score,
        first_seen=_T0 + timedelta(minutes=marker),
        raw_rank=marker,
        bench_version=13,
        confirmation_composites=None if seeds is None else (score,) * len(seeds),
        confirmation_seeds=seeds,
    )


@pytest.mark.parametrize("confirmed_challenger", [False, True])
def test_one_sided_confirmation_defers_both_comparisons(
    confirmed_challenger: bool,
) -> None:
    champion = _entry(1, 0.8, seeds=None if confirmed_challenger else (1, 2, 3))
    challenger = _entry(2, 0.9, seeds=(1, 2, 3) if confirmed_challenger else None)
    assert _dethrone_decision(challenger, champion).dethrones
    decision = _dethrone_decision(challenger, champion, dethrone_seed_full_set=True)
    assert not decision.dethrones
    assert decision.seed_coverage_complete is False
    assert not decision.ceiling_deadlocked


@pytest.mark.parametrize("seeds", [(1,), (1, 1)])
def test_unusable_evidence_cannot_look_like_two_legacy_entries(
    seeds: tuple[int, ...],
) -> None:
    champion = _entry(1, 0.8, seeds=None)
    challenger = _entry(2, 0.9, seeds=seeds)
    assert not _dethrone_decision(
        challenger,
        champion,
        dethrone_seed_full_set=True,
    ).dethrones


def test_wire_lift_preserves_the_presence_of_one_confirmation() -> None:
    entries = [
        LedgerEntry(
            miner_hotkey="5" + str(marker) * 47,
            agent_id=UUID(int=marker),
            composite=score,
            first_seen=_T0 + timedelta(minutes=marker),
            sha256="ab" * 32,
            bench_version=13,
            n=350,
            run_id=str(marker),
            seed=0,
            validator_hotkey="5" + "3" * 47,
            status=AgentStatus.SCORED,
            size_bytes=None,
            signature=None,
            score_proofs=[],
            composite_stderr=None,
            confirmation_composites=None,
            confirmation_seeds=None,
            confirmation_history=[
                ConfirmationScoreRecord(
                    seed=1,
                    composite=score,
                    validator_hotkey="5" + "3" * 47,
                    bench_version=13,
                    signature="cd" * 64,
                )
            ],
        )
        for marker, score in [(1, 0.8), (2, 0.9)]
    ]
    lifted = koth_entries_from_ledger(entries)
    assert all(entry.confirmation_evidence_present for entry in lifted)
    projection = project_koth(lifted, dethrone_seed_full_set=True)
    assert projection is not None and projection.champion.agent_id == UUID(int=1)


@pytest.mark.parametrize("bench_version", [9, 13])
def test_signed_receipt_projection_ignores_unbound_legacy_evidence(
    bench_version: int,
) -> None:
    # Signature/receipt verification happens before the pure projection; this
    # fixture exercises only which already-verified score authority it uses.
    receipt_entry = LedgerEntry.model_construct(
        miner_hotkey="5" + "2" * 47,
        agent_id=UUID(int=2),
        composite=0.1,
        first_seen=_T0 + timedelta(minutes=2),
        bench_version=bench_version,
        v9_confirmation=SimpleNamespace(full_effective_micros=900_000),
        confirmation_composites=[0.1, 0.1],
        confirmation_seeds=[4, 5],
    )
    challenger = koth_entries_from_ledger([receipt_entry])[0]
    champion = replace(_entry(1, 0.8, seeds=(1, 2, 3)), bench_version=bench_version)
    assert challenger.confirmation_receipt_authority
    assert challenger.confirmation_composites is None
    decision = _dethrone_decision(challenger, champion, dethrone_seed_full_set=True)
    assert decision.dethrones and decision.seed_coverage_complete is True


def test_qualifying_partial_lead_does_not_survive_a_sub_band_completion() -> None:
    seeds = tuple(range(15))
    champion = _entry(1, 0.727, seeds=seeds)
    differences = tuple(0.011 + offset for offset in (-0.04, 0.04) * 6 + (0.0,))
    challenger = replace(
        _entry(2, 0.9, seeds=seeds[:13]),
        confirmation_composites=tuple(0.727 + value for value in differences),
    )
    assert _dethrone_decision(challenger, champion).dethrones
    assert not _dethrone_decision(
        challenger,
        champion,
        dethrone_seed_full_set=True,
    ).dethrones
    assert challenger.confirmation_composites is not None
    challenger = replace(
        challenger,
        confirmation_seeds=seeds,
        confirmation_composites=(*challenger.confirmation_composites, 0.71325, 0.71325),
    )
    settled = _dethrone_decision(challenger, champion, dethrone_seed_full_set=True)
    assert settled.seed_coverage_complete is True
    assert settled.challenger_lead == pytest.approx(0.0077)
    assert settled.required_lead == pytest.approx(0.014 * math.exp(-0.254))
    assert not settled.dethrones


def _row(entry: KothEntry) -> LedgerRow:
    return LedgerRow(
        miner_hotkey=entry.miner_hotkey,
        agent_id=entry.agent_id,
        composite=entry.composite,
        tool_mean=entry.composite,
        memory_mean=entry.composite,
        first_seen=entry.first_seen,
        sha256="ab" * 32,
        size_bytes=100,
        run_id=str(entry.agent_id),
        seed=0,
        validator_hotkey="5" + "3" * 47,
        signature="cd" * 64,
        status=AgentStatus.SCORED,
        bench_version=13,
        n=350,
        eligible=True,
    )


def test_public_decisions_serialize_as_finite_numbers_and_report_missing_coverage() -> (
    None
):
    champion = _entry(1, 0.8, seeds=(1, 2, 3))
    challenger = _entry(2, 0.9, seeds=(1, 2))
    emissions = _public_koth_emissions(
        [_row(champion), _row(challenger)],
        stderrs={},
        confirmation_by_seed={
            champion.agent_id: {1: 0.8, 2: 0.8, 3: 0.8},
            challenger.agent_id: {1: 0.9, 2: 0.9},
        },
        dethrone_seed_full_set=True,
    )
    assert emissions is not None and emissions.champion_agent_id == champion.agent_id
    body = emissions.model_dump(mode="json")
    json.dumps(body, allow_nan=False)
    for name in ("raw_leader_decision", "champion_defense"):
        decision = body[name]
        assert decision["seed_coverage_complete"] is False
        assert decision["dethrones"] is False
        assert isinstance(decision["required_lead"], float)


@pytest.mark.asyncio
async def test_retest_cohort_retains_holder_and_incomplete_challenger() -> None:
    champion = _entry(1, 0.8, seeds=(1, 2, 3))
    challenger = _entry(2, 0.9, seeds=(1, 2))
    entries = [champion, challenger]
    snapshot = _KothLaneSnapshot(
        folded_entries=entries,
        raw_emission=tuple(entries),
        all_entries=tuple(entries),
        official_scores={},
        canonical_scores={},
        owner_representatives={},
        owner_by_agent={},
        folded_seeds_by_agent={},
        owner_challengers=(),
    )
    emission, _wave, cohort, _challengers = await _current_retest_cohort(
        cast(AsyncSession, MagicMock()),
        canonical_version=13,
        settings=ContinualRetestSettings(),
        snapshot=snapshot,
        dethrone_seed_full_set=True,
    )
    assert emission[0].agent_id == champion.agent_id
    assert challenger.agent_id in {member.agent_id for member in cohort}
