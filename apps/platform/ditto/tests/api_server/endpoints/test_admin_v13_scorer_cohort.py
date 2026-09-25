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
    RotateV13ScorerCohortRequest,
    activate_pin,
    get_history,
    get_pin,
    get_preflight,
    get_report_only_current_packet,
    rotate_pin,
    router,
)
from ditto.api_server.endpoints.admin_validator_slot_settings import _checksum
from ditto.api_server.scored_runtime_evidence import scored_runtime_evidence_for_lease
from ditto.api_server.v13_scorer_cohort import pinned_validator_allowed
from ditto.db.models import (
    V13ScorerCohortPin,
    V13ScorerCohortRotation,
    ValidatorHeartbeat,
    ValidatorSlotSettingsRevision,
    ValidatorTicket,
)
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

    def managed(hotkey: str, current_packet: dict | None = None):
        current_packet = current_packet or packet
        row = _heartbeat(hotkey, now, versions=[7, 13], protocol_version=18)
        row.benchmark_capacity = {
            "configured_slots": 1,
            "healthy_slots": ["slot-0"],
            "admission": "accepting",
            "active": [],
        }
        assert row.stack is not None and row.capabilities is not None
        row.stack["mode"] = "managed"
        row.stack["release_descriptor_digest"] = current_packet[
            "release_descriptor_digest"
        ]
        for component in row.stack["components"].values():
            component["provenance"] = "signed_descriptor"
            component["image_digest"] = current_packet["scorer_image_digest"]
            component["source_revision"] = current_packet["source_revision"]
        row.capabilities["scorer_benchmarks"]["source_revision"] = current_packet[
            "source_revision"
        ]
        row.capabilities["scorer_benchmarks"]["scored_runtime_env"] = {
            "bench_version": 13,
            "scope": "scorer-injected-env-only",
            "source_revision": current_packet["source_revision"],
            "injected_keys": keys,
            "sha256": current_packet["scorer_env_sha256"],
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
    next_source = "d" * 40
    next_packet = {
        **packet,
        "source_revision": next_source,
        "release_descriptor_digest": "sha256:" + "e" * 64,
        "scorer_image_digest": "sha256:" + "f" * 64,
        "scorer_env_sha256": hashlib.sha256(
            (
                "scored-runtime-env-v1\n13\n" + next_source + "\n" + "\n".join(keys)
            ).encode()
        ).hexdigest(),
    }
    async with session_maker() as session, session.begin():
        for hotkey in _HOTKEYS:
            heartbeat = await session.get(ValidatorHeartbeat, hotkey)
            assert heartbeat is not None
            replacement = managed(hotkey, next_packet)
            heartbeat.stack = replacement.stack
            heartbeat.capabilities = replacement.capabilities
    async with session_maker() as session:
        assert not await pinned_validator_allowed(session, hotkey=_HOTKEYS[0], now=now)
        assert (
            await scored_runtime_evidence_for_lease(
                session,
                attempt_id=uuid4(),
                artifact_sha256="f" * 64,
                policy_version=13,
                bench_version=13,
                now=now,
            )
            is None
        )
        report_evidence = await scored_runtime_evidence_for_lease(
            session,
            attempt_id=uuid4(),
            artifact_sha256="f" * 64,
            policy_version=13,
            bench_version=13,
            now=now,
            report_only_current_packet=True,
        )
        assert report_evidence is not None
        assert report_evidence.scorer_source_revision == next_source
        report_view = await get_report_only_current_packet(None, session)
        assert report_view is not None and not report_view.matches_effective_pin
        observed = await get_preflight(None, session)
        assert [
            item.packet.model_dump(mode="json")
            for item in observed.validators
            if item.hotkey in _HOTKEYS and item.packet is not None
        ] == [next_packet] * 3
    request = RotateV13ScorerCohortRequest.model_validate(
        {
            "hotkeys": list(_HOTKEYS),
            "packet": next_packet,
            "expected_current_packet": packet,
            "expected_current_rotation_id": None,
            "expected_slot_settings_revision": 2,
            "expected_slot_settings_checksum": _checksum(paused),
            "reason": "rotate signed scorer packet after fleet adoption",
            "actor": "operator@example.com",
            "confirmation": "ROTATE V13 SCORER PACKET",
        }
    )
    async with session_maker() as session, session.begin():
        heartbeat = await session.get(ValidatorHeartbeat, _HOTKEYS[0])
        assert heartbeat is not None
        assert heartbeat.benchmark_capacity is not None
        heartbeat.benchmark_capacity = {
            **heartbeat.benchmark_capacity,
            "admission": "draining",
            "healthy_slots": [],
        }
    async with session_maker() as session:
        assert await get_report_only_current_packet(None, session) is None
    async with session_maker() as session:
        with pytest.raises(HTTPException, match="not accepting"):
            await rotate_pin(request, None, session)
    async with session_maker() as session, session.begin():
        heartbeat = await session.get(ValidatorHeartbeat, _HOTKEYS[0])
        assert heartbeat is not None
        assert heartbeat.benchmark_capacity is not None
        heartbeat.benchmark_capacity = {
            **heartbeat.benchmark_capacity,
            "admission": "accepting",
            "healthy_slots": ["slot-0"],
        }
    async with session_maker() as session, session.begin():
        heartbeat = await session.get(ValidatorHeartbeat, _HOTKEYS[0])
        assert heartbeat is not None
        replacement = managed(_HOTKEYS[0], packet)
        heartbeat.stack = replacement.stack
        heartbeat.capabilities = replacement.capabilities
    async with session_maker() as session:
        with pytest.raises(HTTPException, match="member signed packet"):
            await rotate_pin(request, None, session)
    async with session_maker() as session, session.begin():
        heartbeat = await session.get(ValidatorHeartbeat, _HOTKEYS[0])
        assert heartbeat is not None
        replacement = managed(_HOTKEYS[0], next_packet)
        heartbeat.stack = replacement.stack
        heartbeat.capabilities = replacement.capabilities
    live_agent_id = await _seed_agent(
        session_maker, status=AgentStatus.EVALUATING, sha256="e" * 64
    )
    async with session_maker() as session, session.begin():
        live_ticket = ValidatorTicket(
            agent_id=live_agent_id,
            bench_version=13,
            validator_hotkey=_HOTKEYS[0],
            slot_id="slot-0",
            status=TicketStatus.ISSUED,
            issued_at=now,
            deadline=now + timedelta(minutes=30),
        )
        session.add(live_ticket)
    async with session_maker() as session:
        with pytest.raises(HTTPException, match="all live V13 tickets must drain"):
            await rotate_pin(request, None, session)
    async with session_maker() as session, session.begin():
        ticket = await session.get(ValidatorTicket, (live_agent_id, 13, _HOTKEYS[0]))
        assert ticket is not None
        ticket.status = TicketStatus.EXPIRED
    async with session_maker() as session:
        rotated = await rotate_pin(request, None, session)
        assert rotated.rotation_id == 1
    async with session_maker() as session:
        assert await pinned_validator_allowed(session, hotkey=_HOTKEYS[0], now=now)
        history = await get_history(None, session)
        assert len(history) == 2
        assert history[0].packet.source_revision == source
        assert history[1].packet.source_revision == next_source
        assert history[1].previous_packet is not None
        assert history[1].previous_packet.source_revision == source
        assert await session.get(V13ScorerCohortPin, 13) is not None
        rotation = await session.get(V13ScorerCohortRotation, 1)
        assert rotation is not None
        rotation.reason = "tampered"
        with pytest.raises(DBAPIError, match="immutable"):
            await session.flush()
    async with session_maker() as session:
        with pytest.raises(HTTPException, match="current V13 cohort changed"):
            await rotate_pin(request, None, session)
    third_source = "1" * 40
    third_packet = {
        **next_packet,
        "source_revision": third_source,
        "release_descriptor_digest": "sha256:" + "2" * 64,
        "scorer_image_digest": "sha256:" + "3" * 64,
        "scorer_env_sha256": hashlib.sha256(
            (
                "scored-runtime-env-v1\n13\n" + third_source + "\n" + "\n".join(keys)
            ).encode()
        ).hexdigest(),
    }
    async with session_maker() as session, session.begin():
        for hotkey in _HOTKEYS:
            heartbeat = await session.get(ValidatorHeartbeat, hotkey)
            assert heartbeat is not None
            replacement = managed(hotkey, third_packet)
            heartbeat.stack = replacement.stack
            heartbeat.capabilities = replacement.capabilities
    second = request.model_copy(
        update={
            "packet": RotateV13ScorerCohortRequest.model_validate(
                {**request.model_dump(mode="json"), "packet": third_packet}
            ).packet,
            "expected_current_packet": request.packet,
            "expected_current_rotation_id": 1,
        }
    )
    async with session_maker() as session:
        rotated_again = await rotate_pin(second, None, session)
        assert rotated_again.rotation_id == 2
    async with session_maker() as session:
        assert len(await get_history(None, session)) == 3
    async with session_maker() as session:
        with pytest.raises(HTTPException, match="current V13 cohort changed"):
            await rotate_pin(second, None, session)
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
