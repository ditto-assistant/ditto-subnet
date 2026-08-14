"""Wire models for a miner's self-serve read of their own harness diagnostics.

A miner whose submission failed scoring could previously learn only the coarse
class it failed with. The evidence that names the cause -- the harness's own
bounded, redacted output -- existed on a validator host and reached no one.
Agent ``5fdadd33`` burned four leases in 82-108 seconds each behind a bare
``scoring_error``; its owner had no way to see why, and no operator had a way to
tell them that scaled past one-off manual triage.

These models back the route that closes that, authenticated the way every other
miner-owned action already is: an sr25519 signature over a payload naming the
hotkey. The signature proves possession of the hotkey; ownership is then a
straight lookup against ``agents.miner_hotkey``. A miner sees their own agents
and nothing else.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, Field

from ditto.api_models.upload import _SIGNATURE_HEX_PATTERN, _SS58_PATTERN

HARNESS_LOGS_MAX_SKEW_SECONDS = 300
"""How stale a signed harness-logs request may be.

Freshness alone, with no one-time nonce, and deliberately so. This route is a
read of data the signer already owns, so a replayed request returns the caller
their own logs a second time and grants nothing new -- the exposure a nonce
would remove is a captured signature staying useful indefinitely, and a bounded
window removes that at no cost. It matches ``_HEARTBEAT_MAX_SKEW_SECONDS``, the
other signed route whose risk is shaped the same way.

The mutating signed routes (``/validator/job/*``, ``/upload/*``) do consume a
nonce, because for them a replay re-performs an action. Nothing here is
performed twice.
"""


class MinerHarnessLogsRequest(BaseModel):
    """Signed proof that the caller holds the hotkey that owns ``agent_id``.

    Mirrors the proof-of-possession shape of the validator routes rather than
    ``/upload/check``'s: that one binds its signature to a tarball SHA-256, which
    is naturally unique per request, while a read has no such payload of its own
    and needs ``requested_at`` to bound how long a captured signature is worth
    replaying.

    ``agent_id`` is inside the signed bytes on purpose. Signing only the hotkey
    would let a captured signature be re-pointed at any agent the same miner
    owns; binding the pair means a signature authorizes exactly one lookup.
    """

    miner_hotkey: Annotated[
        str,
        Field(
            pattern=_SS58_PATTERN,
            description="Hotkey claiming ownership of ``agent_id``.",
        ),
    ]
    agent_id: Annotated[UUID, Field(description="Agent whose diagnostics to read.")]
    requested_at: Annotated[
        datetime, Field(description="UTC time at which the request was signed.")
    ]
    signature: Annotated[
        str,
        Field(
            pattern=_SIGNATURE_HEX_PATTERN,
            description=(
                "sr25519 signature over "
                "``ditto-harness-logs:v1:{miner_hotkey}:{agent_id}:{requested_at}``."
            ),
        ),
    ]


class MinerHarnessLogAttempt(BaseModel):
    """One validator's attempt at this agent, with whatever it reported.

    Scoped to diagnosis. It carries no score, no ranking, and nothing about any
    other miner's submission -- a miner debugging their own failure needs to know
    what died and what it printed, not where they placed.
    """

    validator_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    bench_version: Annotated[int, Field(ge=1)]
    status: str
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
    is scope-gated for operators and returned here only against a signature
    proving the reader already owns it.
    """


class MinerHarnessLogsResponse(BaseModel):
    """Every recorded attempt at one agent, newest first."""

    agent_id: UUID
    miner_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    agent_status: str
    attempts: list[MinerHarnessLogAttempt]
