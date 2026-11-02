"""Per-sample inference health for one agent's continual retests.

A confirmation sample is an immutable signed composite. Whether it measured the
agent or Platform's inference path is a separate question the score row cannot
answer: on 2026-10-31 two Wonder v6 retests were accepted at 0.65 and 0.70
against a 0.92 median after the gateway refused about half their calls for
exhausted credits, and the only way to see it was to download and aggregate
relay trace segments by hand.

This read pairs each sample with the inference grant that served it (the
newest grant for the same agent, version and validator created at or before the
sample was accepted) and counts that grant's chat requests by terminal status
and error code. It returns counts only: no prompt, answer, dataset or provider
body.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from ditto.db.models import ConfirmationScore, InferenceGrant, InferenceRequest

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

RETEST_SAMPLE_LIMIT = 30
"""Newest samples returned; older ones are already folded and audited."""


@dataclass(frozen=True)
class RetestSampleInference:
    validator_hotkey: str
    seed: int
    composite: float
    run_id: str
    accepted_at: datetime
    void_generation: int
    grant_id: UUID | None
    chat_requests: int
    failed_requests: int
    failure_codes: tuple[tuple[str, int], ...]
    """``(terminal_error_code, count)`` for failed chat requests, most first."""


async def retest_sample_inference(
    session: AsyncSession,
    *,
    agent_id: UUID,
    bench_version: int,
    limit: int = RETEST_SAMPLE_LIMIT,
    run_ids: Collection[str] | None = None,
) -> list[RetestSampleInference]:
    """Newest-first confirmation samples with their serving grant's failures.

    ``run_ids`` narrows the read to exact runs (the void writer's guard), so a
    target older than the newest ``limit`` is still found.
    """
    statement = (
        select(
            ConfirmationScore.validator_hotkey,
            ConfirmationScore.seed,
            ConfirmationScore.composite,
            ConfirmationScore.run_id,
            ConfirmationScore.created_at,
            ConfirmationScore.void_generation,
        )
        .where(
            ConfirmationScore.agent_id == agent_id,
            ConfirmationScore.bench_version == bench_version,
        )
        .order_by(ConfirmationScore.created_at.desc())
        .limit(limit)
    )
    if run_ids is not None:
        statement = statement.where(ConfirmationScore.run_id.in_(list(run_ids)))
    samples = list(await session.execute(statement))
    if not samples:
        return []
    grants_by_validator: dict[str, list[tuple[datetime, UUID]]] = {}
    for grant_id, validator_hotkey, created_at in await session.execute(
        select(
            InferenceGrant.grant_id,
            InferenceGrant.validator_hotkey,
            InferenceGrant.created_at,
        )
        .where(
            InferenceGrant.agent_id == agent_id,
            InferenceGrant.bench_version == bench_version,
        )
        .order_by(InferenceGrant.created_at)
    ):
        grants_by_validator.setdefault(validator_hotkey, []).append(
            (created_at, grant_id)
        )

    def serving_grant(validator_hotkey: str, accepted_at: datetime) -> UUID | None:
        chosen = None
        for created_at, grant_id in grants_by_validator.get(validator_hotkey, ()):
            if created_at > accepted_at:
                break
            chosen = grant_id
        return chosen

    paired = [
        (row, serving_grant(row.validator_hotkey, row.created_at)) for row in samples
    ]
    grant_ids = sorted({grant_id for _, grant_id in paired if grant_id is not None})
    totals: Counter[UUID] = Counter()
    failed: Counter[UUID] = Counter()
    codes: dict[UUID, Counter[str]] = {}
    if grant_ids:
        for grant_id, status, code, count in await session.execute(
            select(
                InferenceRequest.grant_id,
                InferenceRequest.status,
                InferenceRequest.terminal_error_code,
                func.count(),
            )
            .where(
                InferenceRequest.grant_id.in_(grant_ids),
                InferenceRequest.request_kind == "chat",
            )
            .group_by(
                InferenceRequest.grant_id,
                InferenceRequest.status,
                InferenceRequest.terminal_error_code,
            )
        ):
            totals[grant_id] += count
            if status == "failed":
                failed[grant_id] += count
                codes.setdefault(grant_id, Counter())[code or "unknown"] += count
    return [
        RetestSampleInference(
            validator_hotkey=row.validator_hotkey,
            seed=row.seed,
            composite=row.composite,
            run_id=row.run_id,
            accepted_at=row.created_at,
            void_generation=row.void_generation,
            grant_id=grant_id,
            chat_requests=totals[grant_id] if grant_id else 0,
            failed_requests=failed[grant_id] if grant_id else 0,
            failure_codes=tuple(
                sorted(
                    (codes.get(grant_id) or Counter()).items(),
                    key=lambda item: (-item[1], item[0]),
                )
            )
            if grant_id
            else (),
        )
        for row, grant_id in paired
    ]


__all__ = ["RETEST_SAMPLE_LIMIT", "RetestSampleInference", "retest_sample_inference"]
