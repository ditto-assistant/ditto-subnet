"""Historical infrastructure retry accounting.

The columns and helpers stay readable for old attempts, but policy is now
fail-once: infrastructure failures park just like every other failed attempt.
Only an append-only operator recovery may extend the attempt cap. Keeping these
helpers as no-ops makes rolling deployment fail closed while older callers are
still present and preserves the evidence already shown in Backroom.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import func, select

from ditto.db.models import ValidatorTicket

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# Historical limits are pinned to zero so rolling callers cannot mint another
# lease. Existing counters remain visible as audit evidence only.
MAX_INFRA_RETRY_GRANTS = 0

MAX_AGENT_INFRA_RETRY_GRANTS = 0

# Kept only to interpret historical rows and older serialized state.
INFRA_RETRY_BACKOFF_BASE = timedelta(minutes=2)
INFRA_RETRY_BACKOFF_CAP = timedelta(minutes=30)


def infra_retry_backoff(infra_retry_grants: int) -> timedelta:
    """Return the legacy cooldown associated with historical grant evidence.

    ``infra_retry_grants`` is the count *after* this failure bumped it (so the
    first infra failure passes ``1``). Doubles per prior grant, capped at
    :data:`INFRA_RETRY_BACKOFF_CAP`.
    """
    if infra_retry_grants <= 1:
        return INFRA_RETRY_BACKOFF_BASE
    # Clamp the exponent so a large count can't overflow the timedelta multiply;
    # anything past the cap is clamped to it anyway (real inputs are <= 8).
    steps = min(infra_retry_grants - 1, 20)
    scaled = INFRA_RETRY_BACKOFF_BASE * (2**steps)
    return min(scaled, INFRA_RETRY_BACKOFF_CAP)


async def agent_infra_retry_grants(
    session: AsyncSession, *, agent_id: UUID, bench_version: int
) -> int:
    """Read historical no-fault grants for one agent and benchmark version.

    Summed over the agent's ticket rows at one benchmark version, which is the
    scope the starvation actually happens in: a quorum is per (agent, version),
    and each validator holding a slot on it carries its own
    :data:`MAX_INFRA_RETRY_GRANTS` allowance. A new benchmark version is a
    genuinely different workload and deliberately starts this count over.

    Includes the failing ticket's own current count, so the caller passes the
    result straight to :func:`grant_no_fault_retry` as the fleet total *before*
    this failure's bump.
    """
    total = await session.scalar(
        select(func.coalesce(func.sum(ValidatorTicket.infra_retry_grants), 0)).where(
            ValidatorTicket.agent_id == agent_id,
            ValidatorTicket.bench_version == bench_version,
        )
    )
    return int(total or 0)


def grant_no_fault_retry(
    ticket: ValidatorTicket, *, agent_infra_grants: int | None = None
) -> bool:
    """Refuse automatic compensation under the fail-once policy.

    The parameters remain for rolling callers. This function deliberately never
    mutates the ticket; historical counters are evidence only.
    """
    del ticket, agent_infra_grants
    return False
