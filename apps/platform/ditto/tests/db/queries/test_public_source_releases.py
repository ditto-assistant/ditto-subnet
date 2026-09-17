"""Publication predicates require completed emissions before the embargo starts."""

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
    available_public_source_agent_ids,
    list_first_score_quorums,
    list_public_source_releases,
)
from ditto.db.queries.artifact_release_settings import (
    DEFAULT_ARTIFACT_RELEASE_EMBARGO_HOURS,
    ArtifactReleasePolicy,
    artifact_release_policy_as_of,
)
from ditto.db.queries.king_reign import (
    KingEmissionProof,
    get_king_reveal,
    record_emission_confirmed,
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
    emission_confirmed_at: datetime | None = None,
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
    if emission_confirmed_at is not None:
        await record_emission_confirmed(
            session,
            agent_id=agent_id,
            proof=KingEmissionProof(emission_confirmed_at, 100, "0xabc", 1, "digest"),
        )
    await session.flush()
    return agent_id


async def test_window_runs_from_emission_confirmation_not_upload(
    session: AsyncSession,
) -> None:
    """Payout-block time, rather than upload or weights, anchors the embargo."""
    confirmed = datetime(2026, 8, 4, 11, 46, 31, tzinfo=UTC)
    agent_id = await _submission(
        session,
        name="red-dragon",
        crowned_at=datetime(2026, 8, 4, 8, 0, tzinfo=UTC),
        emission_confirmed_at=confirmed,
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
    """Crowned is not enough: completed winner earnings must be proven."""
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
        emission_confirmed_at=datetime(2026, 8, 4, 11, 46, 31, tzinfo=UTC),
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
        emission_confirmed_at=datetime(2026, 8, 4, 11, 46, 31, tzinfo=UTC),
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
        emission_confirmed_at=datetime(2026, 8, 4, 11, 46, 31, tzinfo=UTC),
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
        emission_confirmed_at=confirmed,
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
    assert agent_id in await available_public_source_agent_ids(
        session, quorum=_QUORUM, policy=_PUBLIC_120H, now=available_at
    )
    assert agent_id not in await available_public_source_agent_ids(
        session,
        quorum=_QUORUM,
        policy=_PUBLIC_120H,
        now=available_at - timedelta(seconds=1),
    )


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


async def test_legacy_weights_do_not_release_or_enter_available_index(
    session: AsyncSession,
) -> None:
    confirmed = _UPLOADED + timedelta(hours=2)
    agent_id = await _submission(
        session,
        name="legacy",
        crowned_at=_UPLOADED,
        weight_confirmed_at=confirmed,
    )
    assert (
        await list_public_source_releases(
            session, agent_ids=[agent_id], quorum=_QUORUM, policy=_PUBLIC_120H
        )
        == {}
    )
    assert agent_id not in await available_public_source_agent_ids(
        session,
        quorum=_QUORUM,
        policy=_PUBLIC_120H,
        now=confirmed + timedelta(days=100),
    )
    reveal = (await get_king_reveal(session, agent_ids=[agent_id]))[agent_id]
    assert reveal.weight_confirmed_at == confirmed
    assert reveal.emission_confirmed_at is None
    quorum = (
        await list_first_score_quorums(
            session,
            agent_ids=[agent_id],
            quorum=_QUORUM,
        )
    )[agent_id]
    release = _public_artifact_release(
        status=AgentStatus.SCORED,
        score_quorum=quorum,
        policy=_PUBLIC_120H,
        king_reveal=reveal,
        now=confirmed + timedelta(days=100),
    )
    assert release.download_available is False
    assert release.available_at is None
    assert release.status == "embargoed"


@pytest.mark.parametrize("policy", [_PUBLIC_120H, _NEVER])
async def test_actual_publication_survives_policy_pause_and_agent_suspension(
    session: AsyncSession,
    policy: ArtifactReleasePolicy,
) -> None:
    from ditto.db.models import ArtifactFetchAudit
    from ditto.db.queries.artifact_fetch_audit import ENDPOINT_PUBLIC_ARTIFACT

    agent_id = await _submission(session, name="published", status=AgentStatus.BANNED)
    first_fetch = _UPLOADED + timedelta(hours=1)
    for offset in (0, 2):
        session.add(
            ArtifactFetchAudit(
                agent_id=agent_id,
                endpoint=ENDPOINT_PUBLIC_ARTIFACT,
                requester_kind="public",
                artifact_sha256="p" * 64,
                fetched_at=first_fetch + timedelta(hours=offset),
            )
        )
    await session.flush()
    assert await list_public_source_releases(
        session,
        agent_ids=[agent_id],
        quorum=_QUORUM,
        policy=policy,
    ) == {agent_id: first_fetch}
    # Historical publication must never reopen today's download index.
    assert agent_id not in await available_public_source_agent_ids(
        session,
        quorum=_QUORUM,
        policy=policy,
        now=first_fetch + timedelta(days=100),
    )


@pytest.mark.parametrize(
    "endpoint,kind,sha",
    [
        ("admin.get_screening_artifact", "admin", "p" * 64),
        ("validator.agent_artifact", "validator", "p" * 64),
        ("screener.agent_artifact", "screener", "p" * 64),
        ("public.agent_artifact", "admin", "p" * 64),
        ("public.agent_artifact", "public", "wrong" * 16),
        ("public.agent_artifact", "public", None),
    ],
)
async def test_private_or_mismatched_fetch_is_not_publication(
    session: AsyncSession,
    endpoint: str,
    kind: str,
    sha: str | None,
) -> None:
    from ditto.db.models import ArtifactFetchAudit

    agent_id = await _submission(session, name="private")
    session.add(
        ArtifactFetchAudit(
            agent_id=agent_id,
            endpoint=endpoint,
            requester_kind=kind,
            requester_id=None if kind == "public" else "reader",
            artifact_sha256=sha,
            fetched_at=_UPLOADED,
        )
    )
    await session.flush()
    assert (
        await list_public_source_releases(
            session,
            agent_ids=[agent_id],
            quorum=_QUORUM,
            policy=_NEVER,
        )
        == {}
    )


async def test_copy_before_actual_public_fetch_is_not_exempt(
    session: AsyncSession,
) -> None:
    from ditto.api_server.scoring_gate import (
        PublicSourceRelease,
        evaluate_duplicate_signals,
    )
    from ditto.db.models import ArtifactFetchAudit
    from ditto.db.queries.scores import LedgerRow

    agent_id = await _submission(session, name="published")
    fetched_at = _UPLOADED + timedelta(hours=2)
    session.add(
        ArtifactFetchAudit(
            agent_id=agent_id,
            endpoint="public.agent_artifact",
            requester_kind="public",
            artifact_sha256="p" * 64,
            fetched_at=fetched_at,
        )
    )
    await session.flush()
    releases = await list_public_source_releases(
        session,
        agent_ids=[agent_id],
        quorum=_QUORUM,
        policy=_NEVER,
    )
    sketch = {"v": 1, "k": 256, "card": 20, "m": [f"{i:016x}" for i in range(20)]}
    reference = LedgerRow(
        agent_id=agent_id,
        miner_hotkey="original",
        composite=0.8,
        tool_mean=0.8,
        memory_mean=0.8,
        first_seen=_UPLOADED,
        sha256="p" * 64,
        size_bytes=500000,
        run_id="run",
        seed=42,
        validator_hotkey="validator",
        signature="ab" * 64,
        status=AgentStatus.SCORED,
        content_fingerprint=sketch,
    )
    for submitted_at, expected_hold in (
        (fetched_at - timedelta(seconds=1), True),
        (fetched_at, False),
    ):
        decision = evaluate_duplicate_signals(
            agent_id=uuid4(),
            miner_hotkey="builder",
            sha256="b" * 64,
            composite=0.81,
            size_bytes=520000,
            content_fingerprint=sketch,
            eligible=[reference],
            submitted_at=submitted_at,
            public_source_releases=[
                PublicSourceRelease(k, v) for k, v in releases.items()
            ],
        )
        assert decision.held is expected_hold
