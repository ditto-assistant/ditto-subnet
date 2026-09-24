"""Audited operator control for miner submission cooldown and pricing.

Every change is one append-only revision guarded by ``expected_revision``, an
operator reason, and an exact confirmation phrase. A stale or concurrent write
returns 409 and changes nothing. A new revision takes effect on commit, with no
deploy; reservations issued earlier keep the fee they were quoted at until they
are consumed or expire (``UPLOAD_ADMISSION_TTL``).
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.submission_settings import (
    MAX_SUBMISSION_COOLDOWN_SECONDS,
    MAX_SUBMISSION_FEE_RAO,
    MIN_SUBMISSION_COOLDOWN_SECONDS,
    MIN_SUBMISSION_FEE_RAO,
    SUBMISSION_FEE_DENOMINATION_FIXED_TAO,
    AdminSubmissionSettingsPreview,
    AdminSubmissionSettingsRequest,
    AdminSubmissionSettingsResponse,
    SubmissionFeeBounds,
    SubmissionSettingsProposal,
    format_rao_as_tao,
    submission_settings_confirmation,
)
from ditto.api_models.submission_settings import (
    SubmissionSettingsRevision as RevisionModel,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.models import SubmissionSettingsRevision
from ditto.db.queries.submission_settings import (
    DEFAULT_SUBMISSION_COOLDOWN_SECONDS,
    DEFAULT_SUBMISSION_FEE_RAO,
    UPLOAD_ADMISSION_TTL,
    in_flight_quotes,
    latest_submission_settings,
    require_supported_fee_denomination,
    submission_settings_history,
)

router = APIRouter(prefix="/admin/submission-settings", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]

_HISTORY_LIMIT = 100
_QUOTE_LIFETIME_SECONDS = int(UPLOAD_ADMISSION_TTL.total_seconds())


def _revision(
    row: SubmissionSettingsRevision,
    previous: SubmissionSettingsRevision | None = None,
) -> RevisionModel:
    return RevisionModel(
        revision=row.revision,
        parent_revision=row.parent_revision,
        cooldown_seconds=row.cooldown_seconds,
        fee_amount_rao=row.fee_amount_rao,
        fee_amount_tao=format_rao_as_tao(row.fee_amount_rao),
        # Project the stored denomination; an unreviewed one fails closed (503)
        # rather than being relabelled as fixed TAO.
        fee_denomination=require_supported_fee_denomination(row),
        previous_fee_amount_rao=(
            previous.fee_amount_rao if previous is not None else None
        ),
        previous_cooldown_seconds=(
            previous.cooldown_seconds if previous is not None else None
        ),
        reason=row.reason,
        actor=row.actor,
        created_at=row.created_at,
    )


def _default_revision() -> RevisionModel:
    return RevisionModel(
        revision=0,
        parent_revision=0,
        cooldown_seconds=DEFAULT_SUBMISSION_COOLDOWN_SECONDS,
        fee_amount_rao=DEFAULT_SUBMISSION_FEE_RAO,
        fee_amount_tao=format_rao_as_tao(DEFAULT_SUBMISSION_FEE_RAO),
        fee_denomination=SUBMISSION_FEE_DENOMINATION_FIXED_TAO,
        reason="Built-in submission cooldown and 0.2 TAO fee",
        actor="platform",
        created_at=None,
    )


async def _current(session: AsyncSession) -> RevisionModel:
    rows = await submission_settings_history(session, limit=1)
    return _revision(*rows[0]) if rows else _default_revision()


@router.get("", response_model=AdminSubmissionSettingsResponse)
async def get_settings(
    _admin: AdminDep, session: SessionDep
) -> AdminSubmissionSettingsResponse:
    rows = await submission_settings_history(session, limit=_HISTORY_LIMIT)
    history = [_revision(row, previous) for row, previous in rows]
    return AdminSubmissionSettingsResponse(
        current=history[0] if history else _default_revision(),
        history=history,
        bounds=SubmissionFeeBounds(),
        quote_lifetime_seconds=_QUOTE_LIFETIME_SECONDS,
    )


@router.get("/preview", response_model=AdminSubmissionSettingsPreview)
async def preview_settings_revision(
    _admin: AdminDep,
    session: SessionDep,
    expected_revision: Annotated[int, Query(ge=0)],
    cooldown_seconds: Annotated[
        int,
        Query(ge=MIN_SUBMISSION_COOLDOWN_SECONDS, le=MAX_SUBMISSION_COOLDOWN_SECONDS),
    ],
    fee_amount_rao: Annotated[
        int, Query(ge=MIN_SUBMISSION_FEE_RAO, le=MAX_SUBMISSION_FEE_RAO)
    ],
) -> AdminSubmissionSettingsPreview:
    """Dry-run one revision: the diff, the exact confirmation, and quotes in flight.

    Read-only (a GET, so it is not an audited mutation). Out-of-bounds values
    are rejected with 422 exactly as the apply endpoint would reject them.
    """
    current = await _current(session)
    quotes = await in_flight_quotes(session, proposed_fee_amount_rao=fee_amount_rao)
    fee_changed = fee_amount_rao != current.fee_amount_rao
    cooldown_changed = cooldown_seconds != current.cooldown_seconds
    stale = expected_revision != current.revision
    ratio = (
        str(
            (Decimal(fee_amount_rao) / Decimal(current.fee_amount_rao)).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_EVEN
            )
        )
        if fee_changed
        else None
    )
    return AdminSubmissionSettingsPreview(
        current=current,
        proposed=SubmissionSettingsProposal(
            cooldown_seconds=cooldown_seconds,
            fee_amount_rao=fee_amount_rao,
            fee_amount_tao=format_rao_as_tao(fee_amount_rao),
            fee_denomination=SUBMISSION_FEE_DENOMINATION_FIXED_TAO,
        ),
        expected_revision=expected_revision,
        stale=stale,
        fee_changed=fee_changed,
        cooldown_changed=cooldown_changed,
        fee_change_ratio=ratio,
        applicable=not stale and (fee_changed or cooldown_changed),
        required_confirmation=submission_settings_confirmation(
            cooldown_seconds, fee_amount_rao
        ),
        bounds=SubmissionFeeBounds(),
        quote_lifetime_seconds=_QUOTE_LIFETIME_SECONDS,
        in_flight_quotes=quotes.count,
        in_flight_quotes_at_other_fees=quotes.at_other_fees,
        in_flight_quotes_expire_by=quotes.expire_by,
    )


@router.post("", response_model=RevisionModel)
async def create_settings_revision(
    payload: AdminSubmissionSettingsRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> RevisionModel:
    latest = await latest_submission_settings(session)
    current_fee = (
        latest.fee_amount_rao if latest is not None else DEFAULT_SUBMISSION_FEE_RAO
    )
    fee_amount_rao = payload.fee_amount_rao or current_fee
    expected_confirmation = submission_settings_confirmation(
        payload.cooldown_seconds, fee_amount_rao
    )
    legacy_confirmation = f"SET SUBMISSION COOLDOWN {payload.cooldown_seconds} SECONDS"
    valid_confirmation = (
        {expected_confirmation, legacy_confirmation}
        if payload.fee_amount_rao is None
        else {expected_confirmation}
    )
    if payload.confirmation not in valid_confirmation:
        raise HTTPException(
            status_code=409,
            detail=f"confirmation must be exactly {expected_confirmation}",
        )
    actual_revision = latest.revision if latest is not None else 0
    if payload.expected_revision != actual_revision:
        raise HTTPException(
            status_code=409,
            detail=(
                "submission settings changed; refresh before applying "
                f"(expected {payload.expected_revision}, current {actual_revision})"
            ),
        )
    previous = (
        (latest.fee_amount_rao, latest.cooldown_seconds) if latest is not None else None
    )
    row = SubmissionSettingsRevision(
        parent_revision=actual_revision,
        cooldown_seconds=payload.cooldown_seconds,
        fee_amount_rao=fee_amount_rao,
        fee_denomination=payload.fee_denomination,
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
            detail="submission settings changed concurrently; refresh before applying",
        ) from error
    await session.refresh(row)
    revision = _revision(row)
    if previous is None:
        return revision
    return revision.model_copy(
        update={
            "previous_fee_amount_rao": previous[0],
            "previous_cooldown_seconds": previous[1],
        }
    )
