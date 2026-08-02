"""Worst ops day: the whole fleet is stale, broken, or lying.

Every validator is stale or offline (one erroring with warning-level host
metrics, one claiming work it holds no ticket for, one on software below the
minimum gate), every screener is offline, screening attempts and validator
tickets have expired unswept — but the leaderboard still carries a finalized
field so the page is not empty.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from ditto.api_models.ticket_status import TicketPurpose, TicketStatus
from ditto.db.models import ScreeningAttempt, ValidatorTicket
from ditto.simulator.fabric import DEFAULT_BENCH_VERSION

if TYPE_CHECKING:
    from ditto.simulator.scenarios import ScenarioContext

NAME = "degraded"
DESCRIPTION = (
    "Fleet meltdown: every validator stale/offline (error states, a heartbeat "
    "mismatch, outdated software), screeners down, expired attempts and tickets."
)

_FINALIZED_COMPOSITES = (0.80, 0.75, 0.71, 0.68, 0.64, 0.60)


async def apply(ctx: ScenarioContext) -> None:
    f = ctx.fabric
    async with ctx.session_maker() as session, session.begin():
        # ── the leaderboard still has a finalized field ──────────────────────
        for i, composite in enumerate(_FINALIZED_COMPOSITES, start=1):
            await f.finalized_agent(session, index=i, composite=composite)

        # ── validators: nobody is truly online ───────────────────────────────
        # Stale (silent for 8 minutes, past the 5-minute online window).
        await f.validator_heartbeat(
            session, name="validator-1", seen_ago_seconds=8 * 60
        )
        # Offline for an hour.
        await f.validator_heartbeat(
            session, name="validator-2", state="idle", seen_ago_seconds=60 * 60
        )
        # Erroring with warning-level host metrics. The public SystemMetrics
        # allowlist requires multiples of 5, so 93%/96% render as 95%/95% —
        # still past the >=90 memory and >=95 disk warning thresholds.
        await f.validator_heartbeat(
            session,
            name="validator-3",
            state="error",
            seen_ago_seconds=10 * 60,
            system_metrics=f.system_metrics(
                "validator:validator-3",
                cpu_percent=85,
                memory_percent=95,
                disk_percent=95,
                docker_status="degraded",
                running_containers=6,
                unhealthy_containers=2,
            ),
        )

        # A heartbeat claiming active work with NO matching platform ticket:
        # the only ticket validator-4 ever held for this agent has expired, so
        # the reconciled assignment_state is assignment_mismatch.
        phantom = await f.evaluating_agent(session, index=7)
        session.add(
            ValidatorTicket(
                agent_id=phantom.agent_id,
                validator_hotkey=f.ss58_hotkey("validator:validator-4"),
                status=TicketStatus.EXPIRED,
                purpose=TicketPurpose.CANONICAL_QUORUM,
                purpose_revision=1,
                issued_at=f.hours_ago(5),
                deadline=f.hours_ago(3),
                bench_version=DEFAULT_BENCH_VERSION,
            )
        )
        await session.flush()
        await f.validator_heartbeat(
            session,
            name="validator-4",
            state="running_benchmark",
            active_agent_id=phantom.agent_id,
            seen_ago_seconds=7 * 60,
        )

        # Software below the minimum gate (min 0.7.0 / protocol 4), long gone.
        await f.validator_heartbeat(
            session,
            name="validator-5",
            software_version="0.5.0",
            protocol_version=3,
            seen_ago_seconds=20 * 60,
        )

        # ── screeners: all offline ───────────────────────────────────────────
        await f.screener_heartbeat(
            session, name="screener-1", seen_ago_seconds=2 * 60 * 60
        )
        await f.screener_heartbeat(
            session, name="screener-2", state="error", seen_ago_seconds=45 * 60
        )

        # ── expired screening attempts (leases that were never finished) ─────
        for index in (8, 9):
            agent = await f.uploaded_agent(
                session, index=index, created_at=f.hours_ago(7)
            )
            started = f.hours_ago(6)
            session.add(
                ScreeningAttempt(
                    attempt_id=f.uuid(f"attempt:{agent.agent_id}"),
                    agent_id=agent.agent_id,
                    screener_hotkey=f.ss58_hotkey("screener:screener-1"),
                    policy_version=agent.screening_policy_version,
                    status="expired",
                    started_at=started,
                    deadline=started + timedelta(hours=1),
                )
            )
        await session.flush()
        # A screening lease that blew its deadline but was never swept: the
        # agent still shows "screening" while its screener is offline.
        _, stuck_attempt = await f.screening_agent(session, index=10)
        stuck_attempt.started_at = f.hours_ago(3)
        stuck_attempt.deadline = f.hours_ago(2)

        # ── expired validator tickets on a starved evaluating agent ──────────
        starved = await f.evaluating_agent(session, index=11)
        issued_at = f.hours_ago(5)
        session.add_all(
            [
                ValidatorTicket(
                    agent_id=starved.agent_id,
                    validator_hotkey=f.ss58_hotkey(f"validator:{name}"),
                    status=TicketStatus.EXPIRED,
                    purpose=TicketPurpose.CANONICAL_QUORUM,
                    purpose_revision=1,
                    issued_at=issued_at,
                    deadline=issued_at + timedelta(hours=2),
                    bench_version=DEFAULT_BENCH_VERSION,
                )
                for name in ("validator-1", "validator-2")
            ]
        )
        await session.flush()
