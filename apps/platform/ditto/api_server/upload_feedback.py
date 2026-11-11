"""Upload guidance calculated using Platform's clock."""

from __future__ import annotations

from datetime import UTC, datetime
from math import ceil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ditto.chain import HotkeyRegistration
    from ditto.db.queries.owner_submission_cooldown import OwnerCooldownNotice


def hotkey_not_registered_message(registration: HotkeyRegistration) -> str:
    """Name the evidence behind a 1101 so a lagging cache reads as one.

    A miss confirmed in chain storage gives that finalized block next to the
    metagraph cache's; a miner who registered after that block knows to wait,
    and one who registered before it knows the registration is not there.
    """
    subject = f"hotkey is not registered on netuid {registration.netuid}"
    snapshot = registration.snapshot_block
    if registration.chain_block is None:
        return (
            f"{subject} in Platform's metagraph cache at block {snapshot} "
            "(not re-checked on chain)"
        )
    lag = registration.chain_block - snapshot
    behind = f", {lag} blocks behind" if lag > 0 else ""
    return (
        f"{subject} as of finalized block {registration.chain_block} "
        f"(read from chain storage; Platform's metagraph cache is at block "
        f"{snapshot}{behind})"
    )


def _interval(seconds: int) -> str:
    if seconds % 86400 == 0:
        days = seconds // 86400
        return f"{days} day" if days == 1 else f"{days} days"
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"{hours} hour" if hours == 1 else f"{hours} hours"
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} minute" if minutes == 1 else f"{minutes} minutes"
    return f"{seconds} seconds"


def submission_cooldown_message(
    retry_at: datetime,
    *,
    now: datetime | None = None,
    owner_override: OwnerCooldownNotice | None = None,
) -> str:
    """Keep the retry timestamp and round positive waits up to a minute.

    An operator per-owner override names its interval and expiry so the miner
    knows why the wait is longer than the published cooldown. The operator's
    internal reason is never included.
    """
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    total_minutes = ceil(max(0, (retry_at - current).total_seconds()) / 60)
    hours, minutes = divmod(total_minutes, 60)
    hour_unit = "hour" if hours == 1 else "hours"
    minute_unit = "minute" if minutes == 1 else "minutes"
    prefix = ""
    if owner_override is not None:
        expires_at = owner_override.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        prefix = (
            "an operator submission cooldown applies to this owner: one "
            f"submission per {_interval(owner_override.cooldown_seconds)} "
            f"until {expires_at.isoformat()}. "
        )
    return (
        f"{prefix}owner coldkey may submit again at {retry_at.isoformat()}. "
        f"Please try again in {hours} {hour_unit} and {minutes} {minute_unit}."
    )
