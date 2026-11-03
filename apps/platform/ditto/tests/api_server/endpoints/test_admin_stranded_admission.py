"""Guarded admission of one stranded previous-era submission onto the active era.

The write appends the exact agent to the activated rollout after its frozen
cohort, pins its active-version dataset and audits actor and reason. Every
guard refuses before any dataset is rendered or row written.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.ticket_status import TicketPurpose, TicketStatus
from ditto.api_server.dependencies import get_session
from ditto.db.models import (
    Agent,
    BenchmarkDataset,
    BenchmarkRollout,
    BenchmarkRolloutAudit,
    BenchmarkRolloutMember,
    Score,
    ValidatorTicket,
)
from ditto.db.queries.benchmark_admission import agent_is_admitted
from ditto.db.queries.benchmark_era_stranded import stranded_previous_era_agents
from ditto.db.queries.stranded_admission import (
    EVENT_STRANDED_ADMITTED,
    stranded_admission_confirmation,
)
from ditto_screening_protocol import SCREENING_FLOOR_POLICY_VERSION

pytestmark = pytest.mark.asyncio

_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_TOKEN}"}
_FROM = 13
_ACTIVE = 14
_ROLLOUT_START = datetime(2026, 10, 28, 0, 30, 0, tzinfo=UTC)
_ACTIVATED_AT = _ROLLOUT_START + timedelta(hours=1)
_BEFORE = _ROLLOUT_START - timedelta(days=1)


class _StubGenerator:
    run_size = "full"

    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    async def generate(self, seed: int, bench_version: int = 2) -> str:
        self.calls.append((seed, bench_version))
        return f"{seed:064x}"

    async def aclose(self) -> None:
        return None


@pytest.fixture
def generator(
    app: FastAPI, session_maker: async_sessionmaker[AsyncSession]
) -> _StubGenerator:
    app.state.config = replace(app.state.config, admin_api_token=_TOKEN)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    stub = _StubGenerator()
    app.state.dataset_generator = stub
    return stub


def _sha(name: str) -> str:
    return f"{abs(hash(name)) % (16**64):064x}"


async def _agent(
    session: AsyncSession,
    *,
    name: str,
    hotkey: str,
    screened: bool = True,
    status: AgentStatus = AgentStatus.SCORED,
) -> UUID:
    agent_id = uuid4()
    image: dict[str, Any] = {}
    if screened:
        image = {
            "screened_image_sha256": "e" * 64,
            "screened_image_size_bytes": 1024,
            "screened_image_id": "sha256:" + "e" * 64,
            "screened_image_ref": f"ditto-screen/{agent_id}:latest",
            "screened_image_upload_id": uuid4(),
            "screened_image_verified_at": _BEFORE,
        }
    session.add(
        Agent(
            agent_id=agent_id,
            miner_hotkey=hotkey,
            name=name,
            version=1,
            sha256=_sha(name),
            status=status,
            screening_policy_version=SCREENING_FLOOR_POLICY_VERSION,
            created_at=_BEFORE,
            **image,
        )
    )
    await session.flush()
    for index, composite in enumerate((0.8, 0.9, 0.85)):
        session.add(
            Score(
                agent_id=agent_id,
                bench_version=_FROM,
                validator_hotkey=f"5Validator-{index}",
                run_id=f"{agent_id}-{index}",
                signature=None,
                seed=7 + index,
                composite=composite,
                tool_mean=composite,
                memory_mean=composite,
                median_ms=1,
                n=350,
                details={"bench_version": _FROM},
                generated_at=_BEFORE,
            )
        )
    await session.flush()
    return agent_id


async def _seed(
    maker: async_sessionmaker[AsyncSession],
) -> tuple[UUID, UUID, UUID]:
    """An activated v13->v14 era with one frozen member and one stranded row."""
    async with maker() as session, session.begin():
        rollout = BenchmarkRollout(
            rollout_id=uuid4(),
            from_version=_FROM,
            desired_version=_ACTIVE,
            status="activated",
            cohort_size=5,
            created_at=_ROLLOUT_START,
            activated_at=_ACTIVATED_AT,
            rescore_cohort_target=5,
            priority_cohort_target=5,
        )
        session.add(rollout)
        await session.flush()
        member = await _agent(session, name="frozen", hotkey="5Frozen")
        session.add(
            BenchmarkRolloutMember(
                rollout_id=rollout.rollout_id,
                agent_id=member,
                position=1,
                frozen_miner_hotkey="5Frozen",
                frozen_composite=0.85,
            )
        )
        stranded = await _agent(session, name="stranded", hotkey="5Stranded")
        return rollout.rollout_id, member, stranded


def _payload(agent_id: UUID, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "reason": "operator backfill of the owner's best stranded v13 agent",
        "actor": "operator@example.test",
        "confirmation": stranded_admission_confirmation(agent_id, _ACTIVE),
        "expected_active_version": _ACTIVE,
        "expected_sha256": _sha("stranded"),
        "expected_previous_score_count": 3,
    }
    payload.update(overrides)
    return payload


def _admit_path(agent_id: UUID) -> str:
    return f"/api/v1/admin/benchmark-rollout/stranded-previous-era/{agent_id}/admit"


async def test_admits_one_stranded_agent_pins_dataset_and_audits(
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    generator: _StubGenerator,
) -> None:
    rollout_id, _member, stranded = await _seed(session_maker)
    async with session_maker() as session, session.begin():
        assert not await agent_is_admitted(
            session, bench_version=_ACTIVE, agent_id=stranded
        )

    response = await client.post(
        _admit_path(stranded),
        headers=_HEADERS,
        json=_payload(stranded, expected_sha256=_sha("stranded").upper()),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["admission"] == {
        "rollout_id": str(rollout_id),
        "bench_version": _ACTIVE,
        "position": 2,
        "dataset_sha256": f"{7:064x}",
        "seed_source": "source_scores_canonical_min",
        "rendered": True,
    }
    effect = body["effect"]
    assert effect["agent_id"] == str(stranded)
    assert effect["admitted"] is True
    assert effect["is_member"] is True
    assert effect["ticket_state"] == "awaiting_first_lease"
    assert effect["first_lease_at"] is None
    assert effect["previous_score_count"] == 3
    assert effect["previous_median_composite"] == 0.85
    assert effect["actor"] == "operator@example.test"
    assert generator.calls == [(7, _ACTIVE)]

    async with session_maker() as session, session.begin():
        assert await agent_is_admitted(
            session, bench_version=_ACTIVE, agent_id=stranded
        )
        dataset = await session.get(BenchmarkDataset, (stranded, _ACTIVE))
        assert dataset is not None and dataset.seed == 7
        member = await session.get(BenchmarkRolloutMember, (rollout_id, stranded))
        assert member is not None and member.position == 2
        assert member.frozen_composite == 0.85
        rollout = await session.get(BenchmarkRollout, rollout_id)
        assert rollout is not None
        # The frozen activation cohort is not re-gated or resized.
        assert rollout.cohort_size == 5 and rollout.status == "activated"
        agent = await session.get(Agent, stranded)
        assert agent is not None and agent.status == AgentStatus.SCORED
        audit = await session.scalar(
            select(BenchmarkRolloutAudit).where(
                BenchmarkRolloutAudit.event == EVENT_STRANDED_ADMITTED
            )
        )
        assert audit is not None
        assert audit.payload["reason"].startswith("operator backfill")
        assert audit.payload["agent_sha256"] == _sha("stranded")
        assert audit.payload["previous_score_count"] == 3
        report = await stranded_previous_era_agents(session)
        assert stranded not in {item.agent_id for item in report.items}

    again = await client.post(
        _admit_path(stranded), headers=_HEADERS, json=_payload(stranded)
    )
    assert again.status_code == 409
    assert "not a stranded" in again.text


@pytest.mark.parametrize(
    ("overrides", "detail"),
    [
        ({"confirmation": "ADMIT"}, "exactly to confirm"),
        ({"expected_sha256": "0" * 64}, "artifact SHA changed"),
        ({"expected_previous_score_count": 2}, "score count changed"),
        (
            {
                "expected_active_version": _FROM,
                "confirmation": "ADMIT STRANDED {agent} TO BENCHMARK V13",
            },
            "active benchmark changed",
        ),
    ],
)
async def test_every_guard_refuses_before_rendering(
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    generator: _StubGenerator,
    overrides: dict[str, Any],
    detail: str,
) -> None:
    _rollout_id, _member, stranded = await _seed(session_maker)
    if "confirmation" in overrides:
        overrides["confirmation"] = overrides["confirmation"].format(agent=stranded)

    response = await client.post(
        _admit_path(stranded), headers=_HEADERS, json=_payload(stranded, **overrides)
    )

    assert response.status_code == 409, response.text
    assert detail in response.text
    assert generator.calls == []
    async with session_maker() as session, session.begin():
        assert await session.get(BenchmarkDataset, (stranded, _ACTIVE)) is None


async def test_refuses_cohort_members_unscreened_and_unknown_agents(
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    generator: _StubGenerator,
) -> None:
    _rollout_id, member, _stranded = await _seed(session_maker)
    async with session_maker() as session, session.begin():
        unscreened = await _agent(
            session, name="unscreened", hotkey="5Unscreened", screened=False
        )
        rejected = await _agent(
            session, name="rejected", hotkey="5Rejected", status=AgentStatus.REJECTED
        )

    on_board = await client.post(
        _admit_path(member),
        headers=_HEADERS,
        json=_payload(member, expected_sha256=_sha("frozen")),
    )
    assert on_board.status_code == 409
    assert "not a stranded" in on_board.text

    no_image = await client.post(
        _admit_path(unscreened),
        headers=_HEADERS,
        json=_payload(unscreened, expected_sha256=_sha("unscreened")),
    )
    assert no_image.status_code == 409
    assert "screened image" in no_image.text

    banned_or_rejected = await client.post(
        _admit_path(rejected),
        headers=_HEADERS,
        json=_payload(rejected, expected_sha256=_sha("rejected")),
    )
    assert banned_or_rejected.status_code == 409
    assert "not a stranded" in banned_or_rejected.text

    missing = uuid4()
    unknown = await client.post(
        _admit_path(missing), headers=_HEADERS, json=_payload(missing)
    )
    assert unknown.status_code == 404
    assert generator.calls == []


@pytest.mark.usefixtures("generator")
async def test_effect_read_reports_the_first_active_era_lease(
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _rollout_id, _member, stranded = await _seed(session_maker)
    admitted = await client.post(
        _admit_path(stranded), headers=_HEADERS, json=_payload(stranded)
    )
    assert admitted.status_code == 200, admitted.text

    issued_at = datetime.now(UTC).replace(microsecond=0)
    async with session_maker() as session, session.begin():
        session.add(
            ValidatorTicket(
                agent_id=stranded,
                bench_version=_ACTIVE,
                validator_hotkey="5Validator-a",
                slot_id="slot-0",
                status=TicketStatus.ISSUED,
                purpose=TicketPurpose.CANONICAL_QUORUM,
                purpose_revision=1,
                issued_at=issued_at,
                deadline=issued_at + timedelta(hours=2),
                seed=7,
                dataset_sha256=f"{7:064x}",
                attempt_count=1,
                manual_retry_grants=0,
            )
        )

    path = "/api/v1/admin/benchmark-rollout/stranded-previous-era/admissions"
    assert (await client.get(path)).status_code == 401
    response = await client.get(
        path, headers=_HEADERS, params={"agent_id": str(stranded)}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["active_bench_version"] == _ACTIVE
    assert body["total"] == 1
    (row,) = body["items"]
    assert row["ticket_state"] == "leased"
    assert row["live_lease_count"] == 1
    assert datetime.fromisoformat(row["first_lease_at"]) == issued_at
    assert row["tickets"][0]["validator_hotkey"] == "5Validator-a"

    everything = await client.get(path, headers=_HEADERS)
    assert everything.json()["total"] == 1
    other = await client.get(path, headers=_HEADERS, params={"agent_id": str(uuid4())})
    assert other.json()["total"] == 0


async def test_withdrawal_committed_while_rendering_refuses_the_admission(
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    generator: _StubGenerator,
) -> None:
    """The guards are re-read under the rollout and agent row locks."""
    from ditto.db.models import ValidatorQueueWithdrawal

    _rollout_id, _member, stranded = await _seed(session_maker)
    render = generator.generate

    async def _withdraw_then_render(seed: int, bench_version: int = 2) -> str:
        async with session_maker() as session, session.begin():
            session.add(
                ValidatorQueueWithdrawal(
                    withdrawal_id=uuid4(),
                    agent_id=stranded,
                    bench_version=_ACTIVE,
                    actor="operator",
                    reason="withdrawn while the admission rendered",
                    expected_snapshot="snapshot",
                    score_count=0,
                    ticket_snapshot=[],
                    created_at=datetime.now(UTC),
                )
            )
        return await render(seed, bench_version)

    generator.generate = _withdraw_then_render  # type: ignore[method-assign]
    response = await client.post(
        _admit_path(stranded), headers=_HEADERS, json=_payload(stranded)
    )

    assert response.status_code == 409, response.text
    assert "withdrawn" in response.text
    async with session_maker() as session, session.begin():
        assert await session.get(BenchmarkDataset, (stranded, _ACTIVE)) is None
