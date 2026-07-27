"""Which locally-running benchmarks the platform no longer holds a lease for.

The platform owns lease assignment, so it -- and only it -- can tell a validator
that a lease is gone. Heartbeat protocol 17 carries that answer on the response
(``ValidatorHeartbeatResponse.leases``) and this module turns it into a decision.

It lives apart from :mod:`ditto.validator.worker` deliberately. The one decision
in here can stop a benchmark that has been running for an hour, so it is a pure
function over two explicit inputs: no I/O, no clock, no worker state, nothing to
mock, and nothing that can fail halfway. What the worker keeps is the acting.

**The safety property.** Cancelling is only ever allowed against an explicit,
authoritative, successfully-received roster. Everything else -- a request that
errored, a response that would not parse, a platform too old to have the field,
a roster the platform declined to derive -- is :class:`RosterUnknown` and
cancels nothing. That distinction is carried in the *types* rather than in a
nullable field, because collapsing "I was not told" into "I was told nothing" is
precisely the false-idle verdict that destroyed healthy runs three times in this
system (ditto-platform #437, #443, #496). A reader of this module should not be
able to write that bug without deleting a class.

**Why the comparison is safe without any clock.** ``plan_cancellations`` diffs
the roster against the capacity the validator *advertised in the very request
that produced this response*, not against its live slot table. The platform read
its ledger while handling that request, therefore strictly after this validator
had already claimed every slot the request named. So a slot the platform omits
is one it genuinely holds no lease for -- never one it simply had not heard about
yet. A freshly claimed slot cannot be in the request and so cannot be cancelled
by that request's answer, no matter how the two hosts' clocks disagree.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from ditto.api_models.benchmark_capacity import BenchmarkCapacity
from ditto.api_models.validator import ValidatorHeartbeatResponse

LeaseKey = tuple[str, UUID]
"""``(slot_id, agent_id)`` -- what identifies a lease to both sides.

Deliberately excludes the deadline. The platform re-issues a lease in place
(``deadline = now + ttl``) while the validator keeps signing against the
deadline it was handed at claim time, so mid-run the two legitimately disagree.
The platform draws the same line for the same reason in ``get_live_slot_ticket``:
that drift is normal and says nothing about whether a run is alive. Folding it
into the identity here would let ordinary drift authorize a kill.
"""


@dataclass(frozen=True)
class RosterUnknown:
    """The platform did not tell us which leases we hold.

    Carries no information about any lease. :func:`plan_cancellations` returns
    nothing against it, unconditionally. Produced when the peer is a platform
    older than v17, when the platform's own roster read failed, or any other time
    ``leases`` arrives absent.

    ``reason`` is for the log line only; no caller may branch on its contents.
    """

    reason: str


@dataclass(frozen=True)
class RosterKnown:
    """The platform authoritatively enumerated every lease we hold.

    ``held`` may be empty, and an empty ``RosterKnown`` is a real, actionable
    answer: *you hold nothing*. That is exactly the state an operator eviction
    leaves behind, and it is the whole reason this is a distinct type rather than
    ``frozenset | None`` -- the empty set has to stay reachable and meaningful
    while "no answer" stays inert.
    """

    held: frozenset[LeaseKey]


Roster = RosterKnown | RosterUnknown


def read_roster(response: ValidatorHeartbeatResponse) -> Roster:
    """Classify what a heartbeat response said about this validator's leases.

    Only ever called on a response that was actually received and parsed. A
    heartbeat that raised never reaches here at all, which is the structural
    reason an unreachable platform can never cancel anything.
    """
    if response.leases is None:
        return RosterUnknown("platform sent no lease roster (pre-v17 or unavailable)")
    return RosterKnown(
        frozenset((lease.slot_id, lease.agent_id) for lease in response.leases)
    )


def plan_cancellations(
    roster: Roster, *, advertised: BenchmarkCapacity
) -> tuple[LeaseKey, ...]:
    """Return the advertised slots the platform no longer holds a lease for.

    ``advertised`` must be the ``benchmark_capacity`` carried by the request that
    produced ``roster`` -- see this module's docstring for why that pairing, and
    not the worker's current slot table, is what makes the diff sound.

    Returns keys, not actions. The caller re-checks each slot against live state
    before stopping anything, so a run that finished in the meantime is a plain
    no-op.
    """
    if isinstance(roster, RosterUnknown):
        return ()
    return tuple(
        key
        for slot in advertised.active
        if (key := (slot.slot_id, slot.agent_id)) not in roster.held
    )
