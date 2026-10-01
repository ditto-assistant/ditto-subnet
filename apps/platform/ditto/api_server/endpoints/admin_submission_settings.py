"""Audited operator control for miner submission cooldown and pricing.

Every change is one append-only revision guarded by ``expected_revision``, an
operator reason, and an exact confirmation phrase. A stale or concurrent write
returns 409 and changes nothing. A new revision takes effect on commit, with no
deploy. A reservation issued earlier binds the fee it was quoted at for any
payment that finalizes before it expires (``UPLOAD_ADMISSION_TTL`` after issue);
that payment stays recoverable for the same window after its block time.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
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
    SubmissionFeeDenomination,
    SubmissionSettingsProposal,
    UnsupportedSubmissionSettingsRevision,
    format_rao_as_tao,
    submission_settings_confirmation,
)
from ditto.api_models.submission_settings import (
    SubmissionSettingsRevision as RevisionModel,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.pricing.errors import UnsupportedFeeDenominationError
from ditto.db.models import SubmissionSettingsRevision
from ditto.db.queries.submission_settings import (
    DEFAULT_SUBMISSION_COOLDOWN_SECONDS,
    DEFAULT_SUBMISSION_FEE_RAO,
    UPLOAD_ADMISSION_TTL,
    built_in_submission_settings,
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


def fee_change_ratio_text(proposed_rao: int, current_rao: int) -> str:
    """Proposed / current fee to four decimals, rounded away from 1.

    A changed fee never reads as "1.0000" (an increase rounds up, a decrease
    down), and a tiny ratio keeps four significant digits instead of
    collapsing to "0.0000".
    """
    ratio = Decimal(proposed_rao) / Decimal(current_rao)
    rounding = ROUND_CEILING if ratio > 1 else ROUND_FLOOR
    rounded = ratio.quantize(Decimal("0.0001"), rounding=rounding)
    if rounded == 0:
        rounded = ratio.quantize(
            Decimal(1).scaleb(ratio.adjusted() - 3), rounding=ROUND_FLOOR
        )
    return format(rounded, "f")


def _publishable(row: SubmissionSettingsRevision | None) -> bool:
    if row is None:
        return False
    try:
        require_supported_fee_denomination(row)
    except UnsupportedFeeDenominationError:
        return False
    return True


def _revision(
    row: SubmissionSettingsRevision,
    previous: SubmissionSettingsRevision | None = None,
) -> RevisionModel:
    # A parent in a denomination this build cannot price is never shown as the
    # previous fee: its number is not a TAO amount.
    previous = previous if _publishable(previous) else None
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
        reason=(
            "Built-in submission cooldown and "
            f"{format_rao_as_tao(DEFAULT_SUBMISSION_FEE_RAO)} TAO fee"
        ),
        actor="platform",
        created_at=None,
    )


def _resolve_fee_amount_rao(
    latest: SubmissionSettingsRevision | None, requested: int | None
) -> int:
    """The fee a request proposes; one rule for preview and apply.

    An explicit fee is used as sent (already bounds-checked by validation). A
    fee-less (legacy, cooldown-only) request keeps the current fee, but only
    when that fee is a fixed-TAO amount inside today's safe bounds: re-asserting
    a fee in an unsupported denomination would relabel its number as TAO, and
    re-asserting an out-of-bounds historical fee would bypass the bounds. Both
    are 422 and the operator must send ``fee_amount_rao`` explicitly.
    """
    if requested is not None:
        return requested
    if latest is None:
        return DEFAULT_SUBMISSION_FEE_RAO
    if not _publishable(latest):
        raise HTTPException(
            status_code=422,
            detail=(
                f"current fee denomination {latest.fee_denomination!r} is "
                "unsupported; send fee_amount_rao explicitly"
            ),
        )
    if not MIN_SUBMISSION_FEE_RAO <= latest.fee_amount_rao <= MAX_SUBMISSION_FEE_RAO:
        raise HTTPException(
            status_code=422,
            detail=(
                f"current fee {latest.fee_amount_rao} rao is outside the safe "
                "bounds; send fee_amount_rao explicitly"
            ),
        )
    return latest.fee_amount_rao


def _unsupported(
    row: SubmissionSettingsRevision,
) -> UnsupportedSubmissionSettingsRevision:
    return UnsupportedSubmissionSettingsRevision(
        revision=row.revision,
        parent_revision=row.parent_revision,
        cooldown_seconds=row.cooldown_seconds,
        fee_denomination=row.fee_denomination,
        fee_amount_raw=row.fee_amount_rao,
        reason=row.reason,
        actor=row.actor,
        created_at=row.created_at,
    )


_Current = tuple[RevisionModel | None, UnsupportedSubmissionSettingsRevision | None]


def _current_view(
    rows: list[tuple[SubmissionSettingsRevision, SubmissionSettingsRevision | None]],
) -> _Current:
    """The effective revision for the operator view; never a 503.

    The operator needs to see (and recover from) an effective revision this
    build cannot price, so it is reported raw in ``unsupported_current``
    rather than failing the page. Miner-facing reads still fail closed.
    """
    if not rows:
        return _default_revision(), None
    row, previous = rows[0]
    if not _publishable(row):
        return None, _unsupported(row)
    return _revision(row, previous), None


@router.get("", response_model=AdminSubmissionSettingsResponse)
async def get_settings(
    _admin: AdminDep, session: SessionDep
) -> AdminSubmissionSettingsResponse:
    rows = await submission_settings_history(session, limit=_HISTORY_LIMIT)
    # Reaching the cap means older revisions exist beyond this page.
    capped = len(rows) >= _HISTORY_LIMIT
    current, unsupported_current = _current_view(rows)
    # A revision this build cannot price is omitted from history (and
    # flagged): its fee number is not a TAO amount. An unsupported effective
    # revision is reported raw in unsupported_current instead.
    history: list[RevisionModel] = []
    omitted = False
    for index, (row, previous) in enumerate(rows):
        if index == 0 and unsupported_current is not None:
            # Surfaced in unsupported_current, so not missing from the view.
            continue
        try:
            history.append(_revision(row, previous))
        except UnsupportedFeeDenominationError:
            omitted = True
    return AdminSubmissionSettingsResponse(
        current=current,
        unsupported_current=unsupported_current,
        history=history,
        history_incomplete=omitted or capped,
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
        int | None,
        Query(
            ge=MIN_SUBMISSION_FEE_RAO,
            le=MAX_SUBMISSION_FEE_RAO,
            description=(
                "Omit for a cooldown-only change; the current fee is kept under "
                "the same rule as the apply endpoint."
            ),
        ),
    ] = None,
    fee_denomination: Annotated[
        SubmissionFeeDenomination,
        Query(
            description=(
                "Same field as the apply request; only fixed_tao is accepted, "
                "so preview and apply validate identical inputs."
            )
        ),
    ] = SUBMISSION_FEE_DENOMINATION_FIXED_TAO,
) -> AdminSubmissionSettingsPreview:
    """Dry-run one revision: the diff, the exact confirmation, and quotes in flight.

    Read-only (a GET, so it is not an audited mutation). Out-of-bounds values
    are rejected with 422 exactly as the apply endpoint would reject them, and
    a fee-less (cooldown-only) request resolves the fee by the same rule as
    apply, so the returned confirmation is one apply accepts.
    """
    rows = await submission_settings_history(session, limit=1)
    proposed_fee_rao = _resolve_fee_amount_rao(
        rows[0][0] if rows else None, fee_amount_rao
    )
    current, unsupported_current = _current_view(rows)
    quotes = await in_flight_quotes(session, proposed_fee_amount_rao=proposed_fee_rao)
    if current is not None:
        current_revision = current.revision
        current_cooldown = current.cooldown_seconds
        # Same comparison as apply's no-op check: amount or denomination.
        fee_changed = (
            proposed_fee_rao != current.fee_amount_rao
            or fee_denomination != current.fee_denomination
        )
        ratio = (
            fee_change_ratio_text(proposed_fee_rao, current.fee_amount_rao)
            if proposed_fee_rao != current.fee_amount_rao
            else None
        )
    else:
        assert unsupported_current is not None
        current_revision = unsupported_current.revision
        current_cooldown = unsupported_current.cooldown_seconds
        # The denomination always changes, and no TAO ratio exists.
        fee_changed = True
        ratio = None
    cooldown_changed = cooldown_seconds != current_cooldown
    stale = expected_revision != current_revision
    return AdminSubmissionSettingsPreview(
        current=current,
        unsupported_current=unsupported_current,
        proposed=SubmissionSettingsProposal(
            cooldown_seconds=cooldown_seconds,
            fee_amount_rao=proposed_fee_rao,
            fee_amount_tao=format_rao_as_tao(proposed_fee_rao),
            fee_denomination=fee_denomination,
        ),
        expected_revision=expected_revision,
        stale=stale,
        fee_changed=fee_changed,
        cooldown_changed=cooldown_changed,
        fee_change_ratio=ratio,
        applicable=not stale and (fee_changed or cooldown_changed),
        required_confirmation=submission_settings_confirmation(
            cooldown_seconds, proposed_fee_rao
        ),
        bounds=SubmissionFeeBounds(),
        quote_lifetime_seconds=_QUOTE_LIFETIME_SECONDS,
        in_flight_quotes=quotes.count,
        in_flight_quotes_at_other_fees=quotes.at_other_fees,
        in_flight_quotes_expire_by=quotes.expire_by,
        recoverable_expired_quotes=quotes.recoverable_expired,
        recoverable_expired_quotes_at_other_fees=quotes.recoverable_expired_at_other_fees,
        recoverable_expired_quotes_until=quotes.recoverable_until,
    )


@router.post("", response_model=RevisionModel)
async def create_settings_revision(
    payload: AdminSubmissionSettingsRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> RevisionModel:
    latest = await latest_submission_settings(session)
    fee_amount_rao = _resolve_fee_amount_rao(latest, payload.fee_amount_rao)
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
    parent = latest if latest is not None else built_in_submission_settings()
    if (
        payload.cooldown_seconds == parent.cooldown_seconds
        and fee_amount_rao == parent.fee_amount_rao
        and payload.fee_denomination == parent.fee_denomination
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "proposed submission settings equal the current revision; "
                "nothing to apply"
            ),
        )
    # Snapshot the parent before commit: committing expires loaded attributes.
    parent_snapshot = SubmissionSettingsRevision(
        revision=parent.revision,
        parent_revision=parent.parent_revision,
        cooldown_seconds=parent.cooldown_seconds,
        fee_amount_rao=parent.fee_amount_rao,
        fee_denomination=parent.fee_denomination,
        reason=parent.reason,
        actor=parent.actor,
        created_at=parent.created_at,
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
    # Same rule as history: an unpublishable parent is never shown as the
    # previous fee (its number is not a TAO amount).
    return _revision(row, parent_snapshot)
