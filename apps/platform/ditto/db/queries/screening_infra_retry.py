"""Automatic, bounded retry and a fleet circuit breaker for infrastructure builds.

A screener that reports ``docker-build-infrastructure`` (Docker daemon, BuildKit,
or the build host failed; the miner's archive was never judged) parks the agent
as ``screening_failed``. Unlike the provider codes in
``PROVIDER_BACKOFF_REASON_CODES`` it is retried without an operator, so this
module owns three things and nothing else:

* the per-artifact backoff: ``INFRA_RETRY_BASE_BACKOFF`` doubling with each
  consecutive infrastructure failure, capped at ``INFRA_RETRY_MAX_BACKOFF``, with
  deterministic +/- ``INFRA_RETRY_JITTER_FRACTION`` jitter from the attempt id;
* the fleet breaker: ``BREAKER_DISTINCT_AGENTS`` distinct agents failing with the
  same signature inside ``BREAKER_WINDOW`` hold every retry of that signature for
  ``BREAKER_OPEN_DURATION``, then admit a single probe per
  ``BREAKER_PROBE_INTERVAL`` until one probe gets past the failure;
* the read model both use, so the claim path and a future operator surface read
  one derivation.

Automatic retries are bounded by ``INFRA_AUTO_RETRY_MAX_AGE`` and
``INFRA_AUTO_RETRY_MAX_STREAK``. A capped or aged-out agent stays
``screening_failed`` and still needs a guarded operator review (the existing
Backroom manual retry or clear). It NEVER becomes a miner rejection or a
quarantine: nothing here writes a verdict.

Deliberate behaviour beyond the base spec: while a known-provider breaker is
open, a worker on ANOTHER provider claims those agents by backoff alone (its run
is not on the faulty lane and is never a probe of it), and a failure there trips
that provider's own signature. Workers on the failing provider stay held.

Nothing here is stored. The streak, deadline, and breaker state are recomputed
from ``screening_attempts`` on every call, so they are identical after a restart
and on every worker. The retry count is deliberately NOT the inconclusive count
behind ``MAX_SCREENING_EXPIRIES``: an infrastructure failure says nothing about
the artifact, so it can never park, reject, or quarantine a submission.

Callers that mutate state must hold the screening claim lock (see
``claim_screening_attempts``); it is what makes the one-probe-per-interval rule
atomic across workers.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal, NamedTuple
from uuid import UUID

from sqlalchemy import (
    ColumnElement,
    Select,
    and_,
    exists,
    func,
    literal_column,
    or_,
    select,
)

from ditto.db.models import (
    Agent,
    AgentStatus,
    ScreenedImageUpload,
    ScreenerNode,
    ScreeningAttempt,
    ScreeningQuarantine,
    ScreeningRetryOverride,
    SubmissionImageBuild,
)
from ditto.db.queries.screening_retry import latest_screening_attempt_id

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# Retried automatically. Deliberately separate from PROVIDER_BACKOFF_REASON_CODES,
# whose members are held on reclaim AND counted toward the inconclusive park cap.
# Adding a code here means updating the partial index
# ``screening_attempts_infra_failed_idx`` (models.py, its migration, and
# ``_infra_failure_filters``) in the same change, or the breaker scan silently
# goes back to a sequential scan under the claim lock.
INFRA_AUTO_RETRY_REASON_CODES: tuple[str, ...] = ("docker-build-infrastructure",)

INFRA_RETRY_BASE_BACKOFF = timedelta(minutes=10)
INFRA_RETRY_MAX_BACKOFF = timedelta(minutes=60)
INFRA_RETRY_JITTER_FRACTION = 0.2

# PROPOSED bounds, pending maintainer confirmation. Automatic retries spend
# screener capacity, which #1201 made an explicit operator decision, so they stop
# on their own. Neither is a terminal outcome: the agent stays SCREENING_FAILED,
# nothing is rejected or quarantined, and a Backroom retry or clear starts over.
# * MAX_AGE: only a latest infrastructure failure this recent is retried, so a
#   deploy does not resurrect every long-parked agent at once.
# * MAX_STREAK: consecutive failures after which the operator takes over (about
#   seven hours of retries at the 60 minute cap).
INFRA_AUTO_RETRY_MAX_AGE = timedelta(hours=24)
INFRA_AUTO_RETRY_MAX_STREAK = 8
# Safety bound on the SQL id list a claim receives. It is deliberately unrelated
# to the claim limit: it is applied before the claim's own admission, duplicate
# and ownership filters, so a small bound could be filled by blocked agents and
# starve eligible ones. The SQL ``LIMIT`` does the real bounding; the 24 hour age
# window already keeps this set small.
INFRA_PLAN_MAX_CLAIMABLE = 500

BREAKER_DISTINCT_AGENTS = 3
BREAKER_WINDOW = timedelta(minutes=5)
BREAKER_OPEN_DURATION = timedelta(minutes=10)
BREAKER_PROBE_INTERVAL = timedelta(minutes=5)
# How far back attempt history is read to rebuild breaker state. Probes keep
# failing signatures in view every BREAKER_PROBE_INTERVAL; a breaker older than
# this without a single new event simply re-trips from fresh failures.
BREAKER_HISTORY_LOOKBACK = timedelta(hours=48)

# Recovery needs proof that the worker's local build ran and worked: a ``passed``
# verdict carrying a verified screened-image upload (a policy-only rescreen, a
# deferred source-only review and a pre-build tripwire quarantine finish without
# one), or a deterministic ``docker-build`` rejection (the build ran and judged
# the archive). A quarantine alone is not proof: the static preflight can quarantine
# before any build starts.
_RECOVERY_REJECT_REASON = "docker-build"

# Beyond this the doubling is already clamped; it only keeps the shift small.
_MAX_DOUBLINGS = 16

DecisionState = Literal["backoff", "breaker_held", "probe_due", "due", "capped"]


class InfraSignature(NamedTuple):
    """What must match for two failures to count as the same fault.

    ``provider`` and ``lane`` are ``None`` when the attempt carried no failure
    metadata. Those rows key on the reason code alone and are never merged into
    a known provider/lane, nor two known lanes into each other.
    """

    reason_code: str
    provider: str | None
    lane: str | None


@dataclass(frozen=True)
class BreakerState:
    signature: InfraSignature
    open: bool
    opened_at: datetime | None
    open_until: datetime | None
    last_probe_at: datetime | None
    # When the next probe may start; None while the breaker is closed.
    next_probe_at: datetime | None


@dataclass(frozen=True)
class InfraRetryDecision:
    agent_id: UUID
    attempt_id: UUID
    signature: InfraSignature
    # Consecutive infrastructure failures since the latest operator clear/retry.
    streak: int
    failed_at: datetime
    # End of the per-artifact backoff alone.
    backoff_until: datetime
    # Earliest the agent may be claimed again (backoff, then breaker).
    next_retry_at: datetime
    state: DecisionState
    breaker: BreakerState | None


@dataclass(frozen=True)
class InfraRetryPlan:
    decisions: dict[UUID, InfraRetryDecision]
    breakers: dict[InfraSignature, BreakerState]
    # Bounds ``claimable_agent_ids``; None lists every claimable agent.
    claimable_limit: int | None = None

    @property
    def claimable_agent_ids(self) -> list[UUID]:
        """Agents that may be claimed now, longest-waiting first, bounded.

        Probes are still limited to one per signature by the claim itself.
        """
        due = sorted(
            (
                decision
                for decision in self.decisions.values()
                if decision.state in ("due", "probe_due")
            ),
            key=lambda decision: (decision.failed_at, str(decision.agent_id)),
        )
        if self.claimable_limit is not None:
            due = due[: self.claimable_limit]
        return [decision.agent_id for decision in due]

    @property
    def surplus_probe_candidates(self) -> int:
        """Probe-due agents beyond the one per signature a claim may take."""
        claimable = set(self.claimable_agent_ids)
        per_signature: dict[InfraSignature, int] = defaultdict(int)
        for decision in self.decisions.values():
            if decision.state == "probe_due" and decision.agent_id in claimable:
                per_signature[decision.signature] += 1
        return sum(count - 1 for count in per_signature.values())


def infra_retry_delay(streak: int, attempt_id: UUID) -> timedelta:
    """Hold after the ``streak``-th consecutive infrastructure failure.

    ``BASE * 2**(streak - 1)`` clamped to ``MAX``, scaled by a factor in
    ``[1 - J, 1 + J]`` taken from a SHA-256 of the failing attempt id (Python's
    ``hash`` is salted per process and would break restart stability), then
    clamped to ``MAX`` again so no hold ever exceeds the cap.
    """
    doublings = min(max(streak, 1) - 1, _MAX_DOUBLINGS)
    nominal = min(INFRA_RETRY_MAX_BACKOFF, INFRA_RETRY_BASE_BACKOFF * (2**doublings))
    digest = hashlib.sha256(attempt_id.bytes).digest()
    unit = int.from_bytes(digest[:8], "big") / 2**64
    factor = 1 - INFRA_RETRY_JITTER_FRACTION + 2 * INFRA_RETRY_JITTER_FRACTION * unit
    return min(INFRA_RETRY_MAX_BACKOFF, nominal * factor)


def _infra_failure_filters() -> list[ColumnElement[bool]]:
    """``status = 'failed' AND reason_code IN (...)`` with inlined constants.

    Written as literals, not bind parameters, so Postgres can prove it implies the
    predicate of the partial ``screening_attempts_infra_failed_idx``; a
    parameterized predicate cannot use a partial index in a generic plan.
    """
    codes: list[ColumnElement[str]] = [
        literal_column("'" + code + "'") for code in INFRA_AUTO_RETRY_REASON_CODES
    ]
    assert all("'" not in code for code in INFRA_AUTO_RETRY_REASON_CODES)
    return [
        ScreeningAttempt.status == literal_column("'failed'"),
        ScreeningAttempt.reason_code.in_(codes),
    ]


def failing_agents_query(cutoff: datetime) -> Select[tuple[UUID]]:
    """Agents with an infrastructure failure finished since ``cutoff``."""
    return select(ScreeningAttempt.agent_id).where(
        *_infra_failure_filters(), ScreeningAttempt.finished_at >= cutoff
    )


async def screener_provider(session: AsyncSession, hotkey: str) -> str:
    """Provider of the worker holding ``hotkey`` (``gcp`` for the legacy fleet)."""
    provider = await session.scalar(
        select(ScreenerNode.provider).where(ScreenerNode.screener_hotkey == hotkey)
    )
    return provider or "gcp"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@dataclass(frozen=True)
class _Row:
    attempt_id: UUID
    agent_id: UUID
    status: str
    reason_code: str | None
    started_at: datetime
    finished_at: datetime | None
    provider: str | None
    lane: str | None
    # Provider of the screener node whose hotkey ran the attempt, if enrolled.
    node_provider: str | None = None
    # A Platform-attested image build (not the worker's local build) ran for it.
    has_image_build: bool = False

    # A verified screened-image upload is bound to this attempt.
    has_verified_image: bool = False

    @property
    def placement_provider(self) -> str:
        """Provider of the worker that claimed the attempt.

        Known at claim time and fixed for the attempt's life (the screener
        hotkey never changes), unlike the failure metadata, which appears only
        when the attempt ends. ``gcp`` for the legacy shared fleet, which has no
        node row: the rule ``require_screener`` applies.
        """
        return self.node_provider or "gcp"

    def is_probe_of(self, signature: InfraSignature) -> bool:
        """Whether this retry was placed on the signature's provider.

        A probe is defined by placement only, so it classifies identically while
        running and after it ends, however it later fails, and whichever build
        lane it ends up using. Metadata-free signatures (reason code alone) and
        lane-only ones accept any retry, as before.
        """
        return (
            signature.provider is None or self.placement_provider == signature.provider
        )

    def exercises(self, signature: InfraSignature) -> bool:
        """Strict: the attempt demonstrably ran the signature's provider AND lane.

        Used for recovery only. The lane is the recorded failure lane, else
        ``buildkit`` (the worker's local build). A Platform image build makes the
        lane unknown, and an unknown lane never matches a known one.
        """
        if signature.provider is None and signature.lane is None:
            return True
        provider = self.provider or self.placement_provider
        lane = None if self.has_image_build else (self.lane or "buildkit")
        return (signature.provider is None or provider == signature.provider) and (
            signature.lane is None or lane == signature.lane
        )

    @property
    def is_infra_failure(self) -> bool:
        return self.status == "failed" and (
            self.reason_code in INFRA_AUTO_RETRY_REASON_CODES
        )

    @property
    def failed_at(self) -> datetime:
        return _aware(self.finished_at or self.started_at)

    @property
    def signature(self) -> InfraSignature:
        assert self.reason_code is not None
        return InfraSignature(self.reason_code, self.provider, self.lane)

    @property
    def is_recovery(self) -> bool:
        return (self.status == "passed" and self.has_verified_image) or (
            self.status == "rejected" and self.reason_code == _RECOVERY_REJECT_REASON
        )


def _derive_breakers(
    histories: Iterable[list[_Row]], *, cutoff: datetime
) -> dict[InfraSignature, BreakerState]:
    """Replay attempt history into per-signature breaker state.

    Events per signature, in time order: a *failure* (finish of a failed
    infrastructure attempt), a *probe* (start of the next attempt of an agent
    in the run of failures just before it, when it was placed on that
    signature's provider: see ``_Row.is_probe_of``), and a *recovery* (that probe
    demonstrably running the signature's provider and lane, and finishing with
    proof the local build worked: see ``_Row.is_recovery``). A retry on another
    provider is neither: a failure there is a failure of its own signature, and a
    success there proves nothing about this one. The breaker trips when
    ``BREAKER_DISTINCT_AGENTS`` distinct agents fail inside ``BREAKER_WINDOW``,
    stays tripped through failed probes, and closes on recovery.
    """
    # (time, order, kind, agent_id); order breaks ties failure < probe < recovery.
    events: dict[InfraSignature, list[tuple[datetime, int, str, UUID]]] = defaultdict(
        list
    )
    for rows in histories:
        for index, row in enumerate(rows):
            # The contiguous run of infrastructure failures right before this
            # attempt: every distinct signature in it is retried by this row, not
            # only the last (fail on GCE, retry on Hetzner and fail, retry on GCE
            # and pass must still close the GCE signature).
            run: dict[InfraSignature, None] = {}
            for earlier in reversed(rows[:index]):
                if not earlier.is_infra_failure:
                    break
                run.setdefault(earlier.signature)
            if _aware(row.started_at) >= cutoff:
                for signature in run:
                    if not row.is_probe_of(signature):
                        continue
                    events[signature].append(
                        (_aware(row.started_at), 1, "probe", row.agent_id)
                    )
                    if row.is_recovery and row.exercises(signature):
                        events[signature].append(
                            (row.failed_at, 2, "recovery", row.agent_id)
                        )
            if row.is_infra_failure and row.failed_at >= cutoff:
                events[row.signature].append(
                    (row.failed_at, 0, "failure", row.agent_id)
                )

    breakers: dict[InfraSignature, BreakerState] = {}
    for signature, stream in events.items():
        stream.sort(key=lambda event: (event[0], event[1], str(event[3])))
        recent: list[tuple[datetime, UUID]] = []
        opened_at: datetime | None = None
        last_probe_at: datetime | None = None
        for at, _, kind, agent_id in stream:
            if kind == "recovery":
                recent = []
                opened_at = None
                last_probe_at = None
            elif kind == "probe":
                if opened_at is not None:
                    last_probe_at = at
            elif opened_at is None:
                recent = [(t, a) for t, a in recent if at - t <= BREAKER_WINDOW]
                recent.append((at, agent_id))
                if len({a for _, a in recent}) >= BREAKER_DISTINCT_AGENTS:
                    opened_at = at
                    last_probe_at = None
        open_until = opened_at + BREAKER_OPEN_DURATION if opened_at else None
        next_probe_at = open_until
        if next_probe_at is not None and last_probe_at is not None:
            next_probe_at = max(next_probe_at, last_probe_at + BREAKER_PROBE_INTERVAL)
        breakers[signature] = BreakerState(
            signature=signature,
            open=opened_at is not None,
            opened_at=opened_at,
            open_until=open_until,
            last_probe_at=last_probe_at,
            next_probe_at=next_probe_at,
        )
    return breakers


def _streak(rows: list[_Row], *, since: datetime | None) -> int:
    """Consecutive trailing infrastructure failures after the latest clear."""
    count = 0
    for row in reversed(rows):
        if since is not None and _aware(row.started_at) <= since:
            break
        if not row.is_infra_failure:
            break
        count += 1
    return count


async def plan_infra_retries(
    session: AsyncSession,
    *,
    now: datetime,
    agent_ids: Collection[UUID] | None = None,
    fleet_breakers: bool = False,
    fleet_scan: bool = True,
    claimant_provider: str | None = None,
    claimable_limit: int | None = INFRA_PLAN_MAX_CLAIMABLE,
) -> InfraRetryPlan:
    """Decide, for every agent parked on an infrastructure failure, when it retries.

    A candidate is a ``screening_failed`` agent whose *latest* attempt failed with
    one of ``INFRA_AUTO_RETRY_REASON_CODES`` and carries no operator retry
    override (an override is the manual path and is never held here). Pass
    ``agent_ids`` to restrict the decisions; breaker state is always derived from
    the whole fleet's history. With no candidate there is nothing to hold, so the
    fleet scan is skipped (the hot claim path) unless ``fleet_breakers`` asks for
    every breaker, e.g. for an operator view. ``fleet_scan=False`` skips the
    fleet-wide history query altogether (per-agent backoff and streak only, no
    breaker), for callers such as the unauthenticated public endpoint.

    ``claimant_provider`` is the provider of the worker asking to claim. A breaker
    for a known-provider signature only holds a claimant on that provider; a
    retry claimed elsewhere does not run on the faulty lane (and is not a probe
    of it, see ``_derive_breakers``), so it is decided by backoff alone.

    A candidate must also have failed within ``INFRA_AUTO_RETRY_MAX_AGE``, and
    one whose streak reached ``INFRA_AUTO_RETRY_MAX_STREAK`` is decided
    ``capped``: it stays parked for the operator.
    """
    now = _aware(now)
    cutoff = now - BREAKER_HISTORY_LOOKBACK
    candidate_query = (
        select(ScreeningAttempt.agent_id)
        .join(Agent, Agent.agent_id == ScreeningAttempt.agent_id)
        .where(
            Agent.status == AgentStatus.SCREENING_FAILED,
            ScreeningAttempt.attempt_id == latest_screening_attempt_id(),
            *_infra_failure_filters(),
            ScreeningAttempt.finished_at > now - INFRA_AUTO_RETRY_MAX_AGE,
            ~exists(
                select(ScreeningRetryOverride.override_id).where(
                    ScreeningRetryOverride.attempt_id == ScreeningAttempt.attempt_id
                )
            ),
        )
    )
    if agent_ids is not None:
        if not agent_ids:
            return InfraRetryPlan({}, {}, claimable_limit)
        candidate_query = candidate_query.where(Agent.agent_id.in_(list(agent_ids)))
    candidates = set(await session.scalars(candidate_query))
    if not candidates and not (fleet_breakers and fleet_scan):
        return InfraRetryPlan({}, {}, claimable_limit)

    history_scope: ColumnElement[bool] = ScreeningAttempt.agent_id.in_(candidates)
    if fleet_scan:
        history_scope = or_(
            and_(
                ScreeningAttempt.started_at >= cutoff,
                ScreeningAttempt.agent_id.in_(failing_agents_query(cutoff)),
            ),
            history_scope,
        )
    history_rows = (
        await session.execute(
            select(
                ScreeningAttempt.attempt_id,
                ScreeningAttempt.agent_id,
                ScreeningAttempt.status,
                ScreeningAttempt.reason_code,
                ScreeningAttempt.started_at,
                ScreeningAttempt.finished_at,
                ScreeningAttempt.failure_provider,
                ScreeningAttempt.failure_lane,
                select(ScreenerNode.provider)
                .where(ScreenerNode.screener_hotkey == ScreeningAttempt.screener_hotkey)
                .correlate(ScreeningAttempt)
                .scalar_subquery(),
                exists(
                    select(SubmissionImageBuild.build_id).where(
                        SubmissionImageBuild.attempt_id == ScreeningAttempt.attempt_id
                    )
                ),
                exists(
                    select(ScreenedImageUpload.image_upload_id).where(
                        ScreenedImageUpload.attempt_id == ScreeningAttempt.attempt_id,
                        ScreenedImageUpload.status == "verified",
                    )
                ),
            )
            .where(history_scope)
            .order_by(
                ScreeningAttempt.agent_id,
                ScreeningAttempt.started_at,
                ScreeningAttempt.attempt_id,
            )
        )
    ).all()
    by_agent: dict[UUID, list[_Row]] = defaultdict(list)
    for row in history_rows:
        by_agent[row.agent_id].append(_Row(*row))

    breakers = _derive_breakers(by_agent.values(), cutoff=cutoff) if fleet_scan else {}
    if not candidates:
        return InfraRetryPlan({}, breakers, claimable_limit)

    # "Since the latest operator clear or manual retry": the same two bounds the
    # inconclusive count uses, so one operator action resets both.
    clears = {
        row[0]: row[1]
        for row in (
            await session.execute(
                select(
                    ScreeningQuarantine.agent_id,
                    func.max(ScreeningQuarantine.resolved_at),
                )
                .where(
                    ScreeningQuarantine.agent_id.in_(candidates),
                    ScreeningQuarantine.resolution.in_(("release", "rescreen")),
                )
                .group_by(ScreeningQuarantine.agent_id)
            )
        ).all()
    }
    retries = {
        row[0]: row[1]
        for row in (
            await session.execute(
                select(
                    ScreeningRetryOverride.agent_id,
                    func.max(ScreeningRetryOverride.created_at),
                )
                .where(ScreeningRetryOverride.agent_id.in_(candidates))
                .group_by(ScreeningRetryOverride.agent_id)
            )
        ).all()
    }

    decisions: dict[UUID, InfraRetryDecision] = {}
    for agent_id in candidates:
        rows = by_agent[agent_id]
        latest = rows[-1]
        bounds = [
            _aware(value)
            for value in (clears.get(agent_id), retries.get(agent_id))
            if value is not None
        ]
        streak = _streak(rows, since=max(bounds) if bounds else None)
        backoff_until = latest.failed_at + infra_retry_delay(streak, latest.attempt_id)
        breaker = breakers.get(latest.signature)
        next_retry_at = backoff_until
        state: DecisionState = "backoff" if now < backoff_until else "due"
        if streak >= INFRA_AUTO_RETRY_MAX_STREAK:
            state = "capped"
        elif (
            breaker is not None
            and breaker.open
            and (
                claimant_provider is None
                or latest.signature.provider is None
                or claimant_provider == latest.signature.provider
            )
        ):
            assert breaker.next_probe_at is not None
            next_retry_at = max(backoff_until, breaker.next_probe_at)
            if now >= backoff_until:
                state = "probe_due" if now >= breaker.next_probe_at else "breaker_held"
        decisions[agent_id] = InfraRetryDecision(
            agent_id=agent_id,
            attempt_id=latest.attempt_id,
            signature=latest.signature,
            streak=streak,
            failed_at=latest.failed_at,
            backoff_until=backoff_until,
            next_retry_at=next_retry_at,
            state=state,
            breaker=breaker,
        )
    return InfraRetryPlan(decisions, breakers, claimable_limit)
