from dataclasses import asdict, replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ditto.tests.test_collector_automation import Chain, open_journal, policy
from ditto.treasury.collector import (
    ManualTransfer,
    Settlement,
    TransferCanary,
    arm_manual_transfer,
    tick,
)
from ditto.treasury.manual_worker import process_manual, publish_readiness
from ditto.treasury.service_allocation import ServiceDestination
from ditto_screening_protocol.treasury_manual import ManualEnvelope

GM = "5" + "a" * 47


class Mailbox:
    def __init__(self):
        self.reports, self.acks = [], []
        self.fail = False

    def publish(self, body):
        if self.fail:
            raise TimeoutError()
        self.reports.append(body)

    def ack(self, value):
        self.acks.append(value)


def setup(tmp_path):
    p = policy(destinations=(ServiceDestination("gm", 1000, GM),))
    c = Chain()
    c.observation = replace(c.observation, uid=14)
    c.income = {10: 501}
    j = open_journal(tmp_path, p, "transfer")
    tick(j, p, c, "transfer")
    tick(j, p, c, "transfer", canary=TransferCanary(40, 0))
    c.settlement = Settlement(
        "finalized",
        101,
        "0x" + "c" * 64,
        14,
        extrinsic_index=2,
        extrinsic_hash="0x" + "b" * 64,
    )
    tick(j, p, c, "transfer")
    c.substrate = SimpleNamespace(get_block_hash=lambda _: "0x" + "d" * 64)
    c.query = lambda *_: 123
    req = ManualTransfer(
        str(uuid4()), 1, 10, "gm", 30, 900, 300, "operator manual test"
    )
    env = ManualEnvelope(
        collector_policy_digest=p.digest, destination=GM, request=asdict(req)
    )
    return p, c, j, env, Mailbox()


def test_readiness_does_not_arm_or_send(tmp_path):
    p, c, j, _, mailbox = setup(tmp_path)
    before = list(j.db.iterdump())
    publish_readiness(mailbox, j, p, c)
    assert len(c.sent) == 1 and list(j.db.iterdump()) == before
    assert mailbox.reports[0]["readiness"]["after_operation"] == 1


def test_unknown_delivery_redelivery_never_resigns(tmp_path):
    p, c, j, env, mailbox = setup(tmp_path)
    c.error = True
    with pytest.raises(TimeoutError):
        process_manual(mailbox, j, p, c, "ack", env.model_dump())
    c.settlement = Settlement("pending")
    result = process_manual(mailbox, j, p, c, "ack", env.model_dump())
    assert result.status == "pending" and mailbox.acks == []
    assert len(c.prepared) == len(c.sent) == 2


def test_return_publish_failure_then_replay_preserves_exact_claim(tmp_path):
    p, c, j, env, mailbox = setup(tmp_path)
    process_manual(mailbox, j, p, c, "first", env.model_dump())
    mailbox.fail = True
    with pytest.raises(TimeoutError):
        process_manual(mailbox, j, p, c, "retry", env.model_dump())
    mailbox.fail = False
    result = process_manual(mailbox, j, p, c, "retry", env.model_dump())
    assert result.status == "finalized" and len(c.sent) == 2
    assert result.settlement.extrinsic_hash == "0x" + "b" * 64
    assert mailbox.acks == ["retry"]


def test_changed_uuid_body_or_unapproved_destination_never_signs(tmp_path):
    p, c, j, env, mailbox = setup(tmp_path)
    with pytest.raises(ValueError):
        process_manual(
            mailbox,
            j,
            p,
            c,
            "ack",
            {
                **env.model_dump(),
                "destination": "5" + "b" * 47,
            },
        )
    assert len(c.sent) == 1
    process_manual(mailbox, j, p, c, "ack", env.model_dump())
    changed = env.model_dump()
    changed["request"]["amount_rao"] = 31
    with pytest.raises(ValueError):
        process_manual(mailbox, j, p, c, "ack", changed)
    assert len(c.sent) == 2


def test_expired_unarmed_request_refuses_without_send(tmp_path):
    p, c, j, env, mailbox = setup(tmp_path)
    body = env.model_dump()
    body["request"]["expires_block"] = 99
    result = process_manual(mailbox, j, p, c, "ack", body)
    assert result.status == "refused" and mailbox.acks == ["ack"]
    assert len(c.sent) == 1


def test_expired_armed_request_stops_without_unlocking_or_sending(tmp_path):
    p, c, j, env, mailbox = setup(tmp_path)
    arm_manual_transfer(j, p, c, ManualTransfer(**env.request.model_dump()))
    c.observation = replace(c.observation, block=env.request.expires_block - 100)
    result = process_manual(mailbox, j, p, c, "ack", env.model_dump())
    assert result.status == "failed" and result.settlement is None
    assert len(c.sent) == 1 and mailbox.acks == ["ack"]
    publish_readiness(mailbox, j, p, c)
    assert mailbox.reports[-1]["readiness"]["bounded_claim_available"] is False
    with pytest.raises(ValueError):
        arm_manual_transfer(
            j,
            p,
            c,
            replace(
                ManualTransfer(**env.request.model_dump()),
                request_id=str(uuid4()),
                expires_block=600,
            ),
        )
    assert len(c.sent) == 1


def test_refusal_survives_return_ack_failure_and_later_balance_change(tmp_path):
    p, c, j, env, mailbox = setup(tmp_path)
    c.observation = replace(c.observation, alpha_rao=900)
    mailbox.fail = True
    with pytest.raises(TimeoutError):
        process_manual(mailbox, j, p, c, "ack", env.model_dump())
    c.observation = replace(c.observation, alpha_rao=10000)
    mailbox.fail = False
    result = process_manual(mailbox, j, p, c, "ack", env.model_dump())
    assert result.status == "refused" and len(c.sent) == 1
    assert mailbox.acks == ["ack"]
