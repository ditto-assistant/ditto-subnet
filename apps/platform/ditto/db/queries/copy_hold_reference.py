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

from collections import defaultdict
from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from sqlalchemy import select

from ditto.api_models.source_disclosure import SourceDisclosure
from ditto.db.models import ArtifactReleaseSettingsRevision
from ditto.db.queries.artifact_release import list_public_source_releases
from ditto.db.queries.artifact_release_settings import (
    DEFAULT_ARTIFACT_RELEASE_POLICY,
    ArtifactReleasePolicy,
)
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


@dataclass(frozen=True)
class ReferenceReleaseQuery:
    """One hold whose reference publication should be derived."""

    key: Hashable
    reference_agent_id: UUID
    held_uploaded_at: datetime


def _policy_as_of(
    revisions: Sequence[tuple[int, datetime, str, int]], at: datetime
) -> ArtifactReleasePolicy:
    """In-memory :func:`artifact_release_policy_as_of` over preloaded rows:
    the highest revision created at or before ``at``."""
    eligible = [row for row in revisions if _utc(row[1]) <= at]
    if not eligible:
        return DEFAULT_ARTIFACT_RELEASE_POLICY
    _revision, _created, disclosure, embargo_hours = max(
        eligible, key=lambda row: row[0]
    )
    return ArtifactReleasePolicy(
        disclosure=SourceDisclosure(disclosure), embargo_hours=embargo_hours
    )


async def derive_reference_public_releases(
    session: AsyncSession, queries: Sequence[ReferenceReleaseQuery]
) -> dict[Hashable, ReferencePublicRelease]:
    """Compute the fact for many holds at once. Read-only.

    The release-settings revision table is append-only and tiny, so it is read
    once and each upload's policy is resolved in memory. References are then
    grouped by policy, so the release ledger is read once per distinct policy
    (in practice one or two), never once per hold.
    """
    if not queries:
        return {}
    revisions = [
        (int(revision), created_at, disclosure, int(embargo_hours))
        for revision, created_at, disclosure, embargo_hours in (
            await session.execute(
                select(
                    ArtifactReleaseSettingsRevision.revision,
                    ArtifactReleaseSettingsRevision.created_at,
                    ArtifactReleaseSettingsRevision.disclosure,
                    ArtifactReleaseSettingsRevision.embargo_hours,
                )
            )
        ).all()
    ]
    by_policy: dict[ArtifactReleasePolicy, set[UUID]] = defaultdict(set)
    policy_for: dict[Hashable, ArtifactReleasePolicy] = {}
    for query in queries:
        policy = _policy_as_of(revisions, _utc(query.held_uploaded_at))
        policy_for[query.key] = policy
        by_policy[policy].add(query.reference_agent_id)
    published_by_policy: dict[ArtifactReleasePolicy, dict[UUID, datetime]] = {}
    for policy, reference_ids in by_policy.items():
        published_by_policy[policy] = await list_public_source_releases(
            session,
            agent_ids=sorted(reference_ids, key=str),
            quorum=SCORING_QUORUM,
            policy=policy,
        )
    results: dict[Hashable, ReferencePublicRelease] = {}
    for query in queries:
        uploaded = _utc(query.held_uploaded_at)
        published = published_by_policy[policy_for[query.key]].get(
            query.reference_agent_id
        )
        published_at = _utc(published) if published is not None else None
        results[query.key] = ReferencePublicRelease(
            reference_agent_id=query.reference_agent_id,
            published_at=published_at,
            public_before_upload=(
                published_at is not None and published_at <= uploaded
            ),
            source="derived",
        )
    return results


async def derive_reference_public_release(
    session: AsyncSession,
    *,
    reference_agent_id: UUID,
    held_uploaded_at: datetime,
) -> ReferencePublicRelease:
    """Compute the fact for one hold from the release ledger. Read-only."""
    results = await derive_reference_public_releases(
        session,
        [
            ReferenceReleaseQuery(
                key=reference_agent_id,
                reference_agent_id=reference_agent_id,
                held_uploaded_at=held_uploaded_at,
            )
        ],
    )
    return results[reference_agent_id]
