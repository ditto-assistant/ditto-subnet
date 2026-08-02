"""Stress + odd-values scenario.

260 submissions so ``/public/activity`` paginates (50/page -> 6 pages) with
large status counts, plus every awkward value the dashboard should survive:
unicode names (CJK, emoji, RTL), 1- and 60-char names, 47- and 48-char
hotkeys, composites at exactly 0.0 and ~0.999, median latencies of 0 and
45000 ms, versions 1 and 47, wildly disagreeing per-validator composites,
a 12-version miner for the sparkline, and submissions 3 minutes / 6 months
old.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from ditto.simulator.fabric import Fabric
    from ditto.simulator.scenarios import ScenarioContext

NAME = "edge"
DESCRIPTION = (
    "260 submissions + odd values: unicode/1/60-char names, 0.0/0.999 "
    "composites, disagreeing validators, 12-version sparkline miner."
)

_N_UPLOADED = 100
_N_REJECTED = 100
_N_FINALIZED = 20
_SPARKLINE_VERSIONS = 12

_REJECT_REASONS = (
    ("build failed", "build-compile-error"),
    ("serve failed", "serve-boot-timeout"),
    ("policy violation", "policy-dynamic-exec"),
)


async def _bulk_statuses(session: AsyncSession, f: Fabric) -> None:
    """The volume: enough rows that every status count is large."""
    for i in range(_N_UPLOADED):
        await f.uploaded_agent(
            session, index=1000 + i, created_at=f.minutes_ago(30 + i * 7)
        )
    for i in range(_N_REJECTED):
        reason, code = _REJECT_REASONS[i % len(_REJECT_REASONS)]
        await f.rejected_agent(
            session,
            index=1100 + i,
            screening_reason=reason,
            screening_reason_code=code,
            created_at=f.hours_ago(5 + i),
        )
    for i in range(_N_FINALIZED):
        await f.finalized_agent(
            session,
            index=1200 + i,
            composite=round(0.35 + i * 0.02, 3),
            created_at=f.days_ago(3 + i * 0.5),
        )


async def _evaluating_and_review(session: AsyncSession, f: Fabric) -> None:
    """Mid-pipeline variety: live tickets, partial quorums, holds."""
    # Live issued ticket + a validator mid-run (issued_to names are globally
    # unique per run: one issued ticket per validator hotkey).
    live = await f.evaluating_agent(
        session, index=1220, issued_to=("validator-4",), created_at=f.hours_ago(2)
    )
    await f.validator_heartbeat(
        session,
        name="validator-4",
        state="running_benchmark",
        active_agent_id=live.agent_id,
    )
    await f.evaluating_agent(
        session, index=1221, scored_by=("validator-1",), created_at=f.hours_ago(3)
    )
    # 2-of-3 quorum, no live ticket, far below the floor -> below_score_floor.
    await f.evaluating_agent(
        session,
        index=1222,
        scored_by=("validator-1", "validator-2"),
        composite=0.05,
        created_at=f.hours_ago(4),
    )
    for i in range(5):
        await f.evaluating_agent(session, index=1223 + i, created_at=f.hours_ago(5 + i))
    for i in range(5):
        await f.quarantined_agent(session, index=1230 + i)
    best = await f.finalized_agent(session, index=1235, composite=0.82)
    await f.ath_review_agent(session, index=1236, original=best, composite=0.84)


async def _sparkline_miner(session: AsyncSession, f: Fabric) -> None:
    """One miner with 12 agent versions, composites trending up."""
    hotkey = f.ss58_hotkey("miner:sparkline")
    for i in range(_SPARKLINE_VERSIONS):
        await f.finalized_agent(
            session,
            index=1300 + i,
            composite=round(0.30 + i * 0.04, 3),
            miner_hotkey=hotkey,
            name="sparkline",
            version=i + 1,
            created_at=f.days_ago(90 - i * 7),
        )


async def _odd_values(session: AsyncSession, f: Fabric) -> None:
    """The awkward single rows the UI must not choke on."""
    # Unicode names: CJK, emoji, RTL.
    await f.finalized_agent(session, index=1400, composite=0.52, name="记忆代理-北斗")
    await f.uploaded_agent(session, index=1401, name="🦀🚀-agent")
    await f.rejected_agent(session, index=1402, name="وكيل-الذاكرة")
    # Name-length extremes: 1 char and 60 chars.
    await f.uploaded_agent(session, index=1403, name="x")
    await f.uploaded_agent(session, index=1404, name="long-" + "x" * 55)
    # Composite extremes: exactly 0.0 and ~0.999.
    await f.finalized_agent(session, index=1405, composites=(0.0, 0.0, 0.0))
    await f.finalized_agent(session, index=1406, composites=(0.987, 0.999, 0.9995))
    # Latency extremes: 0 ms and 45000 ms medians.
    await f.finalized_agent(session, index=1407, composite=0.48, median_ms=0)
    await f.finalized_agent(session, index=1408, composite=0.44, median_ms=45_000)
    # Per-validator composites that disagree wildly (median 0.5).
    await f.finalized_agent(session, index=1409, composites=(0.2, 0.5, 0.9))
    # Version extremes: everything else is v1; this one is v47.
    await f.finalized_agent(session, index=1410, composite=0.57, version=47)
    # Timestamp extremes: 3 minutes old and ~6 months old.
    await f.uploaded_agent(session, index=1411, created_at=f.minutes_ago(3))
    await f.rejected_agent(session, index=1412, created_at=f.days_ago(182))
    # Hotkey-length extremes: 47 chars here, 48 chars everywhere else.
    await f.uploaded_agent(
        session, index=1413, miner_hotkey=f.ss58_hotkey("miner:short")[:47]
    )
    await session.flush()


async def apply(ctx: ScenarioContext) -> None:
    f = ctx.fabric
    async with ctx.session_maker() as session, session.begin():
        for validator in ("validator-1", "validator-2", "validator-3"):
            await f.validator_heartbeat(session, name=validator)
        await f.screener_heartbeat(session, name="screener-1")
        await _bulk_statuses(session, f)
        await _evaluating_and_review(session, f)
        await _sparkline_miner(session, f)
        await _odd_values(session, f)
