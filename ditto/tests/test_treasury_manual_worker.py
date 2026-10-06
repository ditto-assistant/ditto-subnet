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
from ditto.treasury.manual_worker import (
    consume_manual,
    process_manual,
    publish_readiness,
)
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


@pytest.mark.parametrize("finalized", [False, True])
def test_expired_armed_redelivery_reconciles_exact_claim(tmp_path, finalized):
    p, c, j, env, mailbox = setup(tmp_path)
    c.error = True
    with pytest.raises(TimeoutError):
        process_manual(mailbox, j, p, c, "first", env.model_dump())
    c.observation = replace(c.observation, block=env.request.expires_block + 10)
    if not finalized:
        c.settlement = Settlement("pending")
    result = process_manual(mailbox, j, p, c, "retry", env.model_dump())
    assert result.status == ("finalized" if finalized else "pending")
    assert mailbox.acks == (["retry"] if finalized else [])
    assert len(c.prepared) == len(c.sent) == 2


@pytest.mark.parametrize(
    "field,value",
    [("destination", "5" + "b" * 47), ("collector_policy_digest", "b" * 64)],
)
def test_poison_unapproved_request_refuses_and_acks_without_send(
    tmp_path, field, value
):
    p, c, j, env, mailbox = setup(tmp_path)
    body = {**env.model_dump(), field: value}
    mailbox.fail = True
    with pytest.raises(TimeoutError):
        consume_manual(mailbox, j, p, c, "first", body)
    assert not mailbox.acks
    mailbox.fail = False
    result = consume_manual(mailbox, j, p, c, "retry", body)
    assert result.status == "refused" and mailbox.acks == ["retry"]
    assert result.collector_policy_digest == body["collector_policy_digest"]
    assert result.request_digest == ManualEnvelope.model_validate(body).digest
    assert len(c.sent) == 1


def test_invalid_contract_and_changed_armed_message_drop_only_invalid_bytes(tmp_path):
    p, c, j, env, mailbox = setup(tmp_path)
    consume_manual(mailbox, j, p, c, "malformed", {})
    assert mailbox.acks == ["malformed"] and mailbox.reports == []
    c.error = True
    with pytest.raises(TimeoutError):
        consume_manual(mailbox, j, p, c, "first", env.model_dump())
    before = list(j.db.iterdump())
    changed = env.model_dump()
    changed["request"]["amount_rao"] += 1
    consume_manual(mailbox, j, p, c, "changed", changed)
    assert list(j.db.iterdump()) == before
    assert mailbox.acks == ["malformed", "changed"]
    c.settlement = Settlement("pending")
    assert (
        consume_manual(mailbox, j, p, c, "original", env.model_dump()).status
        == "pending"
    )
    assert len(c.sent) == 2 and mailbox.reports[-1]["status"] == "pending"


def test_old_finalized_redelivery_does_not_execute_newer_armed_intent(
    tmp_path, monkeypatch
):
    import ditto.treasury.manual_worker as worker

    p, c, j, env, mailbox = setup(tmp_path)
    process_manual(mailbox, j, p, c, "first", env.model_dump())
    prior = process_manual(mailbox, j, p, c, "settled", env.model_dump())
    assert prior.status == "finalized"
    newer = replace(
        ManualTransfer(**env.request.model_dump()),
        request_id=str(uuid4()),
        after_operation=2,
    )
    arm_manual_transfer(j, p, c, newer)
    before = list(j.db.iterdump())

    def never_tick(*_args, **_kwargs):
        pytest.fail("Old terminal redelivery must not execute the current intent")

    monkeypatch.setattr(worker, "tick", never_tick)
    result = consume_manual(mailbox, j, p, c, "old-redelivery", env.model_dump())
    assert result.status == "finalized" and result.settlement == prior.settlement
    assert list(j.db.iterdump()) == before and len(c.sent) == 2
    assert mailbox.acks[-1] == "old-redelivery"


def test_unreconciled_prior_claim_retries_without_durable_refusal(tmp_path):
    p, c, j, env, mailbox = setup(tmp_path)
    j.db.execute("UPDATE operations SET state='dispatching' WHERE id=1")
    with pytest.raises(ValueError, match="prior claim must be finalized"):
        consume_manual(mailbox, j, p, c, "unreconciled", env.model_dump())
    assert mailbox.acks == [] and mailbox.reports == [] and len(c.sent) == 1
    assert not j.db.execute(
        "SELECT 1 FROM events WHERE event='manual_mailbox_refused'"
    ).fetchone()
    j.db.execute("UPDATE operations SET state='finalized' WHERE id=1")
    assert (
        consume_manual(mailbox, j, p, c, "retry", env.model_dump()).status == "pending"
    )
    assert (
        consume_manual(mailbox, j, p, c, "final", env.model_dump()).status
        == "finalized"
    )
    assert mailbox.acks == ["final"] and len(c.sent) == 2


@pytest.mark.parametrize("claim_state", ["absent", "dispatching"])
def test_older_unsettled_intent_cannot_have_a_valid_newer_intent(
    tmp_path, monkeypatch, claim_state
):
    import ditto.treasury.manual_worker as worker

    p, c, j, env, mailbox = setup(tmp_path)
    if claim_state == "absent":
        arm_manual_transfer(j, p, c, ManualTransfer(**env.request.model_dump()))
    else:
        process_manual(mailbox, j, p, c, "initial", env.model_dump())
    # Deliberately corrupt history to model the review's impossible predecessor:
    # valid arm_manual_transfer cannot advance without the old finalized claim.
    newer = replace(
        ManualTransfer(**env.request.model_dump()),
        request_id=str(uuid4()),
        after_operation=2,
    )
    j.event("manual_transfer_armed", {"policy": p.digest, **asdict(newer)})
    before = list(j.db.iterdump())

    def never_tick(*_args, **_kwargs):
        pytest.fail("Unverified history must not execute any current intent")

    monkeypatch.setattr(worker, "tick", never_tick)
    with pytest.raises(
        ValueError, match="manual history advanced without finalized claim"
    ):
        consume_manual(mailbox, j, p, c, "corrupt-history", env.model_dump())
    assert list(j.db.iterdump()) == before and mailbox.acks == []
    assert not j.db.execute(
        "SELECT 1 FROM events WHERE event='manual_mailbox_refused'"
    ).fetchone()
