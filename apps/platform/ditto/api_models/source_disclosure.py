"""Whether public source release ever happens, as a property of subnet policy.

The artifact-release settings board answers *when* the king's source becomes
public. Until now every value it could hold was a number of hours, so it could
not answer *whether it ever does*. :class:`SourceDisclosure` is that second
axis, and it lives on the same append-only, operator-audited revision as the
window -- one setting, one write, one confirmation.

**Subnet-global, and uniform.** There is no per-miner opt-out and no per-
submission flag. Every submission is treated identically; the operator chooses
once for the subnet. That uniformity is doing real work: it means there is no
differential treatment to game, no "the champion is withheld while others are
not", and no negotiation surface between miners and operators.

**Why an enum and not a nullable ``embargo_hours``.** A ``NULL`` window meaning
"never" would overload a column whose entire existing contract is ``NOT NULL``
between two bounds. Three consequences, in descending order of how much they
would hurt:

1. Any write path that failed to set the field would mean *never publish*
   rather than raising. Fail-closed on privacy, but silently -- a malformed
   revision would be indistinguishable from a deliberate one.
2. ``PublicArtifactRelease.embargo_hours`` is a non-null ``int`` on the public
   wire. Making it nullable is a breaking change for every consumer that reads
   it, to express something none of them asked to read as a null.
3. The check constraint stops being a simple range.

With the enum, ``embargo_hours`` keeps its ``NOT NULL`` range constraint in
every policy, and ``never`` is an explicit, greppable value in the DB, the API,
the board and the confirmation phrase.
"""

from __future__ import annotations

from enum import StrEnum


class SourceDisclosure(StrEnum):
    """Whether the public source-release path is reachable at all."""

    PUBLIC = "public"
    """Source is released through the normal king-only, embargoed path."""

    NEVER = "never"
    """Source is never published, whatever the window says.

    Not a retraction: anything already downloaded during an open window stays
    downloaded. Selecting this stops future public serving; it cannot recall
    bytes that are already out.
    """


def release_confirmation(disclosure: SourceDisclosure, embargo_hours: int) -> str:
    """The exact phrase an operator must type to apply a policy.

    ``never`` gets its own phrase rather than an hour count, because there is
    no hour count that means it -- and because a phrase that differed from the
    embargo one only in a number would be too easy to submit by habit.
    """
    if disclosure is SourceDisclosure.NEVER:
        return "SET SOURCE DISCLOSURE NEVER"
    return f"SET SOURCE EMBARGO {embargo_hours} HOURS"
