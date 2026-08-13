"""Consensus activation boundaries for bounded efficiency factors."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest
from fastapi import Request

import ditto.api_server.endpoints.scoring as scoring_mod
from ditto.api_models import LedgerEntry
from ditto.api_models.agent_status import AgentStatus

_MINER = "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"
_VALIDATOR = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"


def test_v20_factor_gate_preserves_legacy_bonus_behavior() -> None:
    legacy_id = uuid4()
    factor_id = uuid4()

    legacy, factors = scoring_mod._fleet_safe_efficiency_adjustments(
        {legacy_id: 0.05},
        {factor_id: 0.85},
        factor_fleet_ready=False,
    )
    assert legacy == {legacy_id: 0.05}
    assert factors == {}

    legacy, factors = scoring_mod._fleet_safe_efficiency_adjustments(
        {legacy_id: 0.05},
        {factor_id: 0.85},
        factor_fleet_ready=True,
    )
    assert legacy == {legacy_id: 0.05}
    assert factors == {factor_id: 0.85}


def test_stale_cache_strips_v20_factor_but_preserves_legacy_bonus() -> None:
    now = datetime.now(UTC)

    def _entry(
        agent_id: UUID,
        *,
        bonus: float | None = None,
        factor: float | None = None,
        effective: float | None = None,
    ) -> LedgerEntry:
        return LedgerEntry(
            miner_hotkey=_MINER,
            agent_id=agent_id,
            composite=0.8,
            n=114,
            first_seen=now,
            sha256="ab" * 32,
            size_bytes=123,
            run_id=f"run-{agent_id}",
            seed=42,
            validator_hotkey=_VALIDATOR,
            bench_version=9,
            signature=None,
            score_proofs=[],
            composite_stderr=None,
            confirmation_composites=None,
            confirmation_seeds=None,
            efficiency_bonus=bonus,
            efficiency_factor=factor,
            effective_composite=effective,
            status=AgentStatus.SCORED,
        )

    legacy_id = uuid4()
    factor_id = uuid4()
    request = cast(
        Request,
        SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    ledger_snapshot=scoring_mod._LedgerSnapshot(
                        entries=[
                            _entry(legacy_id, bonus=0.05, effective=0.84),
                            _entry(factor_id, factor=0.85, effective=0.68),
                        ],
                        generated_at=now,
                        active_bench_version=9,
                    )
                )
            )
        ),
    )

    response = scoring_mod._serve_last_known(
        request, _VALIDATOR, RuntimeError("db down")
    )
    by_id = {entry.agent_id: entry for entry in response.entries}
    assert by_id[legacy_id].efficiency_bonus == pytest.approx(0.05)
    assert by_id[legacy_id].effective_composite == pytest.approx(0.84)
    assert by_id[factor_id].efficiency_factor is None
    assert by_id[factor_id].effective_composite is None
