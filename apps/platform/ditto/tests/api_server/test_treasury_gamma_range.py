"""Real-PG Gamma ranges: the Backroom % moves inside the signed range only."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from ditto.api_models.treasury_runtime import TreasuryRuntimeSettings
from ditto.api_server.treasury_runtime import canonical_settings
from ditto.api_server.treasury_weights import enforcing_pin_from_observation
from ditto.db.models import (
    TreasuryRuntimeRevision,
    TreasurySettingsRevision,
    ValidatorHeartbeat,
)
from ditto.tests.api_server.endpoints.test_admin_treasury_settings import (
    _HEADERS,
    _install,
)
from ditto.tests.api_server.test_treasury_weights import app_state, heartbeat, pin
from ditto_screening_protocol.treasury import TreasuryEmissionPolicy
from ditto_screening_protocol.treasury_approval import TreasuryPolicyApproval
from ditto_screening_protocol.treasury_enforcement import (
    EnforcingTreasuryPin,
    TreasuryFleetMember,
)

URL = "/api/v1/admin/treasury-settings"


def ranged_pin(*, low: int = 0, high: int = 7500) -> EnforcingTreasuryPin:
    """The shared fixture pin re-signed (synthetically) with a Gamma range."""
    p = pin()
    policy = TreasuryEmissionPolicy.model_validate(
        {
            **p.policy.model_dump(mode="json"),
            "buckets": [
                {
                    **b.model_dump(mode="json"),
                    "min_allocation_bps": low,
                    "max_allocation_bps": high,
                }
                for b in p.policy.buckets
            ],
        }
    )
    fleet = [
        TreasuryFleetMember.model_validate(
            {**m.model_dump(mode="json"), "approved_policy_digest": policy.digest}
        )
        for m in p.fleet
    ]
    return EnforcingTreasuryPin(
        epoch_index=p.epoch_index,
        first_block=p.first_block,
        pinned_block=p.pinned_block,
        pinned_block_hash=p.pinned_block_hash,
        policy=policy,
        policy_digest=policy.digest,
        identity=p.identity,
        approval=TreasuryPolicyApproval(policy=policy, signature="0x" + "ab" * 64),
        fleet=fleet,
        allocations=[
            {"bucket_id": b.bucket_id, "allocation_bps": b.allocation_bps}
            for b in policy.buckets
        ],
    )


def settings(p: EnforcingTreasuryPin, gamma_bps: int, **changes) -> dict:
    return {
        "allocation_version": 2,
        "treasury_hotkey": p.policy.collector_hotkey,
        "treasury_coldkey": p.policy.collector_coldkey,
        "service_buckets": [
            {
                "bucket_id": b.bucket_id,
                "purpose": "Gamma service funding",
                "allocation_bps": gamma_bps,
                "holding_coldkey": b.holding_coldkey,
            }
            for b in p.policy.buckets
        ],
        **changes,
    }


async def seed_settings(session, raw: dict, parent: int = 0) -> None:
    session.add(
        TreasurySettingsRevision(
            parent_revision=parent,
            settings=raw,
            checksum=canonical_settings(raw),
            actor="test-principal",
            reason="Public Gamma allocation",
        )
    )
    await session.flush()


async def seed_heartbeat(session, p: EnforcingTreasuryPin) -> None:
    now = datetime.now(UTC)
    row = heartbeat(now)
    row.capabilities["treasury_weights"]["approved_policy_digest"] = p.policy_digest
    session.add(
        ValidatorHeartbeat(
            validator_hotkey=row.validator_hotkey,
            protocol_version=30,
            software_version="0.358.0",
            seen_at=now,
            reported_at=now,
            signature="ab" * 64,
            capabilities=row.capabilities,
            state="idle",
            active_agent_id=None,
            code_digest="a" * 64,
        )
    )
    await session.flush()


async def produce(session, p: EnforcingTreasuryPin) -> EnforcingTreasuryPin:
    schedule = SimpleNamespace(
        subnet_epoch_index=p.epoch_index,
        last_epoch_block=p.first_block,
        block=p.pinned_block,
        block_hash=p.pinned_block_hash,
    )
    shadow = SimpleNamespace(policy=p.policy, identity=p.identity)
    return await enforcing_pin_from_observation(app_state(p), session, shadow, schedule)


async def test_producer_pins_the_current_in_range_gamma_allocation(
    session, monkeypatch
):
    monkeypatch.setattr(
        "ditto.api_server.treasury_weights.verify_public_signature", lambda *_: True
    )
    p = ranged_pin()
    await seed_heartbeat(session, p)
    await seed_settings(session, settings(p, 6000))
    produced = await produce(session, p)
    assert produced.service_bps == 6000
    assert produced.policy.service_bps == 1000
    assert [(a.bucket_id, a.allocation_bps) for a in produced.allocations] == [
        ("gamma", 6000)
    ]


@pytest.mark.parametrize("fault", ["absent", "above_range", "wallet"])
async def test_producer_refuses_gamma_outside_the_signed_range(
    session, monkeypatch, fault
):
    monkeypatch.setattr(
        "ditto.api_server.treasury_weights.verify_public_signature", lambda *_: True
    )
    p = ranged_pin()
    await seed_heartbeat(session, p)
    if fault == "above_range":
        await seed_settings(session, settings(p, 7600))
    elif fault == "wallet":
        await seed_settings(
            session, settings(p, 5000, treasury_hotkey=p.fleet[0].validator_hotkey)
        )
    with pytest.raises(ValueError, match="outside the signed policy range"):
        await produce(session, p)


async def enforce_runtime(session_maker, p: EnforcingTreasuryPin) -> None:
    raw = TreasuryRuntimeSettings(
        mode="enforce",
        approval=p.approval,
        approved_policy_digest=p.policy_digest,
        collector_policy_digest=p.policy.collector_policy_digest,
        managed_validator_hotkeys=tuple(m.validator_hotkey for m in p.fleet),
        activation_epoch=p.epoch_index,
    ).model_dump(mode="json")
    async with session_maker() as s:
        await seed_settings(s, settings(p, p.policy.service_bps))
        s.add(
            TreasuryRuntimeRevision(
                parent_revision=0,
                settings=raw,
                checksum=canonical_settings(raw),
                actor="test-principal",
                reason="Enforce the signed Gamma range",
            )
        )
        await s.commit()


def request(raw: dict, revision: int) -> dict:
    return {
        "expected_revision": revision,
        "settings": raw,
        "reason": "Move Gamma inside its signed range",
        "confirmation": "RECORD TREASURY SHADOW POLICY",
    }


def guard_signatures(app, session_maker, monkeypatch) -> None:
    _install(app, session_maker)
    monkeypatch.setattr(
        "ditto.api_server.treasury_runtime.verify_public_signature", lambda *_: True
    )


async def test_enforcing_gamma_moves_inside_the_signed_range_without_pause(
    app, client, session_maker, monkeypatch
):
    guard_signatures(app, session_maker, monkeypatch)
    p = ranged_pin()
    await enforce_runtime(session_maker, p)
    control = await client.get(URL, headers=_HEADERS)
    assert control.json()["weight_effect"] == "next_epoch"
    revision = 1
    for gamma in (0, 7500):
        moved = await client.post(
            URL, headers=_HEADERS, json=request(settings(p, gamma), revision)
        )
        assert moved.status_code == 200, moved.text
        assert moved.json()["settings"]["service_buckets"][0]["allocation_bps"] == gamma
        revision = moved.json()["revision"]
    above = await client.post(
        URL, headers=_HEADERS, json=request(settings(p, 7501), revision)
    )
    assert above.status_code == 409
    assert (
        above.json()["message"] == "Gamma allocation is outside the signed policy range"
    )
    rewired = settings(p, 5000, treasury_hotkey=p.fleet[0].validator_hotkey)
    wallet = await client.post(URL, headers=_HEADERS, json=request(rewired, revision))
    assert wallet.status_code == 409
    assert wallet.json()["message"] == "Pause Gamma before changing treasury settings"
    async with session_maker() as s:
        latest = await s.scalar(
            select(TreasurySettingsRevision).order_by(
                TreasurySettingsRevision.revision.desc()
            )
        )
        assert latest.revision == revision
        assert latest.settings["service_buckets"][0]["allocation_bps"] == 7500


async def test_fixed_signed_policy_still_requires_pause_and_a_new_signature(
    app, client, session_maker, monkeypatch
):
    guard_signatures(app, session_maker, monkeypatch)
    p = pin()
    await enforce_runtime(session_maker, p)
    control = await client.get(URL, headers=_HEADERS)
    assert control.json()["weight_effect"] == "none"
    response = await client.post(
        URL, headers=_HEADERS, json=request(settings(p, 2000), 1)
    )
    assert response.status_code == 409
    assert response.json()["message"] == "Pause Gamma before changing treasury settings"


@pytest.mark.parametrize("pinned_gamma,stored", [(1000, True), (6000, False)])
async def test_epoch_insert_refuses_a_gamma_change_during_materialization(
    app, session_maker, monkeypatch, pinned_gamma, stored
):
    from unittest.mock import AsyncMock

    from ditto.api_server.endpoints.scoring import _LedgerSnapshot
    from ditto.api_server.ledger_pin import LedgerPinMaterializer
    from ditto.db.models import LedgerEpochSnapshot
    from ditto.tests.api_server.test_ledger_pin import _schedule

    guard_signatures(app, session_maker, monkeypatch)
    schedule = _schedule()
    p = ranged_pin()
    # Settings record 1000 bps; the observed pin claims what it folded.
    await enforce_runtime(session_maker, p)
    raw = p.model_dump(mode="json")
    raw.update(
        epoch_index=schedule.subnet_epoch_index,
        first_block=schedule.last_epoch_block,
        pinned_block=schedule.block,
        pinned_block_hash=schedule.block_hash,
        allocations=[{"bucket_id": "gamma", "allocation_bps": pinned_gamma}],
    )
    raw["identity"].update(
        finalized_block=schedule.block, finalized_block_hash=schedule.block_hash
    )
    observed = EnforcingTreasuryPin.model_validate(raw)
    monkeypatch.setattr(
        "ditto.api_server.endpoints.scoring.materialize_ledger_snapshot",
        AsyncMock(
            return_value=_LedgerSnapshot(
                entries=[], generated_at=datetime.now(UTC), active_bench_version=13
            )
        ),
    )
    monkeypatch.setattr(
        "ditto.api_server.ledger_pin.observe_shadow_treasury",
        AsyncMock(return_value=object()),
    )
    monkeypatch.setattr(
        "ditto.api_server.treasury_weights.enforcing_pin_from_observation",
        AsyncMock(return_value=observed),
    )
    result = await LedgerPinMaterializer().ensure(
        app.state, session_maker, now=datetime.now(UTC), schedule=schedule
    )
    async with session_maker() as s:
        rows = list(await s.scalars(select(LedgerEpochSnapshot)))
    assert (result is not None) is stored
    assert len(rows) == int(stored)
