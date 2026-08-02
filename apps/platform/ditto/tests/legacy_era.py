"""Write rows for a benchmark era that is now retired.

``2026_07_28_enforce_bench_version_floor`` puts a hard floor under the score
ledger: ``scores``, ``confirmation_scores`` and ``benchmark_rollouts`` refuse
anything below :data:`~ditto.db.queries.benchmark_rollout.MIN_SCOREABLE_BENCH_VERSION`,
and ``validator_tickets`` refuses to be created or re-leased beneath it.

Production keeps its v2-v6 rows because those constraints are ``NOT VALID``:
Postgres enforces them on new writes and never re-examines rows that were
already there. A test database has no "already there" -- the harness migrates a
fresh template on every run -- so a test that needs a retired-era row has to
create the same situation deliberately.

That is all this does. It drops the floor, lets the caller insert rows that
predate it, and puts it back exactly as the migration declares it (``NOT
VALID``, so the rows just written are grandfathered in the same way the real
ones are). What comes out the other side is a database in the state production
is actually in: historical retired-era rows present and readable, and the floor
live for everything written from now on.

Reach for this ONLY when the retired era is the point of the test -- rollout
history, the legacy-``bench_version`` report path, retired-era public pages.
A test that just needs *a* score should use the current era instead; using v2
as a generic fixture value is what made this floor look like a 563-test change
in the first place.

The floor is restored even when the body raises, so one failing test cannot
leave a worker database without its constraints and quietly pass the next one.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from datetime import datetime
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.db.models import BenchmarkRollout
from ditto.db.queries.benchmark_rollout import MIN_SCOREABLE_BENCH_VERSION

# (table, constraint) pairs the migration adds as NOT VALID CHECKs, and the
# ticket trigger. Kept in the same shape as the migration so a new floor there
# fails loudly here rather than silently leaving a table unprotected.
_FLOOR_CONSTRAINTS = (
    ("scores", "scores_bench_version_floor"),
    ("confirmation_scores", "confirmation_scores_bench_version_floor"),
    ("benchmark_rollouts", "benchmark_rollout_desired_floor"),
)
_FLOOR_COLUMN = {
    "scores": "bench_version",
    "confirmation_scores": "bench_version",
    "benchmark_rollouts": "desired_version",
}
_TICKET_TRIGGER = "validator_tickets_bench_version_floor"


@contextlib.asynccontextmanager
async def retired_era_writes_allowed(session: AsyncSession) -> AsyncIterator[None]:
    """Allow retired-era rows to be inserted, then restore the floor.

    Yields with the floor lifted. On exit -- including on exception -- every
    constraint is re-added ``NOT VALID`` and the ticket trigger is re-enabled,
    so anything written inside is grandfathered and anything written after is
    refused, which is exactly the production shape.
    """
    for table, constraint in _FLOOR_CONSTRAINTS:
        await session.execute(
            text(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {constraint}")
        )
    await session.execute(
        text(f"ALTER TABLE validator_tickets DISABLE TRIGGER {_TICKET_TRIGGER}")
    )
    await session.commit()
    try:
        yield
    finally:
        await session.commit()
        for table, constraint in _FLOOR_CONSTRAINTS:
            column = _FLOOR_COLUMN[table]
            await session.execute(
                text(
                    f"ALTER TABLE {table} ADD CONSTRAINT {constraint} "
                    f"CHECK ({column} >= {MIN_SCOREABLE_BENCH_VERSION}) NOT VALID"
                )
            )
        await session.execute(
            text(f"ALTER TABLE validator_tickets ENABLE TRIGGER {_TICKET_TRIGGER}")
        )
        await session.commit()


async def grandfather_active_era(
    session: AsyncSession,
    *,
    version: int,
    now: datetime,
    from_version: int | None = None,
) -> BenchmarkRollout:
    """Put a RETIRED era in force, the way production put it in force.

    ``active_bench_version`` answers from durable authority decisions -- the
    newest ``activated`` rollout, or the newest ``authority_selected`` audit.
    With neither on record it answers the FLOOR. That is the honest reply for a
    ledger holding no activation history, and the only one ``request_job`` can
    act on without cutting a lease the ticket trigger would refuse.

    A test whose subject is a TRANSITION cannot live on that fallback. A rollout
    must move the version forward, the newest shipped contract is v7 and the
    floor is 7, so the source era of any rollout that can still be built lies
    beneath the floor -- and if nothing says so, source and target read as the
    same era and the test asserts nothing. Such a test needs the activation that
    gave the source era authority to be on record: exactly the grandfathered
    pre-floor rollout row production still holds, because the floor is ``NOT
    VALID`` and never touched it.

    Call INSIDE :func:`retired_era_writes_allowed`; ``version`` is below
    ``benchmark_rollout_desired_floor`` and the INSERT is refused otherwise.
    Pass a ``now`` that predates anything the test itself creates, so this stays
    history rather than becoming the newest rollout row.

    Every caller should be re-pointed at a 7 -> 8 transition once the v8
    contract lands (ditto-assistant/ditto-platform#513). Only then does a legal
    forward target exist above the floor, and only then can these fixtures state
    their transition without reaching back into retired history.
    """
    rollout = BenchmarkRollout(
        rollout_id=uuid4(),
        from_version=version - 1 if from_version is None else from_version,
        desired_version=version,
        status="activated",
        cohort_size=5,
        created_at=now,
        activated_at=now,
    )
    session.add(rollout)
    await session.flush()
    return rollout
