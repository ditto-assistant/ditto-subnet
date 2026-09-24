"""Activation refuses unpaused outsiders and preserves an exact immutable packet."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.ticket_status import TicketStatus
from ditto.api_models.validator_slot_settings import ValidatorSlotSettings
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.endpoints.admin_v13_scorer_cohort import (
    ActivateV13ScorerCohortRequest,
    activate_pin,
    get_pin,
    get_preflight,
    router,
)
from ditto.api_server.endpoints.admin_validator_slot_settings import _checksum
from ditto.api_server.scored_runtime_evidence import scored_runtime_evidence_for_lease
from ditto.api_server.v13_scorer_cohort import pinned_validator_allowed
from ditto.db.models import ValidatorSlotSettingsRevision, ValidatorTicket
from ditto.tests.api_server.endpoints.test_screener import _seed_agent
from ditto.tests.db.queries.test_benchmark_rollout import _heartbeat

_HOTKEYS = tuple(
    sorted(
        (
            "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
            "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty",
            "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm",
        )
    )
)
_OUTSIDER = "5CqJAjSjv8fjF9uAQpDLyfN1hZEvBjwpFgcGeLbYpcbSaD1C"


@pytest.mark.asyncio
async def test_v13_pin_requires_outsider_pause_then_routes_only_exact_members(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    source = "a" * 40
    keys = ["DITTOBENCH_DB"]
    digest = hashlib.sha256(
        ("scored-runtime-env-v1\n13\n" + source + "\n" + "\n".join(keys)).encode()
    ).hexdigest()
    packet = {
        "source_revision": source,
        "release_descriptor_digest": "sha256:" + "b" * 64,
        "scorer_image_digest": "sha256:" + "c" * 64,
        "scorer_env_sha256": digest,
        "injected_keys": keys,
    }

    def managed(hotkey: str):
        row = _heartbeat(hotkey, now, versions=[7, 13], protocol_version=18)
        row.benchmark_capacity = {
            "configured_slots": 1,
            "healthy_slots": ["slot-0"],
            "admission": "accepting",
            "active": [],
        }
        assert row.stack is not None and row.capabilities is not None
        row.stack["mode"] = "managed"
        row.stack["release_descriptor_digest"] = packet["release_descriptor_digest"]
        for component in row.stack["components"].values():
            component["provenance"] = "signed_descriptor"
            component["image_digest"] = packet["scorer_image_digest"]
        row.capabilities["scorer_benchmarks"]["scored_runtime_env"] = {
            "bench_version": 13,
            "scope": "scorer-injected-env-only",
            "source_revision": source,
            "injected_keys": keys,
            "sha256": digest,
        }
        return row

    empty = ValidatorSlotSettings()
    async with session_maker() as session, session.begin():
        session.add_all([managed(hotkey) for hotkey in _HOTKEYS])
        session.add(_heartbeat(_OUTSIDER, now, versions=[7, 13], protocol_version=18))
        session.add(
            ValidatorSlotSettingsRevision(
                parent_revision=0,
                scope="*",
                settings=empty.model_dump(mode="json"),
                checksum=_checksum(empty),
                reason="test initial settings",
                actor="test",
            )
        )
    async with session_maker() as session:
        preflight = await get_preflight(None, session)
        assert preflight.slot_settings_revision == 1
        assert sum(item.packet is not None for item in preflight.validators) == 3
        assert any(
            item.hotkey == _OUTSIDER and item.capable for item in preflight.validators
        )
    async with session_maker() as session:
        with pytest.raises(
            HTTPException, match="nonmember V13 validator is not paused"
        ):
            await activate_pin(
                ActivateV13ScorerCohortRequest.model_validate(
                    {
                        "hotkeys": list(_HOTKEYS),
                        "packet": packet,
                        "expected_slot_settings_revision": 1,
                        "expected_slot_settings_checksum": _checksum(empty),
                        "reason": "test exact signed scorer cohort",
                        "actor": "test",
                        "confirmation": "PIN V13 SCORER COHORT",
                    }
                ),
                None,
                session,
            )

    paused = ValidatorSlotSettings(paused_validator_hotkeys=[_OUTSIDER])
    async with session_maker() as session, session.begin():
        session.add(
            ValidatorSlotSettingsRevision(
                parent_revision=1,
                scope="*",
                settings=paused.model_dump(mode="json"),
                checksum=_checksum(paused),
                reason="test source validator pause",
                actor="test",
            )
        )
    app = FastAPI()
    app.include_router(router)

    async def test_session():
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_session] = test_session
    app.dependency_overrides[require_admin] = lambda: None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/admin/v13-scorer-cohort",
            json={
                "hotkeys": list(_HOTKEYS),
                "packet": packet,
                "expected_slot_settings_revision": 2,
                "expected_slot_settings_checksum": _checksum(paused),
                "reason": "test exact signed scorer cohort",
                "actor": "test",
                "confirmation": "PIN V13 SCORER COHORT",
            },
        )
    assert response.status_code == 200, response.text
    assert response.json()["hotkeys"] == list(_HOTKEYS)
    async with session_maker() as session:
        assert (await get_pin(None, session)) is not None
        assert await pinned_validator_allowed(session, hotkey=_HOTKEYS[0], now=now)
        assert not await pinned_validator_allowed(session, hotkey=_OUTSIDER, now=now)
        lease = await scored_runtime_evidence_for_lease(
            session,
            attempt_id=uuid4(),
            artifact_sha256="f" * 64,
            policy_version=13,
            bench_version=13,
            now=now,
        )
        assert lease is not None and lease.validator_count == 3
    agent_id = await _seed_agent(
        session_maker, status=AgentStatus.EVALUATING, sha256="f" * 64
    )
    async with session_maker() as session:
        session.add(
            ValidatorTicket(
                agent_id=agent_id,
                bench_version=13,
                validator_hotkey=_OUTSIDER,
                slot_id="slot-0",
                status=TicketStatus.ISSUED,
                issued_at=now,
                deadline=now + timedelta(minutes=30),
            )
        )
        with pytest.raises(DBAPIError, match="outside pinned scorer cohort"):
            await session.flush()
