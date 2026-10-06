"""Real SQL durability/race controls; synthetic custody observations are not proof."""

import asyncio
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ditto.api_models.treasury_manual import ManualPreviewInput, ManualSubmitInput
from ditto.api_server import treasury_manual as manual
from ditto.db.models import TreasuryManualBridgeState, TreasuryManualTransfer
from ditto_screening_protocol.treasury import TreasuryEmissionBucket
from ditto_screening_protocol.treasury_manual import (
    ManualEnvelope,
    ManualReadiness,
    ManualReport,
)

pytestmark = pytest.mark.asyncio
GM = "5" + "a" * 47
POLICY = "a" * 64


@pytest.fixture(autouse=True)
def runtime(monkeypatch):
    current = SimpleNamespace(
        treasury_weight_enforcement=True,
        treasury_approved_collector_policy_digest=POLICY,
        treasury_shadow_policy=SimpleNamespace(
            buckets=[
                TreasuryEmissionBucket(
                    bucket_id="gm", allocation_bps=1000, holding_coldkey=GM
                )
            ]
        ),
    )

    async def read(*_):
        return current

    monkeypatch.setattr(manual, "treasury_runtime", read)
    return current


def report():
    return ManualReport(
        collector_policy_digest=POLICY,
        observed_at=int(time.time()),
        status="readiness",
        readiness=ManualReadiness(
            policy=POLICY,
            after_operation=2,
            previous_state="finalized",
            bounded_claim_available=True,
            finalized_block=1000,
            available_alpha_rao=60000000000,
            max_distribution_rao=100000000000,
            sources=[
                {
                    "source_block": 200,
                    "remaining": [
                        {
                            "bucket_id": "gm",
                            "holding_coldkey": GM,
                            "alpha_rao": 11000000000,
                        }
                    ],
                }
            ],
        ),
    )


async def seed(maker):
    async with maker() as session, session.begin():
        await manual.accept_report(session, None, report().model_dump())


async def get_preview(maker, **changes):
    body = {
        "request_id": str(uuid4()),
        "bucket_id": "gm",
        "amount_rao": 100000000,
        "retained_alpha_rao": 55000000000,
        "reason": "manual inference purchase",
    } | changes
    async with maker() as session:
        return await manual.preview(
            session, None, ManualPreviewInput(**body), enabled=True
        )


def submission(preview):
    return ManualSubmitInput(
        envelope=preview["envelope"],
        confirmation_digest=preview["confirmation_digest"],
        confirmation="TRANSFER SN118 ALPHA ONCE",
    )


async def test_explicit_enablement_freshness_and_pause_fail_closed(
    session_maker, runtime
):
    await seed(session_maker)
    async with session_maker() as session:
        assert (await manual.state(session, None, enabled=False))["blocked_reason"]
        assert (await manual.state(session, None, enabled=True))[
            "blocked_reason"
        ] is None
        runtime.treasury_weight_enforcement = False
        assert (await manual.state(session, None, enabled=True))[
            "blocked_reason"
        ] == "Gamma is paused"
    runtime.treasury_weight_enforcement = True
    async with session_maker() as session, session.begin():
        row = await session.get(TreasuryManualBridgeState, 1)
        row.report = {**row.report, "observed_at": int(time.time()) - 181}
    with pytest.raises(ValueError, match="fresh"):
        await get_preview(session_maker)


async def test_preview_is_readonly_and_preserves_exact_reserve(session_maker):
    await seed(session_maker)
    p = await get_preview(session_maker)
    assert p["envelope"]["request"]["retained_alpha_rao"] == 55000000000
    assert p["spending_authority"] == "not_queued"
    async with session_maker() as session:
        assert (
            await session.get(
                TreasuryManualTransfer, p["envelope"]["request"]["request_id"]
            )
            is None
        )
    for changes in (
        {"bucket_id": "bitsec"},
        {"amount_rao": 12000000000},
        {"retained_alpha_rao": 60000000000},
    ):
        with pytest.raises(ValueError):
            await get_preview(session_maker, **changes)


async def test_same_confirmation_replay_is_one_request_changed_body_refuses(
    session_maker,
):
    await seed(session_maker)
    payload = submission(await get_preview(session_maker))

    async def send(p=payload):
        async with session_maker() as session, session.begin():
            return await manual.submit(
                session, None, p, "operator@example.com", enabled=True
            )

    a, b = await asyncio.gather(send(), send())
    assert a["request_id"] == b["request_id"] and a["status"] == "queued"
    body = payload.envelope.model_dump()
    body["request"]["amount_rao"] += 1
    env = ManualEnvelope.model_validate(body)
    changed = ManualSubmitInput(
        envelope=env, confirmation_digest=env.digest, confirmation=payload.confirmation
    )
    with pytest.raises(ValueError, match="UUID"):
        await send(changed)


async def test_two_independent_clicks_cannot_queue_two_claims(session_maker):
    await seed(session_maker)
    a, b = await get_preview(session_maker), await get_preview(session_maker)

    async def send(p):
        async with session_maker() as session, session.begin():
            return await manual.submit(
                session, None, submission(p), "operator@example.com", enabled=True
            )

    values = await asyncio.gather(send(a), send(b), return_exceptions=True)
    assert sum(isinstance(v, ValueError) for v in values) == 1


async def test_return_inbox_cannot_claim_publication_or_replace_terminal_coordinates(
    session_maker,
):
    await seed(session_maker)
    payload = submission(await get_preview(session_maker))
    async with session_maker() as session, session.begin():
        await manual.submit(
            session, None, payload, "operator@example.com", enabled=True
        )
    terminal = ManualReport(
        collector_policy_digest=POLICY,
        observed_at=int(time.time()),
        request_id=payload.envelope.request.request_id,
        request_digest=payload.envelope.digest,
        status="finalized",
        settlement={
            "epoch_index": 100,
            "block": 1001,
            "block_hash": "0x" + "b" * 64,
            "extrinsic_index": 2,
            "extrinsic_hash": "0x" + "c" * 64,
        },
    )
    async with session_maker() as session, session.begin():
        await manual.accept_report(session, None, terminal.model_dump())
    async with session_maker() as session:
        row = await session.get(
            TreasuryManualTransfer, payload.envelope.request.request_id
        )
        assert row.status == "audit_pending" and row.receipt is None
    changed = terminal.model_dump()
    changed["settlement"]["extrinsic_index"] = 3
    with pytest.raises(ValueError, match="Terminal"):
        async with session_maker() as session, session.begin():
            await manual.accept_report(session, None, changed)
    with pytest.raises(ValueError, match="pending"):
        await get_preview(session_maker)


async def test_automatic_audit_uses_independent_ingress_not_worker_assertion(
    session_maker, monkeypatch
):
    await seed(session_maker)
    payload = submission(await get_preview(session_maker))
    async with session_maker() as session, session.begin():
        await manual.submit(
            session, None, payload, "operator@example.com", enabled=True
        )
        row = await session.get(
            TreasuryManualTransfer, payload.envelope.request.request_id
        )
        row.status = "audit_pending"
        row.report = ManualReport(
            collector_policy_digest=POLICY,
            observed_at=int(time.time()),
            status="finalized",
            request_id=row.request_id,
            request_digest=row.digest,
            settlement={
                "epoch_index": 100,
                "block": 1001,
                "block_hash": "0x" + "b" * 64,
                "extrinsic_index": 2,
                "extrinsic_hash": "0x" + "c" * 64,
            },
        ).model_dump()
    called = []

    async def ingress(_session, chain, selector):
        called.append((chain, selector))
        return SimpleNamespace(
            published=True,
            model_dump=lambda **_: {"receipt_id": "d" * 64, "published": True},
        )

    monkeypatch.setattr(manual, "ingest_receipt", ingress)
    async with session_maker() as session, session.begin():
        row = await session.get(
            TreasuryManualTransfer, payload.envelope.request.request_id
        )
        await manual.publish_audit(session, "independent-chain", row)
        assert row.status == "published" and row.receipt["published"]
    assert called[0][0] == "independent-chain"
    assert called[0][1].amount_atomic == 100000000 and called[0][1].source_block == 200


async def test_admin_button_requires_auth_and_server_actor(app, client, session_maker):
    from dataclasses import replace

    from ditto.api_server.dependencies import get_session

    token = "manual-test-admin-token-at-least-32-characters"
    app.state.config = replace(app.state.config, admin_api_token=token)
    app.state.treasury_manual_loop = SimpleNamespace(enabled=True, last_error=None)

    async def sessions():
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_session] = sessions
    await seed(session_maker)
    url = "/api/v1/admin/treasury-manual"
    assert (await client.get(url)).status_code in {401, 403}
    payload = submission(await get_preview(session_maker)).model_dump()
    headers = {"Authorization": f"Bearer {token}"}
    assert (await client.post(url, headers=headers, json=payload)).status_code == 422
    payload["actor"] = "spoofed@example.com"
    response = await client.post(
        url, headers={**headers, "X-Admin-Actor": "signed-in@example.com"}, json=payload
    )
    assert response.status_code == 200, response.text
    assert response.json()["actor"] == "signed-in@example.com"
    assert response.headers["cache-control"] == "no-store"


async def test_outbox_lost_publish_response_replays_only_exact_uuid(
    session_maker, monkeypatch
):
    from ditto.api_server import treasury_manual_loop as module

    await seed(session_maker)
    payload = submission(await get_preview(session_maker))
    async with session_maker() as session, session.begin():
        await manual.submit(
            session, None, payload, "operator@example.com", enabled=True
        )
    sent = []
    fail = True

    def publish(body):
        sent.append(body)
        if fail:
            raise TimeoutError("ack lost")

    mailbox = SimpleNamespace(pull=lambda: None, publish=publish)
    loop = module.TreasuryManualLoop(
        SimpleNamespace(session_maker=session_maker, config=None), mailbox=mailbox
    )
    monkeypatch.setattr(module, "treasury_runtime", manual.treasury_runtime)
    with pytest.raises(TimeoutError):
        await loop.sweep()
    async with session_maker() as session:
        assert (
            await session.get(
                TreasuryManualTransfer, payload.envelope.request.request_id
            )
        ).status == "queued"
    fail = False
    await loop.sweep()
    assert len(sent) == 2 and sent[0] == sent[1] == payload.envelope.model_dump()


async def test_queued_request_paused_before_dispatch_is_never_sent(
    session_maker, runtime, monkeypatch
):
    from ditto.api_server import treasury_manual_loop as module

    await seed(session_maker)
    payload = submission(await get_preview(session_maker))
    async with session_maker() as session, session.begin():
        await manual.submit(
            session, None, payload, "operator@example.com", enabled=True
        )
    runtime.treasury_weight_enforcement = False
    sent = []
    mailbox = SimpleNamespace(pull=lambda: None, publish=sent.append)
    loop = module.TreasuryManualLoop(
        SimpleNamespace(session_maker=session_maker, config=None), mailbox=mailbox
    )
    monkeypatch.setattr(module, "treasury_runtime", manual.treasury_runtime)
    await loop.sweep()
    assert sent == []
    async with session_maker() as session:
        assert (
            await session.get(
                TreasuryManualTransfer, payload.envelope.request.request_id
            )
        ).status == "refused"


async def test_disabled_bridge_preserves_reason_with_paused_pending_request(
    session_maker, runtime
):
    await seed(session_maker)
    payload = submission(await get_preview(session_maker))
    async with session_maker() as session, session.begin():
        await manual.submit(
            session, None, payload, "operator@example.com", enabled=True
        )
    runtime.treasury_weight_enforcement = False
    async with session_maker() as session:
        assert (await manual.state(session, None, enabled=False))[
            "blocked_reason"
        ] == "Manual custody bridge is disabled"


async def test_bridge_error_does_not_replace_policy_block(
    app, client, session_maker, runtime
):
    from dataclasses import replace

    from ditto.api_server.dependencies import get_session

    token = "manual-test-admin-token-at-least-32-characters"
    app.state.config = replace(app.state.config, admin_api_token=token)
    app.state.treasury_manual_loop = SimpleNamespace(
        enabled=True, last_error="mailbox unavailable"
    )

    async def sessions():
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_session] = sessions
    await seed(session_maker)
    runtime.treasury_weight_enforcement = False
    headers = {"Authorization": f"Bearer {token}"}
    response = await client.get("/api/v1/admin/treasury-manual", headers=headers)
    assert response.status_code == 200
    assert response.json()["blocked_reason"] == "Gamma is paused"
    assert response.json()["bridge_error"] == "mailbox unavailable"
    runtime.treasury_weight_enforcement = True
    response = await client.get("/api/v1/admin/treasury-manual", headers=headers)
    assert response.json()["blocked_reason"] == "mailbox unavailable"


async def test_missing_historical_pin_retries_audit_without_custody_send(
    session_maker, monkeypatch
):
    from ditto.api_server import treasury_manual_loop as module
    from ditto.api_server.treasury_ingress import ReceiptHistoryUnavailable

    await seed(session_maker)
    payload = submission(await get_preview(session_maker))
    async with session_maker() as session, session.begin():
        await manual.submit(
            session, None, payload, "operator@example.com", enabled=True
        )
        row = await session.get(
            TreasuryManualTransfer, payload.envelope.request.request_id
        )
        row.status = "audit_pending"
    sent = []
    mailbox = SimpleNamespace(pull=lambda: None, publish=sent.append)
    loop = module.TreasuryManualLoop(
        SimpleNamespace(session_maker=session_maker, config=None, chain=None),
        mailbox=mailbox,
    )
    attempts = 0

    async def audit(_session, _chain, row):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ReceiptHistoryUnavailable("historical epoch not yet recorded")
        row.status = "published"
        row.last_error = None

    monkeypatch.setattr(module, "publish_audit", audit)
    await loop.sweep()
    async with session_maker() as session:
        row = await session.get(
            TreasuryManualTransfer, payload.envelope.request.request_id
        )
        assert row.status == "audit_pending" and "retry pending" in row.last_error
    await loop.sweep()
    assert attempts == 2 and sent == []
    async with session_maker() as session:
        assert (
            await session.get(
                TreasuryManualTransfer, payload.envelope.request.request_id
            )
        ).status == "published"


async def test_readiness_locks_runtime_before_reading_policy(
    session_maker, monkeypatch
):
    order = []
    old_lock, old_read, old_manual = (
        manual.lock_runtime,
        manual.treasury_runtime,
        manual.lock_manual,
    )

    async def lock(session):
        await old_lock(session)
        order.append("runtime")

    async def read(*args):
        order.append("read")
        return await old_read(*args)

    async def lock_claim(session):
        await old_manual(session)
        order.append("manual")

    monkeypatch.setattr(manual, "lock_runtime", lock)
    monkeypatch.setattr(manual, "treasury_runtime", read)
    monkeypatch.setattr(manual, "lock_manual", lock_claim)
    await seed(session_maker)
    assert order == ["runtime", "read", "manual"]
