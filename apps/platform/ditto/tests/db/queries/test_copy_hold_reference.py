"""Pure helpers behind the copy-hold reference-release fact (SN-499)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from ditto.api_models.source_disclosure import SourceDisclosure
from ditto.db.queries.artifact_release_settings import (
    DEFAULT_ARTIFACT_RELEASE_POLICY,
    ArtifactReleasePolicy,
)
from ditto.db.queries.copy_hold_reference import (
    _policy_as_of,
    reference_release_from_provenance,
)

_T0 = datetime(2026, 11, 1, tzinfo=UTC)


def test_policy_as_of_matches_the_sql_lookup() -> None:
    """Highest revision created at or before the upload; default before any."""
    revisions = [
        (1, _T0, "public", 48),
        (2, _T0 + timedelta(days=2), "public", 24),
        # Created later but numbered lower never happens in practice; the SQL
        # orders by revision, so the in-memory form must too.
        (3, _T0 + timedelta(days=1), "never", 72),
    ]
    assert _policy_as_of(revisions, _T0 - timedelta(hours=1)) == (
        DEFAULT_ARTIFACT_RELEASE_POLICY
    )
    assert _policy_as_of(revisions, _T0 + timedelta(hours=1)) == (
        ArtifactReleasePolicy(disclosure=SourceDisclosure.PUBLIC, embargo_hours=48)
    )
    assert _policy_as_of(revisions, _T0 + timedelta(days=3)) == (
        ArtifactReleasePolicy(disclosure=SourceDisclosure.NEVER, embargo_hours=72)
    )


def test_recorded_fact_round_trips_and_rejects_junk() -> None:
    reference = uuid4()
    fact = reference_release_from_provenance(
        {
            "reference_public_release": {
                "reference_agent_id": str(reference),
                "published_at": _T0.isoformat(),
                "public_before_upload": True,
            }
        }
    )
    assert fact is not None
    assert fact.reference_agent_id == reference
    assert fact.published_at == _T0
    assert fact.source == "recorded"
    assert fact.as_provenance()["public_before_upload"] is True
    assert reference_release_from_provenance({}) is None
    assert (
        reference_release_from_provenance(
            {"reference_public_release": {"reference_agent_id": "nope"}}
        )
        is None
    )
