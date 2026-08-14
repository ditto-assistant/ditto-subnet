"""Audited operator control for hosted inference concurrency and budgets.

Append-only revisions of the policy governing hosted chat and embedding
admission. This is the lever an operator reaches for while *watching* -- raise
it, watch run duration and the proxy's admission latency, raise it again, or
slam it back down -- so it must not be a boot-time env var or a code constant.
Both of those mean a release to turn a number.

Lowering either per-ticket concurrency is an emergency brake, and it is
deliberately safe to pull mid-run: the admission path answers a concurrency
decline with ``503 + Retry-After`` rather than the ``429`` it reserves for a
revoked lease, so a validator holding a live ticket backs off and continues
instead of discarding the run. See
``ditto/db/queries/inference.py`` (``InferenceDecline``) and
``ditto/api_server/endpoints/inference.py``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.inference_concurrency_settings import (
    AdminInferenceConcurrencySettingsRequest,
    AdminInferenceConcurrencySettingsResponse,
    EffectiveInferenceConcurrencySettings,
    InferenceConcurrencySettings,
    InferenceConcurrencySettingsRevision,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.inference_concurrency_settings import (
    DEFAULT_SETTINGS,
    InferenceConcurrencySettingsResolver,
    settings_from_row,
)
from ditto.db.models import (
    InferenceConcurrencySettingsRevision as RevisionRow,
)
from ditto.db.queries.inference_concurrency_settings import (
    GLOBAL_SCOPE,
    insert_inference_concurrency_settings_revision,
    latest_inference_concurrency_settings_revision,
    list_inference_concurrency_settings_revisions,
)

router = APIRouter(prefix="/admin/inference-concurrency-settings", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]
CONFIRMATION = "APPLY INFERENCE CONCURRENCY SETTINGS"


def _checksum(settings: InferenceConcurrencySettings) -> str:
    encoded = json.dumps(
        settings.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _revision(row: RevisionRow) -> InferenceConcurrencySettingsRevision:
    return InferenceConcurrencySettingsRevision(
        revision=row.revision,
        parent_revision=row.parent_revision,
        scope=row.scope,
        settings=InferenceConcurrencySettings.model_validate(row.settings),
        reason=row.reason,
        actor=row.actor,
        created_at=row.created_at,
        checksum=row.checksum,
    )


def _resolver(request: Request) -> InferenceConcurrencySettingsResolver | None:
    """The admission-path cache, when the app has one bound.

    Returns ``None`` rather than raising: a missing resolver means admission is
    reading defaults anyway, so there is nothing to invalidate and no reason to
    fail an operator write.
    """
    return getattr(request.app.state, "inference_concurrency_settings", None)


def _effective(latest: RevisionRow | None) -> EffectiveInferenceConcurrencySettings:
    return EffectiveInferenceConcurrencySettings(
        revision=latest.revision if latest is not None else 0,
        scope=latest.scope if latest is not None else GLOBAL_SCOPE,
        settings=settings_from_row(latest),
        checksum=latest.checksum if latest is not None else "",
        source="revision" if latest is not None else "default",
    )


@router.get("", response_model=AdminInferenceConcurrencySettingsResponse)
async def get_settings(
    _admin: AdminDep, session: SessionDep
) -> AdminInferenceConcurrencySettingsResponse:
    """Current policy, append-only history, the defaults, and what is in force."""
    latest = await latest_inference_concurrency_settings_revision(session)
    history = await list_inference_concurrency_settings_revisions(session)
    return AdminInferenceConcurrencySettingsResponse(
        current=[_revision(latest)] if latest is not None else [],
        history=[_revision(row) for row in history],
        default=DEFAULT_SETTINGS,
        effective=_effective(latest),
    )


@router.post("", response_model=InferenceConcurrencySettingsRevision)
async def create_settings_revision(
    payload: AdminInferenceConcurrencySettingsRequest,
    request: Request,
    _admin: AdminDep,
    session: SessionDep,
) -> InferenceConcurrencySettingsRevision:
    """Append one optimistic, confirmation-gated revision.

    Takes effect within the resolver TTL (five seconds), fleet-wide, with no
    restart. This write invalidates the cache on the serving worker so the
    operator's own next read is never stale; other workers converge within the
    TTL.
    """
    if payload.scope != GLOBAL_SCOPE:
        raise HTTPException(
            status_code=422,
            detail="inference concurrency is subnet-global; scope must be '*'",
        )
    if payload.confirmation != CONFIRMATION:
        raise HTTPException(
            status_code=409,
            detail=f"confirmation must be exactly {CONFIRMATION}",
        )
    latest = await latest_inference_concurrency_settings_revision(
        session, scope=payload.scope
    )
    actual_revision = latest.revision if latest is not None else 0
    if payload.expected_revision != actual_revision:
        raise HTTPException(
            status_code=409,
            detail=(
                "inference concurrency settings changed; refresh before applying "
                f"(expected {payload.expected_revision}, current {actual_revision})"
            ),
        )
    try:
        row = await insert_inference_concurrency_settings_revision(
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
            detail=(
                "inference concurrency settings changed concurrently; refresh and retry"
            ),
        ) from error
    resolver = _resolver(request)
    if resolver is not None:
        resolver.invalidate()
    await session.refresh(row)
    return _revision(row)
