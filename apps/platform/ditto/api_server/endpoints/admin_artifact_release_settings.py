"""Audited operator control for public source-release timing."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.artifact_release_settings import (
    AdminArtifactReleaseSettingsRequest,
    AdminArtifactReleaseSettingsResponse,
    ReceiptDiagnosticRow,
    SourceReleaseEligibilityRow,
    SourceReleaseGateStatus,
)
from ditto.api_models.artifact_release_settings import (
    ArtifactReleaseSettingsRevision as RevisionModel,
)
from ditto.api_models.receipt_diagnostics import ReceiptDiagnosticReport
from ditto.api_models.source_disclosure import SourceDisclosure, release_confirmation
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.models import (
    Agent,
    AgentKingship,
    ArtifactReleaseSettingsRevision,
    SourceEmissionCollectorCursor,
    SourceEmissionPayout,
    SourceEmissionPayoutResolution,
    ValidatorReceiptDiagnostic,
    ValidatorWeightReceipt,
)
from ditto.db.queries.artifact_release_settings import (
    DEFAULT_ARTIFACT_RELEASE_DISCLOSURE,
    DEFAULT_ARTIFACT_RELEASE_EMBARGO_HOURS,
    latest_artifact_release_settings,
)
from ditto.db.queries.king_reign import SOURCE_RELEASE_GATE_VERSION

router = APIRouter(prefix="/admin/artifact-release-settings", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]


def _revision(row: ArtifactReleaseSettingsRevision) -> RevisionModel:
    return RevisionModel(
        revision=row.revision,
        parent_revision=row.parent_revision,
        disclosure=SourceDisclosure(row.disclosure),
        embargo_hours=row.embargo_hours,
        reason=row.reason,
        actor=row.actor,
        created_at=row.created_at,
    )


def _default_revision() -> RevisionModel:
    return RevisionModel(
        revision=0,
        parent_revision=0,
        disclosure=DEFAULT_ARTIFACT_RELEASE_DISCLOSURE,
        embargo_hours=DEFAULT_ARTIFACT_RELEASE_EMBARGO_HOURS,
        reason="Built-in privacy-first default",
        actor="platform",
        created_at=None,
    )


@router.get("", response_model=AdminArtifactReleaseSettingsResponse)
async def get_settings(
    request: Request,
    _admin: AdminDep,
    session: SessionDep,
) -> AdminArtifactReleaseSettingsResponse:
    rows = list(
        await session.scalars(
            select(ArtifactReleaseSettingsRevision)
            .order_by(ArtifactReleaseSettingsRevision.revision.desc())
            .limit(100)
        )
    )
    total, confirmed = (
        await session.execute(
            select(
                func.count(AgentKingship.agent_id),
                func.count(AgentKingship.emission_confirmed_at),
            )
        )
    ).one()
    kings = (
        await session.execute(
            select(AgentKingship, Agent.sha256)
            .join(Agent, Agent.agent_id == AgentKingship.agent_id)
            .order_by(
                AgentKingship.emission_confirmed_at.desc().nullslast(),
                AgentKingship.first_crowned_at.desc(),
                AgentKingship.agent_id,
            )
            .limit(25)
        )
    ).all()
    netuid = request.app.state.config.chain.netuid
    cursor = await session.get(SourceEmissionCollectorCursor, netuid)
    last_payout = await session.scalar(
        select(SourceEmissionPayout)
        .where(
            SourceEmissionPayout.netuid == netuid,
        )
        .order_by(SourceEmissionPayout.block.desc())
        .limit(1)
    )
    resolution = (
        await session.get(
            SourceEmissionPayoutResolution, (netuid, last_payout.block_hash)
        )
        if last_payout is not None
        else None
    )
    unresolved = await session.scalar(
        select(func.count())
        .select_from(SourceEmissionPayout)
        .where(
            SourceEmissionPayout.netuid == netuid,
            SourceEmissionPayout.terminal.is_(False),
            ~select(SourceEmissionPayoutResolution.netuid)
            .where(
                SourceEmissionPayoutResolution.netuid == SourceEmissionPayout.netuid,
                SourceEmissionPayoutResolution.block_hash
                == SourceEmissionPayout.block_hash,
            )
            .exists(),
        )
    )
    # Claims not represented in any resolved payout remain pending evidence.
    receipts = await session.scalar(
        select(func.count())
        .select_from(ValidatorWeightReceipt)
        .where(
            ValidatorWeightReceipt.netuid == netuid,
            ~select(SourceEmissionPayoutResolution.netuid)
            .where(
                SourceEmissionPayoutResolution.netuid == netuid,
                SourceEmissionPayoutResolution.proof.op("@>")(
                    func.jsonb_build_object(
                        "validators",
                        func.jsonb_build_array(
                            func.jsonb_build_object(
                                "receipt_digest", ValidatorWeightReceipt.receipt_digest
                            )
                        ),
                    )
                ),
            )
            .exists(),
        )
    )
    diagnostic_rows = list(
        await session.scalars(
            select(ValidatorReceiptDiagnostic)
            .where(ValidatorReceiptDiagnostic.netuid == netuid)
            .order_by(
                ValidatorReceiptDiagnostic.received_at.desc(),
                ValidatorReceiptDiagnostic.validator_hotkey,
            )
            .limit(65)
        )
    )
    return AdminArtifactReleaseSettingsResponse(
        current=_revision(rows[0]) if rows else _default_revision(),
        history=[_revision(row) for row in rows],
        release_gate=SourceReleaseGateStatus(
            version=SOURCE_RELEASE_GATE_VERSION,
            automatic_confirmation_enabled=request.app.state.config.source_emission_confirmation_enabled,
            collector_cursor_block=cursor.block if cursor else None,
            collector_cursor_hash=cursor.block_hash if cursor else None,
            collector_runtime_code_hash=cursor.runtime_code_hash if cursor else None,
            collector_blocked_reason=cursor.last_blocked_reason if cursor else None,
            last_payout_block=last_payout.block if last_payout else None,
            last_payout_blocked_reason=last_payout.blocked_reason
            if last_payout
            else None,
            last_payout_attributed=resolution is not None,
            unresolved_payout_count=int(unresolved or 0),
            pending_receipt_count=int(receipts or 0),
            receipt_diagnostics=[
                ReceiptDiagnosticRow(
                    report=ReceiptDiagnosticReport.model_validate(row.report),
                    received_at=row.received_at,
                    stale=(datetime.now(UTC) - row.received_at).total_seconds() > 300,
                )
                for row in diagnostic_rows[:64]
            ],
            receipt_diagnostics_has_more=len(diagnostic_rows) > 64,
            pending_kings=total - confirmed,
            confirmed_kings=confirmed,
            rows=[
                SourceReleaseEligibilityRow(
                    agent_id=king.agent_id,
                    artifact_sha256=sha256,
                    crowned_at=king.first_crowned_at,
                    weight_confirmed_at=king.weight_confirmed_at,
                    emission_confirmed_at=king.emission_confirmed_at,
                    emission_block=king.emission_block,
                    emission_block_hash=king.emission_block_hash,
                    emission_epoch_index=king.emission_epoch_index,
                    emission_ledger_digest=king.emission_ledger_digest,
                )
                for king, sha256 in kings
            ],
            rows_has_more=total > 25,
        ),
    )


@router.post("", response_model=RevisionModel)
async def create_settings_revision(
    payload: AdminArtifactReleaseSettingsRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> RevisionModel:
    """Set the global release policy with CAS and an append-only audit record.

    Two values on one revision. ``disclosure`` says whether source is ever
    published; ``embargo_hours`` says how soon, anywhere in the 6-hour-to-one-
    year range. 48 hours remains the community-agreed default, not a cap.

    They are written together because they are one decision. Split across two
    endpoints, a policy change from "48 hours" to "never" would be two writes
    with a window between them in which the subnet was in neither state, and
    the audit would record a transition nobody chose.

    ``embargo_hours`` stays required and in range even when disclosure is
    ``never``: it is retained so that returning to ``public`` restores the
    window the subnet last agreed on, rather than forcing one to be re-chosen
    under whatever pressure prompted the reversal.

    Shortening a window still releases source earlier and cannot be reversed,
    so the console surfaces that warning; the server only enforces the CAS
    revision and the exact confirmation phrase.
    """
    expected_confirmation = release_confirmation(
        payload.disclosure, payload.embargo_hours
    )
    if payload.confirmation != expected_confirmation:
        raise HTTPException(
            status_code=409,
            detail=f"confirmation must be exactly {expected_confirmation}",
        )

    latest = await latest_artifact_release_settings(session)
    actual_revision = latest.revision if latest is not None else 0
    if payload.expected_revision != actual_revision:
        raise HTTPException(
            status_code=409,
            detail=(
                "artifact release settings changed; refresh before applying "
                f"(expected {payload.expected_revision}, current {actual_revision})"
            ),
        )

    row = ArtifactReleaseSettingsRevision(
        parent_revision=actual_revision,
        disclosure=payload.disclosure.value,
        embargo_hours=payload.embargo_hours,
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
                "artifact release settings changed concurrently; "
                "refresh before applying"
            ),
        ) from error
    await session.refresh(row)
    return _revision(row)
