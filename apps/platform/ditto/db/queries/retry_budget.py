"""What a failure that was not the agent's fault costs the miner: nothing.

A ticket carries a bounded per-version attempt budget, and every reissue spends
one. Anything that ends a lease for reasons outside the agent's control has to
hand that attempt back, or a miner pays for faults that were never theirs. Two
things qualify: a validator-side infrastructure failure, and the platform
revoking a live lease itself.

The compensation raises the *cap* rather than rewriting ``attempt_count``. The
ledger keeps saying how many leases this agent actually consumed; the grant says
how many of those the miner should not be billed for. Those are different facts
and collapsing them loses the audit trail.

This lives in its own module because the two places that grant it sit on opposite
sides of an import edge: :mod:`ditto.db.queries.tickets` owns issuance and the
budget arithmetic and already imports :mod:`ditto.db.queries.lease_liveness`, the
revocation gate, so the gate cannot import back. ditto-platform#460 established
the rule -- "do not bill a miner for a lease the platform itself revoked" -- but
implemented it in exactly one call site, the signed ``fail_job`` handler, and the
platform's own revocation path went on billing. One definition imported by both
is what stops that from recurring.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import func, select

from ditto.db.models import ValidatorTicket

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# An infrastructure failure, or a lease the platform revoked, is never the
# agent's fault, so it earns a compensating grant that offsets the attempt the
# reissue consumes. Bounded so a persistent validator-side outage cannot re-lease
# one agent forever.
#
# This bound is per *ticket*, and a ticket row is one (agent, bench version,
# validator) lease slot -- so on its own it bounds one validator's exposure, not
# the fleet's. See :data:`MAX_AGENT_INFRA_RETRY_GRANTS`.
MAX_INFRA_RETRY_GRANTS = 8

# The same allowance, summed across every validator holding a slot on one agent
# at one benchmark version.
#
# ditto-subnet#279 is the argument for this existing at all. `MAX_INFRA_RETRY_GRANTS`
# alone bounds a *validator*, and every validator gets its own eight, so an
# artifact whose run reliably reports a scorer-side infrastructure code collects
# eight free attempts per validator that ever touches it. `mnemox-v55` reached
# `attempts_used: 9` against a base budget of 2 with zero scores, and the family
# held three validator slots per round for a day without ever reaching a verdict.
# The class matters more than the instance: any slow-but-honest artifact starves
# the fleet the same way, because the grant *raises the attempt cap*, so the
# ticket never becomes `exhausted` and no operator is ever told.
#
# 12 remains deliberately separate from the ordinary attempt budget. An
# artifact gets one base lease per quorum validator; signed infrastructure
# failures may earn bounded no-fault grants because billing those failures to
# the miner would conflate validator health with agent behavior. The fleet-wide
# cap still sits above the per-ticket 8 so one validator's local outage is
# absorbed whole in the common case. What it removes is only the unboundedness:
# the fleet's total exposure to one broken artifact is finite instead of
# growing by 8 for every validator that ever touches it.
#
# Past the bound nothing is punished and nothing is lost -- the attempt is simply
# billed as it always was, the ticket walks its ordinary budget down to
# `retry_state = "exhausted"`, and that state is exactly what surfaces the
# artifact on the operator stuck-list for a manual grant. The bound converts an
# invisible infinite loop into a visible finite one.
#
# Constants are spelled as literals rather than imported: `ditto.db.queries.tickets`
# imports this module, so importing `MAX_ATTEMPTS_PER_VERSION` back would close a
# cycle.
MAX_AGENT_INFRA_RETRY_GRANTS = 12

# Infrastructure retries reissue quickly (no 6h agent-failure cooldown) so a
# transient blip recovers fast, but back-to-back re-leases during a *sustained*
# provider/relay outage would hammer the failing provider (an inference burst).
# The cooldown before the next infra retry therefore doubles per grant already
# earned, capped, so the agent is still retried to success while the failing
# provider gets breathing room.
INFRA_RETRY_BACKOFF_BASE = timedelta(minutes=2)
INFRA_RETRY_BACKOFF_CAP = timedelta(minutes=30)


def infra_retry_backoff(infra_retry_grants: int) -> timedelta:
    """Cooldown before an infrastructure-failed lease may be re-leased.

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
    """Total no-fault grants every validator has already minted for one agent.

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
    """Offset the attempt the coming reissue will charge. Returns whether it did.

    Returns ``False`` once :data:`MAX_INFRA_RETRY_GRANTS` is reached, so a
    persistently sick lease cannot mint attempts forever -- the bound is what
    makes an automatic grant safe to hand out without an operator in the loop.

    ``agent_infra_grants`` is the fleet-wide total for this agent and benchmark
    version (from :func:`agent_infra_retry_grants`), counted *before* this
    failure. Passing it additionally applies :data:`MAX_AGENT_INFRA_RETRY_GRANTS`,
    which is the bound the per-ticket one cannot express: eight per validator
    across an unbounded validator pool is unbounded per agent.

    **Pass it for a validator-reported ``infrastructure`` verdict, and only
    that.** There, repetition on one agent is evidence about the artifact --
    every validator in the fleet ran it and every run reported a scorer-side
    infrastructure code -- and the artifact is what should stop being free.
    Leave it ``None`` for a lease the *platform* revoked: repetition there is
    evidence about the platform, and billing the miner for it is exactly the
    rule ditto-platform#460/#497 settled in the other direction. The per-ticket
    bound still applies to that path unchanged.
    """
    if ticket.infra_retry_grants >= MAX_INFRA_RETRY_GRANTS:
        return False
    if (
        agent_infra_grants is not None
        and agent_infra_grants >= MAX_AGENT_INFRA_RETRY_GRANTS
    ):
        return False
    ticket.infra_retry_grants += 1
    return True
