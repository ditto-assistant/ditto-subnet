"""Reads and mutations for miner ↔ Ditto account links and link attempts."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from ditto.db.models import MinerDittoLink, MinerDittoLinkAttempt

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def get_link(session: AsyncSession, *, hotkey: str) -> MinerDittoLink | None:
    """The link row for ``hotkey`` whether or not it is revoked."""
    return await session.get(MinerDittoLink, hotkey)


async def get_active_link(
    session: AsyncSession, *, hotkey: str
) -> MinerDittoLink | None:
    row = await get_link(session, hotkey=hotkey)
    if row is None or row.revoked_at is not None:
        return None
    return row


async def list_links_for_user(
    session: AsyncSession, *, ditto_user_id: str
) -> list[MinerDittoLink]:
    """Every hotkey currently linked to one Ditto account (multi-hotkey view)."""
    stmt = (
        select(MinerDittoLink)
        .where(
            MinerDittoLink.ditto_user_id == ditto_user_id,
            MinerDittoLink.revoked_at.is_(None),
        )
        .order_by(MinerDittoLink.created_at)
    )
    return list((await session.execute(stmt)).scalars().all())


async def upsert_link(
    session: AsyncSession,
    *,
    hotkey: str,
    ditto_user_id: str,
    ditto_email: str | None,
    miner_coldkey: str | None,
    linked_via: str,
    session_id: UUID | None,
    now: datetime,
) -> MinerDittoLink:
    """Bind ``hotkey`` to ``ditto_user_id``, replacing any previous or revoked link."""
    insert_stmt = insert(MinerDittoLink).values(
        miner_hotkey=hotkey,
        ditto_user_id=ditto_user_id,
        ditto_email=ditto_email,
        miner_coldkey=miner_coldkey,
        linked_via=linked_via,
        session_id=session_id,
        created_at=now,
        updated_at=now,
        revoked_at=None,
    )
    upsert_stmt = insert_stmt.on_conflict_do_update(
        index_elements=[MinerDittoLink.miner_hotkey],
        set_={
            "ditto_user_id": insert_stmt.excluded.ditto_user_id,
            "ditto_email": insert_stmt.excluded.ditto_email,
            "miner_coldkey": insert_stmt.excluded.miner_coldkey,
            "linked_via": insert_stmt.excluded.linked_via,
            "session_id": insert_stmt.excluded.session_id,
            "updated_at": insert_stmt.excluded.updated_at,
            "revoked_at": None,
        },
    ).returning(MinerDittoLink)
    return (await session.execute(upsert_stmt)).scalar_one()


async def revoke_link(session: AsyncSession, *, hotkey: str, now: datetime) -> bool:
    stmt = (
        update(MinerDittoLink)
        .where(
            MinerDittoLink.miner_hotkey == hotkey,
            MinerDittoLink.revoked_at.is_(None),
        )
        .values(revoked_at=now, updated_at=now)
        .returning(MinerDittoLink.miner_hotkey)
    )
    return (await session.execute(stmt)).first() is not None


async def create_attempt(
    session: AsyncSession,
    *,
    miner_hotkey: str,
    session_id: UUID,
    state_hash: str,
    nonce: str,
    code_verifier: str,
    client: str,
    return_to: str | None,
    now: datetime,
    expires_at: datetime,
) -> MinerDittoLinkAttempt:
    row = MinerDittoLinkAttempt(
        attempt_id=uuid4(),
        miner_hotkey=miner_hotkey,
        session_id=session_id,
        state_hash=state_hash,
        nonce=nonce,
        code_verifier=code_verifier,
        client=client,
        return_to=return_to,
        status="pending",
        created_at=now,
        expires_at=expires_at,
    )
    session.add(row)
    await session.flush()
    return row


async def get_attempt(
    session: AsyncSession, *, attempt_id: UUID
) -> MinerDittoLinkAttempt | None:
    return await session.get(MinerDittoLinkAttempt, attempt_id)


async def get_attempt_by_state(
    session: AsyncSession, *, state_hash: str, lock: bool = False
) -> MinerDittoLinkAttempt | None:
    stmt = select(MinerDittoLinkAttempt).where(
        MinerDittoLinkAttempt.state_hash == state_hash
    )
    if lock:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def expire_stale_attempt(
    session: AsyncSession, *, attempt: MinerDittoLinkAttempt, now: datetime
) -> MinerDittoLinkAttempt:
    if attempt.status == "pending" and attempt.expires_at <= now:
        attempt.status = "expired"
        attempt.completed_at = now
        await session.flush()
    elif (
        attempt.status == "identity_verified"
        and attempt.accept_expires_at is not None
        and attempt.accept_expires_at <= now
    ):
        attempt.status = "expired"
        attempt.error = "the Ditto account holder did not accept in time"
        attempt.accept_token_hash = None
        attempt.completed_at = now
        await session.flush()
    return attempt
