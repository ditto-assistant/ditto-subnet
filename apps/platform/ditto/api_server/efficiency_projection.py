"""Read-only court projection against one existing frozen efficiency cohort."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_server.config import EfficiencyBonusConfig
from ditto.api_server.efficiency import (
    BONUS_RUN_SIZE,
    MIN_BONUS_BENCH_VERSION,
    audited_efficiency_candidates,
    bonus_for_submission,
    epoch_index_for,
    factor_for_submission,
    is_factor_curve,
    reference_from_snapshot,
)
from ditto.db.queries.efficiency import get_bonus_rows, get_snapshot
from ditto.db.queries.scores import LedgerRow


class EfficiencyProjectionUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class EfficiencyAssignment:
    snapshot_id: UUID
    bonus: float
    factor: float | None
    token_total: float | None


@dataclass(frozen=True)
class FrozenEfficiencyProjection:
    assignments: Mapping[UUID, EfficiencyAssignment]
    context: str
    values_digest: str


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()


async def project_frozen_efficiency(
    session: AsyncSession,
    *,
    config: EfficiencyBonusConfig,
    rows: Sequence[LedgerRow],
    bench_version: int,
    now: datetime,
    restored_ids: frozenset[UUID],
) -> FrozenEfficiencyProjection | None:
    """Project only hypothetical insertions; never create a cohort or assignment.

    Current scored rows must already have any computable assignment. A frozen
    inactive cohort or invalid signed costs legitimately produces no assignment.
    Restored rows use the same accepted costs and frozen formulas as the writer.
    """
    if (
        not config.enabled
        or not config.fold_enabled
        or bench_version < MIN_BONUS_BENCH_VERSION
    ):
        return None
    epoch = epoch_index_for(now, config.epoch_hours)
    snapshot = await get_snapshot(
        session, bench_version=bench_version, run_size=BONUS_RUN_SIZE, epoch_index=epoch
    )
    if snapshot is None:
        raise EfficiencyProjectionUnavailable(
            "current frozen efficiency reference unavailable for court"
        )
    reference = reference_from_snapshot(snapshot)
    stored = await get_bonus_rows(
        session,
        [row.agent_id for row in rows],
        bench_versions={row.agent_id: row.bench_version for row in rows},
        epoch_index=epoch,
    )
    if any(value.snapshot_id != snapshot.snapshot_id for value in stored.values()):
        raise EfficiencyProjectionUnavailable(
            "efficiency assignment reference mismatch for court"
        )
    assignments = {
        key: EfficiencyAssignment(
            value.snapshot_id, value.bonus, value.factor, value.token_total
        )
        for key, value in stored.items()
    }
    candidates = (
        await audited_efficiency_candidates(
            session,
            [
                row
                for row in rows
                if row.eligible
                and (
                    row.agent_id not in stored
                    or (
                        is_factor_curve(reference.curve_version)
                        and stored[row.agent_id].factor is None
                    )
                )
            ],
            curve_version=reference.curve_version,
        )
        if snapshot.active
        else []
    )
    for candidate in candidates:
        factor = factor_for_submission(
            candidate.composite, candidate.memory_mean, candidate.token_total, reference
        )
        if is_factor_curve(reference.curve_version) and factor is None:
            continue
        bonus = (
            0.0
            if is_factor_curve(reference.curve_version)
            else bonus_for_submission(
                candidate.composite,
                candidate.memory_mean,
                candidate.token_total,
                reference,
            )
        )
        if candidate.agent_id not in restored_ids:
            raise EfficiencyProjectionUnavailable(
                "current efficiency assignments unavailable for court"
            )
        assignments[candidate.agent_id] = EfficiencyAssignment(
            snapshot.snapshot_id, bonus, factor, candidate.token_total
        )
    return FrozenEfficiencyProjection(
        assignments=assignments,
        context=_digest([epoch, snapshot.snapshot_id, asdict(reference)]),
        values_digest=_digest(
            [
                [
                    str(row.agent_id),
                    row.bench_version,
                    asdict(assignments[row.agent_id])
                    if row.agent_id in assignments
                    else None,
                ]
                for row in sorted(rows, key=lambda row: str(row.agent_id))
            ]
        ),
    )
