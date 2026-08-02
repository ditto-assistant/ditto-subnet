"""An early-life subnet: a few miners, thin fleet, sparse history.

Two miners hold finalized scores (the older, higher one is both the KOTH
champion and raw #1 — no margin drama yet), one submission sits at 1-of-3
scores in the provisional lane, and history is sparse: one upload waiting for
screening and one old screening rejection. The fleet is a single online
validator and a single idle screener; the two other validator hotkeys that
co-signed the finalized quorums have since gone quiet and report no
heartbeat.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ditto.simulator.scenarios import ScenarioContext

NAME = "quiet"
DESCRIPTION = (
    "Early-life subnet: 2 scored miners, 1 evaluating, 1 validator online, "
    "1 screener, sparse history."
)


async def apply(ctx: ScenarioContext) -> None:
    f = ctx.fabric
    async with ctx.session_maker() as session, session.begin():
        # ── two finalized miners; the older one is champion and raw #1 ──────
        await f.finalized_agent(
            session,
            index=1,
            composite=0.61,
            created_at=f.days_ago(5),
            n_cases=120,
            validator_names=("validator-1", "validator-2", "validator-3"),
            median_ms=1450,
            rich_details=True,
        )
        await f.finalized_agent(
            session,
            index=2,
            composite=0.44,
            created_at=f.days_ago(2.3),
            n_cases=108,
            validator_names=("validator-1", "validator-2", "validator-3"),
            median_ms=4200,
            rich_details=True,
        )

        # ── one provisional submission at 1-of-3 scores ─────────────────────
        await f.evaluating_agent(
            session,
            index=3,
            scored_by=("validator-1",),
            composite=0.52,
            n_cases=116,
            created_at=f.hours_ago(5),
            median_ms=2600,
            rich_details=True,
        )

        # ── sparse history: one waiting upload, one old rejection ───────────
        await f.uploaded_agent(session, index=4)
        await f.rejected_agent(session, index=5, created_at=f.days_ago(4))

        # ── thin fleet: one validator, one screener ─────────────────────────
        await f.validator_heartbeat(
            session, name="validator-1", state="polling", seen_ago_seconds=26.0
        )
        await f.screener_heartbeat(
            session, name="screener-1", state="polling", seen_ago_seconds=39.0
        )
