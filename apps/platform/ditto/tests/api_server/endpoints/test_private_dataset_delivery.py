"""Private dataset delivery proves identity, not merely a permitted hotkey."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import bittensor
import pytest
from sqlalchemy import select

from ditto.api_models.private_dataset import PrivateDatasetRequest
from ditto.api_models.ticket_status import TicketStatus
from ditto.db.models import ValidatorTicket
from ditto.db.queries.private_benchmark_datasets import pin_private_dataset
from ditto.tests.api_server.endpoints.test_benchmark_canary import KEY, issue
from ditto.tests.api_server.endpoints.test_benchmark_canary import ready as ready
from ditto.tests.db.test_private_benchmark_datasets import candidate, identity


@pytest.fixture
async def private_lease(client, ready, session_maker):
    await issue(client, ready)
    agent_id = UUID(ready["agent_id"])
    key = identity()
    async with session_maker() as session, session.begin():
        artifact = await pin_private_dataset(session, identity=key, **candidate(key))
        ticket = await session.scalar(
            select(ValidatorTicket).where(ValidatorTicket.agent_id == agent_id)
        )
        ticket.seed = key.seed
        ticket.dataset_sha256 = artifact.dataset_sha256
        deadline = ticket.deadline
    return agent_id, artifact, deadline


def proof(agent_id, artifact, deadline, *, key=KEY, age=0):
    payload = PrivateDatasetRequest(
        validator_hotkey=key.ss58_address,
        dataset_sha256=artifact.dataset_sha256,
        deadline=deadline,
        requested_at=datetime.now(UTC) - timedelta(seconds=age),
        nonce=uuid4(),
        signature="0" * 128,
    )
    payload.signature = key.sign(payload.signing_message(agent_id)).hex()
    return payload.model_dump(mode="json")


async def test_exact_bytes_and_nonce_replay(client, private_lease):
    agent_id, artifact, deadline = private_lease
    body = proof(*private_lease)
    url = f"/api/v1/validator/agent/{agent_id}/private-dataset"
    result = await client.post(url, json=body)
    assert result.status_code == 200, result.text
    assert result.content == artifact.dataset_bytes
    assert result.headers["cache-control"] == "no-store"
    assert result.headers["x-dataset-sha256"] == artifact.dataset_sha256
    assert (await client.post(url, json=body)).status_code == 409


async def test_spoofed_and_wrong_ticket_proofs_fail(client, private_lease):
    agent_id, artifact, deadline = private_lease
    url = f"/api/v1/validator/agent/{agent_id}/private-dataset"
    spoof = proof(*private_lease)
    spoof["dataset_sha256"] = "f" * 64
    response = await client.post(url, json=spoof)
    assert response.status_code in {400, 401, 403}
    assert artifact.dataset_bytes not in response.content
    for payload in (
        proof(*private_lease, key=bittensor.Keypair.create_from_uri("//Bob")),
        proof(*private_lease, age=3600),
        proof(agent_id, artifact, deadline + timedelta(seconds=1)),
    ):
        response = await client.post(url, json=payload)
        assert response.status_code == 409, response.text
    body = proof(*private_lease)
    body["deadline"] = "2026-09-17T00:00:00"
    assert (await client.post(url, json=body)).status_code == 422


async def test_closed_lease_cannot_read(client, private_lease, session_maker):
    agent_id, _, _ = private_lease
    async with session_maker() as session, session.begin():
        ticket = await session.scalar(
            select(ValidatorTicket).where(ValidatorTicket.agent_id == agent_id)
        )
        ticket.status = TicketStatus.EXPIRED
    response = await client.post(
        f"/api/v1/validator/agent/{agent_id}/private-dataset",
        json=proof(*private_lease),
    )
    assert response.status_code == 409
