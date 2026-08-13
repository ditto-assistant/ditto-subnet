"""The publication predicate the anti-copy gate judges past uploads against.

Release on SN118 is **king-only** and its clock starts at on-chain weight
confirmation, not at upload. That distinction is the whole reason this query
exists rather than a ``created_at + embargo_hours`` expression at the call site:
of ~1600 submissions on the live subnet, 30 have ever been downloadable, and
their unlock times sit anywhere from hours to a week past upload depending on
when validators' revealed weights landed. A gate that assumed otherwise would
exempt copies of artifacts that were never published.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.source_disclosure import SourceDisclosure
from ditto.api_server.endpoints.public import _public_artifact_release
from ditto.db.models import Agent, AgentStatus, ArtifactReleaseSettingsRevision, Score
from ditto.db.queries.artifact_release import (
    list_first_score_quorums,
    list_public_source_releases,
)
from ditto.db.queries.artifact_release_settings import (
    DEFAULT_ARTIFACT_RELEASE_EMBARGO_HOURS,
    ArtifactReleasePolicy,
    artifact_release_policy_as_of,
)
from ditto.db.queries.king_reign import (
    get_king_reveal,
    record_first_crowned,
    record_weight_confirmed,
)

pytestmark = pytest.mark.asyncio

_QUORUM = 3
_PUBLIC_120H = ArtifactReleasePolicy(
    disclosure=SourceDisclosure.PUBLIC, embargo_hours=120
)
_NEVER = ArtifactReleasePolicy(disclosure=SourceDisclosure.NEVER, embargo_hours=120)
_UPLOADED = datetime(2026, 8, 4, 6, 21, 53, tzinfo=UTC)


async def _submission(
    session: AsyncSession,
    *,
    name: str,
    scores: int = _QUORUM,
    status: AgentStatus = AgentStatus.SCORED,
    crowned_at: datetime | None = None,
    weight_confirmed_at: datetime | None = None,
) -> UUID:
    agent_id = uuid4()
    session.add(
        Agent(
            agent_id=agent_id,
            miner_hotkey="5" + name[0].upper() * 47,
            name=name,
            sha256=name[0] * 64,
            size_bytes=524288,
            status=status,
            created_at=_UPLOADED,
        )
    )
    await session.flush()
    for index in range(scores):
        session.add(
            Score(
                agent_id=agent_id,
                validator_hotkey=f"validator-{index}",
                bench_version=8,
                run_id=f"{name}-{index}",
                signature="ab" * 64,
                seed=42,
                composite=0.9,
                tool_mean=0.9,
                memory_mean=0.9,
                median_ms=500,
                n=114,
                generated_at=_UPLOADED + timedelta(minutes=index),
                created_at=_UPLOADED + timedelta(minutes=index),
                updated_at=_UPLOADED + timedelta(minutes=index),
            )
        )
    await session.flush()
    if crowned_at is not None:
        await record_first_crowned(session, agent_id=agent_id, now=crowned_at)
    if weight_confirmed_at is not None:
        await record_weight_confirmed(
            session, agent_id=agent_id, now=weight_confirmed_at
        )
    await session.flush()
    return agent_id


async def test_window_runs_from_weight_confirmation_not_upload(
    session: AsyncSession,
) -> None:
    """The red-dragon v12 timings, which the exemption tests lean on.

    Uploaded 2026-08-04 06:21:53Z, weight-confirmed 11:46:31Z the same day, and
    served publicly from 2026-08-09 11:46:31Z under the 120-hour policy -- five
    hours and change later than ``upload + 120h`` would have said.
    """
    confirmed = datetime(2026, 8, 4, 11, 46, 31, tzinfo=UTC)
    agent_id = await _submission(
        session,
        name="red-dragon",
        crowned_at=datetime(2026, 8, 4, 8, 0, tzinfo=UTC),
        weight_confirmed_at=confirmed,
    )

    releases = await list_public_source_releases(
        session, agent_ids=[agent_id], quorum=_QUORUM, policy=_PUBLIC_120H
    )

    assert releases == {agent_id: datetime(2026, 8, 9, 11, 46, 31, tzinfo=UTC)}
    assert releases[agent_id] != _UPLOADED + timedelta(hours=120)


async def test_never_crowned_submission_is_never_published(
    session: AsyncSession,
) -> None:
    """The overwhelming majority. Source release is king-only."""
    agent_id = await _submission(session, name="commoner")

    assert (
        await list_public_source_releases(
            session, agent_ids=[agent_id], quorum=_QUORUM, policy=_PUBLIC_120H
        )
        == {}
    )


async def test_king_awaiting_on_chain_confirmation_is_not_published(
    session: AsyncSession,
) -> None:
    """Crowned is not enough: commit-reveal has to have landed."""
    agent_id = await _submission(
        session, name="king", crowned_at=datetime(2026, 8, 4, 8, 0, tzinfo=UTC)
    )

    assert (
        await list_public_source_releases(
            session, agent_ids=[agent_id], quorum=_QUORUM, policy=_PUBLIC_120H
        )
        == {}
    )


async def test_below_quorum_king_is_not_published(session: AsyncSession) -> None:
    agent_id = await _submission(
        session,
        name="quorumless",
        scores=2,
        crowned_at=datetime(2026, 8, 4, 8, 0, tzinfo=UTC),
        weight_confirmed_at=datetime(2026, 8, 4, 11, 46, 31, tzinfo=UTC),
    )

    assert (
        await list_public_source_releases(
            session, agent_ids=[agent_id], quorum=_QUORUM, policy=_PUBLIC_120H
        )
        == {}
    )


@pytest.mark.parametrize("status", [AgentStatus.BANNED, AgentStatus.ATH_PENDING_REVIEW])
async def test_non_serving_status_is_not_published(
    session: AsyncSession, status: AgentStatus
) -> None:
    """A banned or re-opened artifact stops being served, so it stops counting.

    Conservative on purpose: it may have been downloadable earlier, and reading
    the status as it stands now can only *withhold* an exemption -- i.e. keep a
    copy hold that a fuller history might have withdrawn -- never invent one.
    """
    agent_id = await _submission(
        session,
        name="pulled",
        status=status,
        crowned_at=datetime(2026, 8, 4, 8, 0, tzinfo=UTC),
        weight_confirmed_at=datetime(2026, 8, 4, 11, 46, 31, tzinfo=UTC),
    )

    assert (
        await list_public_source_releases(
            session, agent_ids=[agent_id], quorum=_QUORUM, policy=_PUBLIC_120H
        )
        == {}
    )


async def test_disclosure_never_publishes_nothing(session: AsyncSession) -> None:
    """Under ``never`` the gate gets an empty set and every copy rule fires."""
    agent_id = await _submission(
        session,
        name="withheld",
        crowned_at=datetime(2026, 8, 4, 8, 0, tzinfo=UTC),
        weight_confirmed_at=datetime(2026, 8, 4, 11, 46, 31, tzinfo=UTC),
    )

    assert (
        await list_public_source_releases(
            session, agent_ids=[agent_id], quorum=_QUORUM, policy=_NEVER
        )
        == {}
    )


async def test_agrees_with_the_public_route_projection(
    session: AsyncSession,
) -> None:
    """Drift guard: two implementations of "is this downloadable", one answer.

    ``_public_artifact_release`` decides what the unauthenticated route serves;
    this query decides what the anti-copy gate treats as already-public. If they
    disagree the gate either excuses copies of private artifacts or keeps
    holding miners for public ones -- so pin them to each other rather than to a
    hand-written expectation.
    """
    confirmed = datetime(2026, 8, 4, 11, 46, 31, tzinfo=UTC)
    agent_id = await _submission(
        session,
        name="king",
        crowned_at=datetime(2026, 8, 4, 8, 0, tzinfo=UTC),
        weight_confirmed_at=confirmed,
    )
    releases = await list_public_source_releases(
        session, agent_ids=[agent_id], quorum=_QUORUM, policy=_PUBLIC_120H
    )
    available_at = releases[agent_id]

    quorum = (
        await list_first_score_quorums(session, agent_ids=[agent_id], quorum=_QUORUM)
    )[agent_id]
    reveal = (await get_king_reveal(session, agent_ids=[agent_id]))[agent_id]

    def serves(now: datetime) -> bool:
        return _public_artifact_release(
            status=AgentStatus.SCORED,
            score_quorum=quorum,
            policy=_PUBLIC_120H,
            king_reveal=reveal,
            now=now,
        ).download_available

    assert serves(available_at) is True
    assert serves(available_at - timedelta(seconds=1)) is False


class TestPolicyAsOf:
    """Judging a past upload needs the window that was in force back then."""

    async def test_no_revision_yields_the_shipped_default(
        self, session: AsyncSession
    ) -> None:
        policy = await artifact_release_policy_as_of(
            session, at=datetime(2026, 7, 1, tzinfo=UTC)
        )
        assert policy.disclosure is SourceDisclosure.PUBLIC
        assert policy.embargo_hours == DEFAULT_ARTIFACT_RELEASE_EMBARGO_HOURS

    async def test_picks_the_revision_in_force_at_the_timestamp(
        self, session: AsyncSession
    ) -> None:
        """A revision governs uploads after it, and only after it.

        This is the property that stops a policy change from retroactively
        rewriting what miners could download. Revisions are appended on top of
        whatever the migration chain already seeded, so the assertions are about
        the boundary between two known revisions rather than absolute numbers.
        """
        head = await session.scalar(
            select(func.max(ArtifactReleaseSettingsRevision.revision))
        )
        first = int(head or 0) + 1
        short_at = datetime(2026, 9, 1, tzinfo=UTC)
        long_at = datetime(2026, 9, 8, tzinfo=UTC)
        for revision, hours, created_at in (
            (first, 24, short_at),
            (first + 1, 240, long_at),
        ):
            session.add(
                ArtifactReleaseSettingsRevision(
                    revision=revision,
                    parent_revision=revision - 1,
                    embargo_hours=hours,
                    disclosure=SourceDisclosure.PUBLIC.value,
                    reason=f"revision {revision}",
                    actor="test",
                    created_at=created_at,
                )
            )
        await session.flush()

        before_any = await artifact_release_policy_as_of(
            session, at=datetime(2026, 7, 1, tzinfo=UTC)
        )
        under_short = await artifact_release_policy_as_of(
            session, at=long_at - timedelta(seconds=1)
        )
        under_long = await artifact_release_policy_as_of(session, at=long_at)

        assert before_any.embargo_hours == DEFAULT_ARTIFACT_RELEASE_EMBARGO_HOURS
        assert under_short.embargo_hours == 24
        assert under_long.embargo_hours == 240
