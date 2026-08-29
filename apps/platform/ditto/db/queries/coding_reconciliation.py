"""Single-artifact reconciliation for the shadow coding pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from ditto.chain.models import BlockInfo
from ditto.coding_selection import CodingPrivateCatalogSource
from ditto.db.models import CodingShadowRunIssuance
from ditto.db.queries.coding_assignments import (
    CodingAssignmentFinalizedSource,
    CodingAssignmentPolicy,
    create_coding_selection_assignment,
)
from ditto.db.queries.coding_issuance import (
    CodingIssuanceFinalizedSource,
    CodingIssuancePolicy,
    issue_finalized_shadow_coding_run,
)


class CodingReconciliationFinalizedSource(
    CodingAssignmentFinalizedSource,
    CodingIssuanceFinalizedSource,
    Protocol,
):
    """Canonical finalized-chain authority needed by both phases."""


class CodingReconciliationState(StrEnum):
    WAITING_FINALITY = "waiting_finality"
    ISSUED = "issued"
    ALREADY_ISSUED = "already_issued"


@dataclass(frozen=True)
class CodingReconciliationPolicy:
    selection_delay_blocks: int
    external_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        self.assignment_policy()
        self.issuance_policy()

    def assignment_policy(self) -> CodingAssignmentPolicy:
        return CodingAssignmentPolicy(
            selection_delay_blocks=self.selection_delay_blocks,
        )

    def issuance_policy(self) -> CodingIssuancePolicy:
        return CodingIssuancePolicy(
            external_timeout_seconds=self.external_timeout_seconds,
        )


@dataclass(frozen=True)
class CodingReconciliationResult:
    state: CodingReconciliationState
    assignment_row_id: UUID
    selection_block_number: int
    run_row_id: UUID | None
    assignment_idempotent: bool
    issuance_idempotent: bool | None
    weight_eligible: bool = False


async def reconcile_shadow_coding_run(
    session: AsyncSession,
    *,
    finalized_source: CodingReconciliationFinalizedSource,
    catalog_source: CodingPrivateCatalogSource,
    agent_id: UUID,
    bench_version: int,
    coding_run_id: str,
    corpus_release_id: str,
    policy: CodingReconciliationPolicy,
) -> CodingReconciliationResult:
    """Advance one explicit artifact from assignment to finalized issuance.

    The caller owns the database transaction. The finalized head read below is
    only a cheap readiness hint; the issuer independently fetches the exact
    assigned height, its timestamp, and catalog proof before persisting a run.
    """

    assignment = await create_coding_selection_assignment(
        session,
        finalized_source=finalized_source,
        agent_id=agent_id,
        bench_version=bench_version,
        coding_run_id=coding_run_id,
        corpus_release_id=corpus_release_id,
        policy=policy.assignment_policy(),
    )
    stored_issuance = await session.get(
        CodingShadowRunIssuance,
        assignment.row.assignment_row_id,
    )
    if stored_issuance is not None:
        issuance = await issue_finalized_shadow_coding_run(
            session,
            assignment_row_id=assignment.row.assignment_row_id,
            finalized_source=finalized_source,
            catalog_source=catalog_source,
            policy=policy.issuance_policy(),
        )
        return CodingReconciliationResult(
            state=CodingReconciliationState.ALREADY_ISSUED,
            assignment_row_id=assignment.row.assignment_row_id,
            selection_block_number=assignment.assignment.selection_block_number,
            run_row_id=issuance.run.run_row_id,
            assignment_idempotent=assignment.idempotent,
            issuance_idempotent=True,
        )
    finalized_head: BlockInfo = await finalized_source.get_finalized_block()
    selection_height = assignment.assignment.selection_block_number
    if finalized_head.number < selection_height:
        return CodingReconciliationResult(
            state=CodingReconciliationState.WAITING_FINALITY,
            assignment_row_id=assignment.row.assignment_row_id,
            selection_block_number=selection_height,
            run_row_id=None,
            assignment_idempotent=assignment.idempotent,
            issuance_idempotent=None,
        )

    issuance = await issue_finalized_shadow_coding_run(
        session,
        assignment_row_id=assignment.row.assignment_row_id,
        finalized_source=finalized_source,
        catalog_source=catalog_source,
        policy=policy.issuance_policy(),
    )
    return CodingReconciliationResult(
        state=(
            CodingReconciliationState.ALREADY_ISSUED
            if issuance.idempotent
            else CodingReconciliationState.ISSUED
        ),
        assignment_row_id=assignment.row.assignment_row_id,
        selection_block_number=selection_height,
        run_row_id=issuance.run.run_row_id,
        assignment_idempotent=assignment.idempotent,
        issuance_idempotent=issuance.idempotent,
    )
