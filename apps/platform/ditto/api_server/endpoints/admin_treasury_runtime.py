"""Explicit Gamma producer control; no transfer or custody authority."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.treasury_activation import TreasuryActivationPreflightRequest
from ditto.api_models.treasury_runtime import (
    AdminTreasuryRuntimeRequest,
    TreasuryRuntimeControl,
    TreasuryRuntimeRevision,
)
from ditto.api_models.treasury_settings import TreasurySettings
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.endpoints.admin_treasury_settings import (
    AUTHENTICATED_PRINCIPAL,
    _latest,
)
from ditto.api_server.treasury_activation import activation_preflight
from ditto.api_server.treasury_runtime import (
    canonical_settings,
    latest_runtime_row,
    lock_runtime,
    runtime_revision,
)
from ditto.db.models import TreasuryRuntimeRevision as RuntimeRow
from ditto_screening_protocol.treasury_approval import (
    verify_policy_approval,
    verify_public_signature,
)

router = APIRouter(prefix="/admin/treasury-runtime", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]


@router.get("", response_model=TreasuryRuntimeControl)
async def get_treasury_runtime(
    response: Response,
    _admin: AdminDep,
    session: SessionDep,
) -> TreasuryRuntimeControl:
    response.headers["Cache-Control"] = "no-store"
    row = await latest_runtime_row(session)
    try:
        return TreasuryRuntimeControl(
            revision=row.revision if row else 0,
            latest=runtime_revision(row) if row else None,
        )
    except ValueError:
        raise HTTPException(
            409,
            "Gamma runtime control is invalid",
            headers={"Cache-Control": "no-store"},
        ) from None


@router.post("", response_model=TreasuryRuntimeRevision)
async def record_treasury_runtime(
    payload: AdminTreasuryRuntimeRequest,
    request: Request,
    response: Response,
    _admin: AdminDep,
    session: SessionDep,
) -> TreasuryRuntimeRevision:
    from ditto.api_server.endpoints.scoring import resolve_ledger_context

    response.headers["Cache-Control"] = "no-store"
    await lock_runtime(session)
    latest = await latest_runtime_row(session)
    current = latest.revision if latest else 0
    if payload.expected_revision != current:
        raise HTTPException(409, "Gamma runtime changed; refresh")
    settings = payload.settings
    if latest is None and getattr(
        request.app.state.config, "treasury_weight_enforcement", False
    ):
        raise HTTPException(
            409,
            "Migrate the enforcing deployment control before recording Gamma runtime",
        )
    try:
        previous = runtime_revision(latest).settings if latest else None
        policy = verify_policy_approval(
            settings.approval,
            expected_policy_digest=settings.approved_policy_digest,
            expected_collector_policy_digest=settings.collector_policy_digest,
            verify_signature=verify_public_signature,
        )
    except ValueError:
        raise HTTPException(
            400, "Invalid Gamma public approval or stored control"
        ) from None
    if previous is not None and previous.mode == "enforce" and settings.mode != "pause":
        raise HTTPException(409, "Pause Gamma before changing or rearming its policy")
    if settings.mode == "pause":
        if previous is None or settings.approval != previous.approval:
            raise HTTPException(409, "Pause must retain the configured public approval")
    else:
        preflight = await activation_preflight(
            request.app.state,
            session,
            TreasuryActivationPreflightRequest(
                approval=settings.approval,
                expected_policy_digest=settings.approved_policy_digest,
                expected_collector_policy_digest=settings.collector_policy_digest,
            ),
            now=datetime.now(UTC),
        )
        if preflight.chain_status != "verified" or preflight.observation is None:
            raise HTTPException(
                409, "Finalized Gamma collector identity is unavailable"
            )
        if settings.mode == "enforce":
            if previous is None or previous.approval != settings.approval:
                raise HTTPException(
                    409, "Observe the exact approved policy before activation"
                )
            if not preflight.fleet_ready_for_proposed_policy:
                raise HTTPException(
                    409,
                    "Every permitted setter and fresh reporter must prove "
                    "the exact Gamma guard",
                )
            if settings.activation_epoch != preflight.observation.epoch_index + 1:
                raise HTTPException(
                    409, "Gamma must activate at the next independently observed epoch"
                )
            context = await resolve_ledger_context(
                request.app.state, session, now=datetime.now(UTC)
            )
            if context.policy.continual_retest.ledger_pin_mode != "epoch":
                raise HTTPException(409, "Gamma requires immutable epoch ledger mode")
            shadow_row = await _latest(session)
            shadow = (
                TreasurySettings.model_validate(shadow_row.settings)
                if shadow_row
                else TreasurySettings()
            )
            if shadow.allocation_version != 2 or (
                shadow.treasury_hotkey,
                shadow.treasury_coldkey,
                sorted(
                    (b.bucket_id, b.allocation_bps, b.holding_coldkey)
                    for b in shadow.service_buckets
                ),
            ) != (
                policy.collector_hotkey,
                policy.collector_coldkey,
                sorted(
                    (b.bucket_id, b.allocation_bps, b.holding_coldkey)
                    for b in policy.buckets
                ),
            ):
                raise HTTPException(
                    409,
                    "Public allocation settings differ from the signed Gamma policy",
                )
    raw = settings.model_dump(mode="json")
    row = RuntimeRow(
        parent_revision=current,
        settings=raw,
        checksum=canonical_settings(raw),
        reason=payload.reason.strip(),
        actor=AUTHENTICATED_PRINCIPAL,
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "Gamma runtime changed concurrently") from None
    await session.refresh(row)
    return runtime_revision(row)
