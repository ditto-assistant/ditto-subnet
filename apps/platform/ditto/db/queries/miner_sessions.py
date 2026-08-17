"""Reads and mutations for miner sessions, device grants, and profiles."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError as SAIntegrityError

from ditto.db.models import (
    MinerDeviceGrant,
    MinerLoginNonce,
    MinerOauthClient,
    MinerOauthCode,
    MinerProfile,
    MinerSession,
    MinerSessionToken,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class MinerGrantConflictError(Exception):
    """A unique grant or nonce constraint was hit."""


async def get_profile(session: AsyncSession, *, hotkey: str) -> MinerProfile | None:
    return await session.get(MinerProfile, hotkey)


async def upsert_profile(
    session: AsyncSession,
    *,
    hotkey: str,
    x_url: str | None,
    github_url: str | None,
    discord_handle: str | None,
    now: datetime,
) -> MinerProfile:
    insert_stmt = insert(MinerProfile).values(
        miner_hotkey=hotkey,
        x_url=x_url,
        github_url=github_url,
        discord_handle=discord_handle,
        updated_at=now,
    )
    upsert_stmt = insert_stmt.on_conflict_do_update(
        index_elements=[MinerProfile.miner_hotkey],
        set_={
            "x_url": insert_stmt.excluded.x_url,
            "github_url": insert_stmt.excluded.github_url,
            "discord_handle": insert_stmt.excluded.discord_handle,
            "updated_at": insert_stmt.excluded.updated_at,
        },
    ).returning(MinerProfile)
    return (await session.execute(upsert_stmt)).scalar_one()


async def record_login_nonce(
    session: AsyncSession,
    *,
    nonce: UUID,
    miner_hotkey: str,
    now: datetime,
) -> None:
    session.add(MinerLoginNonce(nonce=nonce, miner_hotkey=miner_hotkey, used_at=now))
    try:
        await session.flush()
    except SAIntegrityError as exc:
        raise MinerGrantConflictError("login nonce has already been used") from exc


async def create_oauth_client(
    session: AsyncSession,
    *,
    client_id: str,
    client_name: str,
    redirect_uris: list[str],
    now: datetime,
) -> MinerOauthClient:
    row = MinerOauthClient(
        client_id=client_id,
        client_name=client_name,
        redirect_uris=redirect_uris,
        created_at=now,
    )
    session.add(row)
    await session.flush()
    return row


async def get_oauth_client(
    session: AsyncSession, *, client_id: str
) -> MinerOauthClient | None:
    return await session.get(MinerOauthClient, client_id)


async def create_device_grant(
    session: AsyncSession,
    *,
    user_code: str,
    poll_token_hash: str | None,
    scopes: str,
    ttl_seconds: int,
    expires_at: datetime,
    oauth_client_id: str | None,
    redirect_uri: str | None,
    state: str | None,
    code_challenge: str | None,
) -> MinerDeviceGrant:
    row = MinerDeviceGrant(
        grant_id=uuid4(),
        user_code=user_code,
        poll_token_hash=poll_token_hash,
        status="pending",
        scopes=scopes,
        ttl_seconds=ttl_seconds,
        oauth_client_id=oauth_client_id,
        redirect_uri=redirect_uri,
        state=state,
        code_challenge=code_challenge,
        expires_at=expires_at,
    )
    session.add(row)
    try:
        await session.flush()
    except SAIntegrityError as exc:
        raise MinerGrantConflictError("device grant collided; retry") from exc
    return row


async def get_device_grant_by_code(
    session: AsyncSession, *, user_code: str, lock: bool = False
) -> MinerDeviceGrant | None:
    stmt = select(MinerDeviceGrant).where(MinerDeviceGrant.user_code == user_code)
    if lock:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_device_grant(
    session: AsyncSession, *, grant_id: UUID, lock: bool = False
) -> MinerDeviceGrant | None:
    stmt = select(MinerDeviceGrant).where(MinerDeviceGrant.grant_id == grant_id)
    if lock:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def expire_stale_grant(
    session: AsyncSession, *, grant: MinerDeviceGrant, now: datetime
) -> MinerDeviceGrant:
    if grant.status == "pending" and grant.expires_at <= now:
        grant.status = "expired"
        await session.flush()
    return grant


async def create_session(
    session: AsyncSession,
    *,
    miner_hotkey: str,
    scopes: str,
    label: str,
    expires_at: datetime,
    now: datetime,
) -> MinerSession:
    row = MinerSession(
        session_id=uuid4(),
        miner_hotkey=miner_hotkey,
        scopes=scopes,
        label=label,
        created_at=now,
        expires_at=expires_at,
        last_used_at=now,
    )
    session.add(row)
    await session.flush()
    return row


async def issue_session_token(
    session: AsyncSession,
    *,
    session_id: UUID,
    token_hash: str,
    now: datetime,
) -> MinerSessionToken:
    row = MinerSessionToken(
        token_hash=token_hash,
        session_id=session_id,
        issued_at=now,
        last_used_at=now,
    )
    session.add(row)
    await session.flush()
    return row


async def get_session(
    session: AsyncSession, *, session_id: UUID
) -> MinerSession | None:
    return await session.get(MinerSession, session_id)


async def lookup_session_token(
    session: AsyncSession, *, token_hash: str, now: datetime
) -> tuple[MinerSession, MinerSessionToken] | None:
    token = await session.get(MinerSessionToken, token_hash)
    if token is None:
        return None
    row = await session.get(MinerSession, token.session_id)
    if row is None:
        return None
    if row.revoked_at is not None or row.expires_at <= now:
        return None
    token.last_used_at = now
    row.last_used_at = now
    return row, token


async def revoke_session(
    session: AsyncSession, *, row: MinerSession, now: datetime
) -> MinerSession:
    if row.revoked_at is None:
        row.revoked_at = now
        await session.flush()
    return row


async def create_oauth_code(
    session: AsyncSession,
    *,
    code_hash: str,
    grant_id: UUID,
    session_id: UUID,
    redirect_uri: str,
    expires_at: datetime,
) -> MinerOauthCode:
    row = MinerOauthCode(
        code_hash=code_hash,
        grant_id=grant_id,
        session_id=session_id,
        redirect_uri=redirect_uri,
        expires_at=expires_at,
    )
    session.add(row)
    await session.flush()
    return row


async def get_oauth_code(
    session: AsyncSession, *, code_hash: str
) -> MinerOauthCode | None:
    return await session.get(MinerOauthCode, code_hash)


async def consume_oauth_code(
    session: AsyncSession, *, code_hash: str, now: datetime
) -> MinerOauthCode | None:
    stmt = (
        select(MinerOauthCode)
        .where(MinerOauthCode.code_hash == code_hash)
        .with_for_update()
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None or row.consumed_at is not None or row.expires_at <= now:
        return None
    row.consumed_at = now
    await session.flush()
    return row


async def list_recent_agents_for_hotkey(
    session: AsyncSession,
    *,
    hotkey: str,
    limit: int = 25,
) -> list:
    from ditto.db.models import Agent

    stmt = (
        select(Agent)
        .where(Agent.miner_hotkey == hotkey)
        .order_by(Agent.created_at.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def list_reviews_for_hotkey(
    session: AsyncSession,
    *,
    hotkey: str,
    limit: int = 50,
) -> list[tuple[str, object, object]]:
    """Return (kind, review_or_dispute, agent) tuples newest first."""
    from ditto.db.models import Agent, AthReview, ScreeningDispute

    ath_rows = list(
        (
            await session.execute(
                select(AthReview, Agent)
                .join(Agent, Agent.agent_id == AthReview.agent_id)
                .where(Agent.miner_hotkey == hotkey)
                .order_by(AthReview.opened_at.desc())
                .limit(limit)
            )
        ).all()
    )
    dispute_rows = list(
        (
            await session.execute(
                select(ScreeningDispute, Agent)
                .join(Agent, Agent.agent_id == ScreeningDispute.agent_id)
                .where(Agent.miner_hotkey == hotkey)
                .order_by(ScreeningDispute.created_at.desc())
                .limit(limit)
            )
        ).all()
    )
    combined: list[tuple[str, object, object, datetime]] = []
    for review, agent in ath_rows:
        combined.append(("ath", review, agent, review.opened_at))
    for dispute, agent in dispute_rows:
        combined.append(("dispute", dispute, agent, dispute.created_at))
    combined.sort(key=lambda item: item[3], reverse=True)
    return [(kind, item, agent) for kind, item, agent, _ in combined[:limit]]


def session_remaining(*, expires_at: datetime, now: datetime) -> int:
    remaining = expires_at - now
    if remaining <= timedelta(0):
        return 0
    return int(remaining.total_seconds())
