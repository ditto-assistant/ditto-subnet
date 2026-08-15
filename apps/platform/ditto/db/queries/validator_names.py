"""Durable cache for optional public validator metadata."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from ditto.db.models import ValidatorNameCache

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def load_validator_name_cache(
    session: AsyncSession,
) -> tuple[dict[str, str], dict[str, float], datetime] | None:
    rows = list(await session.scalars(select(ValidatorNameCache)))
    if not rows:
        return None
    refreshed_at = max(row.refreshed_at for row in rows)
    current = [row for row in rows if row.refreshed_at == refreshed_at]
    return (
        {
            row.validator_hotkey: row.display_name
            for row in current
            if row.display_name is not None
        },
        {
            row.validator_hotkey: row.stake_weight
            for row in current
            if row.stake_weight is not None
        },
        refreshed_at,
    )


async def replace_validator_name_cache(
    session: AsyncSession,
    *,
    names: dict[str, str],
    stake_weights: dict[str, float],
    refreshed_at: datetime,
) -> None:
    hotkeys = names.keys() | stake_weights.keys()
    if hotkeys:
        values = [
            {
                "validator_hotkey": hotkey,
                "display_name": names.get(hotkey),
                "stake_weight": stake_weights.get(hotkey),
                "refreshed_at": refreshed_at,
            }
            for hotkey in sorted(hotkeys)
        ]
        statement = insert(ValidatorNameCache).values(values)
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=[ValidatorNameCache.validator_hotkey],
                set_={
                    "display_name": statement.excluded.display_name,
                    "stake_weight": statement.excluded.stake_weight,
                    "refreshed_at": statement.excluded.refreshed_at,
                },
            )
        )
        await session.execute(
            delete(ValidatorNameCache).where(
                ValidatorNameCache.validator_hotkey.not_in(hotkeys)
            )
        )
    else:
        await session.execute(delete(ValidatorNameCache))
