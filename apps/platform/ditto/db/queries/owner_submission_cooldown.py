"""Operator per-owner submission cooldown overrides.

The global submission cooldown (:mod:`ditto.db.queries.submission_settings`)
is one interval per payment coldkey for every miner. An override lets an
operator require a longer minimum interval for one owner identity -- a payment
coldkey or a hotkey -- until it expires or is revoked, without touching
anyone else's.

Storage is append-only (:class:`~ditto.db.models.OwnerSubmissionCooldownRevision`):
the newest revision of ``(scope, owner_key)`` is its current state, and every
earlier revision stays as the audit trail.

Enforcement measures the interval from the owner's most recent submission
under *either* identity named in the upload (the uploading hotkey, or any
agent paid by the payment coldkey). That is what makes a hotkey override hold
when the owner pays from a fresh coldkey, and a coldkey override hold when the
owner rotates hotkeys. An override never extends past its own ``expires_at``:
once it lapses only the global cooldown applies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

from sqlalchemy import func, or_, select, text, tuple_

from ditto.db.models import (
    Agent,
    EvaluationPayment,
    OwnerSubmissionCooldownRevision,
    UploadAdmissionReservation,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

OwnerScope = Literal["coldkey", "hotkey"]
OverrideState = Literal["active", "expired", "revoked"]

MIN_OWNER_COOLDOWN_SECONDS = 60
MAX_OWNER_COOLDOWN_SECONDS = 30 * 86400
MAX_OWNER_COOLDOWN_EXPIRY_DAYS = 90
"""How far ahead an override may expire; the interval itself is bounded by
``MAX_OWNER_COOLDOWN_SECONDS``."""


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


@dataclass(frozen=True)
class OwnerCooldownNotice:
    """What a miner is told about the override that is holding them."""

    cooldown_seconds: int
    expires_at: datetime


def override_state(
    row: OwnerSubmissionCooldownRevision, *, now: datetime
) -> OverrideState:
    if row.action == "revoke":
        return "revoked"
    assert row.expires_at is not None
    return "active" if _utc(row.expires_at) > _utc(now) else "expired"


async def current_override(
    session: AsyncSession, *, scope: OwnerScope, owner_key: str
) -> OwnerSubmissionCooldownRevision | None:
    """The newest revision for one owner identity, whatever its state."""
    return await session.scalar(
        select(OwnerSubmissionCooldownRevision)
        .where(
            OwnerSubmissionCooldownRevision.scope == scope,
            OwnerSubmissionCooldownRevision.owner_key == owner_key,
        )
        .order_by(OwnerSubmissionCooldownRevision.revision.desc())
        .limit(1)
    )


async def override_history(
    session: AsyncSession, *, scope: OwnerScope, owner_key: str
) -> list[OwnerSubmissionCooldownRevision]:
    """Every revision of one identity, newest first: the whole audit trail."""
    return list(
        await session.scalars(
            select(OwnerSubmissionCooldownRevision)
            .where(
                OwnerSubmissionCooldownRevision.scope == scope,
                OwnerSubmissionCooldownRevision.owner_key == owner_key,
            )
            .order_by(OwnerSubmissionCooldownRevision.revision.desc())
        )
    )


def _newest_revisions():  # type: ignore[no-untyped-def]
    return (
        select(
            OwnerSubmissionCooldownRevision.scope,
            OwnerSubmissionCooldownRevision.owner_key,
            func.max(OwnerSubmissionCooldownRevision.revision).label("revision"),
        )
        .group_by(
            OwnerSubmissionCooldownRevision.scope,
            OwnerSubmissionCooldownRevision.owner_key,
        )
        .subquery()
    )


def _active_predicate(now: datetime):  # type: ignore[no-untyped-def]
    return (OwnerSubmissionCooldownRevision.action == "set") & (
        OwnerSubmissionCooldownRevision.expires_at > now
    )


async def latest_overrides(
    session: AsyncSession,
    *,
    now: datetime,
    active_only: bool,
    limit: int,
) -> list[OwnerSubmissionCooldownRevision]:
    """The current revision of overridden identities, newest first, bounded."""
    newest = _newest_revisions()
    statement = (
        select(OwnerSubmissionCooldownRevision)
        .join(
            newest,
            (newest.c.scope == OwnerSubmissionCooldownRevision.scope)
            & (newest.c.owner_key == OwnerSubmissionCooldownRevision.owner_key)
            & (newest.c.revision == OwnerSubmissionCooldownRevision.revision),
        )
        .order_by(
            OwnerSubmissionCooldownRevision.created_at.desc(),
            OwnerSubmissionCooldownRevision.revision_id.desc(),
        )
        .limit(limit)
    )
    if active_only:
        statement = statement.where(_active_predicate(now))
    return list(await session.scalars(statement))


async def count_active_overrides(session: AsyncSession, *, now: datetime) -> int:
    newest = _newest_revisions()
    return int(
        await session.scalar(
            select(func.count())
            .select_from(OwnerSubmissionCooldownRevision)
            .join(
                newest,
                (newest.c.scope == OwnerSubmissionCooldownRevision.scope)
                & (newest.c.owner_key == OwnerSubmissionCooldownRevision.owner_key)
                & (newest.c.revision == OwnerSubmissionCooldownRevision.revision),
            )
            .where(_active_predicate(now))
        )
        or 0
    )


async def active_overrides_for(
    session: AsyncSession,
    *,
    miner_coldkey: str | None,
    miner_hotkey: str | None,
    now: datetime,
) -> list[OwnerSubmissionCooldownRevision]:
    """Active overrides naming either identity of one upload."""
    keys: list[tuple[str, str]] = []
    if miner_coldkey:
        keys.append(("coldkey", miner_coldkey))
    if miner_hotkey:
        keys.append(("hotkey", miner_hotkey))
    if not keys:
        return []
    rows = list(
        await session.scalars(
            select(OwnerSubmissionCooldownRevision)
            .where(
                tuple_(
                    OwnerSubmissionCooldownRevision.scope,
                    OwnerSubmissionCooldownRevision.owner_key,
                ).in_(keys)
            )
            .order_by(OwnerSubmissionCooldownRevision.revision.desc())
        )
    )
    newest: dict[tuple[str, str], OwnerSubmissionCooldownRevision] = {}
    for row in rows:
        newest.setdefault((row.scope, row.owner_key), row)
    return [row for row in newest.values() if override_state(row, now=now) == "active"]


async def owner_latest_submission_at(
    session: AsyncSession,
    *,
    miner_coldkey: str | None,
    miner_hotkey: str | None,
) -> datetime | None:
    """The newest submission by the hotkey or paid by the coldkey."""
    clauses = []
    if miner_hotkey:
        clauses.append(Agent.miner_hotkey == miner_hotkey)
    if miner_coldkey:
        clauses.append(
            select(EvaluationPayment.agent_id)
            .where(
                EvaluationPayment.agent_id == Agent.agent_id,
                EvaluationPayment.miner_coldkey == miner_coldkey,
            )
            .correlate(Agent)
            .exists()
        )
    if not clauses:
        return None
    latest = await session.scalar(
        select(func.max(Agent.created_at)).where(or_(*clauses))
    )
    return None if latest is None else _utc(latest)


async def identity_latest_submission_at(
    session: AsyncSession, *, scope: OwnerScope, owner_key: str, now: datetime
) -> datetime | None:
    """The newest submission an override on this one identity can be measured from.

    Enforcement measures from the uploading pair (hotkey and payment coldkey),
    which is only known at upload time. For an operator lookup, take every
    identity this one is linked to by a payment record -- the coldkeys that
    paid for this hotkey, or the hotkeys this coldkey paid for -- so the
    reported ``next_allowed_at`` is never earlier than the one enforcement
    would apply to a pair the owner has already used.
    """
    if scope == "hotkey":
        linked = select(EvaluationPayment.miner_coldkey).where(
            EvaluationPayment.miner_hotkey == owner_key
        )
        paid_by_linked = (
            select(EvaluationPayment.agent_id)
            .where(
                EvaluationPayment.agent_id == Agent.agent_id,
                EvaluationPayment.miner_coldkey.in_(linked),
            )
            .correlate(Agent)
            .exists()
        )
        predicate = or_(Agent.miner_hotkey == owner_key, paid_by_linked)
    else:
        linked_hotkeys = select(EvaluationPayment.miner_hotkey).where(
            EvaluationPayment.miner_coldkey == owner_key
        )
        paid_by_key = (
            select(EvaluationPayment.agent_id)
            .where(
                EvaluationPayment.agent_id == Agent.agent_id,
                EvaluationPayment.miner_coldkey == owner_key,
            )
            .correlate(Agent)
            .exists()
        )
        predicate = or_(paid_by_key, Agent.miner_hotkey.in_(linked_hotkeys))
    latest = await session.scalar(select(func.max(Agent.created_at)).where(predicate))
    latest = None if latest is None else _utc(latest)
    if scope == "hotkey":
        # Enforcement counts another coldkey's live reservation on this hotkey
        # as a submission in flight; report the same bound.
        reserved = await session.scalar(
            select(func.max(UploadAdmissionReservation.created_at)).where(
                UploadAdmissionReservation.miner_hotkey == owner_key,
                UploadAdmissionReservation.expires_at > now,
            )
        )
        if reserved is not None and (latest is None or _utc(reserved) > latest):
            latest = _utc(reserved)
    return latest


async def lock_owner_cooldown_hotkey(session: AsyncSession, miner_hotkey: str) -> None:
    """Serialize admissions sharing one hotkey until the transaction ends.

    The admission paths already hold the payment-coldkey lock; a hotkey
    override must also hold across coldkeys, so two uploads under one hotkey
    paid from different coldkeys cannot both pass it. Always taken after the
    coldkey lock and under its own namespace, so lock order stays coldkey ->
    hotkey everywhere.
    """
    if session.get_bind().dialect.name == "postgresql":
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"owner-cooldown-hotkey:{miner_hotkey}"},
        )


async def owner_override_retry_at(
    session: AsyncSession,
    *,
    miner_coldkey: str | None,
    miner_hotkey: str | None,
    now: datetime,
) -> tuple[datetime, OwnerCooldownNotice] | None:
    """When an active override next allows this owner to submit, if later than now."""
    overrides = await active_overrides_for(
        session, miner_coldkey=miner_coldkey, miner_hotkey=miner_hotkey, now=now
    )
    if not overrides:
        return None
    latest = await owner_latest_submission_at(
        session, miner_coldkey=miner_coldkey, miner_hotkey=miner_hotkey
    )
    if miner_hotkey:
        # A live admission another coldkey reserved for this hotkey is a
        # submission in flight: its payment can still land after this check.
        # Counting it is what makes a hotkey override hold across two
        # coldkeys reserving in parallel (the hotkey lock orders them). The
        # uploader's own coldkey reservation is the existing per-coldkey
        # path's to resolve, so it is excluded here.
        reserved = await session.scalar(
            select(func.max(UploadAdmissionReservation.created_at)).where(
                UploadAdmissionReservation.miner_hotkey == miner_hotkey,
                UploadAdmissionReservation.miner_coldkey != (miner_coldkey or ""),
                UploadAdmissionReservation.expires_at > now,
            )
        )
        if reserved is not None and (latest is None or _utc(reserved) > latest):
            latest = _utc(reserved)
    if latest is None:
        return None
    best: tuple[datetime, OwnerCooldownNotice] | None = None
    for row in overrides:
        assert row.cooldown_seconds is not None and row.expires_at is not None
        expires_at = _utc(row.expires_at)
        retry_at = min(latest + timedelta(seconds=row.cooldown_seconds), expires_at)
        if retry_at <= _utc(now):
            continue
        if best is None or retry_at > best[0]:
            best = (
                retry_at,
                OwnerCooldownNotice(
                    cooldown_seconds=row.cooldown_seconds, expires_at=expires_at
                ),
            )
    return best


__all__ = [
    "MAX_OWNER_COOLDOWN_EXPIRY_DAYS",
    "MAX_OWNER_COOLDOWN_SECONDS",
    "MIN_OWNER_COOLDOWN_SECONDS",
    "OverrideState",
    "OwnerCooldownNotice",
    "OwnerScope",
    "active_overrides_for",
    "count_active_overrides",
    "current_override",
    "identity_latest_submission_at",
    "lock_owner_cooldown_hotkey",
    "latest_overrides",
    "override_history",
    "override_state",
    "owner_latest_submission_at",
    "owner_override_retry_at",
]
