"""Close screening quarantines left active behind a terminal agent ruling.

A screening quarantine is only actionable while its exact agent row is still
``quarantined``. A scored policy rescreen can record an active quarantine
while the agent keeps its board position, and an ATH ruling can then move that
same agent to ``banned``. Before ditto-subnet#2038 that left an active
quarantine that no guarded resolver could close: the screening court refuses
to rule on a terminal agent, and the ATH court does not own quarantine rows.
The orphan inflated active counts and made review age look worse than the
actionable queue.

These writers now close such a row, each inside the transaction that holds
the exact agent lock, and none ever changes the agent's terminal status or its
miner-visible reason:

* ``resolve_copy_review`` (and the batched ATH rulings built on it) closes the
  matching active quarantine in the same transaction as a terminal ATH reject;
* the owner-only ``resolve_review`` CLI exit does the same for its ban;
* the fenced screening batch resolver closes a pre-existing orphan when an
  operator previews and executes ``reject`` for the exact agent UUID and
  artifact SHA-256. The preview names the current terminal ruling
  (:func:`current_terminal_ruling`), the preview token signs it, and execution
  re-derives it under the row locks and refuses if it moved.

Each closure appends a ``screening_quarantine_resolutions`` row and a manual
``screening_review_events`` snapshot whose evidence names the terminal ruling,
so the terminal decision and the reconciled quarantine keep one audit trail.
No public moderation record is written: the agent's public status does not
change, and the terminal ruling remains the authoritative outcome.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.agent_status import AgentStatus
from ditto.db.models import (
    Agent,
    AthReview,
    AthReviewAction,
    ScreeningQuarantine,
    ScreeningQuarantineResolution,
    ScreeningReviewEvent,
)
from ditto.db.queries.screening_review_events import append_manual_review_event

TERMINAL_QUARANTINE_AGENT_STATUSES = frozenset(
    {AgentStatus.BANNED, AgentStatus.REJECTED}
)
"""Agent statuses after which an active quarantine can no longer be ruled on."""

TERMINAL_RECONCILIATION_RESOLUTION = "reject"
"""The only quarantine resolution consistent with a terminal agent ruling."""

ReconciliationSource = Literal["ath_ruling", "cli_ban", "operator_reconciliation"]


@dataclass(frozen=True)
class TerminalRuling:
    """The exact decision that made one agent terminal.

    ``fence()`` is what a preview shows, what the preview token signs, and what
    the execute transaction re-derives under the row locks: the agent status,
    the artifact digest, the ATH review, and the specific reject action.
    """

    agent_status: str
    artifact_sha256: str
    ath_review_id: UUID | None
    ath_action_id: UUID | None
    ath_resolved_at: datetime | None

    def fence(self) -> dict[str, Any]:
        return {
            "agent_status": self.agent_status,
            "artifact_sha256": self.artifact_sha256,
            "ath_review_id": (
                str(self.ath_review_id) if self.ath_review_id is not None else None
            ),
            "ath_action_id": (
                str(self.ath_action_id) if self.ath_action_id is not None else None
            ),
            "ath_resolved_at": (
                self.ath_resolved_at.isoformat()
                if self.ath_resolved_at is not None
                else None
            ),
        }


def is_terminal_quarantine_ghost(quarantine: ScreeningQuarantine, agent: Agent) -> bool:
    """True for an active quarantine whose exact agent is already terminal."""
    return (
        quarantine.status == "active"
        and quarantine.agent_id == agent.agent_id
        and agent.status in TERMINAL_QUARANTINE_AGENT_STATUSES
    )


async def lock_active_quarantines(
    session: AsyncSession, *, agent_id: UUID
) -> list[ScreeningQuarantine]:
    """Row-lock the exact agent's active quarantine, if any.

    The screening resolvers lock the quarantine before the agent. A writer
    that must also close the quarantine takes the same order, so an ATH ruling
    and a concurrent screening resolution serialize instead of deadlocking.
    """
    return list(
        (
            await session.scalars(
                select(ScreeningQuarantine)
                .where(
                    ScreeningQuarantine.agent_id == agent_id,
                    ScreeningQuarantine.status == "active",
                )
                .order_by(ScreeningQuarantine.quarantine_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
    )


async def current_terminal_ruling(
    session: AsyncSession, *, agent: Agent
) -> TerminalRuling | None:
    """The ATH reject that currently holds this exact agent terminal, or ``None``.

    Only a ``banned`` agent whose one ``ath_reviews`` row is resolved as
    ``reject`` qualifies. The ruling is that review's newest action, which
    must be the reject itself: a later reopen or clear means the reject is
    history, not the current ruling. A review that recorded the held artifact
    digest must name this agent's digest. Anything else is unidentified, and
    an unidentified ruling is never reconciled.
    """
    if agent.status != AgentStatus.BANNED:
        return None
    review = await session.scalar(
        select(AthReview)
        .where(AthReview.agent_id == agent.agent_id)
        .execution_options(populate_existing=True)
    )
    if review is None or review.status != "resolved" or review.resolution != "reject":
        return None
    latest = await session.scalar(
        select(AthReviewAction)
        .where(AthReviewAction.review_id == review.review_id)
        .order_by(AthReviewAction.created_at.desc(), AthReviewAction.action_id.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )
    if latest is None or latest.action != "reject":
        return None
    reopen = await session.scalar(
        select(AthReviewAction)
        .where(
            AthReviewAction.review_id == review.review_id,
            AthReviewAction.action == "reopen",
        )
        .order_by(AthReviewAction.created_at.desc(), AthReviewAction.action_id.desc())
        .limit(1)
    )
    held_sha256 = (
        reopen.evidence if reopen is not None else review.original_evidence
    ).get("sha256")
    if held_sha256 is not None and held_sha256 != agent.sha256:
        return None
    return TerminalRuling(
        agent_status=agent.status.value,
        artifact_sha256=agent.sha256,
        ath_review_id=review.review_id,
        ath_action_id=latest.action_id,
        ath_resolved_at=review.resolved_at,
    )


async def terminal_reconciliation_record(
    session: AsyncSession, *, quarantine: ScreeningQuarantine
) -> ScreeningReviewEvent | None:
    """The review event that closed this quarantine behind a terminal ruling.

    ``None`` unless the quarantine's newest manual event is a terminal
    reconciliation. This is the durable replay key: it holds however the
    quarantine was closed (ATH ruling, CLI ban or operator) and whoever wrote
    the reason.
    """
    if (
        quarantine.status != "resolved"
        or quarantine.resolution != TERMINAL_RECONCILIATION_RESOLUTION
    ):
        return None
    event = await session.scalar(
        select(ScreeningReviewEvent)
        .where(
            ScreeningReviewEvent.quarantine_id == quarantine.quarantine_id,
            ScreeningReviewEvent.event_kind == "manual",
        )
        .order_by(
            ScreeningReviewEvent.created_at.desc(), ScreeningReviewEvent.event_id.desc()
        )
        .limit(1)
    )
    if event is None or "terminal_reconciliation" not in (event.evidence or {}):
        return None
    return event


async def reconcile_terminal_quarantine(
    session: AsyncSession,
    *,
    agent: Agent,
    quarantine: ScreeningQuarantine,
    actor: str,
    reason: str,
    now: datetime,
    source: ReconciliationSource,
    ruling: TerminalRuling,
) -> UUID:
    """Resolve one terminal ghost without touching the agent's ruling.

    The caller holds both row locks and passes the ruling it identified under
    them. Returns the appended resolution id.
    """
    if not is_terminal_quarantine_ghost(quarantine, agent):
        raise ValueError("quarantine is not an active row behind a terminal agent")
    if (
        ruling.agent_status != agent.status.value
        or ruling.artifact_sha256 != agent.sha256
    ):
        raise ValueError("terminal ruling does not describe this exact agent")
    quarantine.status = "resolved"
    quarantine.resolved_at = now
    quarantine.resolved_by = actor
    quarantine.resolution = TERMINAL_RECONCILIATION_RESOLUTION
    quarantine.resolution_reason = reason
    resolution_id = uuid4()
    session.add(
        ScreeningQuarantineResolution(
            resolution_id=resolution_id,
            quarantine_id=quarantine.quarantine_id,
            resolution=TERMINAL_RECONCILIATION_RESOLUTION,
            reason=reason,
            actor=actor,
            created_at=now,
        )
    )
    await append_manual_review_event(
        session,
        agent=agent,
        quarantine=quarantine,
        resolution_id=resolution_id,
        resolution=TERMINAL_RECONCILIATION_RESOLUTION,
        reason=reason,
        actor=actor,
        prior_agent_status=agent.status,
        next_agent_status=agent.status,
        created_at=now,
        terminal_reconciliation={"source": source, **ruling.fence()},
    )
    return resolution_id


async def close_quarantines_for_terminal_ruling(
    session: AsyncSession,
    *,
    agent: Agent,
    ruling: TerminalRuling,
    source: ReconciliationSource,
    actor: str,
    reason: str,
    now: datetime,
) -> list[UUID]:
    """Close every active quarantine behind a just-recorded terminal ruling.

    ``ruling`` is the decision being written in this same transaction: the
    ATH review and its new reject action, or the legacy CLI ban (which never
    resolves an ``ath_reviews`` row, so it names no action).

    Runs after the agent row is locked and moved to its terminal status.
    Re-reading under that lock also catches a quarantine a screener committed
    before the agent lock was granted.
    """
    closed: list[UUID] = []
    for quarantine in await lock_active_quarantines(session, agent_id=agent.agent_id):
        await reconcile_terminal_quarantine(
            session,
            agent=agent,
            quarantine=quarantine,
            actor=actor,
            reason=reason,
            now=now,
            source=source,
            ruling=ruling,
        )
        closed.append(quarantine.quarantine_id)
    return closed
