"""Tests for single-artifact shadow coding reconciliation."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from ditto.chain.models import BlockInfo
from ditto.db.queries import coding_reconciliation
from ditto.db.queries.coding_reconciliation import (
    CodingReconciliationPolicy,
    CodingReconciliationState,
    reconcile_shadow_coding_run,
)


def _assignment(*, height: int, idempotent: bool) -> SimpleNamespace:
    return SimpleNamespace(
        row=SimpleNamespace(assignment_row_id=uuid4()),
        assignment=SimpleNamespace(selection_block_number=height),
        idempotent=idempotent,
    )


def _session(*, stored_issuance: object | None = None) -> AsyncMock:
    session = AsyncMock()
    session.get = AsyncMock(return_value=stored_issuance)
    return session


class _FinalizedSource:
    def __init__(self, number: int) -> None:
        self.number = number
        self.head_calls = 0

    async def get_finalized_block(self) -> BlockInfo:
        self.head_calls += 1
        return BlockInfo(number=self.number, hash="0x" + "11" * 32)

    async def get_finalized_block_hash(self, block_number: int) -> str:
        return "0x" + f"{block_number % 256:02x}" * 32

    async def get_block_timestamp(self, block_hash: str) -> int:
        del block_hash
        return 1_800_000_000


def test_reconciliation_policy_reuses_phase_bounds() -> None:
    policy = CodingReconciliationPolicy(
        selection_delay_blocks=20,
        external_timeout_seconds=12.5,
    )
    assert policy.assignment_policy().selection_delay_blocks == 20
    assert policy.issuance_policy().external_timeout_seconds == 12.5
    with pytest.raises(ValueError, match="selection delay"):
        CodingReconciliationPolicy(selection_delay_blocks=0)
    with pytest.raises(ValueError, match="timeout"):
        CodingReconciliationPolicy(
            selection_delay_blocks=20,
            external_timeout_seconds=0.5,
        )


async def test_reconciler_waits_without_touching_private_catalog(monkeypatch) -> None:
    assignment = _assignment(height=120, idempotent=False)
    create = AsyncMock(return_value=assignment)
    issue = AsyncMock()
    monkeypatch.setattr(
        coding_reconciliation, "create_coding_selection_assignment", create
    )
    monkeypatch.setattr(
        coding_reconciliation, "issue_finalized_shadow_coding_run", issue
    )
    source = _FinalizedSource(number=119)

    result = await reconcile_shadow_coding_run(
        _session(),
        finalized_source=source,
        catalog_source=AsyncMock(),
        agent_id=uuid4(),
        bench_version=12,
        coding_run_id="shadow-run-001",
        corpus_release_id="private-corpus-v1",
        policy=CodingReconciliationPolicy(selection_delay_blocks=20),
    )

    assert result.state is CodingReconciliationState.WAITING_FINALITY
    assert result.run_row_id is None
    assert result.weight_eligible is False
    assert source.head_calls == 1
    issue.assert_not_awaited()


@pytest.mark.parametrize(
    "idempotent,state",
    [
        (False, CodingReconciliationState.ISSUED),
        (True, CodingReconciliationState.ALREADY_ISSUED),
    ],
)
async def test_reconciler_issues_only_after_finalized_readiness(
    monkeypatch,
    idempotent: bool,
    state: CodingReconciliationState,
) -> None:
    assignment = _assignment(height=120, idempotent=True)
    run_id = uuid4()
    create = AsyncMock(return_value=assignment)
    issue = AsyncMock(
        return_value=SimpleNamespace(
            idempotent=idempotent,
            run=SimpleNamespace(run_row_id=run_id),
        )
    )
    monkeypatch.setattr(
        coding_reconciliation, "create_coding_selection_assignment", create
    )
    monkeypatch.setattr(
        coding_reconciliation, "issue_finalized_shadow_coding_run", issue
    )
    source = _FinalizedSource(number=120)
    catalog = AsyncMock()

    session = _session()
    result = await reconcile_shadow_coding_run(
        session,
        finalized_source=source,
        catalog_source=catalog,
        agent_id=uuid4(),
        bench_version=12,
        coding_run_id="shadow-run-001",
        corpus_release_id="private-corpus-v1",
        policy=CodingReconciliationPolicy(selection_delay_blocks=20),
    )

    assert result.state is state
    assert result.run_row_id == run_id
    assert result.assignment_idempotent is True
    assert result.issuance_idempotent is idempotent
    issue.assert_awaited_once_with(
        session,
        assignment_row_id=assignment.row.assignment_row_id,
        finalized_source=source,
        catalog_source=catalog,
        policy=CodingReconciliationPolicy(selection_delay_blocks=20).issuance_policy(),
    )


async def test_reconciler_replays_stored_issuance_without_chain_read(
    monkeypatch,
) -> None:
    assignment = _assignment(height=120, idempotent=True)
    run_id = uuid4()
    monkeypatch.setattr(
        coding_reconciliation,
        "create_coding_selection_assignment",
        AsyncMock(return_value=assignment),
    )
    issue = AsyncMock(
        return_value=SimpleNamespace(
            idempotent=True,
            run=SimpleNamespace(run_row_id=run_id),
        )
    )
    monkeypatch.setattr(
        coding_reconciliation, "issue_finalized_shadow_coding_run", issue
    )
    source = _FinalizedSource(number=0)

    result = await reconcile_shadow_coding_run(
        _session(stored_issuance=object()),
        finalized_source=source,
        catalog_source=AsyncMock(),
        agent_id=uuid4(),
        bench_version=12,
        coding_run_id="shadow-run-001",
        corpus_release_id="private-corpus-v1",
        policy=CodingReconciliationPolicy(selection_delay_blocks=20),
    )

    assert result.state is CodingReconciliationState.ALREADY_ISSUED
    assert result.run_row_id == run_id
    assert source.head_calls == 0


async def test_reconciler_propagates_typed_phase_failures(monkeypatch) -> None:
    failure = TimeoutError("chain unavailable")
    monkeypatch.setattr(
        coding_reconciliation,
        "create_coding_selection_assignment",
        AsyncMock(side_effect=failure),
    )
    issue = AsyncMock()
    monkeypatch.setattr(
        coding_reconciliation, "issue_finalized_shadow_coding_run", issue
    )

    with pytest.raises(TimeoutError, match="chain unavailable"):
        await reconcile_shadow_coding_run(
            _session(),
            finalized_source=_FinalizedSource(number=100),
            catalog_source=AsyncMock(),
            agent_id=uuid4(),
            bench_version=12,
            coding_run_id="shadow-run-001",
            corpus_release_id="private-corpus-v1",
            policy=CodingReconciliationPolicy(selection_delay_blocks=20),
        )
    issue.assert_not_awaited()
