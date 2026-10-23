"""Real-Postgres coverage for the confirmation-lane failure taxonomy.

The premise of this module is itself asserted here: the ordinary chat/embedding
taxonomy reads ``inference_requests`` and cannot see a confirmation failure at
all, which is how LongMem lost 83% of its bundles to provider rejections while
``get_inference_failure_taxonomy`` reported ``rate_limited_failures: 0``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.db.queries.confirmation_failure_taxonomy import (
    CONFIRMATION_FAILURE_WINDOWS_SECONDS,
    load_confirmation_failure_taxonomy_rows,
)
from ditto.db.queries.confirmation_inference import (
    begin_confirmation_inference_request,
    ensure_confirmation_inference_grants,
    finish_confirmation_inference_request,
)
from ditto.db.queries.inference_failure_taxonomy import (
    load_inference_failure_taxonomy_rows,
)
from ditto.tests.confirmation_evidence_fixtures import verification_profile
from ditto.tests.db.queries.test_confirmation_bundles import (
    reserve_and_issue,
    seed_bundle,
)

pytestmark = pytest.mark.asyncio

_JUDGE_MODEL = "openai/gpt-4o-2024-08-06"

# The bundle/ticket fixtures are built around a fixed clock, so grants only mint
# when `now` sits inside the ticket window. The taxonomy, by contrast, is
# relative to the database clock. Rather than add a test-only clock seam to the
# production SQL, rows are minted at the fixture clock and then moved into the
# window under test -- the aggregation is what is being tested, not the clock.
_FIXTURE_NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


async def _move_requests_into_window(session: AsyncSession) -> None:
    await session.execute(
        text(
            """
            UPDATE confirmation_inference_requests
               SET started_at   = now() - interval '5 minutes',
                   completed_at = CASE
                                    WHEN completed_at IS NULL THEN NULL
                                    ELSE now() - interval '5 minutes'
                                         + make_interval(secs => EXTRACT(
                                             EPOCH FROM (completed_at - started_at)))
                                  END
            """
        )
    )


async def _one_call(
    session: AsyncSession,
    *,
    grant,
    bearer: str,
    now: datetime,
    status: str,
    upstream_provider: str | None,
    cost_microusd: int,
) -> None:
    nonce = uuid4()
    begun = await begin_confirmation_inference_request(
        session,
        grant_id=grant.grant_id,
        nonce=nonce,
        bearer=bearer,
        model=grant.model,
        token_reservation=64,
        max_chargeable_tokens=256,
        now=now,
    )
    begun_grant, _request = begun
    await finish_confirmation_inference_request(
        session,
        grant_id=grant.grant_id,
        nonce=nonce,
        generation=begun_grant.generation,
        status=status,
        prompt_tokens=0 if status == "failed" else 155,
        completion_tokens=0 if status == "failed" else 2,
        cost_microusd=cost_microusd,
        upstream_provider=upstream_provider,
        now=now + timedelta(milliseconds=177 if status == "failed" else 1244),
    )


async def _seed_reader_success_and_judge_rejection(
    session: AsyncSession,
) -> datetime:
    """Reproduce bundle 05e1ebab: reader serves, judge dies pre-provider."""
    now = _FIXTURE_NOW
    _, revision, settings, bundle = await seed_bundle(session)
    _, ticket = await reserve_and_issue(
        session, bundle=bundle, revision=revision, policy=settings
    )
    offers = await ensure_confirmation_inference_grants(
        session,
        ticket=ticket,
        broker_public_key="A" * 43,
        profile=verification_profile(),
        now=now,
    )
    by_lane = {grant.lane: (grant, bearer) for grant, bearer in offers}
    reader_grant, reader_bearer = by_lane["reader"]
    judge_grant, judge_bearer = by_lane["judge"]
    await _one_call(
        session,
        grant=reader_grant,
        bearer=reader_bearer,
        now=now,
        status="completed",
        upstream_provider="Amazon Bedrock",
        cost_microusd=687,
    )
    # The shape that ends a bundle: nothing selected, nothing billed.
    await _one_call(
        session,
        grant=judge_grant,
        bearer=judge_bearer,
        now=now,
        status="failed",
        upstream_provider=None,
        cost_microusd=0,
    )
    await _move_requests_into_window(session)
    return now


async def test_confirmation_taxonomy_separates_the_lane_that_died(
    session: AsyncSession,
) -> None:
    async with session.begin():
        await _seed_reader_success_and_judge_rejection(session)

    async with session.begin():
        lanes, groups = await load_confirmation_failure_taxonomy_rows(session)

    by_key = {(int(r["window_seconds"]), str(r["lane"])): r for r in lanes}
    assert len(by_key) == 3 * len(CONFIRMATION_FAILURE_WINDOWS_SECONDS)

    hour = CONFIRMATION_FAILURE_WINDOWS_SECONDS[0]
    judge = by_key[(hour, "judge")]
    assert int(judge["calls"]) == 1
    assert int(judge["failed"]) == 1
    assert int(judge["completed"]) == 0
    # Both are what distinguish a pre-provider rejection from a billed failure.
    assert int(judge["failed_without_provider"]) == 1
    assert int(judge["failed_without_cost"]) == 1
    assert int(judge["bundles_with_failure"]) == 1

    reader = by_key[(hour, "reader")]
    assert int(reader["calls"]) == 1
    assert int(reader["completed"]) == 1
    assert int(reader["failed"]) == 0
    assert int(reader["bundles_with_failure"]) == 0

    # An untouched lane must report zero, not the one all-NULL row a LEFT JOIN
    # counts when the totals use a bare count(*).
    embedding = by_key[(hour, "embedding")]
    assert int(embedding["calls"]) == 0
    assert int(embedding["failed"]) == 0

    judge_groups = [
        g
        for g in groups
        if int(g["window_seconds"]) == hour and str(g["lane"]) == "judge"
    ]
    assert judge_groups, "the failing judge call produced no group row"
    failed = [g for g in judge_groups if str(g["status"]) == "failed"]
    assert len(failed) == 1
    assert failed[0]["upstream_provider"] is None
    assert str(failed[0]["provider_basis"]) == "unknown"

    reader_groups = [
        g
        for g in groups
        if int(g["window_seconds"]) == hour and str(g["lane"]) == "reader"
    ]
    assert len(reader_groups) == 1
    assert str(reader_groups[0]["upstream_provider"]) == "Amazon Bedrock"
    assert str(reader_groups[0]["provider_basis"]) == "confirmed_selected"
    assert int(failed[0]["bundles_affected"]) == 1


async def test_ordinary_taxonomy_cannot_see_a_confirmation_failure(
    session: AsyncSession,
) -> None:
    """The gap this module exists to close, asserted rather than asserted-about.

    If this ever fails because the ordinary taxonomy grew confirmation
    coverage, delete this module instead of widening it.
    """
    async with session.begin():
        await _seed_reader_success_and_judge_rejection(session)

    async with session.begin():
        lanes, groups = await load_inference_failure_taxonomy_rows(session)

    assert {str(row["request_kind"]) for row in lanes} == {"chat", "embedding"}
    assert all(str(group["model"]) != _JUDGE_MODEL for group in groups)
    assert all(int(row["failed"]) == 0 for row in lanes)


async def test_a_still_started_request_is_in_flight_and_not_settled(
    session: AsyncSession,
) -> None:
    """An open request must not be counted as settled.

    `confirmation_inference_requests.status` is CHECK-constrained to
    ('started', 'completed', 'failed', 'canceled') -- there is no 'active'.
    The first version of this query spelled in-flight as `status = 'active'`,
    which no row can ever satisfy, so `in_flight` was structurally zero and
    every open request was silently counted as settled instead.
    """
    async with session.begin():
        now = _FIXTURE_NOW
        _, revision, settings, bundle = await seed_bundle(session)
        _, ticket = await reserve_and_issue(
            session, bundle=bundle, revision=revision, policy=settings
        )
        offers = await ensure_confirmation_inference_grants(
            session,
            ticket=ticket,
            broker_public_key="A" * 43,
            profile=verification_profile(),
            now=now,
        )
        by_lane = {grant.lane: (grant, bearer) for grant, bearer in offers}
        judge_grant, judge_bearer = by_lane["judge"]
        # Begin without finishing: this is the in-flight shape.
        await begin_confirmation_inference_request(
            session,
            grant_id=judge_grant.grant_id,
            nonce=uuid4(),
            bearer=judge_bearer,
            model=judge_grant.model,
            token_reservation=64,
            max_chargeable_tokens=256,
            now=now,
        )
        await _move_requests_into_window(session)

    async with session.begin():
        lanes, groups = await load_confirmation_failure_taxonomy_rows(session)

    hour = CONFIRMATION_FAILURE_WINDOWS_SECONDS[0]
    judge = {
        (int(r["window_seconds"]), str(r["lane"])): r for r in lanes
    }[(hour, "judge")]

    assert int(judge["calls"]) == 1
    assert int(judge["in_flight"]) == 1, "an open request must count as in flight"
    assert int(judge["settled"]) == 0, "an open request must not count as settled"
    assert int(judge["completed"]) == 0
    assert int(judge["failed"]) == 0
    # It is not a failure, so it must not reach any failure-derived counter.
    assert int(judge["failed_without_provider"]) == 0
    assert int(judge["bundles_with_failure"]) == 0
    # Groups deliberately break out every status, so the open request appears
    # with status 'started'. What must not exist is a 'failed' judge group.
    judge_groups = [
        g
        for g in groups
        if int(g["window_seconds"]) == hour and str(g["lane"]) == "judge"
    ]
    assert [str(g["status"]) for g in judge_groups] == ["started"]
    # duration_ms is NULL until a request completes; the group COALESCEs that
    # to 0, which is only readable because `status` sits beside it.
    assert int(judge_groups[0]["max_duration_ms"]) == 0
