"""Audited operator control for per-instance L2/L3 reviewer settings."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.screener import ScreenerReviewSettingsStatus
from ditto.api_models.screener_review_settings import (
    AdminScreenerReviewSettingsRequest,
    AdminScreenerReviewSettingsResponse,
    AppliedScreenerReviewSettings,
    ScreenerPolicyManifestView,
    ScreenerReviewSettings,
    policy_manifest_digest,
)
from ditto.api_models.screener_review_settings import (
    ScreenerReviewSettingsRevision as RevisionModel,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.shadow_review import shadow_review_observation
from ditto.db.models import (
    ScreenerHeartbeat,
    ScreenerReviewSettingsRevision,
    ScreenerShadowReview,
)
from ditto_screening_protocol import SCREENING_POLICY_VERSION

router = APIRouter(prefix="/admin/screener-review-settings", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]
_SCOPE_RE = re.compile(r"^(?:\*|[a-zA-Z0-9._-]{1,63})$")
_APPLIED_FRESHNESS = timedelta(minutes=5)


def _checksum(settings: ScreenerReviewSettings) -> str:
    encoded = json.dumps(
        settings.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _revision(row: ScreenerReviewSettingsRevision) -> RevisionModel:
    return RevisionModel(
        revision=row.revision,
        parent_revision=row.parent_revision,
        scope=row.scope,
        settings=ScreenerReviewSettings.model_validate_json(json.dumps(row.settings)),
        reason=row.reason,
        actor=row.actor,
        created_at=row.created_at,
        checksum=row.checksum,
    )


def _policy_manifest(
    row: ScreenerReviewSettingsRevision,
) -> ScreenerPolicyManifestView:
    settings = ScreenerReviewSettings.model_validate(row.settings)
    return ScreenerPolicyManifestView(
        revision=row.revision,
        scope=row.scope,
        policy_version=SCREENING_POLICY_VERSION,
        profile=settings.policy_manifest_profile,
        rotation_id=settings.policy_manifest_rotation_id,
        digest=policy_manifest_digest(
            settings.policy_manifest_profile,
            settings.policy_manifest_rotation_id,
        ),
        reason=row.reason,
        actor=row.actor,
        created_at=row.created_at,
    )


def _effective_row(
    current_by_scope: dict[str, ScreenerReviewSettingsRevision], instance_id: str
) -> ScreenerReviewSettingsRevision | None:
    exact = current_by_scope.get(instance_id)
    if exact is not None and exact.settings.get("mode") != "inherit":
        return exact
    return current_by_scope.get("*")


async def _rows(session: AsyncSession) -> list[ScreenerReviewSettingsRevision]:
    return list(
        await session.scalars(
            select(ScreenerReviewSettingsRevision).order_by(
                ScreenerReviewSettingsRevision.revision.desc()
            )
        )
    )


@router.get("", response_model=AdminScreenerReviewSettingsResponse)
async def get_settings(
    _admin: AdminDep,
    session: SessionDep,
) -> AdminScreenerReviewSettingsResponse:
    """Return current settings, append-only history, and addressable instances."""
    rows = await _rows(session)
    current_by_scope: dict[str, ScreenerReviewSettingsRevision] = {}
    for row in rows:
        current_by_scope.setdefault(row.scope, row)
    instances = sorted(
        set(await session.scalars(select(ScreenerHeartbeat.instance_id)))
        | {scope for scope in current_by_scope if scope != "*"}
    )
    applied: list[AppliedScreenerReviewSettings] = []
    heartbeats = list(await session.scalars(select(ScreenerHeartbeat)))
    for heartbeat in heartbeats:
        envelope = heartbeat.system_metrics
        raw = envelope.get("review_settings") if isinstance(envelope, dict) else None
        if not isinstance(raw, dict):
            continue
        try:
            status = ScreenerReviewSettingsStatus.model_validate(raw)
        except ValueError:
            continue
        expected = _effective_row(current_by_scope, heartbeat.instance_id)
        if expected is None:
            default_settings = ScreenerReviewSettings()
            expected_revision = 0
            expected_scope = "builtin-default"
            expected_checksum = _checksum(default_settings)
            scope_matches = status.scope in {"builtin-default", "bootstrap"}
        else:
            expected_revision = expected.revision
            expected_scope = expected.scope
            expected_checksum = expected.checksum
            scope_matches = status.scope == expected_scope
        expected_settings = (
            ScreenerReviewSettings.model_validate(expected.settings)
            if expected is not None
            else default_settings
        )
        seen_at = heartbeat.seen_at
        if seen_at.tzinfo is None:
            seen_at = seen_at.replace(tzinfo=UTC)
        fresh = datetime.now(UTC) - seen_at <= _APPLIED_FRESHNESS
        applied.append(
            AppliedScreenerReviewSettings(
                instance_id=heartbeat.instance_id,
                revision=status.revision,
                scope=status.scope,
                mode=status.mode,
                checksum=status.checksum,
                source=status.source,
                seen_at=heartbeat.seen_at,
                fresh=fresh,
                matches_effective=(
                    status.revision == expected_revision
                    and scope_matches
                    and status.checksum == expected_checksum
                    and status.policy_manifest_digest
                    == policy_manifest_digest(
                        expected_settings.policy_manifest_profile,
                        expected_settings.policy_manifest_rotation_id,
                    )
                ),
                expected_revision=expected_revision,
                expected_scope=expected_scope,
                expected_checksum=expected_checksum,
                policy_manifest_profile=status.policy_manifest_profile,
                policy_manifest_rotation_id=status.policy_manifest_rotation_id,
                policy_manifest_digest=status.policy_manifest_digest,
                expected_policy_manifest_digest=policy_manifest_digest(
                    expected_settings.policy_manifest_profile,
                    expected_settings.policy_manifest_rotation_id,
                ),
            )
        )
    shadow_rows = list(
        await session.scalars(
            select(ScreenerShadowReview)
            .order_by(ScreenerShadowReview.created_at.desc())
            .limit(100)
        )
    )
    return AdminScreenerReviewSettingsResponse(
        current=[_revision(row) for row in current_by_scope.values()],
        history=[_revision(row) for row in rows[:200]],
        known_instances=instances,
        applied_instances=sorted(applied, key=lambda item: item.instance_id),
        shadow_observations=[shadow_review_observation(row) for row in shadow_rows],
        policy_manifests=[_policy_manifest(row) for row in rows[:200]],
    )


@router.post("", response_model=RevisionModel)
async def create_settings_revision(
    payload: AdminScreenerReviewSettingsRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> RevisionModel:
    """Append one optimistic, idempotency-safe settings revision."""
    if not _SCOPE_RE.fullmatch(payload.scope):
        raise HTTPException(status_code=422, detail="invalid screener settings scope")
    if payload.settings.mode == "inherit" and payload.scope == "*":
        raise HTTPException(
            status_code=409,
            detail="inherit is only valid for an exact worker scope",
        )
    apply_confirmation = (
        f"APPLY SCREENER REVIEW {payload.scope} {payload.settings.mode.upper()}"
    )
    latest = await session.scalar(
        select(ScreenerReviewSettingsRevision)
        .where(ScreenerReviewSettingsRevision.scope == payload.scope)
        .order_by(ScreenerReviewSettingsRevision.revision.desc())
        .limit(1)
    )
    actual_revision = latest.revision if latest is not None else 0
    rotate_confirmation = (
        f"ROTATE SCREENER POLICY {payload.scope} "
        f"{payload.settings.policy_manifest_rotation_id}"
    )
    rotate_only = False
    if latest is not None:
        previous = ScreenerReviewSettings.model_validate(latest.settings)
        proposed_without_manifest = payload.settings.model_copy(
            update={
                "policy_manifest_profile": previous.policy_manifest_profile,
                "policy_manifest_rotation_id": previous.policy_manifest_rotation_id,
            }
        )
        rotate_only = proposed_without_manifest == previous
    if payload.confirmation != apply_confirmation and not (
        rotate_only and payload.confirmation == rotate_confirmation
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                f"confirmation must be exactly {apply_confirmation}; manifest-only "
                f"rotations may use {rotate_confirmation}"
            ),
        )
    if payload.expected_revision != actual_revision:
        raise HTTPException(
            status_code=409,
            detail=(
                "screener review settings changed; refresh before applying "
                f"(expected {payload.expected_revision}, current {actual_revision})"
            ),
        )
    row = ScreenerReviewSettingsRevision(
        parent_revision=actual_revision,
        scope=payload.scope,
        settings=payload.settings.model_dump(mode="json"),
        checksum=_checksum(payload.settings),
        reason=payload.reason.strip(),
        actor=payload.actor.strip(),
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                "screener review settings changed concurrently; refresh before applying"
            ),
        ) from error
    await session.refresh(row)
    return _revision(row)
