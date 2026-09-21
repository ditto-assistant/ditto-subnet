"""Authenticated read view of the automatic infrastructure-retry state (#475).

Exposes the effective policy, a count per decision state, the parked agents
with their backoff deadlines, and each signature's circuit breaker. It is a
read model over ``plan_infra_retries`` (one fleet scan per request), not a gate:
the claim recomputes everything under its own lock.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Annotated, get_args
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.admin_screening_infra_retry import (
    InfraRetryAgentView,
    InfraRetryBreakerPhase,
    InfraRetryBreakerView,
    InfraRetryClaimOutlook,
    InfraRetryPolicy,
    InfraRetryState,
    InfraRetrySummary,
    ScreeningInfraRetryView,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.models import (
    Agent,
    AgentStatus,
    ScreeningAttempt,
    ScreeningRetryOverride,
)
from ditto.db.queries import screening_infra_retry as infra
from ditto.db.queries.screening import prerequisite_screening_predicates
from ditto.db.queries.screening_retry import latest_screening_attempt_id

router = APIRouter(prefix="/admin", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]

# Row bounds for one response. The summary always counts everything.
INFRA_RETRY_VIEW_MAX_AGENTS = 200
INFRA_RETRY_VIEW_MAX_BREAKERS = 50

_BASIS = (
    "Derived from screening attempt history at read time; nothing is stored. "
    "Agents whose latest infrastructure failure is older than "
    "auto_retry_max_age_seconds are counted in aged_out_agents but not listed "
    "individually, and an agent that reached auto_retry_max_streak is "
    "'capped': both wait for an operator retry. The breaker is per signature "
    "(reason code, provider, lane); half_open means its open window elapsed and "
    "probes are allowed. A breaker with a known provider holds and probes only "
    "workers on that provider: a worker on another provider can still claim "
    "those agents by backoff alone (that run is not a probe). A signature with "
    "no provider holds every worker. This view is computed with no particular "
    "claimant, so breaker_held and waiting_breaker mean held for workers on "
    "the signature's provider. agents are ordered by earliest next_retry_at "
    "first. claim_outlook 'ready' means admitted with backoff and breaker "
    "elapsed; the claim may still skip it (one probe per signature per pass, "
    "ownership rules)."
)


def _seconds(value: object) -> int:
    return int(value.total_seconds())  # type: ignore[attr-defined]


def infra_retry_policy() -> InfraRetryPolicy:
    return InfraRetryPolicy(
        auto_retry_reason_codes=list(infra.INFRA_AUTO_RETRY_REASON_CODES),
        base_backoff_seconds=_seconds(infra.INFRA_RETRY_BASE_BACKOFF),
        max_backoff_seconds=_seconds(infra.INFRA_RETRY_MAX_BACKOFF),
        jitter_fraction=infra.INFRA_RETRY_JITTER_FRACTION,
        auto_retry_max_age_seconds=_seconds(infra.INFRA_AUTO_RETRY_MAX_AGE),
        auto_retry_max_streak=infra.INFRA_AUTO_RETRY_MAX_STREAK,
        plan_max_claimable=infra.INFRA_PLAN_MAX_CLAIMABLE,
        breaker_distinct_agents=infra.BREAKER_DISTINCT_AGENTS,
        breaker_window_seconds=_seconds(infra.BREAKER_WINDOW),
        breaker_open_seconds=_seconds(infra.BREAKER_OPEN_DURATION),
        breaker_probe_interval_seconds=_seconds(infra.BREAKER_PROBE_INTERVAL),
        breaker_history_lookback_seconds=_seconds(infra.BREAKER_HISTORY_LOOKBACK),
    )


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _breaker_phase(
    breaker: infra.BreakerState | None, now: datetime
) -> InfraRetryBreakerPhase | None:
    if breaker is None:
        return None
    if not breaker.open:
        return "closed"
    assert breaker.open_until is not None
    return "open" if now < breaker.open_until else "half_open"


async def _aged_out_agents(session: AsyncSession, now: datetime) -> int:
    """Count parked agents the planner ignores because the failure is too old.

    The planner's candidate predicate with the age bound inverted (its own is
    ``finished_at > now - MAX_AGE``); one query on the partial infra index.
    """
    return int(
        await session.scalar(
            select(func.count())
            .select_from(ScreeningAttempt)
            .join(Agent, Agent.agent_id == ScreeningAttempt.agent_id)
            .where(
                Agent.status == AgentStatus.SCREENING_FAILED,
                ScreeningAttempt.attempt_id == latest_screening_attempt_id(),
                *infra._infra_failure_filters(),
                ScreeningAttempt.finished_at <= now - infra.INFRA_AUTO_RETRY_MAX_AGE,
                ~exists(
                    select(ScreeningRetryOverride.override_id).where(
                        ScreeningRetryOverride.attempt_id == ScreeningAttempt.attempt_id
                    )
                ),
            )
        )
        or 0
    )


def _outlook(state: InfraRetryState, admitted: bool) -> InfraRetryClaimOutlook:
    if state == "capped":
        return "needs_operator"
    if not admitted:
        return "not_admitted"
    if state == "backoff":
        return "waiting_backoff"
    if state == "breaker_held":
        return "waiting_breaker"
    return "ready"


@router.get("/screening-infra-retries", response_model=ScreeningInfraRetryView)
async def screening_infra_retries(
    _admin: AdminDep, session: SessionDep
) -> ScreeningInfraRetryView:
    """Report infrastructure-retry policy, parked agents, and breakers."""
    now = _utcnow()
    plan = await infra.plan_infra_retries(
        session, now=now, fleet_breakers=True, claimable_limit=None
    )
    decisions = plan.decisions
    _, admitted_predicate = await prerequisite_screening_predicates(session)
    admitted_ids: set[UUID] = set()
    if decisions:
        admitted_ids = set(
            await session.scalars(
                select(Agent.agent_id).where(
                    Agent.agent_id.in_(list(decisions)), admitted_predicate
                )
            )
        )

    ordered = sorted(
        decisions.values(),
        key=lambda d: (d.next_retry_at, d.failed_at, str(d.agent_id)),
    )
    by_state = Counter(d.state for d in ordered)
    agents = [
        InfraRetryAgentView(
            agent_id=d.agent_id,
            attempt_id=d.attempt_id,
            reason_code=d.signature.reason_code,
            provider=d.signature.provider,
            lane=d.signature.lane,
            consecutive_failures=d.streak,
            failed_at=d.failed_at,
            backoff_until=d.backoff_until,
            next_retry_at=d.next_retry_at,
            state=d.state,
            breaker_phase=_breaker_phase(d.breaker, now),
            admitted=d.agent_id in admitted_ids,
            claim_outlook=_outlook(d.state, d.agent_id in admitted_ids),
        )
        for d in ordered[:INFRA_RETRY_VIEW_MAX_AGENTS]
    ]

    parked = Counter(d.signature for d in ordered)
    breaker_states = sorted(
        plan.breakers.values(),
        key=lambda b: (
            not b.open,
            -(b.opened_at.timestamp() if b.opened_at else 0.0),
            b.signature.reason_code,
            b.signature.provider or "",
            b.signature.lane or "",
        ),
    )
    breakers = [
        InfraRetryBreakerView(
            reason_code=b.signature.reason_code,
            provider=b.signature.provider,
            lane=b.signature.lane,
            phase=_breaker_phase(b, now) or "closed",
            opened_at=b.opened_at,
            open_until=b.open_until,
            last_probe_at=b.last_probe_at,
            next_probe_at=b.next_probe_at,
            parked_agents=parked.get(b.signature, 0),
        )
        for b in breaker_states[:INFRA_RETRY_VIEW_MAX_BREAKERS]
    ]
    return ScreeningInfraRetryView(
        generated_at=now,
        basis=_BASIS,
        policy=infra_retry_policy(),
        summary=InfraRetrySummary(
            parked_agents=len(ordered),
            by_state={
                state: by_state.get(state, 0) for state in get_args(InfraRetryState)
            },
            not_admitted=sum(1 for d in ordered if d.agent_id not in admitted_ids),
            aged_out_agents=await _aged_out_agents(session, now),
            open_breakers=sum(
                1 for b in breaker_states if _breaker_phase(b, now) == "open"
            ),
            half_open_breakers=sum(
                1 for b in breaker_states if _breaker_phase(b, now) == "half_open"
            ),
            breakers_total=len(breaker_states),
        ),
        agents=agents,
        agents_limit=INFRA_RETRY_VIEW_MAX_AGENTS,
        agents_truncated=len(ordered) > INFRA_RETRY_VIEW_MAX_AGENTS,
        breakers=breakers,
        breakers_limit=INFRA_RETRY_VIEW_MAX_BREAKERS,
        breakers_truncated=len(breaker_states) > INFRA_RETRY_VIEW_MAX_BREAKERS,
    )
