"""Real-PG Gamma controls: no activation from intent, stale CAS or partial fleet."""

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from ditto.db.models import TreasuryRuntimeRevision, TreasurySettingsRevision
from ditto.tests.api_server.endpoints.test_admin_treasury_settings import (
    _HEADERS,
    _install,
)
from ditto.tests.api_server.test_treasury_weights import add_runtime, app_state, pin

URL = "/api/v1/admin/treasury-runtime"


@pytest.mark.parametrize(
    "error", [ValueError("invalid control"), SQLAlchemyError("unavailable")]
)
@pytest.mark.parametrize("pin_mode", ["epoch", "time"])
async def test_scoring_runtime_read_refuses_unverifiable_control(
    monkeypatch, error, pin_mode
):
    from unittest.mock import AsyncMock

    from ditto.api_models import LedgerResponse
    from ditto.api_server.endpoints.scoring import (
        _require_statistical_cap_requester,
        _serve_epoch_pin,
    )

    monkeypatch.setattr(
        "ditto.api_server.treasury_runtime.treasury_runtime",
        AsyncMock(side_effect=error),
    )
    state = SimpleNamespace(config=SimpleNamespace())
    context = SimpleNamespace(
        policy=SimpleNamespace(
            continual_retest=SimpleNamespace(ledger_pin_mode=pin_mode)
        )
    )
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    now = datetime.now(UTC)
    with pytest.raises(HTTPException) as refused:
        await _serve_epoch_pin(request, None, "validator", context=context, now=now)
    assert refused.value.status_code == 503
    with pytest.raises(HTTPException) as refused:
        await _require_statistical_cap_requester(
            None,
            "validator",
            LedgerResponse(entries=[], count=0),
            now=now,
            app_state=state,
        )
    assert refused.value.status_code == 503


@pytest.mark.parametrize("initial_enforcement", [False, True])
async def test_scoring_refreshes_runtime_after_epoch_materialization(
    monkeypatch, initial_enforcement
):
    from unittest.mock import AsyncMock

    from ditto.api_server.endpoints.scoring import _serve_epoch_pin

    read = AsyncMock(
        side_effect=[
            SimpleNamespace(treasury_weight_enforcement=initial_enforcement),
            SimpleNamespace(treasury_weight_enforcement=not initial_enforcement),
        ]
    )
    monkeypatch.setattr("ditto.api_server.treasury_runtime.treasury_runtime", read)
    materializer = SimpleNamespace(
        ensure=AsyncMock(return_value=None), latest=AsyncMock(return_value=None)
    )
    state = SimpleNamespace(
        config=SimpleNamespace(chain=SimpleNamespace(netuid=118)),
        ledger_pin_materializer=materializer,
        session_maker=object(),
    )
    context = SimpleNamespace(
        policy=SimpleNamespace(
            continual_retest=SimpleNamespace(ledger_pin_mode="epoch")
        )
    )
    session = SimpleNamespace(in_transaction=lambda: False)

    async def serve():
        return await _serve_epoch_pin(
            SimpleNamespace(app=SimpleNamespace(state=state)),
            session,
            "validator",
            context=context,
            now=datetime.now(UTC),
        )

    if initial_enforcement:
        assert await serve() is None
        materializer.latest.assert_awaited_once()
    else:
        with pytest.raises(HTTPException) as refused:
            await serve()
        assert refused.value.status_code == 503
        assert refused.value.detail == "enforcing treasury epoch unavailable"
    assert read.await_count == 2
    materializer.ensure.assert_awaited_once()


@pytest.mark.parametrize(
    "error", [ValueError("invalid control"), SQLAlchemyError("unavailable")]
)
async def test_scoring_runtime_read_failure_after_materialization_refuses(
    monkeypatch, error
):
    from unittest.mock import AsyncMock

    from ditto.api_server.endpoints.scoring import _serve_epoch_pin

    read = AsyncMock(
        side_effect=[SimpleNamespace(treasury_weight_enforcement=False), error]
    )
    monkeypatch.setattr("ditto.api_server.treasury_runtime.treasury_runtime", read)
    materializer = SimpleNamespace(ensure=AsyncMock(return_value=None))
    state = SimpleNamespace(
        config=SimpleNamespace(),
        ledger_pin_materializer=materializer,
        session_maker=object(),
    )
    context = SimpleNamespace(
        policy=SimpleNamespace(
            continual_retest=SimpleNamespace(ledger_pin_mode="epoch")
        )
    )
    with pytest.raises(HTTPException) as refused:
        await _serve_epoch_pin(
            SimpleNamespace(app=SimpleNamespace(state=state)),
            SimpleNamespace(in_transaction=lambda: False),
            "validator",
            context=context,
            now=datetime.now(UTC),
        )
    assert refused.value.status_code == 503
    assert read.await_count == 2


@pytest.mark.parametrize("enforcement", [False, True])
def test_durable_runtime_refuses_cached_fallback_after_non_enforcing_read(
    enforcement,
):
    from ditto.api_server.endpoints.scoring import _serve_last_known

    # An observe/pause read can precede another process's activation. After the
    # DB fails, that earlier read cannot prove that the latest control is safe.
    request = SimpleNamespace(
        state=SimpleNamespace(
            treasury_runtime=SimpleNamespace(
                revision=1, treasury_weight_enforcement=enforcement
            )
        ),
        app=SimpleNamespace(
            state=SimpleNamespace(
                config=SimpleNamespace(treasury_weight_enforcement=False),
                ledger_snapshot=SimpleNamespace(treasury_pin=None),
            )
        ),
    )
    with pytest.raises(HTTPException) as refused:
        _serve_last_known(request, "validator", SQLAlchemyError("unavailable"))
    assert refused.value.status_code == 503
    assert refused.value.detail == "treasury ledger verification unavailable"


def payload(mode="observe", revision=0):
    p = pin()
    return {
        "expected_revision": revision,
        "settings": {
            "version": 1,
            "mode": mode,
            "approval": p.approval.model_dump(mode="json"),
            "approved_policy_digest": p.policy_digest,
            "collector_policy_digest": p.policy.collector_policy_digest,
            "managed_validator_hotkeys": [m.validator_hotkey for m in p.fleet],
            "activation_epoch": p.epoch_index + 1 if mode == "enforce" else None,
        },
        "reason": "Activate only the exact public approval under full fleet proof",
        "confirmation": f"GAMMA {mode.upper()} {p.policy_digest}",
    }


def setup(app, session_maker, monkeypatch):
    _install(app, session_maker)
    app.state.config = replace(
        app.state.config,
        treasury_shadow_policy=None,
        treasury_shadow_approval=None,
        treasury_approved_policy_digest=None,
        treasury_approved_collector_policy_digest=None,
        treasury_weight_enforcement=False,
    )
    app.state.chain = app_state(pin()).chain
    for module in (
        "treasury_activation",
        "treasury_runtime",
        "endpoints.admin_treasury_runtime",
    ):
        monkeypatch.setattr(
            f"ditto.api_server.{module}.verify_public_signature", lambda *_: True
        )

    async def epoch_context(*_, **__):
        return SimpleNamespace(
            policy=SimpleNamespace(
                continual_retest=SimpleNamespace(ledger_pin_mode="epoch")
            )
        )

    monkeypatch.setattr(
        "ditto.api_server.endpoints.scoring.resolve_ledger_context", epoch_context
    )


async def seed_shadow(session_maker):
    from ditto.api_server.treasury_runtime import canonical_settings

    p = pin().policy
    raw = {
        "allocation_version": 2,
        "treasury_hotkey": p.collector_hotkey,
        "treasury_coldkey": p.collector_coldkey,
        "service_buckets": [
            {**b.model_dump(), "purpose": "Test service funding"} for b in p.buckets
        ],
    }
    async with session_maker() as s:
        s.add(
            TreasurySettingsRevision(
                parent_revision=0,
                settings=raw,
                checksum=canonical_settings(raw),
                actor="test-principal",
                reason="Existing public shadow proposal",
            )
        )
        await s.commit()


async def test_observe_persists_across_processes_without_config_mutation(
    app, client, session_maker, monkeypatch
):
    from ditto.api_server.treasury_runtime import treasury_runtime

    setup(app, session_maker, monkeypatch)
    original = app.state.config
    r = await client.post(URL, headers=_HEADERS, json=payload())
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    assert r.json()["actor"] == "platform_admin_token"
    assert app.state.config is original
    async with session_maker() as s:
        runtime = await treasury_runtime(s, original)
        assert runtime.revision == r.json()["revision"]
        assert runtime.treasury_shadow_approval == pin().approval
        assert not runtime.treasury_weight_enforcement
    control = await client.get(URL, headers=_HEADERS)
    assert control.json()["can_enforce_weights"] is False
    assert control.json()["transfers_enabled"] is False
    # A second identical request cannot reuse the old CAS token.
    assert (await client.post(URL, headers=_HEADERS, json=payload())).status_code == 409


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "roster_drift",
        "epoch",
        "shadow",
        "ledger",
        "signature",
        "confirmation",
        "chain",
        "direct",
    ],
)
async def test_activation_refuses_missing_or_changed_proof(
    app, client, session_maker, monkeypatch, fault
):
    setup(app, session_maker, monkeypatch)
    revision = 0
    if fault != "direct":
        first = await client.post(URL, headers=_HEADERS, json=payload())
        assert first.status_code == 200
        revision = first.json()["revision"]
    await seed_shadow(session_maker)
    if fault != "missing":
        async with session_maker() as s:
            await add_runtime(s, datetime.now(UTC))
            await s.commit()
    data = payload("enforce", revision)
    if fault == "roster_drift":
        data["settings"]["managed_validator_hotkeys"].append(
            pin().policy.collector_hotkey
        )
    elif fault == "epoch":
        data["settings"]["activation_epoch"] -= 1
    elif fault == "shadow":
        from ditto.api_server.treasury_runtime import canonical_settings

        async with session_maker() as s:
            row = await s.scalar(select(TreasurySettingsRevision))
            raw = {
                **row.settings,
                "treasury_hotkey": pin().fleet[0].validator_hotkey,
            }
            s.add(
                TreasurySettingsRevision(
                    parent_revision=row.revision,
                    settings=raw,
                    checksum=canonical_settings(raw),
                    actor="test",
                    reason="Changed public collector",
                )
            )
            await s.commit()
    elif fault == "ledger":

        async def no_epoch(*_, **__):
            return SimpleNamespace(
                policy=SimpleNamespace(
                    continual_retest=SimpleNamespace(ledger_pin_mode="time")
                )
            )

        monkeypatch.setattr(
            "ditto.api_server.endpoints.scoring.resolve_ledger_context", no_epoch
        )
    elif fault == "signature":
        monkeypatch.setattr(
            "ditto.api_server.endpoints.admin_treasury_runtime.verify_public_signature",
            lambda *_: False,
        )
    elif fault == "confirmation":
        data["confirmation"] = "ENABLE"
    elif fault == "chain":
        app.state.chain.get_treasury_dispatch_observation.side_effect = RuntimeError(
            "private provider error"
        )
    r = await client.post(URL, headers=_HEADERS, json=data)
    assert r.status_code in (400, 409, 422), r.text
    assert "private provider error" not in r.text
    async with session_maker() as s:
        rows = list(await s.scalars(select(TreasuryRuntimeRevision)))
        assert all(row.settings["mode"] == "observe" for row in rows)


async def test_enforce_then_pause_retains_approval_and_refuses_pinned_dispatch(
    app, client, session_maker, monkeypatch
):
    from ditto.api_server.treasury_weights import require_enforcing_requester

    setup(app, session_maker, monkeypatch)
    r = await client.post(URL, headers=_HEADERS, json=payload())
    await seed_shadow(session_maker)
    async with session_maker() as s:
        await add_runtime(s, datetime.now(UTC))
        await s.commit()
    activated = await client.post(
        URL, headers=_HEADERS, json=payload("enforce", r.json()["revision"])
    )
    assert activated.status_code == 200, activated.text
    active_rev = activated.json()["revision"]
    # Neither rearm nor a new observe approval can silently downgrade active mode.
    for mode in ("observe", "enforce"):
        assert (
            await client.post(URL, headers=_HEADERS, json=payload(mode, active_rev))
        ).status_code == 409
    changed = payload("pause", active_rev)
    changed["settings"]["managed_validator_hotkeys"].append(
        pin().policy.collector_hotkey
    )
    assert (await client.post(URL, headers=_HEADERS, json=changed)).status_code == 409
    paused = await client.post(URL, headers=_HEADERS, json=payload("pause", active_rev))
    assert paused.status_code == 200, paused.text
    assert paused.json()["settings"]["approval"] == r.json()["settings"]["approval"]
    async with session_maker() as s:
        with pytest.raises(ValueError, match="paused"):
            await require_enforcing_requester(
                s,
                pin(),
                pin().fleet[0].validator_hotkey,
                now=datetime.now(UTC),
                app_state=app.state,
            )


async def test_invalid_durable_history_never_falls_back_to_env(
    app, client, session_maker, monkeypatch
):
    from ditto.api_server.treasury_runtime import treasury_runtime

    setup(app, session_maker, monkeypatch)
    assert (await client.post(URL, headers=_HEADERS, json=payload())).status_code == 200
    async with session_maker() as s:
        row = await s.scalar(select(TreasuryRuntimeRevision))
        s.add(
            TreasuryRuntimeRevision(
                parent_revision=row.revision,
                settings=row.settings,
                checksum="0" * 64,
                reason="Corrupt control injection test",
                actor="test",
            )
        )
        await s.commit()
    assert (await client.get(URL, headers=_HEADERS)).status_code == 409
    assert (
        await client.post(URL, headers=_HEADERS, json=payload(revision=2))
    ).status_code == 409
    assert (
        await client.get(
            "/api/v1/admin/treasury-settings/ledger-readiness", headers=_HEADERS
        )
    ).status_code == 409
    request = payload()["settings"]
    preflight = {
        "approval": request["approval"],
        "expected_policy_digest": request["approved_policy_digest"],
        "expected_collector_policy_digest": request["collector_policy_digest"],
    }
    assert (
        await client.post(
            "/api/v1/admin/treasury-settings/activation-preflight",
            headers=_HEADERS,
            json=preflight,
        )
    ).status_code == 409
    async with session_maker() as s:
        with pytest.raises(ValueError, match="checksum"):
            await treasury_runtime(s, app.state.config)


async def test_unauthenticated_write_refuses(app, client, session_maker, monkeypatch):
    setup(app, session_maker, monkeypatch)
    assert (await client.post(URL, json=payload())).status_code in (401, 403)


async def test_cas_lock_serializes_two_writers(app, client, session_maker, monkeypatch):
    import asyncio

    setup(app, session_maker, monkeypatch)
    results = await asyncio.gather(
        *[client.post(URL, headers=_HEADERS, json=payload()) for _ in range(2)]
    )
    assert sorted(r.status_code for r in results) == [200, 409]
    async with session_maker() as s:
        assert len(list(await s.scalars(select(TreasuryRuntimeRevision)))) == 1


async def test_append_only_runtime_history_is_enforced_by_postgres(
    app, client, session_maker, monkeypatch
):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    setup(app, session_maker, monkeypatch)
    assert (await client.post(URL, headers=_HEADERS, json=payload())).status_code == 200
    for sql in (
        "TRUNCATE treasury_runtime_revisions",
        "DELETE FROM treasury_runtime_revisions",
        "UPDATE treasury_runtime_revisions SET actor='forged'",
    ):
        async with session_maker() as s:
            with pytest.raises(DBAPIError, match="append only"):
                await s.execute(text(sql))


async def test_env_enforcement_cannot_be_overridden_silently(
    app, client, session_maker, monkeypatch
):
    setup(app, session_maker, monkeypatch)
    app.state.config = replace(app.state.config, treasury_weight_enforcement=True)
    assert (await client.post(URL, headers=_HEADERS, json=payload())).status_code == 409


async def test_concurrent_control_change_refuses_epoch_insert(
    app, client, session_maker, monkeypatch
):
    from unittest.mock import AsyncMock

    from ditto.api_server.ledger_pin import LedgerPinMaterializer
    from ditto.db.models import LedgerEpochSnapshot
    from ditto.tests.api_server.test_ledger_pin import _schedule, _snapshot

    setup(app, session_maker, monkeypatch)
    monkeypatch.setattr(
        "ditto.api_server.endpoints.scoring.materialize_ledger_snapshot",
        AsyncMock(return_value=_snapshot([])),
    )

    async def change_during_observation(*_, **__):
        result = await client.post(URL, headers=_HEADERS, json=payload())
        assert result.status_code == 200, result.text
        return None

    monkeypatch.setattr(
        "ditto.api_server.ledger_pin.observe_shadow_treasury", change_during_observation
    )
    materializer = LedgerPinMaterializer()
    result = await materializer.ensure(
        app.state, session_maker, now=datetime.now(UTC), schedule=_schedule()
    )
    assert result is None
    assert materializer.newest_known is None
    async with session_maker() as s:
        assert list(await s.scalars(select(LedgerEpochSnapshot))) == []


@pytest.mark.parametrize("kind,expected", [("database", 503), ("invalid", 409)])
@pytest.mark.parametrize("endpoint", ["ledger-readiness", "activation-preflight"])
async def test_admin_read_runtime_failure_is_bounded(
    app, client, session_maker, monkeypatch, kind, expected, endpoint
):
    from sqlalchemy.exc import SQLAlchemyError

    setup(app, session_maker, monkeypatch)

    async def failed(*_, **__):
        raise (
            SQLAlchemyError("private database detail")
            if kind == "database"
            else ValueError("private corruption detail")
        )

    monkeypatch.setattr("ditto.api_server.treasury_runtime.treasury_runtime", failed)
    if endpoint == "ledger-readiness":
        response = await client.get(
            "/api/v1/admin/treasury-settings/ledger-readiness", headers=_HEADERS
        )
    else:
        settings = payload()["settings"]
        response = await client.post(
            "/api/v1/admin/treasury-settings/activation-preflight",
            headers=_HEADERS,
            json={
                "approval": settings["approval"],
                "expected_policy_digest": settings["approved_policy_digest"],
                "expected_collector_policy_digest": settings["collector_policy_digest"],
                "managed_validator_hotkeys": settings["managed_validator_hotkeys"],
            },
        )
    assert response.status_code == expected, response.text
    assert "private" not in response.text
    app.state.chain.get_treasury_dispatch_observation.assert_not_awaited()
    async with session_maker() as session:
        assert not list(await session.scalars(select(TreasuryRuntimeRevision)))
