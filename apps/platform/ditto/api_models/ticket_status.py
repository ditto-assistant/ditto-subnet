"""Canonical ``TicketStatus`` lifecycle enum.

Lives in :mod:`ditto.api_models` (not ``ditto.db``) for the same reason as
:class:`ditto.api_models.agent_status.AgentStatus`: it is a wire value that
appears in HTTP response bodies *and* in the database column, and keeping the
source of truth here lets the wire models stay free of any database dependency
so the same ``api_models`` package can be vendored into the validator side.

:mod:`ditto.db.models` re-imports this enum for the ``validator_tickets.status``
column and binds it to the Postgres ENUM type ``ticketstatus``.
"""

from __future__ import annotations

from enum import StrEnum


class TicketStatus(StrEnum):
    """Lifecycle state machine values for a validator scoring ticket.

    Matches the Postgres ENUM type ``ticketstatus``. A ticket is the right to
    score one agent: the platform issues at most three per agent to three
    distinct validators (the k=3 pool).

    - ``issued``: granted, awaiting a score before ``deadline``.
    - ``scored``: the validator posted a valid score in time; the slot is spent.
    - ``expired``: the deadline passed with no score, or a late score arrived;
      the slot re-opens for another validator.
    """

    ISSUED = "issued"
    SCORED = "scored"
    EXPIRED = "expired"


class TicketPurpose(StrEnum):
    """Authoritative reason the platform issued the current ticket lease."""

    LEGACY_UNCLASSIFIED = "legacy_unclassified"
    CANONICAL_QUORUM = "canonical_quorum"
    CONTINUAL_RETEST = "continual_retest"
