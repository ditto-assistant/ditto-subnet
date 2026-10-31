"""Operator diagnostic isolation and single-validator lease (real Postgres)."""

# ruff: noqa: F811  (the imported ``ready`` fixture is re-bound as an argument)

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import bittensor
import pytest
from sqlalchemy import select

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.operator_diagnostic import ISOLATE_CONFIRMATION
from ditto.api_models.ticket_status import TicketPurpose, TicketStatus
from ditto.db.models import (
    Agent,
    ScoreAuditEntry,
    ValidatorQueueWithdrawal,
    ValidatorTicket,
)
from ditto.db.queries.benchmark_rollout import active_bench_version
from ditto.tests.api_server.endpoints.test_admin_benchmark_rollout import _HEADERS
from ditto.tests.api_server.endpoints.test_benchmark_canary import (  # noqa: F401
    HOTKEY,
    ready,
)

pytestmark = pytest.mark.asyncio
OPERATOR = bittensor.Keypair.create_from_uri("//Bob").ss58_address
DIGEST = "e" * 64


@pytest.fixture
def operator_env(monkeypatch):
    monkeypatch.setenv("OPERATOR_DIAGNOSTIC_HOTKEYS", OPERATOR)


async def _add_agent(session_maker, *, owner: str, status: AgentStatus) -> UUID:
    agent_id = uuid4()
    now = datetime.now(UTC)
    async with session_maker() as session, session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=owner,
                name="diagnostic",
                sha256=DIGEST,
                status=status,
                screening_policy_version=9,
                screened_image_sha256=DIGEST,
                screened_image_size_bytes=1024,
                screened_image_id=f"sha256:{DIGEST}",
                screened_image_ref=f"ditto-screen/{agent_id}:latest",
                screened_image_upload_id=uuid4(),
                screened_image_verified_at=now,
                created_at=now,
            )
        )
    return agent_id


async def _isolate(client, agent_id: UUID):
    return await client.post(
        f"/api/v1/admin/operator-diagnostic/{agent_id}/isolate",
        headers=_HEADERS,
        json={
            "expected_artifact_sha256": DIGEST,
            "actor": "operator@example.com",
            "reason": "isolate an operator-owned diagnostic agent",
            "confirmation": ISOLATE_CONFIRMATION,
        },
    )


async def _pass_screening(session_maker, agent_id: UUID, bench_version: int) -> None:
    """Simulate the screening pass: evaluating, seeded, queued for a version."""
    async with session_maker() as session, session.begin():
        agent = await session.get(Agent, agent_id)
        agent.status = AgentStatus.EVALUATING
        agent.dataset_seed_block = 100
        agent.dataset_seed_block_hash = "ab" * 32
        # The canary in these tests targets v13; make sure the isolation covers
        # it even when the fixture's active era differs.
        if not await session.scalar(
            select(ValidatorQueueWithdrawal.withdrawal_id).where(
                ValidatorQueueWithdrawal.agent_id == agent_id,
                ValidatorQueueWithdrawal.bench_version == bench_version,
            )
        ):
            session.add(
                ValidatorQueueWithdrawal(
                    withdrawal_id=uuid4(),
                    agent_id=agent_id,
                    bench_version=bench_version,
                    actor="operator@example.com",
                    reason="isolate an operator-owned diagnostic agent",
                    expected_snapshot="operator-diagnostic-isolation",
                    score_count=0,
                    ticket_snapshot=[],
                )
            )


def _canary(ready, agent_id: UUID) -> dict:
    return {
        **ready,
        "canary_id": str(uuid4()),
        "agent_id": str(agent_id),
        "expected_artifact_sha256": DIGEST,
        "expected_screened_image_sha256": DIGEST,
        "confirmation": f"ISSUE CANARY V13 {agent_id}",
    }


@pytest.mark.usefixtures("operator_env")
async def test_isolated_operator_agent_gets_exactly_one_lease(
    client,
    ready,
    session_maker,
):
    agent_id = await _add_agent(
        session_maker, owner=OPERATOR, status=AgentStatus.SCREENING
    )
    response = await _isolate(client, agent_id)
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["shadow"] is True
    assert view["operator_owned"] is True
    assert view["withdrawals"] and all(w["in_force"] for w in view["withdrawals"])
    async with session_maker() as session:
        active = await active_bench_version(session)
        audit = await session.scalar(
            select(ScoreAuditEntry).where(
                ScoreAuditEntry.agent_id == agent_id,
                ScoreAuditEntry.event == "operator_diagnostic_isolated",
            )
        )
        assert audit is not None
    assert active in {w["bench_version"] for w in view["withdrawals"]}

    await _pass_screening(session_maker, agent_id, 13)
    issued = await client.post(
        "/api/v1/admin/benchmark-canaries",
        headers=_HEADERS,
        json=_canary(ready, agent_id),
    )
    assert issued.status_code == 200, issued.text
    async with session_maker() as session:
        tickets = (
            await session.scalars(
                select(ValidatorTicket).where(ValidatorTicket.agent_id == agent_id)
            )
        ).all()
    assert [(t.validator_hotkey, t.purpose) for t in tickets] == [
        (HOTKEY, TicketPurpose.BENCHMARK_CANARY)
    ]

    read = await client.get(
        f"/api/v1/admin/operator-diagnostic/{agent_id}", headers=_HEADERS
    )
    assert read.status_code == 200, read.text
    assert read.json()["diagnostic_lease_count"] == 1
    assert read.json()["ordinary_lease_count"] == 0


@pytest.mark.usefixtures("operator_env")
async def test_non_operator_agent_is_refused(
    client,
    ready,
    session_maker,
):
    agent_id = await _add_agent(
        session_maker, owner="miner-somebody", status=AgentStatus.SCREENING
    )
    response = await _isolate(client, agent_id)
    assert response.status_code == 409
    assert "operator hotkey" in response.json()["message"]
    await _pass_screening(session_maker, agent_id, 13)
    issued = await client.post(
        "/api/v1/admin/benchmark-canaries",
        headers=_HEADERS,
        json=_canary(ready, agent_id),
    )
    assert issued.status_code == 409


@pytest.mark.usefixtures("operator_env", "ready")
async def test_isolation_refused_after_screening(client, session_maker):
    agent_id = await _add_agent(
        session_maker, owner=OPERATOR, status=AgentStatus.EVALUATING
    )
    response = await _isolate(client, agent_id)
    assert response.status_code == 409
    assert "precede dispatch" in response.json()["message"]


@pytest.mark.usefixtures("ready")
async def test_disabled_without_configured_hotkeys(client, session_maker, monkeypatch):
    monkeypatch.delenv("OPERATOR_DIAGNOSTIC_HOTKEYS", raising=False)
    agent_id = await _add_agent(
        session_maker, owner=OPERATOR, status=AgentStatus.SCREENING
    )
    response = await _isolate(client, agent_id)
    assert response.status_code == 409


@pytest.mark.usefixtures("operator_env")
async def test_ordinarily_leased_agent_is_refused(
    client,
    ready,
    session_maker,
):
    agent_id = await _add_agent(
        session_maker, owner=OPERATOR, status=AgentStatus.SCREENING
    )
    assert (await _isolate(client, agent_id)).status_code == 200
    await _pass_screening(session_maker, agent_id, 13)
    now = datetime.now(UTC)
    async with session_maker() as session, session.begin():
        session.add(
            ValidatorTicket(
                agent_id=agent_id,
                bench_version=13,
                validator_hotkey="someone-else",
                slot_id="slot-0",
                status=TicketStatus.EXPIRED,
                issued_at=now - timedelta(hours=3),
                deadline=now - timedelta(hours=1),
                attempt_count=1,
                manual_retry_grants=0,
            )
        )
    issued = await client.post(
        "/api/v1/admin/benchmark-canaries",
        headers=_HEADERS,
        json=_canary(ready, agent_id),
    )
    assert issued.status_code == 409
