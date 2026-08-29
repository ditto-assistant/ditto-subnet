"""Audited operator control for the required screening-policy version.

Screening policy text ships with the build, but raising the version the
screening queue REQUIRES is a fairness decision: every miner gets the same
notice period, and the activation time is public and scheduled rather than
"whenever an operator wakes up." This router stores the schedule; the queue,
claim, heartbeat, and outcome paths read the effective version from
``ditto.api_server.screener_policy_activation``.

Deliberately a separate router prefix from the screener protocol router: this
is subnet policy that Backroom may set over MCP, like the queue-policy and
source-review-settings boards.

Two safety rules live here rather than in the model, because both need state
the model cannot see:

* **Optimistic concurrency.** ``expected_revision`` must match the newest
  stored revision so two operators (or a stale Backroom tab) cannot schedule
  past each other; the write flushes inside the request transaction so the
  database's unique ``(parent_revision)`` constraint is the final arbiter.
* **Fail-safe bounds.** ``target_policy_version`` must be at least the floor and
  at most the version this build implements: scheduling a version no deployed
  worker implements would fail the whole screening fleet closed at activation
  time.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.screener_policy_activation import (
    CONFIRMATION,
    ScheduleScreenerPolicyActivationRequest,
    ScreenerPolicyActivationRevision,
    ScreenerPolicyActivationView,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.screener_policy_activation import (
    EffectiveScreenerPolicy,
    resolve_screener_policy_activation,
)
from ditto.db.models import ScreenerPolicyActivation as ActivationRow
from ditto.db.queries.screener_policy_activation import (
    insert_screener_policy_activation,
    latest_screener_policy_activation,
    list_screener_policy_activations,
)
from ditto_screening_protocol import (
    SCREENING_FLOOR_POLICY_VERSION,
    SCREENING_POLICY_VERSION,
)

router = APIRouter(prefix="/admin/screener-policy-activation", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]


def _revision_view(
    row: ActivationRow, *, now: datetime
) -> ScreenerPolicyActivationRevision:
    return ScreenerPolicyActivationRevision(
        revision=row.revision,
        parent_revision=row.parent_revision,
        target_policy_version=row.target_policy_version,
        activate_at=row.activate_at,
        rescreen_scored=row.rescreen_scored,
        reason=row.reason,
        actor=row.actor,
        created_at=row.created_at,
        state="due" if row.activate_at <= now else "pending",
    )


def _view(
    policy: EffectiveScreenerPolicy,
    latest: ActivationRow | None,
    history: list[ActivationRow],
) -> ScreenerPolicyActivationView:
    now = datetime.now(UTC)
    return ScreenerPolicyActivationView(
        effective_policy_version=policy.required_policy_version,
        floor_policy_version=SCREENING_FLOOR_POLICY_VERSION,
        builtin_policy_version=SCREENING_POLICY_VERSION,
        latest=_revision_view(latest, now=now) if latest is not None else None,
        revisions=[_revision_view(row, now=now) for row in history],
    )


async def _latest_revision(session: AsyncSession) -> int:
    latest = await latest_screener_policy_activation(session)
    return latest.revision if latest is not None else 0


@router.get("", response_model=ScreenerPolicyActivationView)
async def get_activation(
    _admin: AdminDep, session: SessionDep
) -> ScreenerPolicyActivationView:
    """The effective required version, the governing schedule, and history."""
    policy = await resolve_screener_policy_activation(session)
    latest = await latest_screener_policy_activation(session)
    history = list(await list_screener_policy_activations(session))
    return _view(policy, latest, history)


@router.post("", response_model=ScreenerPolicyActivationView)
async def schedule_activation(
    payload: ScheduleScreenerPolicyActivationRequest,
    request: Request,
    _admin: AdminDep,
    session: SessionDep,
) -> ScreenerPolicyActivationView:
    """Append one optimistic, confirmation-gated schedule revision.

    The required version rises to ``target_policy_version`` when
    ``activate_at`` passes, not when this row is written — miners get the full
    notice window. Superseding an earlier schedule is a normal append: the
    newest due revision governs.
    """
    if payload.confirmation != CONFIRMATION:
        raise HTTPException(
            status_code=409,
            detail=f"confirmation must be exactly {CONFIRMATION}",
        )
    if payload.activate_at.tzinfo is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "activate_at must carry a timezone offset (for example "
                "2026-08-29T09:00:00-04:00 for 9 a.m. Eastern); a naive "
                "datetime would silently mean server-local time"
            ),
        )
    activate_at = payload.activate_at.astimezone(UTC)
    if activate_at <= datetime.now(UTC):
        raise HTTPException(
            status_code=422,
            detail=(
                "activate_at must be in the future; an activation is "
                "notice, not a retroactive rule change"
            ),
        )
    if payload.target_policy_version < SCREENING_FLOOR_POLICY_VERSION:
        raise HTTPException(
            status_code=422,
            detail=(
                f"target_policy_version must be at least the current floor "
                f"{SCREENING_FLOOR_POLICY_VERSION}"
            ),
        )
    if payload.target_policy_version > SCREENING_POLICY_VERSION:
        raise HTTPException(
            status_code=422,
            detail=(
                f"target_policy_version {payload.target_policy_version} exceeds "
                f"the version this build implements "
                f"({SCREENING_POLICY_VERSION}); deploy the build that "
                "implements it, then schedule the activation"
            ),
        )
    actual_revision = await _latest_revision(session)
    if payload.expected_revision != actual_revision:
        raise HTTPException(
            status_code=409,
            detail=(
                "screener policy activation changed; refresh before applying "
                f"(expected {payload.expected_revision}, current {actual_revision})"
            ),
        )
    actor = (payload.actor or "backroom").strip()
    try:
        await insert_screener_policy_activation(
            session,
            parent_revision=actual_revision,
            target_policy_version=payload.target_policy_version,
            activate_at=activate_at,
            rescreen_scored=payload.rescreen_scored,
            reason=payload.reason.strip(),
            actor=actor,
        )
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="screener policy activation changed concurrently; refresh and retry",
        ) from error
    resolver = getattr(request.app.state, "screener_policy_activation", None)
    if resolver is not None:
        resolver.invalidate()
    policy = await resolve_screener_policy_activation(session)
    latest = await latest_screener_policy_activation(session)
    history = list(await list_screener_policy_activations(session))
    return _view(policy, latest, history)
