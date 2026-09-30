"""The signed-in miner's list of their own review holds."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import bittensor
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_server.deferred_source_review import INTEGRITY_DOUBLE_CHECK_REASON
from ditto.db.models import AgentStatus, AthReview
from ditto.tests.api_server.endpoints.test_miner_logs import _login
from ditto.tests.api_server.endpoints.test_validator import (
    _install_chain,
    _install_db,
    _seed_agent,
)


@pytest.mark.asyncio
async def test_legacy_double_check_reason_reads_neutrally_to_the_miner(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A stored top-five double-check hold must not accuse the miner (#562)."""
    legacy_reason = (
        "Top-five rank qualified this submission for an integrity double-check"
    )
    operator_reason = "Possible source similarity requires operator review."
    miner = bittensor.Keypair.create_from_uri("//Alice")
    legacy_id = await _seed_agent(
        session_maker,
        status=AgentStatus.ATH_PENDING_REVIEW,
        miner_hotkey=miner.ss58_address,
    )
    copy_id = await _seed_agent(
        session_maker,
        status=AgentStatus.ATH_PENDING_REVIEW,
        name="beta-agent",
        miner_hotkey=miner.ss58_address,
        sha256="ef" * 32,
    )
    async with session_maker() as session, session.begin():
        for agent_id, reason, kind in (
            (legacy_id, legacy_reason, "deferred_source_review"),
            (copy_id, operator_reason, "copy"),
        ):
            session.add(
                AthReview(
                    review_id=uuid4(),
                    agent_id=agent_id,
                    status="pending",
                    opened_at=datetime.now(UTC),
                    original_duplicate_of=None,
                    original_reason=reason,
                    original_policy_version=13,
                    original_evidence={},
                    algorithm_provenance={"review_kind": kind},
                )
            )
    _install_db(app, session_maker)
    _install_chain(app)
    token = await _login(client, keypair=miner)

    response = await client.get(
        "/api/v1/me/reviews", headers={"authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200, response.text
    details = {
        review["agent_id"]: review["detail"] for review in response.json()["reviews"]
    }
    assert details == {
        str(legacy_id): INTEGRITY_DOUBLE_CHECK_REASON,
        str(copy_id): operator_reason,
    }
    assert legacy_reason not in response.text
