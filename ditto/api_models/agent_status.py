"""Canonical ``AgentStatus`` lifecycle enum.

Lives in :mod:`ditto.api_models` (not ``ditto.db``) because it is a wire
value that appears in HTTP response bodies. Keeping the source of truth
here lets the wire models stay free of any database dependency, so the
miner/validator side can use ``api_models`` without pulling in SQLAlchemy.

The platform side (``ditto-platform``) keeps a byte-identical copy of this
module and binds the enum to the Postgres ENUM type ``agentstatus``. The
two copies must stay in sync (there is no shared package — the OpenAPI
schema is the contract between the repos).
"""

from __future__ import annotations

from enum import StrEnum


class AgentStatus(StrEnum):
    """Lifecycle state machine values for an agent submission.

    Matches the Postgres ENUM type ``agentstatus`` on the platform side.
    :class:`enum.StrEnum` (Python 3.11+) makes values usable as plain
    strings so they serialize directly to JSON.
    """

    UPLOADED = "uploaded"
    SCREENING = "screening"
    SCREENING_PASSED = "screening_passed"
    SCREENING_FAILED = "screening_failed"
    EVALUATING = "evaluating"
    SCORED = "scored"
    LIVE = "live"
    ATH_PENDING_REVIEW = "ath_pending_review"
    BANNED = "banned"
