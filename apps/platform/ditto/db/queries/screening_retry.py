"""Shared SQL predicates for operator-authorized screening retries."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import ColumnElement, exists, select
from sqlalchemy.sql.selectable import ScalarSelect

from ditto.db.models import Agent, ScreeningAttempt, ScreeningRetryOverride


def latest_screening_attempt_id() -> ScalarSelect[UUID]:
    """Return the latest attempt id correlated to the current agent."""
    return (
        select(ScreeningAttempt.attempt_id)
        .where(ScreeningAttempt.agent_id == Agent.agent_id)
        .order_by(
            ScreeningAttempt.started_at.desc(),
            ScreeningAttempt.attempt_id.desc(),
        )
        .limit(1)
        .correlate(Agent)
        .scalar_subquery()
    )


def failed_screening_retry_authorized() -> ColumnElement[bool]:
    """Match the exact latest-attempt override required by the claim path."""
    return exists(
        select(ScreeningRetryOverride.override_id).where(
            ScreeningRetryOverride.attempt_id == latest_screening_attempt_id()
        )
    )
