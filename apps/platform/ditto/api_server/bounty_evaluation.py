"""Bounty evaluation records (#2944): estimates, actuals and pilot decisions.

A bounty pilot is judged against building the same work internally: what the
work was estimated to cost, what it cost, and whether to expand, revise or
stop the pilot. Operators record each as an append-only history:

- an **entry** is one estimate or actual of one metric: build, reviewer,
  integration or operator hours, or turnaround days. The latest entry of a
  bounty's kind and metric is current;
- a **decision** is expand, revise or stop, with how far the work was
  integrated and the reasons for it. The latest decision is current.

Each write names the record it supersedes (0 before the first), so one
operator never overwrites another's record unseen, and an exact retry returns
the record it made. The operator view shows every record with its operator
and reason; the report shows current values and the current decision with its
reasons, and never who recorded them.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, TypeVar
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select

from ditto.api_models.bounty import (
    BountyEvaluationDecisionRequest,
    BountyEvaluationDecisionView,
    BountyEvaluationEntryRequest,
    BountyEvaluationEntryView,
    BountyEvaluationView,
    bounty_evaluation_decision_confirmation,
    bounty_evaluation_entry_confirmation,
)
from ditto.api_server import bounty_claims
from ditto.api_server.bounty_claims import locked_bounty
from ditto.db.models import BountyEvaluationDecision, BountyEvaluationEntry

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

Record = TypeVar("Record", BountyEvaluationEntry, BountyEvaluationDecision)


def _now(clock: Callable[[], datetime] | None) -> datetime:
    return (clock or bounty_claims.utcnow)()


def _confirm(given: str, expected: str) -> None:
    if given != expected:
        raise HTTPException(
            409, f"explicit confirmation is required: type '{expected}'"
        )


def _record_id(record: BountyEvaluationEntry | BountyEvaluationDecision) -> int:
    if isinstance(record, BountyEvaluationEntry):
        return record.entry_id
    return record.decision_id


def _successor(history: Sequence[Record], expected: int) -> Record | None:
    """The record that superseded ``expected`` (0: the first), if any."""
    if expected == 0:
        return history[0] if history else None
    ids = [_record_id(record) for record in history]
    if expected not in ids:
        return None
    following = ids.index(expected) + 1
    return history[following] if following < len(history) else None


def entry_view(entry: BountyEvaluationEntry) -> BountyEvaluationEntryView:
    return BountyEvaluationEntryView(
        entry_id=entry.entry_id,
        kind=entry.kind,  # type: ignore[arg-type]
        metric=entry.metric,  # type: ignore[arg-type]
        value=entry.value,
        note=entry.note,
        reason=entry.reason,
        actor=entry.actor,
        recorded_at=entry.recorded_at,
    )


def decision_view(decision: BountyEvaluationDecision) -> BountyEvaluationDecisionView:
    return BountyEvaluationDecisionView(
        decision_id=decision.decision_id,
        decision=decision.decision,  # type: ignore[arg-type]
        integration_status=decision.integration_status,  # type: ignore[arg-type]
        reasons=decision.reasons,
        actor=decision.actor,
        recorded_at=decision.recorded_at,
    )


async def evaluation_view(
    session: AsyncSession, bounty_id: UUID
) -> BountyEvaluationView:
    """Every evaluation record of one bounty, oldest first."""
    entries = await session.scalars(
        select(BountyEvaluationEntry)
        .where(BountyEvaluationEntry.bounty_id == bounty_id)
        .order_by(BountyEvaluationEntry.entry_id)
    )
    decisions = await session.scalars(
        select(BountyEvaluationDecision)
        .where(BountyEvaluationDecision.bounty_id == bounty_id)
        .order_by(BountyEvaluationDecision.decision_id)
    )
    return BountyEvaluationView(
        entries=[entry_view(entry) for entry in entries],
        decisions=[decision_view(decision) for decision in decisions],
    )


async def record_entry(
    maker: async_sessionmaker[AsyncSession],
    *,
    bounty_id: UUID,
    body: BountyEvaluationEntryRequest,
    actor: str,
    clock: Callable[[], datetime] | None = None,
) -> BountyEvaluationEntryView:
    """Record an estimate or actual, superseding the expected current one."""
    if not body.reason.strip():
        raise HTTPException(400, "reason must not be blank")
    if body.note is not None and not body.note.strip():
        raise HTTPException(400, "note must not be blank; omit it instead")
    async with maker() as session, session.begin():
        bounty = await locked_bounty(session, bounty_id)
        _confirm(
            body.confirmation,
            bounty_evaluation_entry_confirmation(
                repo=bounty.repo,
                issue=bounty.issue_number,
                kind=body.kind,
                metric=body.metric,
            ),
        )
        history = list(
            await session.scalars(
                select(BountyEvaluationEntry)
                .where(
                    BountyEvaluationEntry.bounty_id == bounty_id,
                    BountyEvaluationEntry.kind == body.kind,
                    BountyEvaluationEntry.metric == body.metric,
                )
                .order_by(BountyEvaluationEntry.entry_id)
            )
        )
        current = history[-1].entry_id if history else 0
        if current != body.expected_entry_id:
            # An exact retry of the write that superseded the expected entry.
            made = _successor(history, body.expected_entry_id)
            if made is not None and (
                made.value,
                made.note,
                made.reason,
                made.actor,
            ) == (
                body.value,
                body.note,
                body.reason,
                actor,
            ):
                return entry_view(made)
            raise HTTPException(
                409,
                f"the bounty's {body.kind} of {body.metric} changed; its current "
                f"entry is {current}",
            )
        row = BountyEvaluationEntry(
            bounty_id=bounty_id,
            kind=body.kind,
            metric=body.metric,
            value=body.value,
            note=body.note,
            reason=body.reason,
            actor=actor,
            # Read under the lock, so entries' times order them as they were made.
            recorded_at=_now(clock),
        )
        session.add(row)
        await session.flush()
        return entry_view(row)


async def record_decision(
    maker: async_sessionmaker[AsyncSession],
    *,
    bounty_id: UUID,
    body: BountyEvaluationDecisionRequest,
    actor: str,
    clock: Callable[[], datetime] | None = None,
) -> BountyEvaluationDecisionView:
    """Record the pilot decision, superseding the expected current one."""
    if not body.reasons.strip():
        raise HTTPException(400, "reasons must not be blank")
    async with maker() as session, session.begin():
        bounty = await locked_bounty(session, bounty_id)
        _confirm(
            body.confirmation,
            bounty_evaluation_decision_confirmation(
                repo=bounty.repo, issue=bounty.issue_number, decision=body.decision
            ),
        )
        history = list(
            await session.scalars(
                select(BountyEvaluationDecision)
                .where(BountyEvaluationDecision.bounty_id == bounty_id)
                .order_by(BountyEvaluationDecision.decision_id)
            )
        )
        current = history[-1].decision_id if history else 0
        if current != body.expected_decision_id:
            # An exact retry of the write that superseded the expected decision.
            made = _successor(history, body.expected_decision_id)
            if made is not None and (
                made.decision,
                made.integration_status,
                made.reasons,
                made.actor,
            ) == (body.decision, body.integration_status, body.reasons, actor):
                return decision_view(made)
            raise HTTPException(
                409,
                f"the bounty's evaluation decision changed; its current decision "
                f"is {current}",
            )
        row = BountyEvaluationDecision(
            bounty_id=bounty_id,
            decision=body.decision,
            integration_status=body.integration_status,
            reasons=body.reasons,
            actor=actor,
            recorded_at=_now(clock),
        )
        session.add(row)
        await session.flush()
        return decision_view(row)
