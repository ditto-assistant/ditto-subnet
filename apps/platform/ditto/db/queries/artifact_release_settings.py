"""Effective public artifact-release settings."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.source_disclosure import SourceDisclosure
from ditto.db.models import ArtifactReleaseSettingsRevision

DEFAULT_ARTIFACT_RELEASE_EMBARGO_HOURS = 48
MIN_ARTIFACT_RELEASE_EMBARGO_HOURS = 6
# The ceiling is a range bound, not a recommendation: 48 hours stays the
# community-agreed operative default, and the extra headroom exists so an
# operator can hold the king's source private for a bounded stretch (a
# disclosure window, an unresolved dispute) without a code change. One year is
# the longest finite window anyone has proposed; past it the honest value is
# `SourceDisclosure.NEVER`, so the range and the terminal option meet with no
# gap between them.
MAX_ARTIFACT_RELEASE_EMBARGO_HOURS = 8760

DEFAULT_ARTIFACT_RELEASE_DISCLOSURE = SourceDisclosure.PUBLIC
"""What the subnet does absent any operator revision.

Public, because that is the status quo every existing submission was made
under. A default of ``never`` would retroactively withhold source that miners
submitted expecting the published release contract.
"""


@dataclass(frozen=True)
class ArtifactReleasePolicy:
    """The effective release policy: whether source is published, and how soon.

    One value, read together. A caller that took only the hours could not tell
    a 48-hour window from a permanent withholding, and every caller is a public
    read path -- so the two fields travel as a unit rather than as two lookups
    a future caller could half-perform.
    """

    disclosure: SourceDisclosure
    embargo_hours: int

    @property
    def releases_publicly(self) -> bool:
        return self.disclosure is SourceDisclosure.PUBLIC


DEFAULT_ARTIFACT_RELEASE_POLICY = ArtifactReleasePolicy(
    disclosure=DEFAULT_ARTIFACT_RELEASE_DISCLOSURE,
    embargo_hours=DEFAULT_ARTIFACT_RELEASE_EMBARGO_HOURS,
)


async def latest_artifact_release_settings(
    session: AsyncSession,
) -> ArtifactReleaseSettingsRevision | None:
    return await session.scalar(
        select(ArtifactReleaseSettingsRevision)
        .order_by(ArtifactReleaseSettingsRevision.revision.desc())
        .limit(1)
    )


async def artifact_release_policy(session: AsyncSession) -> ArtifactReleasePolicy:
    latest = await latest_artifact_release_settings(session)
    if latest is None:
        return DEFAULT_ARTIFACT_RELEASE_POLICY
    return ArtifactReleasePolicy(
        disclosure=SourceDisclosure(latest.disclosure),
        embargo_hours=latest.embargo_hours,
    )
