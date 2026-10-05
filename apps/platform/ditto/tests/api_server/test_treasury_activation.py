"""Prospective public policy reads preserve managed-fleet and authority gates."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from ditto.api_server.treasury_activation import setter_preflight
from ditto.api_server.treasury_weights import treasury_fleet_members
from ditto.tests.api_server.test_treasury_weights import heartbeat, pin


@pytest.mark.parametrize(
    "fault,expected",
    [
        ("none", "ready"),
        ("absent", "missing_heartbeat"),
        ("stale", "heartbeat_outside_window"),
        ("future", "heartbeat_outside_window"),
        ("missing", "missing_guard"),
        ("invalid", "invalid_heartbeat"),
        ("legacy", "unsupported_protocol"),
        ("policy", "policy_mismatch"),
        ("collector", "policy_mismatch"),
    ],
)
def test_status_matches_authoritative_gate(fault, expected):
    now = datetime.now(UTC)
    row = heartbeat(now)
    if fault == "absent":
        row = None
    elif fault == "stale":
        row.seen_at -= timedelta(minutes=16)
    elif fault == "future":
        row.seen_at += timedelta(seconds=1)
    elif fault == "missing":
        row.capabilities.pop("treasury_weights")
    elif fault == "invalid":
        row.capabilities = None
    elif fault == "legacy":
        row.protocol_version = 29
    elif fault in {"policy", "collector"}:
        field = (
            "approved_policy_digest" if fault == "policy" else "collector_policy_digest"
        )
        row.capabilities["treasury_weights"][field] = "f" * 64
    p = pin()
    result = setter_preflight(
        row,
        hotkey=p.fleet[0].validator_hotkey,
        required=True,
        now=now,
        policy_digest=p.policy_digest,
        collector_digest=p.policy.collector_policy_digest,
    )
    assert result.status == expected
    if expected == "ready":
        assert (
            treasury_fleet_members(
                [row],
                now=now,
                policy_digest=p.policy_digest,
                collector_digest=p.policy.collector_policy_digest,
            )
            == p.fleet
        )
    else:
        with pytest.raises((ValueError, TypeError)):
            treasury_fleet_members(
                [row] if row else [],
                now=now,
                policy_digest=p.policy_digest,
                collector_digest=p.policy.collector_policy_digest,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "missing_setter",
        "extra_legacy",
        "stale_required",
        "chain",
        "identity_timeout",
        "identity_step_timeout",
        "roster_timeout",
        "roster_connection",
        "reader_missing",
        "identity",
        "empty",
        "duplicate",
        "signature",
        "digest",
    ],
)
async def test_public_preflight_is_bounded_read_only_and_exact(
    app, client, session_maker, monkeypatch, fault
):
    from ditto.tests.api_server.endpoints.test_admin_treasury_settings import (
        _HEADERS,
        _URL,
        _install,
    )
    from ditto.tests.api_server.test_treasury_weights import add_runtime, app_state
    from ditto_screening_protocol.treasury_identity import TreasuryDispatchObservation

    _install(app, session_maker)
    p = pin()
    state = app_state(p)
    # Prospective signed policy is deliberately NOT configured in Platform.
    app.state.config = replace(
        app.state.config,
        treasury_shadow_approval=None,
        treasury_approved_policy_digest=None,
        treasury_approved_collector_policy_digest=None,
    )
    app.state.chain = state.chain
    monkeypatch.setattr(
        "ditto.api_server.treasury_activation.verify_public_signature",
        lambda *_: fault != "signature",
    )
    now = datetime.now(UTC)
    async with session_maker() as session:
        row = await add_runtime(session, now)
        if fault == "extra_legacy":
            from ditto.db.models import ValidatorHeartbeat

            session.add(
                ValidatorHeartbeat(
                    validator_hotkey=p.policy.collector_hotkey,
                    software_version="0.350.0",
                    protocol_version=29,
                    code_digest="a" * 64,
                    state="idle",
                    capabilities=heartbeat(now).capabilities,
                    signature="ab" * 64,
                    reported_at=now,
                    seen_at=now,
                )
            )
        if fault == "stale_required":
            row.seen_at = now - timedelta(minutes=16)
        await session.commit()
    if fault == "missing_setter":
        state.chain.get_treasury_weight_setters.return_value = (
            *state.chain.get_treasury_weight_setters.return_value,
            p.policy.collector_hotkey,
        )
    elif fault == "chain":
        state.chain.get_treasury_dispatch_observation.side_effect = RuntimeError(
            "PRIVATE PROVIDER ERROR"
        )
    elif fault == "identity_timeout":
        state.chain.get_treasury_dispatch_observation.side_effect = TimeoutError(
            "PRIVATE PROVIDER ERROR"
        )
    elif fault == "identity_step_timeout":
        from ditto.chain.errors import ChainTreasuryReadTimeoutError

        state.chain.get_treasury_dispatch_observation.side_effect = (
            ChainTreasuryReadTimeoutError("epoch_storage")
        )
    elif fault == "roster_timeout":
        state.chain.get_treasury_weight_setters.side_effect = TimeoutError(
            "PRIVATE PROVIDER ERROR"
        )
    elif fault == "roster_connection":
        state.chain.get_treasury_weight_setters.side_effect = ConnectionError(
            "PRIVATE PROVIDER ERROR"
        )
    elif fault == "reader_missing":
        state.chain.get_treasury_dispatch_observation.side_effect = AttributeError(
            "PRIVATE PROVIDER ERROR"
        )
    elif fault == "identity":
        bad = p.identity.model_copy(
            update={"owner_coldkey": p.policy.buckets[0].holding_coldkey}
        )
        state.chain.get_treasury_dispatch_observation.return_value = (
            TreasuryDispatchObservation(
                identity=bad,
                epoch_index=p.epoch_index,
                first_block=p.first_block,
                finalized_block=bad.finalized_block,
                finalized_block_hash=bad.finalized_block_hash,
            )
        )
    elif fault == "empty":
        state.chain.get_treasury_weight_setters.return_value = ()
    elif fault == "duplicate":
        state.chain.get_treasury_weight_setters.return_value = (
            p.fleet[0].validator_hotkey,
        ) * 2
    payload = {
        "approval": p.approval.model_dump(mode="json"),
        "expected_policy_digest": "f" * 64 if fault == "digest" else p.policy_digest,
        "expected_collector_policy_digest": p.policy.collector_policy_digest,
        "managed_validator_hotkeys": [m.validator_hotkey for m in p.fleet],
    }
    if fault == "missing_setter":
        payload["managed_validator_hotkeys"].append(p.policy.collector_hotkey)
    response = await client.post(
        f"{_URL}/activation-preflight", headers=_HEADERS, json=payload
    )
    assert response.headers["cache-control"] == "no-store"
    if fault in {"signature", "digest"}:
        assert response.status_code == 400
        state.chain.get_treasury_dispatch_observation.assert_not_awaited()
    else:
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["fleet_ready_for_proposed_policy"] is (
            fault in {"none", "extra_legacy"}
        )
        assert result["can_enforce_weights"] is False
        assert result["copy_behavior_verified"] is False
        assert result["configured_policy_matches"] is False
        assert result["weight_effect"] == "none"
        assert "PRIVATE PROVIDER ERROR" not in response.text
        if fault not in {
            "chain",
            "identity",
            "identity_timeout",
            "identity_step_timeout",
            "reader_missing",
        }:
            state.chain.get_treasury_weight_setters.assert_awaited_once_with(
                p.policy, block_hash=p.identity.finalized_block_hash
            )
        failures = {
            "chain": ("identity", "unavailable"),
            "identity_timeout": ("identity", "timeout"),
            "identity_step_timeout": ("identity", "timeout"),
            "roster_timeout": ("setter_roster", "timeout"),
            "roster_connection": ("setter_roster", "connection"),
            "reader_missing": ("identity", "reader_unavailable"),
            "identity": ("identity", "invalid_evidence"),
            "empty": ("setter_roster", "invalid_evidence"),
            "duplicate": ("setter_roster", "invalid_evidence"),
        }
        assert (
            result["chain_failure_stage"],
            result["chain_failure_kind"],
        ) == failures.get(fault, (None, None))
        assert result["chain_failure_step"] == (
            "epoch_storage" if fault == "identity_step_timeout" else None
        )
        if fault == "missing_setter":
            assert result["required_setter_count"] == 2
            assert {s["status"] for s in result["setters"]} == {
                "ready",
                "missing_heartbeat",
            }
        if fault == "extra_legacy":
            assert result["required_setter_count"] == 1
            assert len(result["setters"]) == 1
            assert result["setters"][0]["status"] == "ready"
    # No settings revision or observation was created by a read-only POST.
    settings = (await client.get(_URL, headers=_HEADERS)).json()
    assert settings["revision"] == 0
    assert settings["history"] == []


@pytest.mark.asyncio
async def test_preflight_requires_admin(app, client, session_maker):
    from ditto.tests.api_server.endpoints.test_admin_treasury_settings import (
        _URL,
        _install,
    )

    _install(app, session_maker)
    p = pin()
    payload = {
        "approval": p.approval.model_dump(mode="json"),
        "expected_policy_digest": p.policy_digest,
        "expected_collector_policy_digest": p.policy.collector_policy_digest,
        "managed_validator_hotkeys": [m.validator_hotkey for m in p.fleet],
    }
    assert (
        await client.post(f"{_URL}/activation-preflight", json=payload)
    ).status_code in {401, 403}


@pytest.mark.asyncio
@pytest.mark.parametrize("large_roster", [False, True])
async def test_independent_inventory_does_not_block_managed_fleet(
    app, client, session_maker, monkeypatch, large_roster
):
    from ditto.db.models import ValidatorHeartbeat
    from ditto.tests.api_server.endpoints.test_admin_treasury_settings import (
        _HEADERS,
        _URL,
        _install,
    )
    from ditto.tests.api_server.test_treasury_weights import add_runtime, app_state

    _install(app, session_maker)
    p = pin()
    state = app_state(p)
    app.state.chain = state.chain
    monkeypatch.setattr(
        "ditto.api_server.treasury_activation.verify_public_signature", lambda *_: True
    )
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    hotkeys = [
        "5" + "1" * 44 + "1" + alphabet[i // 58] + alphabet[i % 58] for i in range(513)
    ]
    now = datetime.now(UTC)
    async with session_maker() as session:
        await add_runtime(session, now)
        if not large_roster:
            session.add_all(
                [
                    ValidatorHeartbeat(
                        validator_hotkey=h,
                        software_version="0.350.0",
                        protocol_version=30,
                        code_digest="a" * 64,
                        state="idle",
                        capabilities=heartbeat(now).capabilities,
                        signature="ab" * 64,
                        reported_at=now,
                        seen_at=now,
                    )
                    for h in hotkeys
                ]
            )
        await session.commit()
    if large_roster:
        state.chain.get_treasury_weight_setters.return_value = (
            p.fleet[0].validator_hotkey,
            *hotkeys,
        )
    response = await client.post(
        f"{_URL}/activation-preflight",
        headers=_HEADERS,
        json={
            "approval": p.approval.model_dump(mode="json"),
            "expected_policy_digest": p.policy_digest,
            "expected_collector_policy_digest": p.policy.collector_policy_digest,
            "managed_validator_hotkeys": [m.validator_hotkey for m in p.fleet],
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(result["setters"]) == 1
    assert result["truncated"] is False
    assert result["fleet_ready_for_proposed_policy"] is True
    assert result["blocking_reasons"] == []
    assert result["required_setter_count"] == 1
    assert result["chain_permitted_setter_count"] == (514 if large_roster else 1)
    assert result["can_enforce_weights"] is False


def test_unread_inventory_does_not_claim_a_missing_heartbeat():
    p = pin()
    result = setter_preflight(
        None,
        hotkey=p.fleet[0].validator_hotkey,
        required=True,
        now=datetime.now(UTC),
        policy_digest=p.policy_digest,
        collector_digest=p.policy.collector_policy_digest,
        inventory_complete=False,
    )
    assert result.status == "inventory_not_checked"


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["none", "stale", "missing", "permit_lost"])
async def test_three_managed_of_thirteen_permitted(
    app, client, session_maker, monkeypatch, fault
):
    from ditto.db.models import ValidatorHeartbeat
    from ditto.tests.api_server.endpoints.test_admin_treasury_settings import (
        _HEADERS,
        _URL,
        _install,
    )
    from ditto.tests.api_server.test_treasury_weights import app_state

    _install(app, session_maker)
    p = pin()
    state = app_state(p)
    app.state.chain = state.chain
    monkeypatch.setattr(
        "ditto.api_server.treasury_activation.verify_public_signature", lambda *_: True
    )
    # Valid synthetic Address strings; external setters have no Platform heartbeat.
    keys = [p.fleet[0].validator_hotkey] + ["5" + "1" * 46 + c for c in "23456789ABCD"]
    managed = keys[:3]
    state.chain.get_treasury_weight_setters.return_value = tuple(
        keys[1:] if fault == "permit_lost" else keys
    )
    now = datetime.now(UTC)
    async with session_maker() as session:
        for i, h in enumerate(managed):
            if fault == "missing" and i == 2:
                continue
            session.add(
                ValidatorHeartbeat(
                    validator_hotkey=h,
                    software_version="0.350.0",
                    protocol_version=30,
                    code_digest="a" * 64,
                    state="idle",
                    capabilities=heartbeat(now).capabilities,
                    signature="ab" * 64,
                    reported_at=now,
                    seen_at=now - timedelta(minutes=16)
                    if fault == "stale" and i == 2
                    else now,
                )
            )
        await session.commit()
    response = await client.post(
        f"{_URL}/activation-preflight",
        headers=_HEADERS,
        json={
            "approval": p.approval.model_dump(mode="json"),
            "expected_policy_digest": p.policy_digest,
            "expected_collector_policy_digest": p.policy.collector_policy_digest,
            "managed_validator_hotkeys": managed,
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["gate_scope"] == "managed_validators"
    assert result["required_setter_count"] == 3
    assert result["chain_permitted_setter_count"] == (
        12 if fault == "permit_lost" else 13
    )
    assert {r["validator_hotkey"] for r in result["setters"]} == set(managed)
    assert result["fleet_ready_for_proposed_policy"] is (fault == "none")
    assert result["copy_behavior_verified"] is False
    if fault == "permit_lost":
        assert "managed_setter_not_permitted" in result["blocking_reasons"]
    elif fault != "none":
        assert "setter_proof_missing" in result["blocking_reasons"]


def test_explicit_managed_roster_is_bounded_and_distinct():
    from pydantic import ValidationError

    from ditto.api_models.treasury_activation import TreasuryActivationPreflightRequest
    from ditto.api_models.treasury_runtime import TreasuryRuntimeSettings

    p = pin()
    common = {
        "approval": p.approval,
        "expected_policy_digest": p.policy_digest,
        "expected_collector_policy_digest": p.policy.collector_policy_digest,
    }
    h = p.fleet[0].validator_hotkey
    for keys in [(h, h), (h,) * 129]:
        with pytest.raises(ValidationError):
            TreasuryActivationPreflightRequest(**common, managed_validator_hotkeys=keys)
    for keys in [(), (h, h), (h,) * 129]:
        with pytest.raises(ValidationError):
            TreasuryRuntimeSettings(
                mode="observe",
                approval=p.approval,
                approved_policy_digest=p.policy_digest,
                collector_policy_digest=p.policy.collector_policy_digest,
                managed_validator_hotkeys=keys,
            )


@pytest.mark.asyncio
async def test_no_operator_roster_never_defaults_to_all_chain_validators(
    app, client, session_maker, monkeypatch
):
    from ditto.tests.api_server.endpoints.test_admin_treasury_settings import (
        _HEADERS,
        _URL,
        _install,
    )
    from ditto.tests.api_server.test_treasury_weights import add_runtime, app_state

    _install(app, session_maker)
    p = pin()
    app.state.chain = app_state(p).chain
    monkeypatch.setattr(
        "ditto.api_server.treasury_activation.verify_public_signature", lambda *_: True
    )
    async with session_maker() as session:
        await add_runtime(session, datetime.now(UTC))
        await session.commit()
    response = await client.post(
        f"{_URL}/activation-preflight",
        headers=_HEADERS,
        json={
            "approval": p.approval.model_dump(mode="json"),
            "expected_policy_digest": p.policy_digest,
            "expected_collector_policy_digest": p.policy.collector_policy_digest,
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["fleet_ready_for_proposed_policy"] is False
    assert result["managed_validator_hotkeys"] == []
    assert "managed_roster_missing" in result["blocking_reasons"]
