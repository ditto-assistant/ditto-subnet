"""Real concurrency proofs for exact v9 bundle and spend serialization."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.confirmation_bundles import ConfirmationBundleSettings
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.db.models import (
    Agent,
    ConfirmationBudgetDay,
    ConfirmationBudgetReservation,
    ConfirmationBundle,
)
from ditto.db.queries.confirmation_bundles import (
    StaleConfirmationBudget,
    get_or_create_confirmation_bundle,
    insert_confirmation_bundle_settings_revision,
    reserve_confirmation_bundle_budget,
    settle_confirmation_bundle_budget,
)
from ditto.tests.confirmation_evidence_fixtures import (
    active_settings,
    base_proof_kwargs,
    verification_profile,
)

pytestmark = pytest.mark.asyncio

_NOW = datetime(2026, 8, 8, 12, tzinfo=UTC)
_POLICY = active_settings().model_copy(
    update={
        "daily_bundle_cap": 1,
        "daily_dollar_cap_microusd": 100_000,
        "per_bundle_request_cap": 20,
        "per_bundle_token_cap": 2_000,
    }
)


def _checksum(policy: ConfirmationBundleSettings) -> str:
    encoded = json.dumps(
        policy.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class Seeded:
    revision: int
    agent_ids: tuple[UUID, ...]
    bundle_ids: tuple[UUID, ...]


async def _seed(
    maker: async_sessionmaker[AsyncSession],
    *,
    artifact_sha256s: tuple[str, ...],
) -> Seeded:
    agent_ids = tuple(uuid4() for _ in artifact_sha256s)
    async with maker() as session, session.begin():
        revision = await insert_confirmation_bundle_settings_revision(
            session,
            parent_revision=0,
            scope="*",
            settings=_POLICY.model_dump(mode="json"),
            checksum=_checksum(_POLICY),
            reason="operator approved confirmation concurrency test",
            actor="operator@example.com",
        )
        for index, (agent_id, digest) in enumerate(
            zip(agent_ids, artifact_sha256s, strict=True)
        ):
            session.add(
                Agent(
                    agent_id=agent_id,
                    miner_hotkey=f"5Miner-{index}",
                    name=f"agent-{index}",
                    sha256=digest,
                    status=AgentStatus.SCORED,
                    screening_policy_version=SCREENING_POLICY_VERSION,
                    created_at=_NOW,
                )
            )
        await session.flush()
        bundle_ids: list[UUID] = []
        for agent_id in agent_ids:
            resolution = await get_or_create_confirmation_bundle(
                session,
                agent_id=agent_id,
                bench_version=9,
                **base_proof_kwargs(quality_micros=800_000, stderr_micros=10_000),
                settings_revision=revision.revision,
                settings=_POLICY,
                verification_profile=verification_profile(),
            )
            assert resolution.bundle is not None
            bundle_ids.append(resolution.bundle.bundle_id)
    return Seeded(
        revision=revision.revision,
        agent_ids=agent_ids,
        bundle_ids=tuple(bundle_ids),
    )


async def test_same_digest_concurrent_resolution_creates_one_bundle(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    digest = "b" * 64
    agent_ids = (uuid4(), uuid4())
    async with session_maker() as session, session.begin():
        revision = await insert_confirmation_bundle_settings_revision(
            session,
            parent_revision=0,
            scope="*",
            settings=_POLICY.model_dump(mode="json"),
            checksum=_checksum(_POLICY),
            reason="operator approved exact key concurrency test",
            actor="operator@example.com",
        )
        for index, agent_id in enumerate(agent_ids):
            session.add(
                Agent(
                    agent_id=agent_id,
                    miner_hotkey=f"5Miner-{index}",
                    name=f"rename-{index}",
                    sha256=digest,
                    status=AgentStatus.SCORED,
                    screening_policy_version=SCREENING_POLICY_VERSION,
                    created_at=_NOW,
                )
            )
    gate = asyncio.Event()

    async def resolve(agent_id: UUID) -> UUID:
        async with session_maker() as session, session.begin():
            await gate.wait()
            result = await get_or_create_confirmation_bundle(
                session,
                agent_id=agent_id,
                bench_version=9,
                **base_proof_kwargs(quality_micros=800_000, stderr_micros=10_000),
                settings_revision=revision.revision,
                settings=_POLICY,
                verification_profile=verification_profile(),
            )
            assert result.bundle is not None
            return result.bundle.bundle_id

    tasks = [asyncio.create_task(resolve(agent_id)) for agent_id in agent_ids]
    gate.set()
    bundle_ids = await asyncio.gather(*tasks)
    assert bundle_ids[0] == bundle_ids[1]
    async with session_maker() as session:
        assert (
            await session.scalar(select(func.count()).select_from(ConfirmationBundle))
            == 1
        )


async def test_last_daily_bundle_slot_is_never_oversubscribed(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed(session_maker, artifact_sha256s=("1" * 64, "2" * 64))
    gate = asyncio.Event()

    async def reserve(bundle_id: UUID) -> str:
        async with session_maker() as session, session.begin():
            await gate.wait()
            try:
                decision = await reserve_confirmation_bundle_budget(
                    session,
                    bundle_id=bundle_id,
                    reservation_id=uuid4(),
                    now=_NOW,
                    expected_revision=0,
                    settings_revision=seeded.revision,
                    settings=_POLICY,
                    reserve_microusd=40_000,
                )
            except StaleConfirmationBudget:
                return "stale"
            return decision.blocked_reason or "reserved"

    tasks = [asyncio.create_task(reserve(bundle_id)) for bundle_id in seeded.bundle_ids]
    gate.set()
    outcomes = await asyncio.gather(*tasks)
    assert sorted(outcomes) == ["reserved", "stale"]
    async with session_maker() as session:
        budget = await session.get(ConfirmationBudgetDay, _NOW.date())
        assert budget is not None
        assert budget.issued_attempts == 1
        assert budget.outstanding_reserved_microusd == 40_000
        assert (
            await session.scalar(
                select(func.count()).select_from(ConfirmationBudgetReservation)
            )
            == 1
        )


async def test_stale_loser_refreshes_into_visible_bundle_cap_block(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed(session_maker, artifact_sha256s=("1" * 64, "2" * 64))
    async with session_maker() as session, session.begin():
        first = await reserve_confirmation_bundle_budget(
            session,
            bundle_id=seeded.bundle_ids[0],
            reservation_id=uuid4(),
            now=_NOW,
            expected_revision=0,
            settings_revision=seeded.revision,
            settings=_POLICY,
            reserve_microusd=40_000,
        )
        assert first.reservation is not None
    async with session_maker() as session, session.begin():
        blocked = await reserve_confirmation_bundle_budget(
            session,
            bundle_id=seeded.bundle_ids[1],
            reservation_id=uuid4(),
            now=_NOW,
            expected_revision=1,
            settings_revision=seeded.revision,
            settings=_POLICY,
            reserve_microusd=40_000,
        )
        assert blocked.reservation is None
        assert blocked.blocked_reason == "bundle_cap"


async def test_two_settlements_cannot_double_count_actual_cost(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed(session_maker, artifact_sha256s=("1" * 64,))
    reservation_id = uuid4()
    async with session_maker() as session, session.begin():
        decision = await reserve_confirmation_bundle_budget(
            session,
            bundle_id=seeded.bundle_ids[0],
            reservation_id=reservation_id,
            now=_NOW,
            expected_revision=0,
            settings_revision=seeded.revision,
            settings=_POLICY,
            reserve_microusd=40_000,
        )
        assert decision.reservation is not None
    gate = asyncio.Event()

    async def settle() -> str:
        async with session_maker() as session, session.begin():
            await gate.wait()
            try:
                result = await settle_confirmation_bundle_budget(
                    session,
                    reservation_id=reservation_id,
                    expected_revision=1,
                    actual_microusd=35_000,
                    failed_attempt=False,
                    settled_at=_NOW,
                )
            except StaleConfirmationBudget:
                return "stale"
            return "replay" if result.replayed else "settled"

    tasks = [asyncio.create_task(settle()), asyncio.create_task(settle())]
    gate.set()
    outcomes = await asyncio.gather(*tasks)
    assert sorted(outcomes) == ["settled", "stale"]
    async with session_maker() as session:
        budget = await session.get(ConfirmationBudgetDay, _NOW.date())
        assert budget is not None
        assert budget.outstanding_reserved_microusd == 0
        assert budget.settled_microusd == 35_000
        assert budget.revision == 2


async def test_different_utc_days_do_not_share_a_budget_lock_or_cap(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    seeded = await _seed(session_maker, artifact_sha256s=("1" * 64, "2" * 64))
    times = (
        datetime(2026, 8, 8, 23, 59, tzinfo=UTC),
        datetime(2026, 8, 9, 0, 1, tzinfo=UTC),
    )
    gate = asyncio.Event()

    async def reserve(bundle_id: UUID, now: datetime) -> str:
        async with session_maker() as session, session.begin():
            await gate.wait()
            result = await reserve_confirmation_bundle_budget(
                session,
                bundle_id=bundle_id,
                reservation_id=uuid4(),
                now=now,
                expected_revision=0,
                settings_revision=seeded.revision,
                settings=_POLICY,
                reserve_microusd=40_000,
            )
            return result.blocked_reason or "reserved"

    tasks = [
        asyncio.create_task(reserve(bundle_id, now))
        for bundle_id, now in zip(seeded.bundle_ids, times, strict=True)
    ]
    gate.set()
    assert await asyncio.gather(*tasks) == ["reserved", "reserved"]
    async with session_maker() as session:
        rows = list(
            await session.scalars(
                select(ConfirmationBudgetDay).order_by(ConfirmationBudgetDay.utc_day)
            )
        )
        assert [row.issued_attempts for row in rows] == [1, 1]
        assert [row.revision for row in rows] == [1, 1]
