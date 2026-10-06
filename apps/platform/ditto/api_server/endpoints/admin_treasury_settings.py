"""Audited shadow-only treasury policy; this endpoint cannot move funds."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.treasury_activation import (
    TreasuryActivationPreflight,
    TreasuryActivationPreflightRequest,
)
from ditto.api_models.treasury_readiness import TreasuryLedgerReadiness
from ditto.api_models.treasury_settings import (
    AdminTreasurySettingsRequest,
    TreasuryObserverSettings,
    TreasurySettings,
    TreasurySettingsControl,
    TreasurySettingsRevision,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.treasury_shadow import shadow_readiness
from ditto.db.models import TreasurySettingsRevision as RevisionRow
from ditto.db.queries.ledger_epochs import latest_pin

router = APIRouter(prefix="/admin/treasury-settings", tags=["admin"])
AUTHENTICATED_PRINCIPAL = "platform_admin_token"
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]


def _revision(row: RevisionRow) -> TreasurySettingsRevision:
    return TreasurySettingsRevision(
        revision=row.revision,
        parent_revision=row.parent_revision,
        settings=TreasurySettings.model_validate(row.settings),
        checksum=row.checksum,
        reason=row.reason,
        actor=row.actor,
        created_at=row.created_at,
    )


async def _latest(session: AsyncSession) -> RevisionRow | None:
    return await session.scalar(
        select(RevisionRow).order_by(RevisionRow.revision.desc()).limit(1)
    )


@router.post("/activation-preflight", response_model=TreasuryActivationPreflight)
async def get_treasury_activation_preflight(
    payload: TreasuryActivationPreflightRequest,
    request: Request,
    response: Response,
    _admin: AdminDep,
    session: SessionDep,
) -> TreasuryActivationPreflight:
    from datetime import UTC, datetime

    from ditto.api_server.treasury_activation import activation_preflight

    response.headers["Cache-Control"] = "no-store"
    try:
        return await activation_preflight(
            request.app.state, session, payload, now=datetime.now(UTC)
        )
    except ValueError:
        raise HTTPException(
            400,
            "Invalid public treasury approval or preflight evidence",
            headers={"Cache-Control": "no-store"},
        ) from None


@router.get("/ledger-readiness", response_model=TreasuryLedgerReadiness)
async def get_treasury_ledger_readiness(
    request: Request, _admin: AdminDep, session: SessionDep
) -> TreasuryLedgerReadiness:
    state = request.app.state
    from ditto.api_server.treasury_runtime import treasury_runtime

    try:
        config = await treasury_runtime(session, state.config)
    except SQLAlchemyError:
        raise HTTPException(503, "Gamma runtime control is unavailable") from None
    except ValueError:
        raise HTTPException(409, "Gamma runtime control is invalid") from None
    row = await latest_pin(session, netuid=state.config.chain.netuid)
    readiness = shadow_readiness(state, row, runtime=config)
    if readiness.configured_proposal is None:
        return readiness
    from datetime import UTC, datetime

    from ditto.api_server.treasury_weights import (
        current_managed_weight_setters,
        read_treasury_fleet,
    )

    try:
        fleet = await read_treasury_fleet(
            session,
            now=datetime.now(UTC),
            policy_digest=readiness.configured_proposal.digest,
            collector_digest=readiness.configured_proposal.collector_policy_digest,
            required_hotkeys=config.treasury_managed_validator_hotkeys,
        )
    except ValueError:
        return readiness.model_copy(
            update={
                "fleet_gate": "not_ready",
                "blocking_reasons": [*readiness.blocking_reasons, "fleet_not_ready"],
            }
        )
    pin = readiness.stored_enforcing_pin
    if not readiness.enforcement_configured or pin is None:
        # Fresh reports alone cannot prove an active epoch for the managed roster.
        return readiness
    from ditto_screening_protocol.treasury_enforcement import (
        require_treasury_weight_authority,
    )

    try:
        if (
            fleet != pin.fleet
            or readiness.proposal_approval_status != "verified"
            or set(config.treasury_managed_validator_hotkeys)
            != {m.validator_hotkey for m in pin.fleet}
        ):
            raise ValueError("enforcing pin differs from approved live fleet")
        if (
            config.activation_epoch is not None
            and pin.epoch_index < config.activation_epoch
        ):
            raise ValueError("stored pin predates runtime activation")
        observed = await state.chain.get_treasury_dispatch_observation(pin.policy)
        chain_keys = await current_managed_weight_setters(
            state.chain,
            pin.policy,
            block_hash=observed.finalized_block_hash,
            managed_hotkeys=config.treasury_managed_validator_hotkeys,
        )
        if not chain_keys or not set(
            config.treasury_managed_validator_hotkeys
        ).issubset(chain_keys):
            raise ValueError("chain weight-setter roster differs from live fleet")
        require_treasury_weight_authority(
            pin,
            expected_policy_digest=config.treasury_approved_policy_digest,
            expected_collector_policy_digest=config.treasury_approved_collector_policy_digest,
            local_capability=fleet[0],
            current_identity=observed.identity,
            netuid=state.config.chain.netuid,
            current_epoch_index=observed.epoch_index,
            current_first_block=observed.first_block,
            finalized_block=observed.finalized_block,
            finalized_block_hash=observed.finalized_block_hash,
        )
    except Exception:
        return readiness.model_copy(
            update={
                "fleet_gate": "not_ready",
                "blocking_reasons": [
                    *readiness.blocking_reasons,
                    "enforcing_pin_unverified",
                ],
            }
        )
    return readiness.model_copy(
        update={
            "fleet_gate": "ready",
            "offline_epoch_verified": True,
            "can_enforce_weights": True,
            "blocking_reasons": [
                reason
                for reason in readiness.blocking_reasons
                if reason != "current_epoch_not_checked"
            ],
        }
    )


@router.get("/revisions/{revision}", response_model=TreasuryObserverSettings)
async def get_treasury_observer_settings(
    revision: Annotated[int, Path(gt=0, le=2_147_483_647)],
    response: Response,
    _admin: AdminDep,
    session: SessionDep,
) -> TreasuryObserverSettings:
    response.headers["Cache-Control"] = "no-store"
    row = await session.get(RevisionRow, revision)
    if row is None:
        raise HTTPException(
            404,
            "Historical treasury revision not found",
            headers={"Cache-Control": "no-store"},
        )
    try:
        # Validate supported semantics, but retain the full raw JSON for the
        # independent checksum. Never substitute defaults for corrupt history.
        TreasurySettings.model_validate(row.settings)
        encoded = json.dumps(
            row.settings, sort_keys=True, separators=(",", ":")
        ).encode()
        if len(encoded) > 65_536:
            raise ValueError("historical settings exceed observer bound")
        checksum = hashlib.sha256(encoded).hexdigest()
        if checksum != row.checksum:
            raise ValueError("historical checksum mismatch")
        return TreasuryObserverSettings(
            revision=row.revision, checksum=row.checksum, settings=row.settings
        )
    except (ValidationError, ValueError, TypeError):
        raise HTTPException(
            409,
            "Historical treasury settings are invalid",
            headers={"Cache-Control": "no-store"},
        ) from None


@router.get("", response_model=TreasurySettingsControl)
async def get_treasury_settings(
    _admin: AdminDep, session: SessionDep
) -> TreasurySettingsControl:
    latest = await _latest(session)
    history = list(
        await session.scalars(
            select(RevisionRow).order_by(RevisionRow.revision.desc()).limit(200)
        )
    )
    effective = (
        TreasurySettings.model_validate(latest.settings)
        if latest
        else TreasurySettings()
    )
    return TreasurySettingsControl(
        effective=effective,
        revision=latest.revision if latest else 0,
        miner_bps=effective.miner_bps,
        history=[_revision(row) for row in history],
    )


@router.post("", response_model=TreasurySettingsRevision)
async def record_treasury_settings(
    payload: AdminTreasurySettingsRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> TreasurySettingsRevision:
    from ditto.api_server.treasury_runtime import (
        latest_runtime_row,
        lock_runtime,
        runtime_revision,
    )

    await lock_runtime(session)
    runtime_row = await latest_runtime_row(session)
    if runtime_row is not None:
        try:
            runtime = runtime_revision(runtime_row)
        except ValueError:
            raise HTTPException(409, "Gamma runtime control is invalid") from None
        if runtime.settings.mode == "enforce":
            raise HTTPException(409, "Pause Gamma before changing treasury settings")
    latest = await _latest(session)
    current = latest.revision if latest else 0
    if payload.expected_revision != current:
        raise HTTPException(status_code=409, detail="treasury policy changed; refresh")
    canonical = json.dumps(
        payload.settings.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    row = RevisionRow(
        parent_revision=current,
        settings=payload.settings.model_dump(mode="json"),
        checksum=hashlib.sha256(canonical.encode()).hexdigest(),
        reason=payload.reason.strip(),
        # The shared bearer token proves only this principal. An actor in the
        # request body or X-Admin-Actor header is caller-controlled and cannot
        # be treated as an authenticated human identity.
        actor=AUTHENTICATED_PRINCIPAL,
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="treasury policy changed concurrently"
        ) from error
    await session.refresh(row)
    return _revision(row)
