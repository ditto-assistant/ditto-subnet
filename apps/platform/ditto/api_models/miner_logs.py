"""Wire models for a signed-in miner's read of their own harness diagnostics.

A miner whose submission failed scoring could previously learn only the coarse
class it failed with. The evidence that names the cause -- the harness's own
bounded, redacted output -- existed on a validator host and reached no one.
Agent ``5fdadd33`` burned four leases in 82-108 seconds each behind a bare
``scoring_error``; its owner had no way to see why, and no operator had a way to
tell them that scaled past one-off manual triage.

These models back the session-authenticated route that closes that. The caller
must already hold a miner session for the hotkey that owns the agent (dashboard
login, ``ditto login``, or hosted MCP). A miner sees their own agents and
nothing else.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ditto.api_models.upload import _SS58_PATTERN


class MinerHarnessLogAttempt(BaseModel):
    """One validator ticket for this agent, with whatever it last reported.

    ``validator_tickets`` is a mutable per-agent/version/validator row, not an
    attempt ledger. Reissue restamps ``issued_at`` and increments
    ``attempt_count`` while retaining the prior failure fields. ``stale`` is
    true when ``container_log_tail`` belongs to an earlier lease than the one
    ``issued_at`` describes -- do not compute a runtime from
    ``failed_at - issued_at`` in that case.

    Scoped to diagnosis. It carries no score, no ranking, and nothing about any
    other miner's submission.
    """

    model_config = ConfigDict(extra="ignore")

    validator_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    bench_version: Annotated[int, Field(ge=1)]
    status: str
    attempt_count: Annotated[int, Field(ge=1)]
    issued_at: datetime
    deadline: datetime
    failed_at: datetime | None = None
    failure_reason: str | None = None
    """Coarse class the validator reported: how the platform responded."""
    failure_detail: str | None = None
    """The validator's own code behind ``failure_reason``, when it sent one."""
    container_log_tail: str | None = None
    """This harness's own bounded, redacted stdout/stderr tail.

    ``None`` means none was reported: a validator predating the field, a failure
    with no container behind it, or a container that printed nothing.

    Safe to return to this caller precisely because it is *their* output. It can
    contain their own source through a stack trace, which is why the same field
    is scope-gated for operators and returned here only against a session for
    the owning hotkey.
    """
    log_tail_attempt: int | None = None
    """``attempt_count`` of the lease that wrote ``container_log_tail``."""
    stale: bool = False
    """The tail or failure fields belong to a superseded lease.

    True when a tail is present and ``log_tail_attempt != attempt_count``, or
    when ``failed_at`` predates the current ``issued_at``. The current lease
    has not produced this evidence.
    """


class MinerHarnessLogsResponse(BaseModel):
    """Every recorded validator ticket for one agent, newest first."""

    model_config = ConfigDict(extra="ignore")

    agent_id: UUID
    miner_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    agent_status: str
    attempts: list[MinerHarnessLogAttempt]
