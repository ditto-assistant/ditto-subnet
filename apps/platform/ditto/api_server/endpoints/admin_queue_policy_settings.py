"""Audited operator control for validator-queue policy.

Append-only revisions of the queue policy: cohort sizing, the per-validator
fresh-vs-cohort lane split, the provisional contender lane, and the
previous-generation carryover gate. Every one of these was a source literal that
had to be changed, reviewed and deployed to retune -- and the queue gets retuned
whenever miner behaviour shifts, which is often.

Deliberately a **separate router prefix** from ``admin_benchmark_rollout``: that
router owns ``POST /admin/benchmark-rollout/{desired_version}``, so a
``/settings`` sub-path there would sit in the same namespace as a version
segment and depend on declaration order to not be parsed as one. It is also a
different kind of control -- rollout *activation* is a typed-confirmation UI
action, while this is subnet policy that backroom may set over MCP.

Two safety rules live in this module rather than in the model, because both need
database state the model cannot see:

* **Rollout-locked fields.** ``lane_cycle_size`` and ``fresh_submission_slots``
  are refused while a rollout is open. See :func:`_assert_lane_change_safe`.
* **Cache invalidation.** The hot-path resolver is TTL-cached, so a write
  invalidates it: an operator who changes a lane and immediately re-reads the
  board must see their own write, not a stale five-second cache.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.queue_policy_settings import (
    AdminQueuePolicySettingsRequest,
    AdminQueuePolicySettingsResponse,
    EffectiveQueuePolicySettings,
    QueuePolicySettings,
    QueuePolicySettingsRevision,
    rollout_locked_change,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.queue_policy_settings import (
    DEFAULT_SETTINGS,
    QueuePolicySettingsResolver,
    settings_from_row,
)
from ditto.db.models import QueuePolicySettingsRevision as RevisionRow
from ditto.db.queries.benchmark_rollout import open_rollout
from ditto.db.queries.queue_policy_settings import (
    GLOBAL_SCOPE,
    insert_queue_policy_settings_revision,
    latest_queue_policy_settings_revision,
    list_queue_policy_settings_revisions,
)

router = APIRouter(prefix="/admin/queue-policy-settings", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]
CONFIRMATION = "APPLY QUEUE POLICY SETTINGS"


def _checksum(settings: QueuePolicySettings) -> str:
    encoded = json.dumps(
        settings.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _revision(row: RevisionRow) -> QueuePolicySettingsRevision:
    return QueuePolicySettingsRevision(
        revision=row.revision,
        parent_revision=row.parent_revision,
        scope=row.scope,
        settings=QueuePolicySettings.model_validate(row.settings),
        reason=row.reason,
        actor=row.actor,
        created_at=row.created_at,
        checksum=row.checksum,
    )


def _resolver(request: Request) -> QueuePolicySettingsResolver | None:
    """The hot-path cache, when the app has one bound.

    Returns ``None`` rather than raising: a missing resolver means the queue is
    reading defaults anyway, so there is nothing to invalidate and no reason to
    fail an operator write.
    """
    return getattr(request.app.state, "queue_policy_settings", None)


async def _effective(
    session: AsyncSession, latest: RevisionRow | None
) -> EffectiveQueuePolicySettings:
    """What the queue uses now, plus what an open rollout already froze.

    Reporting the open rollout's frozen targets beside the configured ones is how
    an operator sees, without reading the source, that raising a cohort size does
    not resize the transition currently in flight.
    """
    settings = settings_from_row(latest)
    rollout = await open_rollout(session)
    rescore_frozen = rollout.rescore_cohort_target if rollout is not None else None
    priority_frozen = rollout.priority_cohort_target if rollout is not None else None
    overrides = (
        rescore_frozen is not None and rescore_frozen != settings.rescore_cohort_size
    ) or (
        priority_frozen is not None and priority_frozen != settings.priority_cohort_size
    )
    return EffectiveQueuePolicySettings(
        revision=latest.revision if latest is not None else 0,
        scope=latest.scope if latest is not None else GLOBAL_SCOPE,
        settings=settings,
        checksum=latest.checksum if latest is not None else "",
        source="revision" if latest is not None else "default",
        rollout_is_open=rollout is not None,
        open_rollout_desired_version=(
            rollout.desired_version if rollout is not None else None
        ),
        open_rollout_rescore_cohort_target=rescore_frozen,
        open_rollout_priority_cohort_target=priority_frozen,
        open_rollout_overrides_setting=overrides,
    )


async def _assert_lane_change_safe(
    session: AsyncSession,
    *,
    current: QueuePolicySettings,
    proposed: QueuePolicySettings,
) -> None:
    """Refuse a lane-modulus change while a benchmark rollout is open.

    The lane a validator serves is ``jobs it completed since rollout start %
    lane_cycle_size``. That counter is measured from the open rollout's start, so
    changing the modulus (or which residues are the fresh lane) does not
    *retune* the split -- it discontinuously reassigns every validator's current
    position in the cycle. A validator whose next poll was going to take cohort
    work silently takes fresh work instead, and vice versa, with no bound on how
    long the fleet stays skewed.

    Refusing is strictly better than trying to make it safe, because the
    mechanism has no effect at all when no rollout is open: the lane check is
    only reached on the rollout path. So there is no operational cost to waiting
    for the transition to finish, and every other field on the board stays
    writable meanwhile.
    """
    changed = rollout_locked_change(current, proposed)
    if not changed:
        return
    rollout = await open_rollout(session)
    if rollout is None:
        return
    raise HTTPException(
        status_code=409,
        detail=(
            f"cannot change {', '.join(changed)} while the benchmark v"
            f"{rollout.desired_version} rollout is open: the lane counter is "
            "measured from rollout start, so changing the cycle mid-rollout "
            "reassigns every validator's lane discontinuously. Wait for the "
            "rollout to activate or be superseded, or apply a revision that "
            "leaves the lane fields unchanged."
        ),
    )


@router.get("", response_model=AdminQueuePolicySettingsResponse)
async def get_settings(
    _admin: AdminDep, session: SessionDep
) -> AdminQueuePolicySettingsResponse:
    """Current policy, append-only history, the defaults, and what is in force."""
    latest = await latest_queue_policy_settings_revision(session)
    history = await list_queue_policy_settings_revisions(session)
    return AdminQueuePolicySettingsResponse(
        current=[_revision(latest)] if latest is not None else [],
        history=[_revision(row) for row in history],
        default=DEFAULT_SETTINGS,
        effective=await _effective(session, latest),
    )


@router.post("", response_model=QueuePolicySettingsRevision)
async def create_settings_revision(
    payload: AdminQueuePolicySettingsRequest,
    request: Request,
    _admin: AdminDep,
    session: SessionDep,
) -> QueuePolicySettingsRevision:
    """Append one optimistic, confirmation-gated revision.

    Cohort sizes take effect at the NEXT
    ``POST /admin/benchmark-rollout/{version}``; an already-open rollout keeps
    the targets it froze at start. Every other field takes effect within the
    resolver TTL, and this write invalidates that cache so the operator's own
    next read is never stale.
    """
    if payload.scope != GLOBAL_SCOPE:
        raise HTTPException(
            status_code=422,
            detail="queue policy is subnet-global; scope must be '*'",
        )
    if payload.confirmation != CONFIRMATION:
        raise HTTPException(
            status_code=409,
            detail=f"confirmation must be exactly {CONFIRMATION}",
        )
    latest = await latest_queue_policy_settings_revision(session, scope=payload.scope)
    actual_revision = latest.revision if latest is not None else 0
    if payload.expected_revision != actual_revision:
        raise HTTPException(
            status_code=409,
            detail=(
                "queue policy settings changed; refresh before applying "
                f"(expected {payload.expected_revision}, current {actual_revision})"
            ),
        )
    await _assert_lane_change_safe(
        session,
        current=settings_from_row(latest),
        proposed=payload.settings,
    )
    try:
        row = await insert_queue_policy_settings_revision(
            session,
            parent_revision=actual_revision,
            scope=payload.scope,
            settings=payload.settings.model_dump(mode="json"),
            checksum=_checksum(payload.settings),
            reason=payload.reason.strip(),
            actor=payload.actor.strip(),
        )
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="queue policy settings changed concurrently; refresh and retry",
        ) from error
    resolver = _resolver(request)
    if resolver is not None:
        resolver.invalidate()
    await session.refresh(row)
    return _revision(row)
