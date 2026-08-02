"""A realistic mid-life subnet with a full KOTH emissions story.

Fourteen miners hold finalized agents with composite medians spread across
[0.35, 0.87]. The **oldest** high scorer is the incumbent emissions champion
while a *newer* agent holds raw rank #1 inside the statistical dethrone band, so the
leaderboard's champion-vs-rank-1 explanation renders with a real decision
(``dethrones=false``). Two miners carry a second agent version whose newer
run scores *below* the older one, a 4-miner participation tail earns the
non-champion emissions share, and a handful of provisional (1- and 2-of-3)
contenders sit in the leaderboard's provisional lane — one of them below the
score-continuation floor. The fleet shows six validators (four online: one
running a benchmark with signed progress, one updating weights, two polling;
one paused; one stale) and two screeners (one actively screening with a live
progress envelope).

Note on ``n``: the platform ranks only full-benchmark runs
(``n >= MIN_ELIGIBLE_CASES == 100``), so the ranked field runs 104-156 cases;
two small smoke runs (44 and 64 cases) are included to exercise the
unranked-but-surfaced transparency lane.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ditto.db.models import Agent
    from ditto.simulator.scenarios import ScenarioContext

NAME = "network"
DESCRIPTION = (
    "Mid-life subnet: 14 finalized miners with a KOTH champion-vs-raw-#1 "
    "story, provisional contenders, 6 validators, 2 screeners."
)

# The ranked field: (index, median composite, age in days, n cases,
# validator-name triple, pinned per-score median latency in ms).
#
# KOTH math (fixed margin = 0.007 composite points; statistical lead from the
# ±(0.012/0.015) quorum jitter ~= 0.018): index 1 is
# the oldest eligible entry at 0.850, index 2 leads raw rank #1 at 0.862 but
# its 0.012 lead does not clear the required lead, so the incumbent keeps the
# crown and the dashboard explains why raw #1 is not the champion. Everyone
# else stays >= 0.02 below the champion so no other entry can dethrone.
_RANKED = (
    (1, 0.850, 21.0, 132, ("validator-1", "validator-2", "validator-3"), 740),
    (2, 0.862, 3.2, 148, ("validator-2", "validator-3", "validator-4"), 380),
    (3, 0.830, 12.0, 128, ("validator-1", "validator-3", "validator-5"), 1150),
    (4, 0.810, 9.0, 116, ("validator-1", "validator-2", "validator-6"), 1980),
    (5, 0.790, 15.0, 124, ("validator-2", "validator-4", "validator-6"), 890),
    (6, 0.760, 6.0, 140, ("validator-3", "validator-4", "validator-5"), 2450),
    (7, 0.720, 18.0, 108, ("validator-1", "validator-4", "validator-5"), 3300),
    (8, 0.680, 11.0, 120, ("validator-2", "validator-5", "validator-6"), 1520),
    (9, 0.630, 8.0, 112, ("validator-1", "validator-2", "validator-4"), 4700),
    (10, 0.550, 5.0, 104, ("validator-3", "validator-5", "validator-6"), 2800),
    (11, 0.470, 14.0, 136, ("validator-1", "validator-3", "validator-6"), 6400),
    (12, 0.350, 19.0, 156, ("validator-2", "validator-3", "validator-6"), 8900),
)

# Small smoke runs: finalized but unranked (n < 100), surfaced for
# transparency with ``eligible=false``.
_SMOKE = (
    (13, 0.580, 4.0, 64, ("validator-1", "validator-4", "validator-6"), 310),
    (14, 0.410, 10.0, 44, ("validator-2", "validator-4", "validator-5"), 520),
)

# Second agent versions where the *older* run outscores the newer one: the
# per-miner leaderboard fold keeps representing these miners by the older,
# better agent. (new index, base index, newer composite, age days, n, latency)
_REGRESSED_V2 = (
    (15, 3, 0.740, 1.5, 124, 1240),
    (16, 5, 0.710, 2.1, 118, 950),
)


async def apply(ctx: ScenarioContext) -> None:
    f = ctx.fabric
    async with ctx.session_maker() as session, session.begin():
        # ── finalized field: 12 ranked + 2 smoke runs ───────────────────────
        by_index: dict[int, Agent] = {}
        for index, composite, age_days, n_cases, validators, latency in _RANKED:
            by_index[index] = await f.finalized_agent(
                session,
                index=index,
                composite=composite,
                created_at=f.days_ago(age_days),
                n_cases=n_cases,
                validator_names=validators,
                median_ms=latency,
                rich_details=True,
            )
        for index, composite, age_days, n_cases, validators, latency in _SMOKE:
            await f.finalized_agent(
                session,
                index=index,
                composite=composite,
                created_at=f.days_ago(age_days),
                n_cases=n_cases,
                run_size="small",
                validator_names=validators,
                median_ms=latency,
                rich_details=True,
            )

        # ── two miners whose newer version regressed ────────────────────────
        for index, base_index, composite, age_days, n_cases, latency in _REGRESSED_V2:
            base = by_index[base_index]
            await f.finalized_agent(
                session,
                index=index,
                composite=composite,
                created_at=f.days_ago(age_days),
                n_cases=n_cases,
                validator_names=("validator-1", "validator-2", "validator-5"),
                miner_hotkey=base.miner_hotkey,
                name=base.name,
                version=2,
                median_ms=latency,
                rich_details=True,
            )

        # ── provisional contenders (evaluating, partial quorum) ─────────────
        # 2-of-3 above the score-continuation floor (5th finalized = 0.79).
        await f.evaluating_agent(
            session,
            index=20,
            scored_by=("validator-1", "validator-3"),
            composite=0.80,
            n_cases=132,
            created_at=f.hours_ago(7),
            median_ms=1080,
            rich_details=True,
        )
        # 1-of-3, mid-field.
        await f.evaluating_agent(
            session,
            index=21,
            scored_by=("validator-4",),
            composite=0.58,
            n_cases=120,
            created_at=f.hours_ago(4),
            median_ms=3900,
            rich_details=True,
        )
        # 2-of-3 below the floor, no live ticket: renders below_score_floor.
        await f.evaluating_agent(
            session,
            index=22,
            scored_by=("validator-5", "validator-6"),
            composite=0.60,
            n_cases=116,
            created_at=f.hours_ago(9),
            median_ms=5600,
            rich_details=True,
        )
        # 1-of-3 with a live issued ticket: validator-2 is benchmarking it now.
        bench = await f.evaluating_agent(
            session,
            index=23,
            issued_to=("validator-2",),
            scored_by=("validator-1",),
            composite=0.66,
            n_cases=124,
            created_at=f.hours_ago(2),
            median_ms=1700,
            rich_details=True,
        )

        # ── screening / intake activity ─────────────────────────────────────
        await f.uploaded_agent(session, index=30)
        screening, attempt = await f.screening_agent(session, index=31)
        await f.rejected_agent(session, index=32, created_at=f.hours_ago(13))
        await f.quarantined_agent(session, index=33, created_at=f.hours_ago(20))

        # ── validator fleet: 4 online, 1 paused, 1 stale ────────────────────
        await f.validator_heartbeat(
            session, name="validator-1", state="polling", seen_ago_seconds=24.0
        )
        # Synchronized running_benchmark: signed progress bound to the exact
        # live ticket deadline the fabric issued (minutes_from_now(50) for the
        # first ``issued_to`` name).
        progress: dict[str, Any] = {
            "stage": "running_benchmark",
            "completed": 68,
            "total": 124,
            "ticket_deadline": f.minutes_from_now(50).isoformat(),
        }
        await f.validator_heartbeat(
            session,
            name="validator-2",
            state="running_benchmark",
            seen_ago_seconds=12.0,
            active_agent_id=bench.agent_id,
            benchmark_progress=progress,
        )
        await f.validator_heartbeat(
            session, name="validator-3", state="updating_weights", seen_ago_seconds=41.0
        )
        await f.validator_heartbeat(
            session, name="validator-4", state="polling", seen_ago_seconds=53.0
        )
        await f.validator_heartbeat(
            session, name="validator-5", state="paused", seen_ago_seconds=19.0
        )
        await f.validator_heartbeat(
            session, name="validator-6", state="polling", seen_ago_seconds=8 * 60.0
        )

        # ── screeners: one actively screening, one idle ─────────────────────
        await f.screener_heartbeat(
            session,
            name="screener-1",
            state="screening",
            seen_ago_seconds=15.0,
            active_agent_id=screening.agent_id,
            screening_progress={
                "stage": "source_review_60",
                "started_at": int(attempt.started_at.timestamp()),
            },
        )
        await f.screener_heartbeat(
            session, name="screener-2", state="polling", seen_ago_seconds=48.0
        )
