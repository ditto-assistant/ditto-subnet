"""Reads and mutations against ``miner_avatars``."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from ditto.db.models import MinerAvatar, MinerAvatarNonce

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.ext.asyncio import AsyncSession


async def get_miner_avatar(session: AsyncSession, *, hotkey: str) -> MinerAvatar | None:
    return await session.get(MinerAvatar, hotkey)


async def list_miner_avatars(
    session: AsyncSession, *, hotkeys: Iterable[str]
) -> dict[str, MinerAvatar]:
    keys = list(hotkeys)
    if not keys:
        return {}
    rows = list(
        (
            await session.execute(
                select(MinerAvatar).where(MinerAvatar.miner_hotkey.in_(keys))
            )
        )
        .scalars()
        .all()
    )
    return {row.miner_hotkey: row for row in rows}


async def record_avatar_nonce(
    session: AsyncSession,
    *,
    nonce: UUID,
    miner_hotkey: str,
    now: datetime,
) -> None:
    session.add(MinerAvatarNonce(nonce=nonce, miner_hotkey=miner_hotkey, used_at=now))


async def upsert_miner_avatar(
    session: AsyncSession,
    *,
    miner_hotkey: str,
    object_key: str,
    content_type: str,
    sha256: str,
    nonce: UUID,
    now: datetime,
) -> MinerAvatar:
    stmt = insert(MinerAvatar).values(
        miner_hotkey=miner_hotkey,
        object_key=object_key,
        content_type=content_type,
        sha256=sha256,
        nonce=nonce,
        updated_at=now,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[MinerAvatar.miner_hotkey],
        set_={
            "object_key": stmt.excluded.object_key,
            "content_type": stmt.excluded.content_type,
            "sha256": stmt.excluded.sha256,
            "nonce": stmt.excluded.nonce,
            "updated_at": stmt.excluded.updated_at,
        },
    ).returning(MinerAvatar)
    return (await session.execute(stmt)).scalar_one()


async def delete_miner_avatar(
    session: AsyncSession, *, hotkey: str
) -> MinerAvatar | None:
    row = await get_miner_avatar(session, hotkey=hotkey)
    if row is None:
        return None
    await session.delete(row)
    await session.flush()
    return row
