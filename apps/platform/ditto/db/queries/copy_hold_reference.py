"""Whether a copy hold's reference agent was public when the held agent uploaded.

SN-499: near-copy holds whose reference is a publicly released agent sat for
days in an oldest-first queue. A fork of a public release is the hold an
operator can usually rule fastest, and the one miners most often dispute. So
the hold records the fact when it opens, and the operator queue sorts those
holds first.

This is evidence, not a ruling. A fork of a public agent can still carry
unreleased content, and the copy gate's alibi test decides that. Nothing here
clears, rejects, or changes a hold.

"Public" means what the anti-copy gate means:
:func:`ditto.db.queries.artifact_release.list_public_source_releases`, read
under the release policy in force when the held agent uploaded, with the
release time at or before that upload (the gate's ``available <= submitted``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from ditto.db.queries.artifact_release import list_public_source_releases
from ditto.db.queries.artifact_release_settings import artifact_release_policy_as_of
from ditto.db.queries.scores import SCORING_QUORUM

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

PROVENANCE_KEY = "reference_public_release"
"""``ath_reviews.algorithm_provenance`` key the hold records at open."""

ReferenceReleaseSource = Literal["recorded", "derived"]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True)
class ReferencePublicRelease:
    """Publication of a copy hold's reference, relative to the held upload."""

    reference_agent_id: UUID
    published_at: datetime | None
    """Earliest public release of the reference's exact artifact, if any. It
    can postdate the held upload; ``public_before_upload`` says which."""
    public_before_upload: bool
    source: ReferenceReleaseSource
    """``recorded`` when read from the hold's provenance, ``derived`` when
    computed at read time for a hold opened before SN-499."""

    def as_provenance(self) -> dict[str, Any]:
        return {
            "reference_agent_id": str(self.reference_agent_id),
            "published_at": (
                self.published_at.isoformat() if self.published_at else None
            ),
            "public_before_upload": self.public_before_upload,
        }


def reference_release_from_provenance(
    provenance: Mapping[str, Any] | None,
) -> ReferencePublicRelease | None:
    """The fact a hold recorded at open, or ``None`` for an older hold."""
    if not isinstance(provenance, Mapping):
        return None
    raw = provenance.get(PROVENANCE_KEY)
    if not isinstance(raw, Mapping):
        return None
    try:
        reference = UUID(str(raw["reference_agent_id"]))
    except (KeyError, ValueError):
        return None
    published_raw = raw.get("published_at")
    published_at: datetime | None = None
    if isinstance(published_raw, str):
        try:
            published_at = _utc(datetime.fromisoformat(published_raw))
        except ValueError:
            published_at = None
    return ReferencePublicRelease(
        reference_agent_id=reference,
        published_at=published_at,
        public_before_upload=raw.get("public_before_upload") is True,
        source="recorded",
    )


async def derive_reference_public_release(
    session: AsyncSession,
    *,
    reference_agent_id: UUID,
    held_uploaded_at: datetime,
) -> ReferencePublicRelease:
    """Compute the fact from the release ledger. Read-only."""
    uploaded = _utc(held_uploaded_at)
    policy = await artifact_release_policy_as_of(session, at=uploaded)
    releases = await list_public_source_releases(
        session,
        agent_ids=[reference_agent_id],
        quorum=SCORING_QUORUM,
        policy=policy,
    )
    published = releases.get(reference_agent_id)
    published_at = _utc(published) if published is not None else None
    return ReferencePublicRelease(
        reference_agent_id=reference_agent_id,
        published_at=published_at,
        public_before_upload=published_at is not None and published_at <= uploaded,
        source="derived",
    )
