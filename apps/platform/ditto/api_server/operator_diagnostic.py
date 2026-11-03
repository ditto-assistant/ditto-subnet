"""Operator diagnostic targets: operator-owned shadow agents leased one at a time.

An operator sometimes needs to run one exact artifact on one exact validator
without that artifact ever entering the public scoring queue -- for example a
fixture or probe the operator itself uploaded. The benchmark-canary lease
already provides the single-validator, non-authoritative run; it is limited to
finalized (scored or live) agents because every other agent is a miner's
in-flight submission.

This module defines the one narrow exception. An agent is a *diagnostic
target* only when every one of these holds, re-checked under the agent lock at
issue time and again whenever the validator polls for the lease:

* it was uploaded by a hotkey listed in ``OPERATOR_DIAGNOSTIC_HOTKEYS``;
* it is ``shadow`` (never ranked, never weight- or emission-eligible);
* it is ``evaluating`` and holds no score for any validator;
* an in-force queue withdrawal covers the requested benchmark version; and
* no ordinary scoring lease was ever issued for it -- the withdrawal landed
  before normal dispatch could reach it, so the only leases it has ever had
  are operator diagnostics.

Isolation (shadow + withdrawal) can only be applied before the agent first
becomes ``evaluating`` and while it has no leases at all, so a target that
qualifies here was never visible to normal dispatch.
"""

from __future__ import annotations

import os
import re
from collections.abc import Collection

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.ticket_status import TicketPurpose
from ditto.db.models import Agent, Score, ValidatorQueueWithdrawal, ValidatorTicket
from ditto.db.queries.queue_removal import removal_in_force

OPERATOR_DIAGNOSTIC_HOTKEYS_ENV = "OPERATOR_DIAGNOSTIC_HOTKEYS"
_SS58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{48}$")

# Statuses an agent may be isolated from: before screening passes, so the
# withdrawal is in force before normal dispatch could ever see it.
ISOLATABLE_STATUSES = frozenset(
    {AgentStatus.UPLOADED, AgentStatus.SCREENING, AgentStatus.SCREENING_PASSED}
)


def operator_diagnostic_hotkeys(raw: str | None = None) -> frozenset[str]:
    """Parse the comma-separated operator hotkey allowlist (empty = disabled)."""
    value = os.environ.get(OPERATOR_DIAGNOSTIC_HOTKEYS_ENV, "") if raw is None else raw
    return frozenset(
        item.strip() for item in value.split(",") if _SS58.match(item.strip())
    )


async def _ever_scored(session: AsyncSession, agent_id: object) -> bool:
    return bool(
        await session.scalar(select(exists().where(Score.agent_id == agent_id)))
    )


async def _ever_ordinarily_leased(session: AsyncSession, agent_id: object) -> bool:
    return bool(
        await session.scalar(
            select(
                exists().where(
                    ValidatorTicket.agent_id == agent_id,
                    ValidatorTicket.purpose != TicketPurpose.BENCHMARK_CANARY,
                )
            )
        )
    )


async def isolation_refusal(
    session: AsyncSession,
    agent: Agent | None,
    *,
    operator_hotkeys: Collection[str],
) -> str | None:
    """Why ``agent`` may not be isolated now, or ``None`` when it may."""
    if agent is None:
        return "agent not found"
    if not operator_hotkeys or agent.miner_hotkey not in operator_hotkeys:
        return "agent is not owned by a configured operator hotkey"
    if agent.status not in ISOLATABLE_STATUSES:
        return "agent has already left screening; isolation must precede dispatch"
    if await session.scalar(
        select(exists().where(ValidatorTicket.agent_id == agent.agent_id))
    ):
        return "agent already has a validator lease"
    if await _ever_scored(session, agent.agent_id):
        return "agent already has a score"
    return None


async def diagnostic_target_refusal(
    session: AsyncSession,
    agent: Agent | None,
    *,
    bench_version: int,
    operator_hotkeys: Collection[str],
) -> str | None:
    """Why ``agent`` is not a diagnostic target, or ``None`` when it is."""
    if agent is None:
        return "agent not found"
    if not operator_hotkeys or agent.miner_hotkey not in operator_hotkeys:
        return "agent is not owned by a configured operator hotkey"
    if not agent.shadow:
        return "agent is not shadow"
    if agent.status != AgentStatus.EVALUATING:
        return "agent is not evaluating"
    if await _ever_scored(session, agent.agent_id):
        return "agent has a score"
    withheld = await session.scalar(
        select(
            exists().where(
                ValidatorQueueWithdrawal.agent_id == agent.agent_id,
                ValidatorQueueWithdrawal.bench_version == bench_version,
                removal_in_force(),
            )
        )
    )
    if not withheld:
        return "agent is not withheld from this benchmark queue"
    if await _ever_ordinarily_leased(session, agent.agent_id):
        return "agent was leased by ordinary dispatch"
    return None
