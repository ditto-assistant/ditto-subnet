"""Fail-closed trust boundary for a known-benign V13 control.

A ``recorded_unverified`` approval row, including its caller-supplied
``X-Admin-Actor``, is not a trusted control. Only two distinct authenticated
reviewers plus a still-verified exact image and attempt can produce a trusted
projection. That projection carries digests, never private challenge bytes.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.v13_private_generation import (
    V13TrustedKnownBenignApproval,
    V13TrustedKnownBenignReviewer,
)
from ditto.api_server.v13_benign_identity import VerifiedBenignPrincipal
from ditto.db.models import (
    Agent,
    ScreenedImageUpload,
    ScreeningAttempt,
    V13KnownBenignAttestation,
    V13KnownBenignControlApproval,
)


def _utc_stamp(value: datetime) -> str:
    return (
        value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def _canonical_digest(value: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _unavailable(detail: str) -> HTTPException:
    return HTTPException(status_code=409, detail=detail)


async def load_trusted_known_benign_approval(
    session: AsyncSession, approval: V13KnownBenignControlApproval
) -> V13TrustedKnownBenignApproval:
    """Return a verified projection or refuse. Never reads ``approval.actor``."""
    agent = await session.get(Agent, approval.agent_id)
    attempt = await session.get(ScreeningAttempt, approval.attempt_id)
    if (
        agent is None
        or attempt is None
        or attempt.agent_id != approval.agent_id
        or attempt.policy_version != 13
        or attempt.artifact_sha256 != approval.artifact_sha256
        or agent.sha256.lower() != approval.artifact_sha256
    ):
        raise _unavailable("stale control attempt")
    verified_image = await session.scalar(
        select(ScreenedImageUpload.image_upload_id).where(
            ScreenedImageUpload.agent_id == agent.agent_id,
            ScreenedImageUpload.attempt_id == attempt.attempt_id,
            ScreenedImageUpload.screener_hotkey == attempt.screener_hotkey,
            ScreenedImageUpload.sha256 == approval.image_sha256,
            ScreenedImageUpload.status == "verified",
        )
    )
    if verified_image is None:
        raise _unavailable("stale control image")
    reviewers = list(
        await session.scalars(
            select(V13KnownBenignAttestation)
            .where(
                V13KnownBenignAttestation.approval_id == approval.approval_id,
                V13KnownBenignAttestation.review_evidence_sha256
                == approval.review_evidence_sha256,
            )
            .order_by(
                V13KnownBenignAttestation.attested_at,
                V13KnownBenignAttestation.principal_sub,
            )
        )
    )
    pair = reviewers[:2]
    if len(pair) < 2 or pair[0].principal_sub == pair[1].principal_sub:
        raise _unavailable("benign control provenance unavailable")
    if pair[0].principal_email == pair[1].principal_email:
        raise _unavailable("benign control provenance unavailable")
    completed_at = max(row.attested_at for row in pair)
    receipt_reviewers = [
        V13TrustedKnownBenignReviewer(
            principal_sub=row.principal_sub,
            assertion_sha256=row.assertion_sha256,
            attested_at=row.attested_at,
        )
        for row in sorted(pair, key=lambda row: row.principal_sub)
    ]
    provenance_receipt = _canonical_digest(
        {
            "revision": "v13-known-benign-provenance-v1",
            "approval_id": str(approval.approval_id),
            "agent_id": str(approval.agent_id),
            "attempt_id": str(approval.attempt_id),
            "artifact_sha256": approval.artifact_sha256,
            "image_sha256": approval.image_sha256,
            "profile_sha256": approval.profile_sha256,
            "approval_receipt_sha256": approval.approval_receipt_sha256,
            "review_evidence_sha256": approval.review_evidence_sha256,
            "reviewers": [
                {
                    "principal_sub": row.principal_sub,
                    "assertion_sha256": row.assertion_sha256,
                    "attested_at": _utc_stamp(row.attested_at),
                }
                for row in receipt_reviewers
            ],
        }
    )
    return V13TrustedKnownBenignApproval(
        approval_id=approval.approval_id,
        agent_id=approval.agent_id,
        attempt_id=approval.attempt_id,
        artifact_sha256=approval.artifact_sha256,
        image_sha256=approval.image_sha256,
        profile_sha256=approval.profile_sha256,
        review_evidence_sha256=approval.review_evidence_sha256,
        approval_receipt_sha256=approval.approval_receipt_sha256,
        approved_at=approval.approved_at,
        provenance_status="two_person_authenticated",
        authenticated_reviewers=2,
        provenance_review_evidence_sha256=approval.review_evidence_sha256,
        provenance_receipt_sha256=provenance_receipt,
        completed_at=completed_at,
        reviewers=receipt_reviewers,
    )


def generator_conflicts(
    trusted: V13TrustedKnownBenignApproval,
    principal: VerifiedBenignPrincipal,
    *,
    reviewer_emails: set[str],
) -> bool:
    """True when the generation principal is also an approving reviewer."""
    subs = {row.principal_sub for row in trusted.reviewers}
    return principal.sub in subs or principal.email in reviewer_emails
