"""Audited shadow-only treasury policy; this endpoint cannot move funds."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

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


@router.get("/ledger-readiness", response_model=TreasuryLedgerReadiness)
async def get_treasury_ledger_readiness(
    request: Request, _admin: AdminDep, session: SessionDep
) -> TreasuryLedgerReadiness:
    state = request.app.state
    row = await latest_pin(session, netuid=state.config.chain.netuid)
    readiness = shadow_readiness(state, row)
    if readiness.configured_proposal is None:
        return readiness
    from datetime import UTC, datetime

    from ditto.api_server.treasury_weights import read_treasury_fleet

    try:
        fleet = await read_treasury_fleet(
            session,
            now=datetime.now(UTC),
            policy_digest=readiness.configured_proposal.digest,
            collector_digest=readiness.configured_proposal.collector_policy_digest,
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
        # Fresh reports alone cannot prove the complete chain-active roster.
        return readiness
    from ditto_screening_protocol.treasury_enforcement import (
        require_treasury_weight_authority,
    )

    try:
        if fleet != pin.fleet or readiness.proposal_approval_status != "verified":
            raise ValueError("enforcing pin differs from approved live fleet")
        observed = await state.chain.get_treasury_dispatch_observation(pin.policy)
        chain_keys = await state.chain.get_treasury_weight_setters(
            pin.policy, block_hash=observed.finalized_block_hash
        )
        if not chain_keys or not set(chain_keys).issubset(
            {member.validator_hotkey for member in fleet}
        ):
            raise ValueError("chain weight-setter roster differs from live fleet")
        require_treasury_weight_authority(
            pin,
            expected_policy_digest=state.config.treasury_approved_policy_digest,
            expected_collector_policy_digest=state.config.treasury_approved_collector_policy_digest,
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
