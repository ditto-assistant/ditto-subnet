"""An unsigned expiry releases only an exact, never-dispatched intent."""

import json
from dataclasses import replace
from uuid import uuid4

import pytest

from ditto.tests.test_treasury_manual_worker import setup
from ditto.treasury.collector import (
    ManualTransfer,
    arm_manual_transfer,
    manual_transfer_readiness,
    tick,
)
from ditto.treasury.manual_expiry import expire_unsigned_manual
from ditto.treasury.manual_worker import process_manual


def expired(tmp_path):
    p, c, j, env, mailbox = setup(tmp_path)
    request = ManualTransfer(**env.request.model_dump())
    arm_manual_transfer(j, p, c, request)
    c.observation = replace(c.observation, block=request.expires_block)
    return p, c, j, env, mailbox


def test_unsigned_expiry_preserves_history_and_needs_fresh_confirmation(tmp_path):
    p, c, j, env, mailbox = expired(tmp_path)
    before = [tuple(r) for r in j.db.execute("SELECT * FROM operations")]
    proof = expire_unsigned_manual(
        j, p, c, env.request.request_id, reason="recover expired unsigned intent"
    )
    assert proof["finalized_block"] == env.request.expires_block
    assert [tuple(r) for r in j.db.execute("SELECT * FROM operations")] == before
    assert manual_transfer_readiness(j, p, c)["bounded_claim_available"]
    # Old messages, even if re-delivered after recovery, cannot reserve or send.
    assert (
        process_manual(mailbox, j, p, c, "old", env.model_dump()).failure_code
        == "expired_unsigned"
    )
    assert tick(j, p, c, "transfer") == "canary_spent"
    fresh = replace(
        ManualTransfer(**env.request.model_dump()),
        request_id=str(uuid4()),
        expires_block=600,
    )
    arm_manual_transfer(j, p, c, fresh)
    assert len(c.prepared) == len(c.sent) == 1
    assert (
        tick(j, p, c, "transfer", manual_request_id=fresh.request_id) == "dispatching"
    )
    assert len(c.sent) == 2
    # An old redelivery must not borrow the newly reserved operation.
    assert (
        process_manual(mailbox, j, p, c, "old-again", env.model_dump()).status
        == "failed"
    )
    assert len(c.sent) == 2
    assert (
        j.db.execute(
            "SELECT count(*) FROM events WHERE event='manual_transfer_armed'"
        ).fetchone()[0]
        == 2
    )


@pytest.mark.parametrize(
    "mode", ["not_expired", "unknown", "finalized", "wrong_uuid", "changed_policy"]
)
def test_recovery_refuses_live_or_signed_claim_without_mutation(tmp_path, mode):
    p, c, j, env, mailbox = expired(tmp_path)
    if mode in {"unknown", "finalized"}:
        c.observation = replace(c.observation, block=100)
        tick(j, p, c, "transfer", manual_request_id=env.request.request_id)
        if mode == "finalized":
            tick(j, p, c, "transfer")
        c.observation = replace(c.observation, block=600)
    elif mode == "not_expired":
        c.observation = replace(c.observation, block=env.request.expires_block - 1)
    elif mode == "changed_policy":
        p = replace(p, revision=p.revision + 1)
    before = list(j.db.iterdump())
    with pytest.raises(ValueError):
        expire_unsigned_manual(
            j,
            p,
            c,
            str(uuid4()) if mode == "wrong_uuid" else env.request.request_id,
            reason="explicit unsigned recovery",
        )
    assert list(j.db.iterdump()) == before


def test_expired_uuid_cannot_be_rearmed_or_changed(tmp_path):
    p, c, j, env, mailbox = expired(tmp_path)
    expire_unsigned_manual(
        j, p, c, env.request.request_id, reason="explicit unsigned recovery"
    )
    with pytest.raises(ValueError, match="cannot be rearmed"):
        arm_manual_transfer(j, p, c, ManualTransfer(**env.request.model_dump()))
    body = env.model_dump()
    body["request"]["amount_rao"] += 1
    with pytest.raises(ValueError, match="UUID changed"):
        process_manual(mailbox, j, p, c, "changed", body)
    # Fabricated closure over a signed claim fails history verification too.
    row = j.db.execute(
        "SELECT id,payload FROM events WHERE event='manual_unsigned_expired'"
    ).fetchone()
    data = json.loads(row["payload"])
    data["finalized_block"] = env.request.expires_block - 1
    j.db.execute(
        "UPDATE events SET payload=? WHERE id=?", (json.dumps(data), row["id"])
    )
    with pytest.raises(ValueError, match="unsigned expiry"):
        manual_transfer_readiness(j, p, c)


def test_aggregate_recovery_does_not_reuse_failed_intent_for_next_operation(tmp_path):
    from ditto.tests.test_treasury_manual_batch import fixture, intent
    from ditto.tests.test_treasury_manual_worker import Mailbox
    from ditto.treasury.manual_batch import source_operations

    p, c, j, env = fixture(tmp_path)
    arm_manual_transfer(j, p, c, intent(env))
    c.observation = replace(c.observation, block=env.request.expires_block)
    expire_unsigned_manual(
        j, p, c, env.request.request_id, reason="recover expired aggregate request"
    )
    fresh = intent(env)
    fresh = replace(fresh, request_id=str(uuid4()), expires_block=2200)
    arm_manual_transfer(j, p, c, fresh)
    tick(j, p, c, "transfer", manual_request_id=fresh.request_id)
    assert len(source_operations(j, 11)) == 1
    mailbox = Mailbox()
    assert (
        process_manual(mailbox, j, p, c, "old", env.model_dump()).failure_code
        == "expired_unsigned"
    )
    assert len(c.sent) == 2


def test_queued_transfer_precedes_slow_background_work(tmp_path, monkeypatch):
    from scripts import treasury_manual_worker as worker

    p, c, j, env, mailbox = setup(tmp_path)
    mailbox.pull = lambda: ("ack", env.model_dump())

    def must_not_scan(*_args, **_kwargs):
        pytest.fail("queued request must run before background archive scanning")

    monkeypatch.setattr(worker, "observe_earnings", must_not_scan)
    worker.consume_iteration(mailbox, j, p, c)
    assert len(c.sent) == 2


def test_request_arriving_during_catchup_runs_before_next_chunk(tmp_path, monkeypatch):
    from scripts import treasury_manual_worker as worker

    p, c, j, env, mailbox = setup(tmp_path)
    queued = iter([None, ("ack", env.model_dump())])
    mailbox.pull = lambda: next(queued)
    calls = []

    def scan(*_args, **kwargs):
        calls.append(kwargs["max_blocks"])
        j.db.execute("UPDATE cursor SET block=block+8 WHERE id=1")

    monkeypatch.setattr(worker, "observe_earnings", scan)
    worker.consume_iteration(mailbox, j, p, c)
    assert calls == [8] and len(c.sent) == 2
