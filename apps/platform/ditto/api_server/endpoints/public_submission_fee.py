"""Public, source-safe projection of the miner submission fee and its history.

Exposes the current fee, its denomination, the revision and time at which it
took effect, the bounded quote lifetime, and every earlier fee change. Operator
identity and free-text reasons are private (as in the public admin-activity
feed) and never leave this endpoint. The quote a miner actually pays is still
the one ``/upload/check`` reserves; this endpoint is informational.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.submission_settings import (
    SUBMISSION_FEE_DENOMINATION_FIXED_TAO,
    PublicSubmissionFee,
    PublicSubmissionFeeRevision,
    format_rao_as_tao,
)
from ditto.api_server.dependencies import get_session
from ditto.db.models import SubmissionSettingsRevision
from ditto.db.queries.submission_settings import (
    DEFAULT_SUBMISSION_FEE_RAO,
    UPLOAD_ADMISSION_TTL,
    require_supported_fee_denomination,
    submission_settings_history,
)

router = APIRouter(prefix="/public/submission-fee", tags=["public"])

# Revisions are operator-paced (a handful per month); this is a safety cap on
# one read, not a pagination contract.
_SCAN_LIMIT = 5000


def _fee_change(
    row: SubmissionSettingsRevision, previous: SubmissionSettingsRevision | None
) -> PublicSubmissionFeeRevision:
    return PublicSubmissionFeeRevision(
        revision=row.revision,
        fee_denomination=SUBMISSION_FEE_DENOMINATION_FIXED_TAO,
        fee_amount_rao=row.fee_amount_rao,
        fee_amount_tao=format_rao_as_tao(row.fee_amount_rao),
        previous_fee_amount_rao=(
            previous.fee_amount_rao if previous is not None else None
        ),
        previous_fee_amount_tao=(
            format_rao_as_tao(previous.fee_amount_rao) if previous is not None else None
        ),
        effective_at=row.created_at,
    )


@router.get("", response_model=PublicSubmissionFee)
async def public_submission_fee(
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> PublicSubmissionFee:
    rows = await submission_settings_history(session, limit=_SCAN_LIMIT)
    changes = [
        _fee_change(row, previous)
        for row, previous in rows
        if previous is None or previous.fee_amount_rao != row.fee_amount_rao
    ]
    quote_lifetime_seconds = int(UPLOAD_ADMISSION_TTL.total_seconds())
    response.headers["Cache-Control"] = "public, max-age=15"
    if not rows:
        return PublicSubmissionFee(
            policy_revision=0,
            fee_denomination=SUBMISSION_FEE_DENOMINATION_FIXED_TAO,
            fee_amount_rao=DEFAULT_SUBMISSION_FEE_RAO,
            fee_amount_tao=format_rao_as_tao(DEFAULT_SUBMISSION_FEE_RAO),
            fee_revision=0,
            fee_effective_at=None,
            quote_lifetime_seconds=quote_lifetime_seconds,
            history=[],
        )
    latest, _ = rows[0]
    # Never publish a price this build would refuse to quote.
    require_supported_fee_denomination(latest)
    current_change = changes[0] if changes else _fee_change(latest, None)
    return PublicSubmissionFee(
        policy_revision=latest.revision,
        fee_denomination=SUBMISSION_FEE_DENOMINATION_FIXED_TAO,
        fee_amount_rao=latest.fee_amount_rao,
        fee_amount_tao=format_rao_as_tao(latest.fee_amount_rao),
        fee_revision=current_change.revision,
        fee_effective_at=current_change.effective_at,
        quote_lifetime_seconds=quote_lifetime_seconds,
        history=changes[:limit],
        history_truncated=len(changes) > limit,
    )
