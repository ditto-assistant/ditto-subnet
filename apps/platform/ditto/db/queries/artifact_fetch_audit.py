"""Record who was served a miner artifact, and when.

Every path that hands a caller miner source — the tarball bytes, a presigned
URL to them, a single source file, a source diff — appends one row to
``artifact_fetch_audit`` through :func:`record_artifact_fetch`.

Two properties matter more than anything else here:

**The audit must never deny a legitimate fetch.** A validator holding a valid
scoring ticket has already proved possession of its hotkey, passed the chain
permit check, and burned a nonce. If the audit INSERT then fails — a full disk,
a lock timeout, a migration mid-flight — refusing the artifact would convert a
logging fault into a scoring outage across the fleet. So this function is
fail-open: it returns ``False`` and logs, and never raises. The cost is
explicit and accepted: a fetch can happen without leaving a row. That is why
the failure is logged at ``exception`` level under a stable, greppable message
(:data:`AUDIT_WRITE_FAILED`) — a silent audit gap is the one outcome worse than
a loud one, and alerting keys off that string.

**It must not serialize the read path.** Unlike ``score_audit_log``, which
takes a ``FOR UPDATE`` lock on the chain head to keep one linear hash chain,
this table is a plain insert with no chain and no head lock, so concurrent
fetches never queue behind each other.

Writes are synchronous and unbatched, deliberately. Artifact fetches are
bounded by work issuance, not by request traffic: k=3 validator tickets per
agent per benchmark era, one screener claim per screening attempt, and a
handful of operator reads. That is single-digit rows per submission — orders of
magnitude below where INSERT batching earns its cost. Batching would also trade
away exactly the property the table exists for: an in-memory buffer loses its
contents on the crash or kill you most want the record of. If volume ever does
grow, the table is append-only and range-partitionable on ``fetched_at``
without touching any caller.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from ditto.db.models import ArtifactFetchAudit

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Stable log message for a dropped audit row. Alert on this string: it is the
# only signal that an artifact was served without leaving a record.
AUDIT_WRITE_FAILED = "artifact fetch audit write failed"

RequesterKind = Literal["validator", "screener", "admin", "public"]

# Route names, not request paths. Paths carry ids and get reshaped; the question
# this identifier answers is "which door did the fetcher come through".
ENDPOINT_PUBLIC_ARTIFACT = "public.agent_artifact"
ENDPOINT_VALIDATOR_ARTIFACT = "validator.agent_artifact"
ENDPOINT_VALIDATOR_CODING_HARNESS = "validator.coding_harness_launch"
ENDPOINT_VALIDATOR_CODING_CERTIFICATION_HARNESS = (
    "validator.coding_certification_harness_launch"
)
ENDPOINT_SCREENER_ARTIFACT = "screener.agent_artifact"
ENDPOINT_ADMIN_SCREENING_ARTIFACT = "admin.get_screening_artifact"
ENDPOINT_ADMIN_SOURCE_FILES = "admin.list_screening_source_files"
ENDPOINT_ADMIN_SOURCE_FILE = "admin.read_screening_source_file"
ENDPOINT_ADMIN_SOURCE_SEARCH = "admin.search_screening_source"
ENDPOINT_ADMIN_COPY_REVIEW_DIFF = "admin.get_copy_review_source_diff"
ENDPOINT_ADMIN_COPY_REVIEW_DIFF_FILE = "admin.get_copy_review_source_diff_file"


async def record_artifact_fetch(
    session: AsyncSession,
    *,
    agent_id: UUID,
    endpoint: str,
    requester_kind: RequesterKind,
    requester_id: str | None = None,
    requester_instance_id: str | None = None,
    lease_id: UUID | None = None,
    bench_version: int | None = None,
    artifact_sha256: str | None = None,
    source_ip: str | None = None,
    detail: dict[str, Any] | None = None,
) -> bool:
    """Append one audit row for a served artifact. Never raises.

    Call this *after* the fetch has been authorized and the artifact (or its
    presigned URL) has been produced, so a row means "bytes were actually
    served" rather than "someone knocked". Returns ``True`` when the row
    committed and ``False`` when it was dropped — callers ignore the result;
    it exists so tests can assert the fail-open path was taken.

    Commits on its own rather than joining the caller's transaction: a failed
    audit INSERT inside a shared transaction would poison it and take the
    fetch's own work down with it, which is the exact coupling this function
    exists to prevent.
    """
    try:
        session.add(
            ArtifactFetchAudit(
                agent_id=agent_id,
                endpoint=endpoint,
                requester_kind=requester_kind,
                requester_id=requester_id,
                requester_instance_id=requester_instance_id,
                lease_id=lease_id,
                bench_version=bench_version,
                artifact_sha256=artifact_sha256,
                source_ip=source_ip,
                detail=detail,
            )
        )
        await session.commit()
    except Exception:
        # Fail open. The artifact has already been authorized and served; a
        # bookkeeping fault must not turn into a denial. Log loudly instead.
        logger.exception(
            "%s: endpoint=%s agent_id=%s requester_kind=%s requester_id=%s",
            AUDIT_WRITE_FAILED,
            endpoint,
            agent_id,
            requester_kind,
            requester_id,
        )
        try:
            await session.rollback()
        except Exception:
            logger.exception("artifact fetch audit rollback failed")
        return False
    return True
