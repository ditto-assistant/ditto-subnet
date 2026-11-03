"""Operator per-owner submission cooldown overrides, on real Postgres.

Every key here is a made-up base58 string; no real miner identity belongs in
tests.
"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_server.dependencies import get_session
from ditto.api_server.upload_feedback import submission_cooldown_message
from ditto.db.models import (
    Agent,
    AgentStatus,
    EvaluationPayment,
    OwnerSubmissionCooldownRevision,
)
from ditto.db.queries.agents import (
    SubmissionCooldownError,
    submission_cooldown_decision,
)
from ditto.db.queries.owner_submission_cooldown import OwnerCooldownNotice
from ditto.db.queries.submission_settings import (
    EffectiveSubmissionSettings,
    consume_or_enforce_upload_admission,
    reserve_upload_admission,
)

pytestmark = pytest.mark.asyncio

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}
_URL = "/api/v1/admin/submission-settings/owner-cooldowns"
_HOTKEY = "5FakeHotkeyAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
_COLDKEY = "5FakeCoLdkeyBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
_FRESH_COLDKEY = "5FakeFreshCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
_OTHER_HOTKEY = "5FakeOtherDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD"
_PAYMENT_ADDRESS = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
_REASON = "repeated copy rejects; slow this owner down"


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_ADMIN_TOKEN)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


def _set(
    *,
    scope: str = "hotkey",
    key: str = _HOTKEY,
    seconds: int = 86400,
    days: int = 14,
    expected: int = 0,
    confirmation: str | None = None,
) -> dict[str, object]:
    return {
        "scope": scope,
        "owner_key": key,
        "cooldown_seconds": seconds,
        "expires_in_days": days,
        "expected_revision": expected,
        "reason": _REASON,
        "actor": "operator@example.com",
        "confirmation": confirmation
        or (
            f"SET OWNER SUBMISSION COOLDOWN {scope.upper()} {key} "
            f"{seconds} SECONDS FOR {days} DAYS"
        ),
    }


async def _seed_submission(
    session: AsyncSession,
    *,
    hotkey: str,
    coldkey: str,
    created_at: datetime,
) -> None:
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey=hotkey,
        name=f"agent-{uuid4().hex[:8]}",
        sha256=uuid4().hex * 2,
        status=AgentStatus.UPLOADED,
        created_at=created_at,
    )
    async with session.begin():
        session.add(agent)
        await session.flush()
        session.add(
            EvaluationPayment(
                block_hash="0x" + agent.agent_id.hex,
                extrinsic_index=0,
                agent_id=agent.agent_id,
                miner_hotkey=hotkey,
                miner_coldkey=coldkey,
                amount_rao=1,
                dest_address=_PAYMENT_ADDRESS,
                timestamp=created_at,
            )
        )


async def _add_override(
    session: AsyncSession,
    *,
    scope: str,
    key: str,
    seconds: int = 86400,
    expires_at: datetime,
    revision: int = 1,
    created_at: datetime | None = None,
) -> None:
    async with session.begin():
        session.add(
            OwnerSubmissionCooldownRevision(
                scope=scope,
                owner_key=key,
                revision=revision,
                action="set",
                cooldown_seconds=seconds,
                expires_at=expires_at,
                reason=_REASON,
                actor="test",
                created_at=created_at or datetime.now(UTC) - timedelta(days=1),
            )
        )


async def test_set_read_and_revoke_are_audited_with_cas(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    empty = await client.get(_URL, headers=_HEADERS)
    assert empty.status_code == 200, empty.text
    assert empty.json()["active_count"] == 0

    wrong = await client.post(
        _URL, headers=_HEADERS, json=_set(confirmation="SET OWNER SUBMISSION COOLDOWN")
    )
    assert wrong.status_code == 409
    assert "confirmation must be exactly" in wrong.text

    stale = await client.post(_URL, headers=_HEADERS, json=_set(expected=3))
    assert stale.status_code == 409
    assert "expected revision 3, current 0" in stale.text

    created = await client.post(_URL, headers=_HEADERS, json=_set())
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["current"]["revision"] == 1
    assert body["current"]["state"] == "active"
    assert body["current"]["cooldown_seconds"] == 86400
    assert body["current"]["actor"] == "operator@example.com"
    expires_at = datetime.fromisoformat(body["current"]["expires_at"])
    assert (
        timedelta(days=13, hours=23)
        < expires_at - datetime.now(UTC)
        <= timedelta(days=14)
    )

    replay = await client.post(_URL, headers=_HEADERS, json=_set())
    assert replay.status_code == 409

    listed = await client.get(
        _URL, headers=_HEADERS, params={"scope": "hotkey", "owner_key": _HOTKEY}
    )
    assert listed.status_code == 200
    assert listed.json()["active_count"] == 1
    assert listed.json()["lookup"]["current"]["revision"] == 1
    assert listed.json()["lookup"]["global_cooldown_seconds"] == 3600

    revoke_body = {
        "scope": "hotkey",
        "owner_key": _HOTKEY,
        "expected_revision": 1,
        "reason": "operator lifted the cooldown early",
        "actor": "operator@example.com",
        "confirmation": f"REVOKE OWNER SUBMISSION COOLDOWN HOTKEY {_HOTKEY}",
    }
    revoked = await client.post(f"{_URL}/revoke", headers=_HEADERS, json=revoke_body)
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["current"]["state"] == "revoked"
    assert [row["revision"] for row in revoked.json()["history"]] == [2, 1]

    again = await client.post(
        f"{_URL}/revoke", headers=_HEADERS, json={**revoke_body, "expected_revision": 2}
    )
    assert again.status_code == 409

    after = await client.get(_URL, headers=_HEADERS)
    assert after.json()["active_count"] == 0
    assert after.json()["items"] == []
    inactive = await client.get(
        _URL, headers=_HEADERS, params={"include_inactive": "true"}
    )
    assert [row["state"] for row in inactive.json()["items"]] == ["revoked"]


async def test_rejects_unsafe_bounds_and_half_lookups(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    for payload in (
        _set(seconds=30),
        _set(seconds=31 * 86400),
        _set(days=0),
        _set(days=91),
        _set(scope="owner"),
        _set(key="not a key"),
    ):
        response = await client.post(_URL, headers=_HEADERS, json=payload)
        assert response.status_code == 422, payload
    half = await client.get(_URL, headers=_HEADERS, params={"scope": "hotkey"})
    assert half.status_code == 422
    unauthenticated = await client.get(_URL)
    assert unauthenticated.status_code in (401, 403)


async def test_hotkey_override_holds_a_fresh_payment_coldkey(
    session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    await _seed_submission(
        session, hotkey=_HOTKEY, coldkey=_COLDKEY, created_at=now - timedelta(hours=2)
    )
    await _add_override(
        session, scope="hotkey", key=_HOTKEY, expires_at=now + timedelta(days=14)
    )
    # A fresh coldkey has no global cooldown, but the hotkey override holds it
    # for 24h from the hotkey's last submission.
    async with session.begin():
        decision = await submission_cooldown_decision(
            session,
            miner_coldkey=_FRESH_COLDKEY,
            miner_hotkey=_HOTKEY,
            cooldown=timedelta(hours=1),
            now=now,
        )
    assert decision is not None
    assert decision.owner_override is not None
    assert decision.owner_override.cooldown_seconds == 86400
    assert abs(decision.retry_at - (now + timedelta(hours=22))) < timedelta(seconds=1)

    # Another hotkey on another coldkey is untouched.
    async with session.begin():
        assert (
            await submission_cooldown_decision(
                session,
                miner_coldkey=_FRESH_COLDKEY,
                miner_hotkey=_OTHER_HOTKEY,
                cooldown=timedelta(hours=1),
                now=now,
            )
            is None
        )


async def test_coldkey_override_holds_a_rotated_hotkey_and_blocks_admission(
    session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    await _seed_submission(
        session, hotkey=_HOTKEY, coldkey=_COLDKEY, created_at=now - timedelta(hours=3)
    )
    await _add_override(
        session, scope="coldkey", key=_COLDKEY, expires_at=now + timedelta(days=14)
    )
    settings = EffectiveSubmissionSettings(
        revision=1, cooldown_seconds=3600, payment_address=_PAYMENT_ADDRESS
    )
    with pytest.raises(SubmissionCooldownError) as reserved:
        async with session.begin():
            await reserve_upload_admission(
                session,
                miner_coldkey=_COLDKEY,
                miner_hotkey=_OTHER_HOTKEY,
                sha256="a" * 64,
                settings=settings,
                now=now,
            )
    assert reserved.value.owner_override is not None
    with pytest.raises(SubmissionCooldownError) as legacy:
        async with session.begin():
            await consume_or_enforce_upload_admission(
                session,
                miner_coldkey=_COLDKEY,
                miner_hotkey=_OTHER_HOTKEY,
                sha256="a" * 64,
                admission_token=None,
                settings=settings,
                now=now,
            )
    assert legacy.value.retry_at > now + timedelta(hours=20)
    message = submission_cooldown_message(
        legacy.value.retry_at, now=now, owner_override=legacy.value.owner_override
    )
    assert message.startswith(
        "an operator submission cooldown applies to this owner: one submission "
        "per 1 day until "
    )
    assert _REASON not in message


async def test_override_never_outlives_its_expiry_or_shortens_global(
    session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    await _seed_submission(
        session,
        hotkey=_HOTKEY,
        coldkey=_COLDKEY,
        created_at=now - timedelta(minutes=10),
    )
    # Expires in one hour, so a 24h interval is capped at the expiry.
    await _add_override(
        session, scope="hotkey", key=_HOTKEY, expires_at=now + timedelta(hours=1)
    )
    async with session.begin():
        decision = await submission_cooldown_decision(
            session,
            miner_coldkey=_COLDKEY,
            miner_hotkey=_HOTKEY,
            cooldown=timedelta(hours=1),
            now=now,
        )
    assert decision is not None
    assert decision.retry_at <= now + timedelta(hours=1)

    # A short override never relaxes the global cooldown.
    await _add_override(
        session,
        scope="coldkey",
        key=_COLDKEY,
        seconds=60,
        expires_at=now + timedelta(days=1),
    )
    async with session.begin():
        decision = await submission_cooldown_decision(
            session,
            miner_coldkey=_COLDKEY,
            miner_hotkey=_HOTKEY,
            cooldown=timedelta(hours=2),
            now=now,
        )
    assert decision is not None
    assert decision.owner_override is None
    assert abs(decision.retry_at - (now + timedelta(minutes=110))) < timedelta(
        seconds=1
    )


async def test_expired_override_is_inert_and_revisions_are_append_only(
    session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    await _seed_submission(
        session,
        hotkey=_HOTKEY,
        coldkey=_COLDKEY,
        created_at=now - timedelta(hours=2),
    )
    await _add_override(
        session,
        scope="hotkey",
        key=_HOTKEY,
        expires_at=now - timedelta(minutes=1),
        created_at=now - timedelta(days=2),
    )
    async with session.begin():
        assert (
            await submission_cooldown_decision(
                session,
                miner_coldkey=_COLDKEY,
                miner_hotkey=_HOTKEY,
                cooldown=timedelta(hours=1),
                now=now,
            )
            is None
        )
    rows = list(await session.scalars(select(OwnerSubmissionCooldownRevision)))
    assert len(rows) == 1


async def test_hotkey_override_serializes_parallel_reservations_across_coldkeys(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    await _seed_submission(
        session, hotkey=_HOTKEY, coldkey=_COLDKEY, created_at=now - timedelta(days=2)
    )
    await _add_override(
        session, scope="hotkey", key=_HOTKEY, expires_at=now + timedelta(days=14)
    )
    settings = EffectiveSubmissionSettings(
        revision=1, cooldown_seconds=3600, payment_address=_PAYMENT_ADDRESS
    )
    first_holds = asyncio.Event()
    release_first = asyncio.Event()

    async def first() -> None:
        async with session_maker() as one, one.begin():
            await reserve_upload_admission(
                one,
                miner_coldkey=_COLDKEY,
                miner_hotkey=_HOTKEY,
                sha256="a" * 64,
                settings=settings,
                now=now,
            )
            first_holds.set()
            await release_first.wait()

    async def second() -> None:
        await first_holds.wait()
        async with session_maker() as two, two.begin():
            await reserve_upload_admission(
                two,
                miner_coldkey=_FRESH_COLDKEY,
                miner_hotkey=_HOTKEY,
                sha256="b" * 64,
                settings=settings,
                now=now,
            )

    first_task = asyncio.create_task(first())
    second_task = asyncio.create_task(second())
    await first_holds.wait()
    await asyncio.sleep(0.3)
    # The second reservation waits on the hotkey lock the first still holds.
    assert not second_task.done()
    release_first.set()
    await first_task
    with pytest.raises(SubmissionCooldownError) as blocked:
        await second_task
    assert blocked.value.owner_override is not None
    assert blocked.value.retry_at > now + timedelta(hours=23)


async def test_lookup_measures_from_linked_identities(
    app: FastAPI,
    client: httpx.AsyncClient,
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    now = datetime.now(UTC)
    await _seed_submission(
        session, hotkey=_HOTKEY, coldkey=_COLDKEY, created_at=now - timedelta(hours=5)
    )
    # A newer submission from another hotkey, paid by the same coldkey.
    await _seed_submission(
        session,
        hotkey=_OTHER_HOTKEY,
        coldkey=_COLDKEY,
        created_at=now - timedelta(hours=1),
    )
    created = await client.post(_URL, headers=_HEADERS, json=_set())
    assert created.status_code == 200, created.text
    lookup = created.json()
    latest = datetime.fromisoformat(lookup["latest_submission_at"])
    assert abs(latest - (now - timedelta(hours=1))) < timedelta(seconds=1)
    async with session.begin():
        decision = await submission_cooldown_decision(
            session,
            miner_coldkey=_COLDKEY,
            miner_hotkey=_HOTKEY,
            cooldown=timedelta(hours=1),
            now=now,
        )
    assert decision is not None
    assert abs(
        datetime.fromisoformat(lookup["next_allowed_at"]) - decision.retry_at
    ) < timedelta(seconds=1)


async def test_lookup_counts_a_live_reservation_on_the_hotkey(
    app: FastAPI,
    client: httpx.AsyncClient,
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    now = datetime.now(UTC)
    await _seed_submission(
        session, hotkey=_HOTKEY, coldkey=_COLDKEY, created_at=now - timedelta(days=3)
    )
    settings = EffectiveSubmissionSettings(
        revision=1, cooldown_seconds=3600, payment_address=_PAYMENT_ADDRESS
    )
    async with session.begin():
        await reserve_upload_admission(
            session,
            miner_coldkey=_FRESH_COLDKEY,
            miner_hotkey=_HOTKEY,
            sha256="c" * 64,
            settings=settings,
            now=now,
        )
    created = await client.post(_URL, headers=_HEADERS, json=_set())
    assert created.status_code == 200, created.text
    latest = datetime.fromisoformat(created.json()["latest_submission_at"])
    assert abs(latest - now) < timedelta(seconds=1)
    next_allowed = datetime.fromisoformat(created.json()["next_allowed_at"])
    assert next_allowed > now + timedelta(hours=23)


async def test_listing_is_bounded_and_filters_active_in_the_query(
    app: FastAPI,
    client: httpx.AsyncClient,
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    now = datetime.now(UTC)
    await _add_override(
        session, scope="hotkey", key=_HOTKEY, expires_at=now + timedelta(days=1)
    )
    await _add_override(
        session, scope="hotkey", key=_OTHER_HOTKEY, expires_at=now + timedelta(days=1)
    )
    await _add_override(
        session,
        scope="coldkey",
        key=_COLDKEY,
        expires_at=now - timedelta(minutes=1),
        created_at=now - timedelta(days=2),
    )
    page = await client.get(_URL, headers=_HEADERS, params={"limit": 1})
    assert page.status_code == 200, page.text
    assert page.json()["active_count"] == 2
    assert len(page.json()["items"]) == 1
    assert page.json()["truncated"] is True
    everything = await client.get(
        _URL, headers=_HEADERS, params={"include_inactive": "true"}
    )
    assert sorted(row["state"] for row in everything.json()["items"]) == [
        "active",
        "active",
        "expired",
    ]
    assert everything.json()["truncated"] is False


async def test_override_message_reports_unaligned_intervals_exactly() -> None:
    now = datetime(2026, 11, 3, tzinfo=UTC)
    message = submission_cooldown_message(
        now + timedelta(seconds=90),
        now=now,
        owner_override=OwnerCooldownNotice(
            cooldown_seconds=90, expires_at=now + timedelta(days=1)
        ),
    )
    assert "one submission per 90 seconds until" in message
