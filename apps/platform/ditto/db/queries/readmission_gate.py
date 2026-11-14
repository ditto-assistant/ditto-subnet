"""When the SN-485 readmission fee and self-serve readmission are active.

A *readmission* is the owner of an existing agent lineage asking Ditto to score
it again: opting an existing agent into a new benchmark era, or re-running an
unchanged agent after one of its evaluations completed (``ditto upload
--allow-identical-rescore``). The design is ``docs/design/readmission-fee.md``.

Both are gated by one predicate, :func:`readmission_activation`. Readmission is
active only when **all** of these hold:

* an operator has written ``readmission_announced_at`` on the submission
  settings revision (it is null by default, and null means inactive);
* the active benchmark is v19 or later (``READMISSION_MIN_BENCH_VERSION``): the
  fee applies from v19, never to v18;
* at least seven days have passed since the announcement
  (``READMISSION_NOTICE_PERIOD``).

The announcement date is therefore the kill switch: clearing it turns
readmission off at once. Nothing is ever charged retroactively. A submission
made before activation keeps the behaviour it had when it was made, and the
callers that classify uploads compare each upload's own time against
:attr:`ReadmissionActivation.effective_at`.

The price is not a setting of its own. It is always exactly the resolved
upload (default) fee, ``fee_amount_rao`` on the same revision
(:func:`readmission_fee_rao`), so it can never exceed the upload fee.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from ditto.api_models.submission_settings import ReadmissionInactiveReason
from ditto.db.queries.benchmark_rollout import active_bench_version

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from ditto.db.queries.submission_settings import EffectiveSubmissionSettings

READMISSION_MIN_BENCH_VERSION = 19
"""The first benchmark era the readmission fee applies to (SN-485 section 7)."""
READMISSION_NOTICE_PERIOD = timedelta(days=7)
"""The minimum notice between the announcement and the fee going live."""
READMISSION_ANNOUNCEMENT_MAX_BACKDATE = timedelta(hours=1)
"""How far before the write a newly recorded announcement may be dated.

A backdated announcement would shorten the notice miners actually got. The
small allowance covers an operator writing the setting just after posting."""
READMISSION_ANNOUNCEMENT_MAX_LEAD = timedelta(days=30)
"""How far ahead an announcement may be scheduled."""


@dataclass(frozen=True)
class ReadmissionActivation:
    """Whether readmission is live right now, and every reason it is not."""

    active: bool
    inactive_reasons: tuple[ReadmissionInactiveReason, ...]
    """Empty when active; otherwise every unmet condition, in the order
    ``announcement_unset``, ``bench_version_below_minimum``,
    ``notice_period``."""
    announced_at: datetime | None
    effective_at: datetime | None
    """``announced_at`` plus the notice period: the earliest moment the fee
    can apply. ``None`` while no announcement is recorded."""
    active_bench_version: int
    min_bench_version: int
    notice_period_seconds: int
    fee_amount_rao: int
    """What a readmission costs once active: the upload default fee."""
    settings_revision: int


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def readmission_fee_rao(settings: EffectiveSubmissionSettings) -> int:
    """The readmission price: always exactly the resolved upload fee."""
    return settings.fee_amount_rao


def readmission_activation(
    *,
    announced_at: datetime | None,
    active_bench_version: int,
    fee_amount_rao: int,
    settings_revision: int,
    now: datetime,
) -> ReadmissionActivation:
    """The pure activation predicate (see the module docstring)."""
    reasons: list[ReadmissionInactiveReason] = []
    announced = _utc(announced_at) if announced_at is not None else None
    effective_at = (
        announced + READMISSION_NOTICE_PERIOD if announced is not None else None
    )
    if announced is None:
        reasons.append("announcement_unset")
    if active_bench_version < READMISSION_MIN_BENCH_VERSION:
        reasons.append("bench_version_below_minimum")
    if effective_at is not None and _utc(now) < effective_at:
        reasons.append("notice_period")
    return ReadmissionActivation(
        active=not reasons,
        inactive_reasons=tuple(reasons),
        announced_at=announced,
        effective_at=effective_at,
        active_bench_version=active_bench_version,
        min_bench_version=READMISSION_MIN_BENCH_VERSION,
        notice_period_seconds=int(READMISSION_NOTICE_PERIOD.total_seconds()),
        fee_amount_rao=fee_amount_rao,
        settings_revision=settings_revision,
    )


async def current_readmission_activation(
    session: AsyncSession,
    *,
    settings: EffectiveSubmissionSettings,
    bench_version: int | None = None,
    now: datetime | None = None,
) -> ReadmissionActivation:
    """Resolve activation against the live active benchmark.

    ``bench_version`` lets a caller that already resolved the active version
    reuse it instead of reading the rollout again.
    """
    return readmission_activation(
        announced_at=settings.readmission_announced_at,
        active_bench_version=(
            bench_version
            if bench_version is not None
            else await active_bench_version(session)
        ),
        fee_amount_rao=readmission_fee_rao(settings),
        settings_revision=settings.revision,
        now=now or datetime.now(UTC),
    )


def submitted_before_activation(
    activation: ReadmissionActivation, submitted_at: datetime
) -> bool:
    """Whether a submission predates the fee going live (never charged)."""
    return activation.effective_at is None or _utc(submitted_at) < (
        activation.effective_at
    )


class ReadmissionAnnouncementError(ValueError):
    """A newly recorded announcement date is outside the allowed window."""


def validate_new_announcement(value: datetime, *, now: datetime) -> datetime:
    """Normalize a newly written announcement date, or refuse it.

    Only a *changed* value is checked: carrying the current date forward
    unchanged on a later revision never re-validates it.
    """
    if value.tzinfo is None:
        raise ReadmissionAnnouncementError(
            "readmission_announced_at must carry a timezone"
        )
    normalized = value.astimezone(UTC).replace(microsecond=0)
    current = _utc(now)
    if normalized < current - READMISSION_ANNOUNCEMENT_MAX_BACKDATE:
        raise ReadmissionAnnouncementError(
            "readmission_announced_at may not be backdated more than "
            f"{int(READMISSION_ANNOUNCEMENT_MAX_BACKDATE.total_seconds())} seconds: "
            "a backdated announcement would shorten the notice miners got"
        )
    if normalized > current + READMISSION_ANNOUNCEMENT_MAX_LEAD:
        raise ReadmissionAnnouncementError(
            "readmission_announced_at may be scheduled at most "
            f"{READMISSION_ANNOUNCEMENT_MAX_LEAD.days} days ahead"
        )
    return normalized


__all__ = [
    "READMISSION_ANNOUNCEMENT_MAX_BACKDATE",
    "READMISSION_ANNOUNCEMENT_MAX_LEAD",
    "READMISSION_MIN_BENCH_VERSION",
    "READMISSION_NOTICE_PERIOD",
    "ReadmissionActivation",
    "ReadmissionAnnouncementError",
    "ReadmissionInactiveReason",
    "current_readmission_activation",
    "readmission_activation",
    "readmission_fee_rao",
    "submitted_before_activation",
    "validate_new_announcement",
]
