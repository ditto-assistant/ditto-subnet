"""Ticket-scoped inference grant lifecycle."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ditto.api_models.ticket_status import TicketStatus
from ditto.api_server.inference_routing import benchmark_model, select_route
from ditto.db.models import InferenceGrant, InferenceRequest, ValidatorTicket
from ditto.metrics import INFERENCE_ADMISSION_AT_CAPACITY

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from ditto.api_server.config import InferenceProxyConfig


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def bearer_digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


USAGE_ACCOUNTING_VERSION = 2
"""The metering contract new grants are booked under.

Bumped when what gets *recorded* as a grant's token usage changes, so that a
total is never silently compared across two different meters. See
``InferenceGrant.usage_accounting_version``.
"""


@dataclass(frozen=True)
class LeaseModelUsage:
    """What one lease actually spent on the language model.

    Read off the grant bound to a single ticket lease, which is where the
    platform's own inference proxy books usage as it charges requests. The
    validator never sees these numbers and cannot report them, so this is the
    authoritative -- and unspoofable-by-the-validator -- account of whether a
    run used the model at all.

    ``chat_*`` counts the reader model. Embeddings are tracked separately by
    the grant and deliberately excluded: a retrieval-only agent embeds
    heavily, so folding embeddings in would erase exactly the signal this
    exists to measure.
    """

    chat_calls: int
    prompt_tokens: int
    completion_tokens: int
    accounting_version: int

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


async def get_lease_model_usage(
    session: AsyncSession,
    *,
    ticket: ValidatorTicket,
) -> LeaseModelUsage | None:
    """Return the model spend booked against ``ticket``'s lease, if any.

    Keyed on the same four columns as the ``inference_grants_ticket_lease``
    unique constraint, so this resolves the one grant that belongs to this
    exact lease rather than guessing by time proximity. Returns ``None`` when
    no grant exists -- the proxy was disabled, or the lease predates it --
    which callers must treat as *unknown*, never as *unused*.

    Read-only: this takes no lock and adds no column to ``inference_grants``,
    which is a hot table.
    """
    grant = await session.scalar(
        select(InferenceGrant).where(
            InferenceGrant.agent_id == ticket.agent_id,
            InferenceGrant.bench_version == ticket.bench_version,
            InferenceGrant.validator_hotkey == ticket.validator_hotkey,
            InferenceGrant.ticket_deadline == _aware(ticket.deadline),
        )
    )
    if grant is None:
        return None
    return LeaseModelUsage(
        chat_calls=int(grant.request_count or 0),
        prompt_tokens=int(grant.prompt_tokens or 0),
        completion_tokens=int(grant.completion_tokens or 0),
        accounting_version=int(grant.usage_accounting_version or 0),
    )


async def ensure_inference_grant(
    session: AsyncSession,
    *,
    ticket: ValidatorTicket,
    config: InferenceProxyConfig,
    supported_profiles: tuple[str, ...] | None = None,
    calibration_manifest_sha256: str | None = None,
) -> InferenceGrant | None:
    """Create or return the one grant bound to this exact live lease.

    Creation is race-free at the database level, not by convention. ``SELECT
    ... FOR UPDATE`` locks rows that exist; when the grant has not been created
    yet there is nothing to lock, so two callers racing on the same lease -- two
    concurrent offer/heartbeat calls for one ticket -- can both miss and both
    insert. The ``inference_grants_ticket_lease`` unique constraint has always
    made the dangerous outcome impossible: a single ticket could never actually
    obtain two grants and therefore never double its request or token budget.
    What was missing was handling the losing side, which surfaced the conflict
    as an unhandled IntegrityError and a 500 on an otherwise valid offer. The
    loser now adopts the winner's row, which is the answer it wanted anyway.

    The savepoint spans route selection as well as the insert, so a loser also
    rolls back the ``selected_ticket_count`` increment ``select_route`` applies:
    it never held a ticket, so it must not be counted as having been offered
    one.
    """
    if not config.enabled or ticket.status != TicketStatus.ISSUED:
        return None
    deadline = _aware(ticket.deadline)
    lease = (
        select(InferenceGrant)
        .where(
            InferenceGrant.agent_id == ticket.agent_id,
            InferenceGrant.bench_version == ticket.bench_version,
            InferenceGrant.validator_hotkey == ticket.validator_hotkey,
            InferenceGrant.ticket_deadline == deadline,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    grant = await session.scalar(lease)
    if grant is None:
        model = benchmark_model(ticket.bench_version)
        if model not in config.allowed_models:
            return None
        route_provider: str | None = config.provider
        route_profile: str | None = f"legacy-config-{config.provider}"
        route_quantization: str | None = None
        route_prompt_price_per_token: float | None = None
        route_completion_price_per_token: float | None = None
        try:
            async with session.begin_nested():
                if ticket.bench_version >= 7:
                    route = await select_route(
                        session,
                        model=model,
                        now=datetime.now(UTC),
                        supported_profiles=supported_profiles,
                        calibration_manifest_sha256=calibration_manifest_sha256,
                        routing_mode=config.routing_mode,
                        bench_version=ticket.bench_version,
                    )
                    if route is None:
                        return None
                    route_provider = route.provider
                    route_profile = route.profile_revision
                    route_quantization = route.quantization
                    route_prompt_price_per_token = route.prompt_price_per_token
                    route_completion_price_per_token = route.completion_price_per_token
                grant = InferenceGrant(
                    grant_id=uuid4(),
                    agent_id=ticket.agent_id,
                    bench_version=ticket.bench_version,
                    validator_hotkey=ticket.validator_hotkey,
                    slot_id=ticket.slot_id,
                    ticket_deadline=deadline,
                    status="pending",
                    bearer_digest=None,
                    broker_public_key=None,
                    generation=0,
                    allowed_models=[model],
                    route_provider=route_provider,
                    route_profile=route_profile,
                    route_quantization=route_quantization,
                    route_prompt_price_per_token=route_prompt_price_per_token,
                    route_completion_price_per_token=route_completion_price_per_token,
                    request_budget=config.request_budget,
                    token_budget=config.token_budget,
                    embedding_model=config.embedding_model,
                    embedding_profile=config.embedding_profile,
                    embedding_provider=config.embedding_provider,
                    embedding_dimensions=config.embedding_dimensions,
                    embedding_request_budget=config.embedding_request_budget,
                    embedding_token_budget=config.embedding_token_budget,
                    embedding_request_count=0,
                    embedding_tokens=0,
                    embedding_cost_microusd=0,
                    embedding_active_requests=0,
                    request_count=0,
                    usage_accounting_version=USAGE_ACCOUNTING_VERSION,
                    prompt_tokens=0,
                    completion_tokens=0,
                    cost_microusd=0,
                    active_requests=0,
                    expires_at=deadline,
                )
                session.add(grant)
                await session.flush()
        except IntegrityError:
            # Another caller created this lease's grant first. Its row is the
            # one grant for this ticket; adopt it instead of inserting a second.
            return await session.scalar(lease)
    return grant


async def activate_inference_grant(
    session: AsyncSession,
    *,
    grant_id: UUID,
    validator_hotkey: str,
    broker_public_key: str,
    now: datetime,
    config: InferenceProxyConfig,
) -> tuple[InferenceGrant, str] | None:
    """Rotate the broker binding and return a fresh opaque bearer.

    Rotation is restart-safe: the prior bearer becomes invalid immediately and
    a fresh validator signature is required for every exchange.
    """
    snapshot = await session.get(InferenceGrant, grant_id)
    if snapshot is None or snapshot.validator_hotkey != validator_hotkey:
        return None
    ticket = await session.get(
        ValidatorTicket,
        (snapshot.agent_id, snapshot.bench_version, snapshot.validator_hotkey),
        with_for_update=True,
    )
    grant = await session.scalar(
        select(InferenceGrant)
        .where(InferenceGrant.grant_id == grant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if grant is None:
        return None
    if (
        grant.validator_hotkey != validator_hotkey
        or ticket is None
        or ticket.status != TicketStatus.ISSUED
        or _aware(ticket.deadline) != _aware(grant.ticket_deadline)
        or _aware(ticket.deadline) <= now
        or grant.status in {"revoked", "exhausted"}
    ):
        grant.status = "revoked"
        return None
    started = list(
        (
            await session.scalars(
                select(InferenceRequest)
                .where(
                    InferenceRequest.grant_id == grant.grant_id,
                    InferenceRequest.status == "started",
                )
                .with_for_update()
            )
        ).all()
    )
    stale_cutoff = now - timedelta(seconds=config.timeout_seconds * 2)
    if any(_aware(request.started_at) >= stale_cutoff for request in started):
        # A restart may rotate only after every previous generation call has
        # either settled or crossed the provider timeout recovery window.
        return None
    for request in started:
        request.status = "canceled"
        request.prompt_tokens = request.reserved_tokens
        request.completed_at = now
        if request.request_kind == "chat":
            grant.prompt_tokens += request.reserved_tokens
        else:
            grant.embedding_tokens += request.reserved_tokens
    bearer = secrets.token_urlsafe(32)
    grant.bearer_digest = bearer_digest(bearer)
    grant.broker_public_key = broker_public_key.rstrip("=")
    grant.generation += 1
    grant.status = "active"
    grant.slot_id = ticket.slot_id
    grant.expires_at = _aware(ticket.deadline)
    grant.active_requests = 0
    grant.embedding_active_requests = 0
    grant.updated_at = now
    await session.flush()
    return grant, bearer


async def revoke_ticket_inference(
    session: AsyncSession,
    *,
    ticket: ValidatorTicket,
    now: datetime,
) -> None:
    grants = list(
        (
            await session.scalars(
                select(InferenceGrant)
                .where(
                    InferenceGrant.agent_id == ticket.agent_id,
                    InferenceGrant.bench_version == ticket.bench_version,
                    InferenceGrant.validator_hotkey == ticket.validator_hotkey,
                    InferenceGrant.status.in_(("pending", "active")),
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    requests_by_grant: dict[UUID, list[InferenceRequest]] = {}
    if grants:
        requests = list(
            await session.scalars(
                select(InferenceRequest)
                .where(
                    InferenceRequest.grant_id.in_([grant.grant_id for grant in grants]),
                    InferenceRequest.status == "started",
                )
                .order_by(InferenceRequest.grant_id, InferenceRequest.nonce)
                .with_for_update()
            )
        )
        for request in requests:
            requests_by_grant.setdefault(request.grant_id, []).append(request)
    for grant in grants:
        for request in requests_by_grant.get(grant.grant_id, []):
            request.status = "canceled"
            request.prompt_tokens = request.reserved_tokens
            request.completed_at = now
            if request.request_kind == "chat":
                grant.prompt_tokens += request.reserved_tokens
            else:
                grant.embedding_tokens += request.reserved_tokens
        grant.status = "revoked"
        grant.active_requests = 0
        grant.embedding_active_requests = 0
        grant.updated_at = now


class InferenceDecline(StrEnum):
    """Why an admission was refused. Every refusal has one; none is silent.

    Historically :func:`begin_inference_request` returned ``None`` for every
    refusal and the endpoint mapped all of them to ``429``. That collapsed
    unrelated events into one status code, and dittobench-api #103 documents the
    damage: on the ticket path the broker reads *any* ``429`` as "the lease is
    gone" and discards the whole run.

    #473 named three of them. The rest stayed behind a bare ``None`` -> ``4100``,
    and one of those was the defect that made #473 inert: a spent **token**
    budget answered ``4100``, which the broker classifies as transient and
    retries at ~2.5/sec for two minutes until the run dies as
    ``model_relay_unavailable``. 1009 declines on one lease, every one of them
    ``4100``, with 1h21m still on the clock. A refusal nobody can name is a
    refusal nobody can act on, so this enum now covers the whole surface.

    Terminal, and the caller must not retry:

    * :attr:`GRANT_REVOKED` — the lease really is dead (the ticket expired, was
      reassigned, or the deadline moved). Fatal, and correctly so.
    * :attr:`LEASE_EXPIRED` — the grant's own ``expires_at`` has passed. Same
      practical advice as revocation, different cause, and worth separating
      because one is the platform acting and the other is just the clock.
    * :attr:`BUDGET_EXHAUSTED` — the **request-count** allowance is spent.
    * :attr:`TOKEN_BUDGET_EXHAUSTED` — the **token** allowance is spent. Split
      from the above deliberately: they are tuned by different operator dials
      and a harness that hits the token wall at call 400 is behaving very
      differently from one that hits the request wall at call 8192.
    * :attr:`MODEL_NOT_PERMITTED` — the grant does not pin this model.
    * :attr:`NONCE_REPLAYED` — this (grant, nonce) already exists. Repeating it
      can never succeed; the caller must mint a fresh nonce.
    * :attr:`GRANT_NOT_EXCHANGED` — minted but never exchanged for a bearer, so
      there is no live lease here yet. The broker must exchange first.
    * :attr:`RESERVATION_TOO_LARGE` — the request alone is bigger than the whole
      token allowance. Distinct from exhaustion on purpose: nothing has been
      spent, and a lease must not be declared dead because one call was
      oversized.

    Retryable, and the caller should back off and return:

    * :attr:`AT_CAPACITY` — nothing is wrong at all; the lane was momentarily
      full, or the grant's in-flight reservations momentarily cover the
      remaining token headroom. Both clear on their own.

    And the one honest remainder:

    * :attr:`UNATTRIBUTED` — refused, and naming the reason would tell an
      unauthenticated caller about somebody else's lease. This is a *deliberate*
      refusal to explain, not an accident, which is why it is a named member
      rather than a ``None`` return: the reader of a ``4100`` should be able to
      tell "we decided not to say" from "nobody ever wired this up".

    Conflating a full lane with a dead lease is what killed ``banblackycat``: 17
    capacity declines, read as 17 dead leases. It is also why the status code is
    not the discriminator. The endpoint answers the retryable decline with
    ``503 + Retry-After`` and every terminal one with ``429``, but the
    *authoritative* signal is the numeric ``error_code`` in the error body
    (``ditto/api_server/middleware/error_envelope.py``). A status code carries
    about two bits; application semantics need more, and every attempt to
    encode them in the status has cost a run.
    """

    GRANT_REVOKED = "grant_revoked"
    BUDGET_EXHAUSTED = "budget_exhausted"
    AT_CAPACITY = "at_capacity"
    TOKEN_BUDGET_EXHAUSTED = "token_budget_exhausted"
    LEASE_EXPIRED = "lease_expired"
    MODEL_NOT_PERMITTED = "model_not_permitted"
    NONCE_REPLAYED = "nonce_replayed"
    GRANT_NOT_EXCHANGED = "grant_not_exchanged"
    RESERVATION_TOO_LARGE = "reservation_too_large"
    UNATTRIBUTED = "unattributed"


# Both lanes return AT_CAPACITY now. Chat used to be excluded on the grounds
# that its limits are boot-time constants which cannot move under a live ticket
# -- true, and still true, but it was never the whole argument. A chat lane can
# hit its rate or concurrency ceiling under perfectly ordinary load with nothing
# whatsoever wrong with the lease, and answering that with the same ``429`` that
# means "your lease is dead" is how a healthy run gets thrown away.


def _at_capacity(request_kind: str, scope: str) -> InferenceDecline:
    """Record which admission gate declined, then answer the one wire value.

    The return is always :attr:`InferenceDecline.AT_CAPACITY`; only the metric
    knows the difference. Keeping the decline itself undifferentiated is the
    point -- a broker's correct response to every one of these is identical
    (back off, retry), so splitting the wire contract would add a decision the
    caller must not make. The operator's question is the opposite one, and it
    has no answer today: *which* ceiling is binding, if any.
    """
    INFERENCE_ADMISSION_AT_CAPACITY.labels(lane=request_kind, scope=scope).inc()
    return InferenceDecline.AT_CAPACITY


def _first_exhausted_scope(
    gates: tuple[tuple[str, int, int], ...],
) -> str | None:
    """Return the first ``(scope, observed, limit)`` gate that is at its limit.

    First rather than all: the caller declines on any one of them, so the
    remaining gates are not evaluated as causes. Ordering is therefore
    significant and is narrowest-to-widest, so a report reads as the tightest
    binding constraint rather than whichever check happened to be written last.
    """
    for scope, observed, limit in gates:
        if observed >= limit:
            return scope
    return None


async def begin_inference_request(
    session: AsyncSession,
    *,
    grant_id: UUID,
    nonce: UUID,
    bearer: str,
    model: str,
    token_reservation: int,
    max_chargeable_tokens: int | None = None,
    now: datetime,
    config: InferenceProxyConfig,
    request_kind: str = "chat",
) -> tuple[InferenceGrant, InferenceRequest] | InferenceDecline:
    """Atomically consume one nonce and reserve bounded proxy capacity.

    Returns the reservation on success and an :class:`InferenceDecline` on every
    refusal. There is no unnamed refusal left: this function used to return a
    bare ``None`` for "a bad bearer, a grant that is not this caller's, a model
    the grant does not permit, a replayed nonce, an expired clock, a grant
    minted but never exchanged, or a token budget spent" -- seven unrelated
    conditions behind one anonymous ``4100``, of which a spent token budget was
    the one that quietly ended runs.

    The reasons an authenticated caller can act on are now each named. The two
    that stay anonymous -- an unknown grant id and a failed bearer comparison --
    return :attr:`InferenceDecline.UNATTRIBUTED`, which is still ``4100`` on the
    wire but is now a *decision* recorded in the type rather than a gap. The
    caller maps every member to a status code and a stable numeric error code;
    see :class:`InferenceDecline`.

    Ordering is the security property, not an extra check. Every named decline
    below sits *after* the bearer comparison, because the reason a grant is
    unusable is information about somebody else's lease and is disclosed only to
    a caller that has already proved it holds this grant's bearer.

    Locking model: the grant row taken ``FOR UPDATE`` below is the only
    serialization point, and every invariant that spends a budget is scoped to
    that one grant -- reserved tokens, request count, per-ticket concurrency,
    per-ticket rate, stale reclamation, and the nonce replay guard all filter on
    ``grant_id``. Postgres serializes writers of one row by construction, so
    two reservations against the same grant cannot both pass a budget check,
    while reservations against different grants proceed fully in parallel.

    ``populate_existing`` on that locking read is load-bearing, not decoration.
    The unlocked ``session.get`` above puts the row in the identity map, and by
    default SQLAlchemy will hand a later query the object it already has
    without overwriting its attributes from the new result. The FOR UPDATE
    select would then block correctly, wait its turn correctly, and still
    evaluate every budget check against the values it read *before* acquiring
    the lock -- so concurrent reservations would each see a stale
    ``request_count`` and collectively overspend the grant. The old global
    advisory lock hid this: it was taken before the unlocked read, so nothing
    could commit between them and the stale value was always current anyway.
    Removing the lock without this is silent accounting corruption, and
    ``test_reservations_on_one_grant_serialize_and_respect_the_budget`` fails
    against real Postgres if it is dropped.

    This previously also took ``pg_advisory_xact_lock(hashtextextended(
    'inference', 0))`` -- one lock, with a constant key, for every reservation
    on the platform. It was held for the whole transaction (roughly eight
    statements), so it serialized the entire fleet's reservation path and put a
    hard ceiling on horizontal scaling. It was never what protected the
    money-critical invariants; the grant row lock already did, and no other
    caller in this module ever took the advisory lock, so it also provided no
    mutual exclusion with grant creation, activation, revocation, or finish.

    What it did cover is the cross-grant admission rails below (per-validator
    and global in-flight counts and per-minute rates). Those aggregate across
    every grant, so no row lock can make them exact, and making them exact
    would require reintroducing exactly the global barrier being removed. They
    are therefore best-effort: a burst of simultaneous reservations can
    overshoot a rail by at most the number of racers, which is acceptable for
    operational load-shedding backstops with headroom. The per-ticket rails
    directly above them stay exact, and those are the ones a miner can target.
    Best-effort does not change what a refusal *means*: a cross-grant rail that
    does trip still answers :attr:`InferenceDecline.AT_CAPACITY`, so the caller
    reports a healthy-but-full lane rather than a dead lease.
    """
    if request_kind not in {"chat", "embedding"}:
        return InferenceDecline.UNATTRIBUTED
    snapshot = await session.get(InferenceGrant, grant_id)
    if snapshot is None:
        return InferenceDecline.UNATTRIBUTED
    ticket = await session.get(
        ValidatorTicket,
        (snapshot.agent_id, snapshot.bench_version, snapshot.validator_hotkey),
        with_for_update=True,
    )
    grant = await session.scalar(
        select(InferenceGrant)
        .where(InferenceGrant.grant_id == grant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    # THE authentication gate. Everything below it may name a reason; nothing
    # above it may. Keep the bearer comparison fused with the existence checks
    # so an unknown grant and a wrong bearer are indistinguishable, and keep
    # every *other* condition below it -- the model check in particular used to
    # be fused in here, which meant a caller holding the right bearer could not
    # be told the one thing it could actually fix.
    if (
        grant is None
        or grant.bearer_digest is None
        or not secrets.compare_digest(grant.bearer_digest, bearer_digest(bearer))
    ):
        return InferenceDecline.UNATTRIBUTED
    if _aware(grant.expires_at) <= now:
        return InferenceDecline.LEASE_EXPIRED
    if (
        model not in grant.allowed_models
        if request_kind == "chat"
        else grant.bench_version < 7 or model != grant.embedding_model
    ):
        return InferenceDecline.MODEL_NOT_PERMITTED
    # This gate is what makes the terminal declines *persistent*. The first
    # refusal below sets the status; every subsequent call in the run lands
    # here, and without this branch the whole tail of the run would decay back
    # to an unnamed refusal -- which is precisely the window in which a harness
    # needs to know whether to retry, wind down, or give up.
    #
    # ``exhausted`` is one status covering two different spent allowances, and
    # the tail of a run must not be told the wrong one. Rather than remembering
    # which wall was hit, ask the same question the wall asked: would a request
    # of *this* size still fit in the token allowance? That re-derivation is two
    # already-loaded columns and it stays correct in the case a stored reason
    # would get wrong -- exhaustion recorded a request short of the budget, so
    # ``spent`` sits just below ``token_budget`` rather than at it.
    if grant.status != "active":
        if grant.status == "exhausted":
            if (
                request_kind == "chat"
                and grant.prompt_tokens + grant.completion_tokens + token_reservation
                > grant.token_budget
            ):
                return InferenceDecline.TOKEN_BUDGET_EXHAUSTED
            return InferenceDecline.BUDGET_EXHAUSTED
        if grant.status == "revoked":
            return InferenceDecline.GRANT_REVOKED
        # "pending" -- minted but never exchanged. Unreachable in practice,
        # since a pending grant has no bearer digest and fails the gate above,
        # but named rather than silent: an anonymous refusal is now something
        # this function has to choose, not something it falls into.
        return InferenceDecline.GRANT_NOT_EXCHANGED
    stale_cutoff = now - timedelta(seconds=config.timeout_seconds * 2)
    stale_requests = list(
        (
            await session.scalars(
                select(InferenceRequest)
                .where(
                    InferenceRequest.grant_id == grant.grant_id,
                    InferenceRequest.status == "started",
                    InferenceRequest.request_kind == request_kind,
                    InferenceRequest.started_at < stale_cutoff,
                )
                .with_for_update()
            )
        ).all()
    )
    for stale in stale_requests:
        stale.status = "canceled"
        stale.prompt_tokens = stale.reserved_tokens
        stale.completed_at = now
        if request_kind == "chat":
            grant.prompt_tokens += stale.reserved_tokens
        else:
            grant.embedding_tokens += stale.reserved_tokens
    if stale_requests:
        await session.flush()
        active_count = int(
            await session.scalar(
                select(func.count()).where(
                    InferenceRequest.grant_id == grant.grant_id,
                    InferenceRequest.status == "started",
                    InferenceRequest.request_kind == request_kind,
                )
            )
            or 0
        )
        if request_kind == "chat":
            grant.active_requests = active_count
        else:
            grant.embedding_active_requests = active_count
    if (
        ticket is None
        or ticket.status != TicketStatus.ISSUED
        or _aware(ticket.deadline) != _aware(grant.ticket_deadline)
        or _aware(ticket.deadline) <= now
    ):
        grant.status = "revoked"
        return InferenceDecline.GRANT_REVOKED
    if request_kind == "chat" and grant.request_count >= grant.request_budget:
        # Still terminal: the allowance is spent and no amount of waiting brings
        # it back, so the broker must not retry. What changes is that the caller
        # can now say *which* terminal this is. "Your lease died" and "you spent
        # your budget" call for opposite reactions from a harness -- discard
        # versus wind down and submit -- and until now they were the same byte.
        grant.status = "exhausted"
        return InferenceDecline.BUDGET_EXHAUSTED
    if (
        request_kind == "embedding"
        and grant.embedding_request_count >= grant.embedding_request_budget
    ):
        # Deliberately not a status change. The embedding allowance is 100,000
        # against ~671 used per run, so reaching it means something pathological
        # rather than a strategy being thorough, and killing the grant outright
        # would also take the chat lane down with it.
        return InferenceDecline.BUDGET_EXHAUSTED
    if token_reservation < 1:
        # A caller-shape error, not an allowance problem. Nothing to attribute.
        return InferenceDecline.UNATTRIBUTED
    active_reserved = await session.scalar(
        select(func.coalesce(func.sum(InferenceRequest.reserved_tokens), 0)).where(
            InferenceRequest.grant_id == grant.grant_id,
            InferenceRequest.status == "started",
            InferenceRequest.request_kind == request_kind,
        )
    )
    spent = (
        grant.prompt_tokens + grant.completion_tokens
        if request_kind == "chat"
        else grant.embedding_tokens
    )
    token_budget = (
        grant.token_budget if request_kind == "chat" else grant.embedding_token_budget
    )
    if spent + int(active_reserved or 0) + token_reservation > token_budget:
        # Three very different events used to share this one anonymous exit, and
        # telling them apart is exactly what a broker needs.
        if token_reservation > token_budget:
            # The request is larger than the entire allowance, so it would not
            # have fit in a brand-new grant either. That is a caller-shape
            # problem, and emphatically not a reason to declare a lease spent
            # when it has consumed nothing.
            return InferenceDecline.RESERVATION_TOO_LARGE
        if spent + token_reservation > token_budget:
            # The allowance has less room left than one request needs. No amount
            # of waiting brings it back, so say so and stop. This is the branch
            # that answered a bare ``None`` -- and therefore 4100, which the
            # broker reads as transient and retries at ~2.5/sec for two minutes
            # until the run dies as ``model_relay_unavailable``. It is the whole
            # reason the heaviest v7 agents failed with hours left on the lease.
            #
            # Note that ``spent`` never actually reaches ``token_budget`` on
            # this path, which is why the exhaustion check in
            # ``finish_inference_request`` (``>= token_budget``) did not cover
            # it: the run stalls a request short of the line and stays there.
            if request_kind == "chat":
                # Terminal, and recorded as terminal, exactly as request-count
                # exhaustion is: a spent allowance does not come back, so the
                # status gate above answers every later call in the run without
                # re-deriving it. Deliberately not done for embeddings, whose
                # 1B allowance means reaching it is pathological rather than
                # thorough -- and killing the grant would take chat down too.
                grant.status = "exhausted"
            return InferenceDecline.TOKEN_BUDGET_EXHAUSTED
        # Only the *in-flight* reservations push the total over. Nothing is
        # spent yet and they settle within the provider timeout, so this is
        # backpressure, not exhaustion -- answering it terminally would throw
        # away a run that still had room.
        return _at_capacity(request_kind, "token_reservation")
    active_requests = (
        grant.active_requests
        if request_kind == "chat"
        else grant.embedding_active_requests
    )
    per_ticket_concurrency = (
        config.per_ticket_concurrency
        if request_kind == "chat"
        else config.embedding_per_ticket_concurrency
    )
    if active_requests >= per_ticket_concurrency:
        # Healthy lease, lane momentarily full. This is the limit an operator
        # tunes from backroom, so it is also the one most likely to move under a
        # live run -- it must degrade to backpressure, never to a lost run.
        return _at_capacity(request_kind, "per_ticket")

    # Fast replay path avoids an ORM identity collision in the common case;
    # the composite primary key and nested transaction remain authoritative
    # for concurrent attempts on different platform workers.
    if await session.get(InferenceRequest, (grant.grant_id, nonce)) is not None:
        return InferenceDecline.NONCE_REPLAYED

    active_column = (
        InferenceGrant.active_requests
        if request_kind == "chat"
        else InferenceGrant.embedding_active_requests
    )
    validator_active = await session.scalar(
        select(func.coalesce(func.sum(active_column), 0)).where(
            InferenceGrant.validator_hotkey == grant.validator_hotkey,
            InferenceGrant.status == "active",
        )
    )
    global_active = await session.scalar(
        select(func.coalesce(func.sum(active_column), 0)).where(
            InferenceGrant.status == "active"
        )
    )
    minute_start = now - timedelta(minutes=1)
    validator_recent = await session.scalar(
        select(func.count())
        .select_from(InferenceRequest)
        .join(InferenceGrant, InferenceGrant.grant_id == InferenceRequest.grant_id)
        .where(
            InferenceGrant.validator_hotkey == grant.validator_hotkey,
            InferenceRequest.started_at >= minute_start,
            InferenceRequest.request_kind == request_kind,
        )
    )
    ticket_recent = await session.scalar(
        select(func.count()).where(
            InferenceRequest.grant_id == grant.grant_id,
            InferenceRequest.started_at >= minute_start,
            InferenceRequest.request_kind == request_kind,
        )
    )
    global_recent = await session.scalar(
        select(func.count()).where(
            InferenceRequest.started_at >= minute_start,
            InferenceRequest.request_kind == request_kind,
        )
    )
    per_validator_concurrency = (
        config.per_validator_concurrency
        if request_kind == "chat"
        else config.embedding_per_validator_concurrency
    )
    global_concurrency = (
        config.global_concurrency
        if request_kind == "chat"
        else config.embedding_global_concurrency
    )
    per_ticket_rpm = (
        config.per_ticket_requests_per_minute
        if request_kind == "chat"
        else config.embedding_per_ticket_requests_per_minute
    )
    per_validator_rpm = (
        config.per_validator_requests_per_minute
        if request_kind == "chat"
        else config.embedding_per_validator_requests_per_minute
    )
    global_rpm = (
        config.global_requests_per_minute
        if request_kind == "chat"
        else config.embedding_global_requests_per_minute
    )
    # One decline, five reasons. The caller still sees a single AT_CAPACITY --
    # the wire contract is deliberately unchanged, because a broker must treat
    # every one of these identically (back off and retry). The gates are
    # enumerated rather than or-ed only so the metric can name which one tripped:
    # "the lane declined" and "the *global* ceiling declined" are the same event
    # to the validator and completely different events to the operator deciding
    # whether a ceiling needs to move.
    exhausted_scope = _first_exhausted_scope(
        (
            ("per_validator", int(validator_active or 0), per_validator_concurrency),
            ("global", int(global_active or 0), global_concurrency),
            ("per_ticket_rpm", int(ticket_recent or 0), per_ticket_rpm),
            ("per_validator_rpm", int(validator_recent or 0), per_validator_rpm),
            ("global_rpm", int(global_recent or 0), global_rpm),
        )
    )
    if exhausted_scope is not None:
        return _at_capacity(request_kind, exhausted_scope)

    request = InferenceRequest(
        grant_id=grant.grant_id,
        nonce=nonce,
        generation=grant.generation,
        status="started",
        request_kind=request_kind,
        model=model,
        reserved_tokens=token_reservation,
        max_chargeable_tokens=(
            token_reservation
            if max_chargeable_tokens is None
            else max(token_reservation, max_chargeable_tokens)
        ),
        started_at=now,
    )
    try:
        async with session.begin_nested():
            session.add(request)
            await session.flush()
    except IntegrityError:
        # The composite primary key is the distributed replay guard.
        return InferenceDecline.NONCE_REPLAYED
    if request_kind == "chat":
        grant.request_count += 1
        grant.active_requests += 1
    else:
        grant.embedding_request_count += 1
        grant.embedding_active_requests += 1
    grant.updated_at = now
    await session.flush()
    return grant, request


async def finish_inference_request(
    session: AsyncSession,
    *,
    grant_id: UUID,
    nonce: UUID,
    generation: int,
    status: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost_microusd: int,
    usage_available: bool,
    now: datetime,
    upstream_provider: str | None = None,
    timed_out: bool = False,
    latency_ms: int | None = None,
    upstream_attempts: int = 0,
    openrouter_attempts: int = 0,
    fallback_phase: int = 0,
    terminal_error_code: str | None = None,
) -> bool:
    snapshot = await session.get(InferenceGrant, grant_id)
    if snapshot is None:
        return False
    ticket = await session.get(
        ValidatorTicket,
        (snapshot.agent_id, snapshot.bench_version, snapshot.validator_hotkey),
        with_for_update=True,
    )
    grant = await session.scalar(
        select(InferenceGrant)
        .where(InferenceGrant.grant_id == grant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    request = await session.get(
        InferenceRequest, (grant_id, nonce), with_for_update=True
    )
    if (
        grant is None
        or request is None
        or request.status not in {"started", "canceled"}
        or request.generation != generation
    ):
        return False
    was_started = request.status == "started"
    if not was_started and (
        request.prompt_tokens > 0
        or request.completion_tokens > 0
        or request.cost_microusd > 0
    ):
        return False
    deliverable = (
        status == "completed"
        and usage_available
        and grant.status == "active"
        and grant.generation == generation
        and was_started
        and _aware(grant.expires_at) > now
        and ticket is not None
        and ticket.status == TicketStatus.ISSUED
        and _aware(ticket.deadline) == _aware(grant.ticket_deadline)
        and _aware(ticket.deadline) > now
    )
    prompt_tokens = max(0, prompt_tokens)
    completion_tokens = max(0, completion_tokens)
    cost_microusd = max(0, cost_microusd)
    if not usage_available:
        # Every provider outcome without trusted usage is conservatively
        # charged to its reservation, including timeout and transport failure.
        prompt_tokens = request.reserved_tokens
        completion_tokens = 0
    elif prompt_tokens + completion_tokens > request.max_chargeable_tokens:
        # Untrusted provider accounting cannot exceed the byte-derived ceiling
        # or overflow the grant's integer counters.
        #
        # Clamped against ``max_chargeable_tokens``, NOT ``reserved_tokens``.
        # The reservation is an estimate now, and a legitimate token-dense
        # prompt routinely lands a little above it; clamping there would mark
        # ordinary successful calls non-deliverable and 409 them back to the
        # harness. The ceiling is the number that is still a true bound.
        prompt_tokens = request.max_chargeable_tokens
        completion_tokens = 0
        deliverable = False
    request.status = (
        status if was_started and (deliverable or status != "completed") else "canceled"
    )
    request.prompt_tokens = prompt_tokens
    request.completion_tokens = completion_tokens
    request.cost_microusd = cost_microusd
    request.upstream_provider = upstream_provider
    request.upstream_attempts = max(0, upstream_attempts)
    request.openrouter_attempts = max(0, openrouter_attempts)
    request.fallback_phase = min(1, max(0, fallback_phase))
    request.terminal_error_code = terminal_error_code
    request.timed_out = timed_out
    request.latency_ms = latency_ms
    request.completed_at = now
    if request.request_kind == "chat":
        if was_started:
            grant.active_requests = max(0, grant.active_requests - 1)
        grant.prompt_tokens += prompt_tokens
        grant.completion_tokens += completion_tokens
        grant.cost_microusd += cost_microusd
    else:
        if was_started:
            grant.embedding_active_requests = max(
                0, grant.embedding_active_requests - 1
            )
        grant.embedding_tokens += prompt_tokens
        grant.embedding_cost_microusd += cost_microusd
    grant.updated_at = now
    if (
        request.request_kind == "chat"
        and grant.prompt_tokens + grant.completion_tokens >= grant.token_budget
    ):
        grant.status = "exhausted"
    return deliverable


async def ticket_inference_revoked_mid_lease(
    session: AsyncSession,
    *,
    agent_id: UUID,
    bench_version: int,
    validator_hotkey: str,
    ticket_deadline: datetime,
) -> bool:
    """Did the platform itself terminate this still-live lease's inference?

    Answers one question, from the platform's own records rather than from a
    validator's self-report: while this exact lease was open, did the platform
    revoke the inference grant it had minted for it?

    Only two code paths set ``revoked`` while a ticket is still open --
    :func:`activate_inference_grant` and :func:`begin_inference_request` -- and
    both do it for the same family of reasons: the owning ticket is no longer
    ISSUED, its deadline was rewritten, or its deadline has passed. Every one of
    those is platform or validator state. None of them is reachable from
    anything an agent controls, so this cannot be induced by a miner to farm
    free attempts.

    Scoping to ``ticket_deadline`` is what keeps the answer about *this* lease.
    The third writer, :func:`revoke_ticket_inference`, runs only as cleanup once
    a ticket has been scored or failed -- by which point the lease is closed and
    a later attempt carries a fresh deadline -- so its writes cannot be mistaken
    for a mid-lease revocation here.

    ``exhausted`` is deliberately not counted. That status means the agent spent
    the request budget it was given, which is the agent's own doing and must
    keep costing it an attempt.
    """
    revoked = await session.scalar(
        select(func.count())
        .select_from(InferenceGrant)
        .where(
            InferenceGrant.agent_id == agent_id,
            InferenceGrant.bench_version == bench_version,
            InferenceGrant.validator_hotkey == validator_hotkey,
            InferenceGrant.ticket_deadline == ticket_deadline,
            InferenceGrant.status == "revoked",
        )
    )
    return bool(revoked)


__all__ = [
    "USAGE_ACCOUNTING_VERSION",
    "activate_inference_grant",
    "bearer_digest",
    "InferenceDecline",
    "begin_inference_request",
    "ensure_inference_grant",
    "finish_inference_request",
    "revoke_ticket_inference",
    "ticket_inference_revoked_mid_lease",
]
