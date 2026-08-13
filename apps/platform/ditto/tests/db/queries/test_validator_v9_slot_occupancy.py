"""Real-Postgres tests for ordinary/v9 confirmation slot mutual exclusion."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.confirmation_bundles import ConfirmationBundleMode
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.api_models.validator_confirmation import (
    V9ConfirmationClaimRequest,
    V9ConfirmationJobResponse,
)
from ditto.api_server import create_api_server
from ditto.api_server.endpoints.validator_confirmation import (
    request_v9_confirmation_job,
    v9_confirmation_claim_signing_message,
)
from ditto.chain.models import NeuronInfo
from ditto.db.models import (
    Agent,
    BenchmarkDataset,
    ConfirmationBundleSettingsRevision,
    ConfirmationBundleTicket,
    ValidatorTicket,
)
from ditto.db.queries.confirmation_bundles import get_or_create_confirmation_bundle
from ditto.db.queries.tickets import issue_ticket
from ditto.tests.api_server.conftest import make_api_server_config
from ditto.tests.confirmation_evidence_fixtures import (
    ARTIFACT_SHA256,
    VALIDATOR_KEYPAIR,
    active_settings,
    base_proof_kwargs,
    verification_profile,
)

pytestmark = pytest.mark.asyncio

_NOW = datetime.now(UTC)
_TTL = timedelta(minutes=30)


@pytest.fixture
def app() -> FastAPI:
    return create_api_server(make_api_server_config())


def _checksum(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _install(app: FastAPI) -> None:
    profile = verification_profile()
    app.state.confirmation_verification_profiles = {
        (profile.revision, profile.checksum()): profile
    }


def _permitted_chain() -> MagicMock:
    chain = MagicMock()
    chain.get_recent_neurons = AsyncMock(
        return_value=[
            NeuronInfo(
                hotkey=VALIDATOR_KEYPAIR.ss58_address,
                coldkey="5GReceiverColdkeyPlaceholderXXXXXXXXXXXXXXXXXXX",
                uid=1,
                stake=1_000.0,
                validator_permit=True,
            )
        ]
    )
    return chain


def _claim_payload(*, slot_id: str = "longmem-0") -> V9ConfirmationClaimRequest:
    profile = verification_profile()
    nonce = uuid4()
    requested_at = datetime.now(UTC)
    broker_public_key = "A" * 43
    signature = VALIDATOR_KEYPAIR.sign(
        v9_confirmation_claim_signing_message(
            validator_hotkey=VALIDATOR_KEYPAIR.ss58_address,
            slot_id=slot_id,
            profile_revision=profile.revision,
            profile_checksum=profile.checksum(),
            broker_public_key=broker_public_key,
            nonce=nonce,
            requested_at=requested_at,
        )
    ).hex()
    return V9ConfirmationClaimRequest(
        validator_hotkey=VALIDATOR_KEYPAIR.ss58_address,
        slot_id=slot_id,
        profile_revision=profile.revision,
        profile_checksum=profile.checksum(),
        broker_public_key=broker_public_key,
        nonce=nonce,
        requested_at=requested_at,
        signature=signature,
    )


async def _seed_confirmation_and_ordinary_candidates(
    maker: async_sessionmaker[AsyncSession], *, ordinary_count: int = 2
) -> tuple[UUID, list[UUID]]:
    settings = active_settings(mode=ConfirmationBundleMode.SHADOW)
    confirmation_agent_id = uuid4()
    ordinary_agent_ids = [uuid4() for _ in range(ordinary_count)]
    async with maker() as session, session.begin():
        revision = ConfirmationBundleSettingsRevision(
            parent_revision=0,
            scope="*",
            settings=settings.model_dump(mode="json"),
            checksum=_checksum(settings.model_dump(mode="json")),
            reason="test cross-table slot serialization",
            actor="pytest@example.com",
        )
        session.add(revision)
        await session.flush()
        session.add(
            Agent(
                agent_id=confirmation_agent_id,
                miner_hotkey="5ConfirmationMiner",
                name="confirmation-subject",
                sha256=ARTIFACT_SHA256,
                status=AgentStatus.SCORED,
                screening_policy_version=SCREENING_POLICY_VERSION,
                created_at=_NOW - timedelta(days=1),
            )
        )
        for index, agent_id in enumerate(ordinary_agent_ids):
            agent = Agent(
                agent_id=agent_id,
                miner_hotkey=f"5OrdinaryMiner-{index}",
                name=f"ordinary-{index}",
                sha256=f"{index + 10:02x}" * 32,
                status=AgentStatus.EVALUATING,
                screening_policy_version=SCREENING_POLICY_VERSION,
                created_at=_NOW + timedelta(minutes=index),
                screened_image_sha256="12" * 32,
                screened_image_size_bytes=123,
                screened_image_id="sha256:" + "34" * 32,
                screened_image_ref=f"ditto-screen/{agent_id}:latest",
                screened_image_upload_id=uuid4(),
                screened_image_verified_at=_NOW,
            )
            session.add(agent)
            session.add(
                BenchmarkDataset(
                    agent_id=agent_id,
                    bench_version=8,
                    seed=100 + index,
                    sha256=f"{index + 20:02x}" * 32,
                    run_size="full",
                )
            )
        await session.flush()
        resolution = await get_or_create_confirmation_bundle(
            session,
            agent_id=confirmation_agent_id,
            bench_version=9,
            **base_proof_kwargs(),
            settings_revision=revision.revision,
            settings=settings,
            verification_profile=verification_profile(),
        )
        assert resolution.bundle is not None
    return confirmation_agent_id, ordinary_agent_ids


async def _claim_direct(
    app: FastAPI,
    session: AsyncSession,
    *,
    slot_id: str = "longmem-0",
) -> V9ConfirmationJobResponse | Response:
    return await request_v9_confirmation_job(
        _claim_payload(slot_id=slot_id),
        Request({"type": "http", "app": app, "headers": []}),
        Response(),
        _permitted_chain(),
        session,
        VALIDATOR_KEYPAIR.ss58_address,
    )


class TestDisjointSlotOccupancy:
    async def test_live_confirmation_and_ordinary_slots_can_both_be_occupied(
        self,
        app: FastAPI,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _, ordinary_ids = await _seed_confirmation_and_ordinary_candidates(
            session_maker
        )
        _install(app)
        async with session_maker() as session:
            claimed = await _claim_direct(app, session, slot_id="longmem-0")
        assert isinstance(claimed, V9ConfirmationJobResponse)

        async with session_maker() as session, session.begin():
            other_slot = await issue_ticket(
                session,
                validator_hotkey=VALIDATOR_KEYPAIR.ss58_address,
                slot_id="slot-1",
                now=datetime.now(UTC),
                ttl=_TTL,
                bench_version=8,
            )
            same_index = await issue_ticket(
                session,
                validator_hotkey=VALIDATOR_KEYPAIR.ss58_address,
                slot_id="slot-0",
                now=datetime.now(UTC),
                ttl=_TTL,
                bench_version=8,
            )

        assert other_slot is not None
        assert other_slot.agent_id in ordinary_ids
        assert same_index is not None
        async with session_maker() as session:
            confirmation_live = int(
                await session.scalar(
                    select(func.count())
                    .select_from(ConfirmationBundleTicket)
                    .where(
                        ConfirmationBundleTicket.validator_hotkey
                        == VALIDATOR_KEYPAIR.ss58_address,
                        ConfirmationBundleTicket.slot_id == "longmem-0",
                        ConfirmationBundleTicket.status == "issued",
                    )
                )
                or 0
            )
            ordinary_same_slot = int(
                await session.scalar(
                    select(func.count())
                    .select_from(ValidatorTicket)
                    .where(
                        ValidatorTicket.validator_hotkey
                        == VALIDATOR_KEYPAIR.ss58_address,
                        ValidatorTicket.slot_id == "slot-0",
                        ValidatorTicket.status == "issued",
                    )
                )
                or 0
            )
        assert confirmation_live == 1
        assert ordinary_same_slot == 1

    async def test_concurrent_disjoint_allocators_both_win(
        self,
        app: FastAPI,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_confirmation_and_ordinary_candidates(
            session_maker, ordinary_count=1
        )
        _install(app)
        ready = asyncio.Event()

        async def claim_confirmation() -> V9ConfirmationJobResponse | Response:
            await ready.wait()
            async with session_maker() as session:
                return await _claim_direct(app, session, slot_id="longmem-0")

        async def claim_ordinary() -> ValidatorTicket | None:
            await ready.wait()
            async with session_maker() as session, session.begin():
                return await issue_ticket(
                    session,
                    validator_hotkey=VALIDATOR_KEYPAIR.ss58_address,
                    slot_id="slot-0",
                    now=datetime.now(UTC),
                    ttl=_TTL,
                    bench_version=8,
                )

        confirmation_task = asyncio.create_task(claim_confirmation())
        ordinary_task = asyncio.create_task(claim_ordinary())
        ready.set()
        confirmation, ordinary = await asyncio.gather(confirmation_task, ordinary_task)

        assert isinstance(confirmation, V9ConfirmationJobResponse)
        assert ordinary is not None
        async with session_maker() as session:
            ordinary_live = int(
                await session.scalar(
                    select(func.count())
                    .select_from(ValidatorTicket)
                    .where(
                        ValidatorTicket.validator_hotkey
                        == VALIDATOR_KEYPAIR.ss58_address,
                        ValidatorTicket.slot_id == "slot-0",
                        ValidatorTicket.status == "issued",
                        ValidatorTicket.deadline > datetime.now(UTC),
                    )
                )
                or 0
            )
            confirmation_live = int(
                await session.scalar(
                    select(func.count())
                    .select_from(ConfirmationBundleTicket)
                    .where(
                        ConfirmationBundleTicket.validator_hotkey
                        == VALIDATOR_KEYPAIR.ss58_address,
                        ConfirmationBundleTicket.slot_id == "longmem-0",
                        ConfirmationBundleTicket.status == "issued",
                        ConfirmationBundleTicket.deadline > datetime.now(UTC),
                    )
                )
                or 0
            )
        assert (ordinary_live, confirmation_live) == (1, 1)
