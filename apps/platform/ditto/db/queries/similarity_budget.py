"""The similarity signal the ticket allocator reads, and where it comes from.

Queue fairness, not anti-copy
=============================

Everything in this module exists to answer ONE question: *are these two
submissions near-identical enough that they should share a single slice of
fleet concurrency?* It is deliberately **not** a plagiarism, copying, or
anti-cheat mechanism, and nothing here ever holds, rejects, quarantines, or
scores a submission. A grouped submission loses nothing except the right to run
at the same instant as its own near-twin; it keeps its place in the queue, its
score, its emissions and its miner's reputation.

That framing is load-bearing, because the two questions want opposite
error profiles. Copy enforcement must be nearly certain before it acts, since a
false positive costs a miner their submission. Fairness only has to be *right
about the queue*: if two near-identical crates are in fact two honest miners who
converged, the cost of grouping them is that one waits for the other -- the same
wait every miner already accepts behind the owner gate. So this gate may act on
evidence that would be far too thin to reject anybody, and it must never be
re-used as evidence that anybody copied anything.

The problem it solves is concrete. ``owner_capacity_gate`` already serializes
one *owner*, resolved through the payment-time coldkey. An operator who spreads
the same submission across many hotkeys AND many coldkeys defeats it: in the
production window this module was calibrated against, one submission family
occupied 25 of the 76 live evaluations across 15 hotkeys and 6 coldkeys, while
honest miners waited. No owner-linkage rule reaches that, because on every
signal the platform can verify, those really are different owners.

Why the upload-time content fingerprint
=======================================

Four candidate signals already exist on ``agents``. This module uses
:attr:`~ditto.db.models.Agent.content_fingerprint`, and the other three are
unusable *here* for reasons worth writing down:

``code_embedding`` / ``code_embed_model``
    The rename-robust channel, and the obvious first choice -- but it is not
    populated. Every one of the 745 rows in production carries a JSON ``null``
    vector and a ``NULL`` model tag, because the embedder ships behind
    ``CODE_EMBEDDER_URL`` and that is unset. A gate built on it would be inert
    in exactly the incident it was built for.

``structural_fingerprint``
    Genuinely the better signal for *identity* -- it hashes AST shape, so it
    survives renaming that the lexical channel misses. But dittobench computes
    it where the crate is unpacked, and it is written **at score time**. A
    concurrency gate runs when a ticket is issued, which is strictly before the
    first score exists, so the signal is null for precisely the submissions the
    gate has to judge (615 of 745 rows have it at all, and none of them had it
    while they were queued).

``normalized_source_hash``
    Exact equality after canonicalization. It is the right tool for "same
    source repackaged" and it does catch a farm: 21 submissions across 10
    hotkeys share one value in production today. But it is exact, and the
    family this gate targets ships one edited file per submission, so it sees
    almost none of them -- only 3 of 53 rows in that family share a hash.

``content_fingerprint`` is computed at ``/upload/agent`` (100% coverage: 745 of
745 rows have one, 675 of them non-empty), which is early enough, and it is a
*shingle* sketch, so a one-file edit perturbs a handful of shingles rather than
voiding the whole signal.

The starter-kit problem, and why it is already solved
=====================================================

Every miner starts from the same public starter kit, so any similarity metric
over raw source collapses: honest miners share whole byte-identical files with
each other and with the family this gate targets. A metric that cannot separate
the shared scaffold from the miner's own code would merge the entire subnet into
one budget and halve every honest miner's throughput.

:mod:`ditto.api_server.fingerprint` already solved this, for its own reasons.
The v2 fingerprint subtracts every shingle found anywhere in the official
starter-kit mainline history *before* sketching, so ``content_fingerprint`` is a
sketch of the miner-authored **residual** and the scaffold is neutral evidence
by construction. This module inherits that property rather than re-deriving it,
which is why it compares fingerprints and not source.

The measurement confirms the subtraction does its job. Across 13,464 pairs drawn
from 14 distinct production miners, median residual Jaccard is 0.000 and the
99th percentile is 0.465 -- two independent miners who provably share
byte-identical scaffold files score near zero here, because the scaffold is not
in the sketch.

The comparison is only meaningful within one corpus generation, so
:func:`ditto.api_server.fingerprint.content_similarity` refuses to compare
across sketch versions or reference-corpus ids and returns ``(0.0, 0.0)``.
Production currently holds two corpus generations. For a *concurrency* gate that
is the safe direction and costs almost nothing: concurrent submissions are
near-contemporaneous and therefore share a generation, and a cross-generation
pair is simply not grouped.

No migration
============

Nothing here adds a column. Every signal it reads has been on ``agents`` since
the anti-copy work landed, which is the whole point of the module docstring
above -- and it keeps this change off ``validator_tickets``, the hot table whose
``safe_add_column`` rules exist because #481 deadlocked the deploy twice.

What it does add is a read of the ``anticopy`` deferred group on a path that
never touched it. Those columns hold multi-hundred-KB blobs and were deferred
because serializing them on every ``Agent`` read was the dominant DB cost in
production, so this module never loads them by ORM entity: it projects the two
JSON columns for a *bounded* id set -- the candidate plus the fleet's live
leases, at most ``slots x validators`` rows -- and returns plain value objects.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from ditto.api_models.ticket_status import TicketStatus
from ditto.db.models import Agent, ValidatorTicket

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence
    from datetime import datetime
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class SubmissionSketch:
    """One submission's similarity evidence, detached from its ORM row.

    A value object rather than an ``Agent``: the gate needs two JSON blobs and
    an id, and holding a live entity would re-attach the whole deferred
    ``anticopy`` group to the identity map on the ticket path, which is the cost
    the deferral exists to avoid.

    ``fingerprint`` is the raw ``{v, k, card, m, corpus}`` sketch dict exactly as
    :func:`ditto.api_server.fingerprint.compute_content_fingerprint` produced it,
    passed through untouched so the comparison uses the production estimator
    rather than a restatement of it. ``None`` means the submission has no
    comparable evidence -- pre-dating the fingerprint, an unreadable tarball, or
    a residual below the reference-subtraction floor -- and a submission with no
    evidence is never grouped with anything.
    """

    agent_id: UUID
    fingerprint: dict | None

    @property
    def comparable(self) -> bool:
        """Whether this sketch can take part in a comparison at all."""
        return bool(self.fingerprint) and bool((self.fingerprint or {}).get("m"))


async def load_submission_sketches(
    session: AsyncSession, *, agent_ids: Collection[UUID]
) -> dict[UUID, SubmissionSketch]:
    """Load the similarity sketch for a bounded set of submissions.

    Projects the deferred column rather than loading ``Agent`` entities, so the
    ``anticopy`` blobs are read once into plain dicts and never attached to the
    session. Ids with no ``agents`` row are simply absent from the result;
    callers treat a missing sketch the same way they treat an empty one.
    """
    wanted = list(dict.fromkeys(agent_ids))
    if not wanted:
        return {}
    rows = await session.execute(
        select(Agent.agent_id, Agent.content_fingerprint).where(
            Agent.agent_id.in_(wanted)
        )
    )
    return {
        agent_id: SubmissionSketch(agent_id=agent_id, fingerprint=fingerprint)
        for agent_id, fingerprint in rows
    }


async def live_lease_agent_ids(session: AsyncSession, *, now: datetime) -> set[UUID]:
    """Every submission holding a live lease anywhere in the fleet.

    The similarity rail's counterpart to
    :func:`~ditto.db.queries.queue_order.owner_live_lease_agent_ids`, and
    deliberately the same shape: ``ISSUED`` tickets whose deadline has not
    passed, read through without sweeping, because the allocator expires overdue
    tickets on its own pass and a gate that wrote here would be taking a lock to
    answer a question.

    It differs in scope only. The owner rail asks "which of *this owner's*
    submissions are running", because owner identity partitions the fleet
    cheaply. Similarity does not partition anything -- two rows are near-twins
    or they are not, and no key tells you which in advance -- so the candidate
    has to be offered every live lease and compared. That set is bounded by the
    fleet's total slot count (a dozen today), which is what makes an
    all-pairs comparison affordable on the ticket path.
    """
    return set(
        await session.scalars(
            select(ValidatorTicket.agent_id)
            .where(
                ValidatorTicket.status == TicketStatus.ISSUED,
                ValidatorTicket.deadline > now,
            )
            .distinct()
        )
    )


async def live_lease_sketches(
    session: AsyncSession, *, now: datetime, exclude: Sequence[UUID] = ()
) -> tuple[SubmissionSketch, ...]:
    """The sketches of every live lease, minus ``exclude``.

    The one call the gate makes per issuance pass. ``exclude`` drops rows the
    caller has already accounted for -- in practice the candidate itself, which
    may legitimately already hold a lease from another validator and must not be
    counted as its own near-twin.
    """
    skip = set(exclude)
    leased = await live_lease_agent_ids(session, now=now) - skip
    sketches = await load_submission_sketches(session, agent_ids=leased)
    return tuple(
        sketches[agent_id]
        for agent_id in sorted(leased, key=str)
        if agent_id in sketches
    )
