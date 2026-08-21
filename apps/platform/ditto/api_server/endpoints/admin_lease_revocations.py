"""Admin: why the platform ended a validator's lease before its deadline.

``validator_lease_audit`` has recorded the reason and evidence for every
platform-initiated revocation since it shipped, and nothing could read it. A
lease incident then costs a day, because the only table that answers "why was
this run killed" is reachable solely through a psql session against production.

This is the product surface for that question, and the one Backroom wraps. It is
deliberately not the *only* way in: the repo also carries the
``gcloud-ditto-readonly`` skill, which is the diagnostic escape hatch
for the next table someone forgets to expose. An endpoint answers a question
somebody anticipated; direct read access answers the one they did not.

Read-only, so it is trivially MCP-safe. The naming mirrors the path, matching
what Backroom already does for ``get_queue_policy_settings`` and
``list_stuck_submissions``: ``list_lease_revocations``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.admin_lease_revocations import (
    AdminLeaseRevocation,
    AdminLeaseRevocationList,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.queries.lease_liveness import (
    count_lease_revocations,
    list_lease_revocations,
)

router = APIRouter(prefix="/admin/lease-revocations", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]


@router.get("", response_model=AdminLeaseRevocationList)
async def lease_revocations(
    _admin: AdminDep,
    session: SessionDep,
    agent_id: UUID | None = None,
    validator_hotkey: str | None = None,
    action: Annotated[list[str] | None, Query()] = None,
    context: Annotated[list[str] | None, Query()] = None,
    since: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> AdminLeaseRevocationList:
    """List platform-initiated lease revocations, newest first.

    ``agent_id`` and ``validator_hotkey`` are the two indexed filters, and are
    the two questions actually asked in an incident: "why did this submission
    lose its run" and "what is this validator doing to the leases it holds".

    An empty result is itself a finding. It means the platform has not revoked a
    lease in the window, so a run that died did so by some other path -- a
    deadline sweep, or a validator-reported ``fail_job`` -- and the ticket's own
    ``failure_reason`` is where to look next. Reading that emptiness as "no data
    yet" rather than as evidence is a diagnostic trap worth naming here.
    """
    filters = {
        "agent_id": agent_id,
        "validator_hotkey": validator_hotkey,
        "actions": action,
        "contexts": context,
        "since": since,
    }
    total = await count_lease_revocations(session, **filters)  # type: ignore[arg-type]
    rows = await list_lease_revocations(
        session,
        limit=limit,
        offset=offset,
        **filters,  # type: ignore[arg-type]
    )
    return AdminLeaseRevocationList(
        generated_at=datetime.now(UTC),
        total=total,
        revocations=[
            AdminLeaseRevocation(
                audit_id=row.audit_id,
                agent_id=row.agent_id,
                validator_hotkey=row.validator_hotkey,
                slot_id=row.slot_id,
                bench_version=row.bench_version,
                action=row.action,
                reason=row.reason,
                context=row.context,
                recorded_at=row.recorded_at,
                evidence=row.evidence or {},
            )
            for row in rows
        ],
    )
