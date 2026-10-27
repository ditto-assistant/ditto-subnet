from dataclasses import asdict, replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ditto.tests.test_collector_automation import (
    Chain,
    open_journal,
)
from ditto.tests.test_collector_automation import (
    policy as base_policy,
)
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
from ditto_screening_protocol.treasury_manual import (
    ManualEnvelope,
    ManualReadiness,
    ManualSettlement,
)

GM = "5" + "a" * 47


def policy(**changes):
    return base_policy(**{"transfer_delegate": "5" + "b" * 47, **changes})


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


def setup(tmp_path, **policy_changes):
    p = policy(destinations=(ServiceDestination("gm", 1000, GM),))
    p = replace(p, **policy_changes)
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
    assert not j.db.in_transaction
    assert j.db.execute(
        "SELECT 1 FROM events WHERE event='manual_mailbox_refused'"
    ).fetchone()
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


def test_concurrent_finalization_and_new_intent_reexports_only_old_claim(
    tmp_path, monkeypatch
):
    import ditto.treasury.manual_worker as worker

    p, c, j, env, mailbox = setup(tmp_path)
    assert (
        process_manual(mailbox, j, p, c, "initial", env.model_dump()).status
        == "pending"
    )
    original_tick = worker.tick
    newer = replace(
        ManualTransfer(**env.request.model_dump()),
        request_id=str(uuid4()),
        after_operation=2,
    )

    def interleaved_tick(*args, **kwargs):
        # Another process settles the old dispatching operation and advances
        # after worker SELECT, but before the original tick obtains its lock.
        assert (
            original_tick(j, p, c, "transfer", manual_request_id=env.request.request_id)
            == "finalized"
        )
        arm_manual_transfer(j, p, c, newer)
        return original_tick(*args, **kwargs)

    monkeypatch.setattr(worker, "tick", interleaved_tick)
    result = consume_manual(mailbox, j, p, c, "interleaved", env.model_dump())
    assert result.status == "finalized" and result.request_id == env.request.request_id
    assert mailbox.acks == ["interleaved"] and len(c.sent) == 2
    assert j.db.execute("SELECT MAX(id) FROM operations").fetchone()[0] == 2


@pytest.mark.parametrize("missing", ["source_hash", "epoch"])
def test_finalized_claim_waits_for_missing_receipt_coordinates_without_resend(
    tmp_path, missing
):
    p, c, j, env, mailbox = setup(tmp_path)
    process_manual(mailbox, j, p, c, "first", env.model_dump())
    if missing == "source_hash":
        c.substrate.get_block_hash = lambda _: None
        c.query = lambda *_: pytest.fail(
            "Must not read current epoch without source hash"
        )
    else:
        c.query = lambda *_: None
    result = consume_manual(mailbox, j, p, c, "missing", env.model_dump())
    assert result.status == "pending" and result.settlement is None
    assert mailbox.reports[-1]["status"] == "pending" and mailbox.acks == []
    assert len(c.sent) == 2
    c.substrate.get_block_hash = lambda _: "0x" + "d" * 64
    c.query = lambda *_: 123
    result = consume_manual(mailbox, j, p, c, "restored", env.model_dump())
    assert result.status == "finalized" and result.settlement.epoch_index == 123
    assert mailbox.acks == ["restored"] and len(c.sent) == 2


@pytest.mark.parametrize("claim_state", ["armed", "dispatching", "finalized"])
def test_policy_change_never_silently_acks_armed_claim(tmp_path, claim_state):
    p, c, j, env, mailbox = setup(tmp_path)
    arm_manual_transfer(j, p, c, ManualTransfer(**env.request.model_dump()))
    if claim_state != "armed":
        process_manual(mailbox, j, p, c, "initial", env.model_dump())
    if claim_state == "finalized":
        tick(j, p, c, "transfer", manual_request_id=env.request.request_id)
    before, sent = list(j.db.iterdump()), len(c.sent)
    changed = replace(p, max_distribution_rao=p.max_distribution_rao + 1)
    with pytest.raises(ValueError, match="requires its pinned custody policy"):
        consume_manual(mailbox, j, changed, c, "changed", env.model_dump())
    assert mailbox.acks == [] and list(j.db.iterdump()) == before
    assert len(c.sent) == sent
    result = consume_manual(mailbox, j, p, c, "restored", env.model_dump())
    assert result.request_id == env.request.request_id
    if claim_state == "armed":
        assert result.status == "pending" and mailbox.acks == []
    else:
        assert result.status == "finalized" and mailbox.acks == ["restored"]
        assert len(c.sent) == sent


@pytest.mark.parametrize(
    "damage", ["missing", "null", "invalid", "null_json", "bad_json"]
)
def test_finalized_claim_with_incomplete_saved_coordinates_stays_pending(
    tmp_path, damage
):
    import json

    p, c, j, env, mailbox = setup(tmp_path)
    process_manual(mailbox, j, p, c, "initial", env.model_dump())
    tick(j, p, c, "transfer", manual_request_id=env.request.request_id)
    row = j.db.execute("SELECT * FROM operations WHERE id=2").fetchone()
    saved = json.loads(row["settlement_json"])
    if damage == "missing":
        del saved["extrinsic_index"]
    elif damage == "null":
        saved["extrinsic_hash"] = None
    elif damage == "invalid":
        saved["extrinsic_index"] = -1
    damaged = (
        "null"
        if damage == "null_json"
        else "{"
        if damage == "bad_json"
        else json.dumps(saved)
    )
    j.db.execute("UPDATE operations SET settlement_json=? WHERE id=2", (damaged,))
    result = consume_manual(mailbox, j, p, c, "damaged", env.model_dump())
    assert result.status == "pending" and result.settlement is None
    assert mailbox.reports[-1]["status"] == "pending" and mailbox.acks == []
    assert len(c.sent) == 2
    j.db.execute(
        "UPDATE operations SET settlement_json=? WHERE id=2", (row["settlement_json"],)
    )
    result = consume_manual(mailbox, j, p, c, "restored", env.model_dump())
    assert result.status == "finalized" and mailbox.acks == ["restored"]
    assert len(c.sent) == 2


def test_exact_armed_revalidation_refusal_publishes_pending_without_ack(
    tmp_path, monkeypatch
):
    import ditto.treasury.manual_worker as worker
    from ditto.treasury.collector import ManualIntentRefused

    p, c, j, env, mailbox = setup(tmp_path)
    process_manual(mailbox, j, p, c, "initial", env.model_dump())
    original_arm = worker.arm_manual_transfer
    before = list(j.db.iterdump())

    def refused(*_args, **_kwargs):
        raise ManualIntentRefused("Synthetic revalidation refusal")

    monkeypatch.setattr(worker, "arm_manual_transfer", refused)
    mailbox.fail = True
    with pytest.raises(TimeoutError):
        consume_manual(mailbox, j, p, c, "lost-publish", env.model_dump())
    mailbox.fail = False
    result = consume_manual(mailbox, j, p, c, "retry", env.model_dump())
    assert result.status == "pending" and mailbox.acks == []
    assert mailbox.reports[-1]["status"] == "pending"
    assert list(j.db.iterdump()) == before and len(c.sent) == 2
    changed = env.model_dump()
    changed["request"]["amount_rao"] += 1
    assert consume_manual(mailbox, j, p, c, "invalid-bytes", changed) is None
    assert mailbox.acks == ["invalid-bytes"] and len(c.sent) == 2
    monkeypatch.setattr(worker, "arm_manual_transfer", original_arm)
    result = consume_manual(mailbox, j, p, c, "restored", env.model_dump())
    assert result.status == "finalized"
    assert mailbox.acks == ["invalid-bytes", "restored"] and len(c.sent) == 2


def test_empty_journal_readiness_reports_zero_without_spending(tmp_path):
    p = policy(destinations=(ServiceDestination("gm", 1000, GM),))
    c = Chain()
    j = open_journal(tmp_path, p, "transfer")
    mailbox = Mailbox()
    before = list(j.db.iterdump())
    publish_readiness(mailbox, j, p, c)
    readiness = mailbox.reports[0]["readiness"]
    assert readiness["after_operation"] == 0
    assert not readiness["bounded_claim_available"]
    assert not c.sent and list(j.db.iterdump()) == before
    for value in (-1, 2**53, True, 0.5):
        with pytest.raises(ValueError):
            ManualReadiness.model_validate({**readiness, "after_operation": value})


def test_epoch_zero_reports_finalized_receipt_and_keeps_strict_bounds(tmp_path):
    p, c, j, env, mailbox = setup(tmp_path)
    c.query = lambda *_: 0
    process_manual(mailbox, j, p, c, "first", env.model_dump())
    report = process_manual(mailbox, j, p, c, "ack", env.model_dump())
    assert report.status == "finalized" and report.settlement.epoch_index == 0
    assert mailbox.acks == ["ack"] and len(c.sent) == 2
    for value in (-1, 2**53, True, 0.5):
        with pytest.raises(ValueError):
            ManualSettlement.model_validate(
                {**report.settlement.model_dump(), "epoch_index": value}
            )


def test_readiness_publish_failure_still_pulls_and_reconciles_exact_request(
    tmp_path, monkeypatch, capsys
):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "manual_consumer_script",
        Path(__file__).resolve().parents[2] / "scripts/treasury_manual_worker.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    p, c, j, env, mailbox = setup(tmp_path)
    monkeypatch.setattr(module, "observe_earnings", lambda *_, **__: None)

    def failed_readiness(*_):
        raise TimeoutError("raw provider detail must not be logged")

    monkeypatch.setattr(module, "publish_readiness", failed_readiness)
    pulls = []

    def pull():
        pulls.append("pulled")
        return "ack", env.model_dump()

    mailbox.pull = pull
    module.consume_iteration(mailbox, j, p, c)
    module.consume_iteration(mailbox, j, p, c)
    assert pulls == ["pulled", "pulled"]
    assert any(r["status"] == "finalized" for r in mailbox.reports)
    assert mailbox.acks == ["ack"]
    assert len(c.sent) == 2  # Original canary plus one bound manual transfer.
    assert not j.db.in_transaction
    logs = capsys.readouterr().out
    assert "readiness_publish_failed" in logs and "TimeoutError" in logs
    assert "raw provider detail" not in logs


def test_cold_approved_runtime_migration_preserves_used_manual_history(
    tmp_path, monkeypatch
):
    import hashlib
    import json

    from bittensor_wallet import Keypair

    from ditto.treasury import collector_migration as migration
    from ditto.treasury.collector import (
        CollectorJournal,
        manual_transfer_history,
        transfer_canary,
    )
    from ditto_screening_protocol.collector_receipts import (
        AUDITED_COLLECTOR_CODE_HASH,
        V482_COLLECTOR_CODE_HASH,
    )

    key = Keypair.create_from_seed("0x" + "22" * 32)
    # The reviewed live path: revision 8 (v482) -> revision 9 (v483).
    old, c, journal, env, mailbox = setup(
        tmp_path,
        collector_coldkey=key.ss58_address,
        runtime_code_hash=V482_COLLECTOR_CODE_HASH,
        gcp_project="sn118-gamma-custody",
        registration_service_account="sn118-collector-registration@sn118-gamma-custody.iam.gserviceaccount.com",
        transfer_service_account="sn118-collector-transfer@sn118-gamma-custody.iam.gserviceaccount.com",
    )
    process_manual(mailbox, journal, old, c, "first", env.model_dump())
    process_manual(mailbox, journal, old, c, "second", env.model_dump())
    assert (
        journal.db.execute(
            "SELECT state FROM operations ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        == "finalized"
    )
    next_env = env.model_copy(
        update={
            "request": env.request.model_copy(
                update={"request_id": str(uuid4()), "after_operation": 2}
            )
        }
    )
    process_manual(mailbox, journal, old, c, "third", next_env.model_dump())
    process_manual(mailbox, journal, old, c, "fourth", next_env.model_dump())
    source = tmp_path / "snapshot.db"
    journal.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    journal.db.execute("PRAGMA journal_mode=DELETE")
    import sqlite3

    snapshot_db = sqlite3.connect(source)
    journal.db.backup(snapshot_db)
    snapshot_db.close()
    source.chmod(0o600)
    journal.close()
    new = replace(
        old, revision=old.revision + 1, runtime_code_hash=AUDITED_COLLECTOR_CODE_HASH
    )
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    proof = migration.manifest(source, old, new, "transfer", source_sha)
    signature = key.sign(
        (
            "ditto-collector-custody-migration-v1:"
            + hashlib.sha256(migration.canonical(proof).encode()).hexdigest()
        ).encode()
    ).hex()
    target = tmp_path / "migrated.db"
    migration.migrate(source, target, old, new, proof, signature)
    current = CollectorJournal(target, new, "transfer")
    from ditto_screening_protocol import treasury_approval

    original_verify = treasury_approval.verify_public_signature
    verifications = []

    def counted_verify(*args):
        verifications.append(1)
        return original_verify(*args)

    monkeypatch.setattr(treasury_approval, "verify_public_signature", counted_verify)
    requests = manual_transfer_history(current, new)
    assert len(requests) == 2 and len(verifications) == 1
    assert requests[0].request_id == env.request.request_id
    assert (
        transfer_canary(current, new, "transfer", None).max_alpha_rao
        == env.request.amount_rao
    )
    publish_readiness(mailbox, current, new, c)
    assert mailbox.reports[-1]["readiness"]["bounded_claim_available"] is True
    assert len(c.sent) == 3  # No migration or readiness signing.
    raw = current.db.execute(
        "SELECT payload FROM events WHERE event='manual_transfer_armed'"
    ).fetchone()[0]
    assert json.loads(raw)["policy"] == old.digest  # Original bytes preserved.
    # An old-policy intent appended after the migration cannot gain authority.
    current.event("manual_transfer_armed", json.loads(raw))
    with pytest.raises(ValueError, match="policy changed"):
        manual_transfer_history(current, new)
    current.db.execute("DELETE FROM events WHERE id=(SELECT max(id) FROM events)")
    row = current.db.execute(
        "SELECT id,payload FROM events WHERE event='offline_approved_runtime_migration'"
    ).fetchone()
    body = json.loads(row[1])
    body["signature"] = "00" * 64
    current.db.execute(
        "UPDATE events SET payload=? WHERE id=?", (json.dumps(body), row[0])
    )
    with pytest.raises(ValueError, match="cold approval"):
        manual_transfer_history(current, new)
    current.close()


def test_runtime_blocker_reports_only_fixed_public_diagnostics(tmp_path):
    from ditto.treasury.collector_chain import CollectorRuntimeRefused
    from ditto.treasury.manual_worker import publish_observation_blocker
    from ditto_screening_protocol.treasury_manual import ManualReport

    p, c, journal, _, mailbox = setup(tmp_path)
    before = list(journal.db.iterdump())
    error = CollectorRuntimeRefused(
        "runtime_changed",
        "fixed local diagnostic",
        p.runtime_code_hash,
        "0x" + "ab" * 32,
    )
    publish_observation_blocker(mailbox, p, error)
    report = ManualReport.model_validate(mailbox.reports[-1])
    assert report.status == "blocked" and report.readiness is None
    assert report.blocker.observed_runtime_hash == "0x" + "ab" * 32
    publish_observation_blocker(mailbox, p, ValueError("private-provider-token"))
    assert "private-provider-token" not in str(mailbox.reports)
    assert mailbox.reports[-1]["blocker"]["code"] == "observation_unavailable"
    assert list(journal.db.iterdump()) == before and len(c.sent) == 1
    for patch in [
        {"readiness": mailbox.reports[0]["readiness"] or {}},
        {"request_id": str(uuid4())},
        {"blocker": None},
    ]:
        with pytest.raises(ValueError):
            ManualReport.model_validate({**report.model_dump(), **patch})
    journal.close()


def test_request_refusal_after_healthy_readiness_is_not_observation_failure(
    tmp_path, monkeypatch, capsys
):
    import runpy
    from pathlib import Path

    module = runpy.run_path(
        str(Path(__file__).resolve().parents[2] / "scripts/treasury_manual_worker.py")
    )
    consume = module["consume_iteration"]
    p, c, journal, env, mailbox = setup(tmp_path)
    monkeypatch.setitem(consume.__globals__, "observe_earnings", lambda *_, **__: None)
    mailbox.pull = lambda: ("ack", env.model_dump())

    def refused(*_):
        raise ValueError("private request failure")

    monkeypatch.setitem(consume.__globals__, "consume_manual", refused)
    consume(mailbox, journal, p, c)
    assert [x["status"] for x in mailbox.reports] == ["readiness"]
    assert mailbox.acks == [] and len(c.sent) == 1
    log = capsys.readouterr().out
    assert (
        "manual_request_processing_failed" in log
        and "private request failure" not in log
    )
    journal.close()


def test_malformed_expected_runtime_does_not_hide_public_blocker(tmp_path):
    from ditto.treasury.collector_chain import CollectorRuntimeRefused
    from ditto.treasury.manual_worker import publish_observation_blocker

    p, _, journal, _, mailbox = setup(tmp_path)
    error = CollectorRuntimeRefused(
        "policy_runtime_unreviewed", "fixed", "0x" + "AA" * 32
    )
    publish_observation_blocker(mailbox, p, error)
    assert mailbox.reports[-1]["blocker"]["expected_runtime_hash"] is None
    assert mailbox.reports[-1]["blocker"]["code"] == "policy_runtime_unreviewed"
    journal.close()


def test_slow_receipt_catchup_keeps_readiness_inside_freshness_window(
    tmp_path, monkeypatch
):
    import runpy
    from pathlib import Path

    import ditto.treasury.manual_worker as worker

    module = runpy.run_path(
        str(Path(__file__).resolve().parents[2] / "scripts/treasury_manual_worker.py")
    )
    p, c, journal, _, mailbox = setup(tmp_path)
    journal.db.execute("UPDATE cursor SET block=10")
    c.income = {}
    now = [1000]
    scanned, published, pulled = [], [], []
    original_earnings, original_publish = c.earnings, mailbox.publish

    def slow_earnings(policy, block):
        now[0] += 8  # A complete 32-block sweep exceeds the 180s UI freshness gate.
        scanned.append(block)
        return original_earnings(policy, block)

    def publish(body):
        published.append(body["observed_at"])
        original_publish(body)

    def pull():
        pulled.append(True)
        return None

    monkeypatch.setattr(worker.time, "time", lambda: now[0])
    c.earnings, mailbox.publish, mailbox.pull = slow_earnings, publish, pull
    before_operations = list(journal.db.execute("SELECT * FROM operations"))
    module["consume_iteration"](mailbox, journal, p, c)
    assert scanned == list(range(11, 43))  # Existing total bound and exact cursor.
    assert max(b - a for a, b in zip([1000] + published, published, strict=False)) < 180
    assert published[-1] == now[0]
    assert all(r["status"] == "readiness" for r in mailbox.reports)
    assert pulled == [True] * 4  # Poll between chunks; consume at most one message.
    assert not journal.db.in_transaction and len(c.sent) == 1
    assert list(journal.db.execute("SELECT * FROM operations")) == before_operations
    journal.close()


@pytest.mark.parametrize("limit", [0, -1, 33, True, 8.0])
def test_receipt_chunk_bound_refuses_before_history_or_chain(tmp_path, limit):
    from ditto.treasury.collector import observe_earnings

    p, c, journal, _, _ = setup(tmp_path)
    before = list(journal.db.iterdump())
    with pytest.raises(ValueError, match="receipt chunk"):
        observe_earnings(journal, p, object(), max_blocks=limit)
    assert list(journal.db.iterdump()) == before and len(c.sent) == 1
    journal.close()


def test_failed_receipt_chunk_rolls_back_only_unproved_progress(tmp_path):
    from ditto.treasury.collector import observe_earnings

    p, c, journal, _, _ = setup(tmp_path)
    journal.db.execute("UPDATE cursor SET block=10")
    c.income = {12: 5, 21: 7}
    original = c.earnings

    def fail(policy, block):
        if block == 22:
            raise TimeoutError()
        return original(policy, block)

    observe_earnings(journal, p, c, max_blocks=8)
    c.earnings = fail
    with pytest.raises(TimeoutError):
        observe_earnings(journal, p, c, max_blocks=8)
    assert not journal.db.in_transaction
    assert journal.db.execute("SELECT block FROM cursor").fetchone()[0] == 18
    assert (
        journal.db.execute("SELECT amount FROM earnings WHERE block=12").fetchone()[0]
        == 5
    )
    assert (
        journal.db.execute("SELECT 1 FROM earnings WHERE block=21").fetchone() is None
    )
    c.earnings = original
    observe_earnings(journal, p, c, max_blocks=8)
    assert journal.db.execute("SELECT block FROM cursor").fetchone()[0] == 26
    assert (
        journal.db.execute("SELECT amount FROM earnings WHERE block=21").fetchone()[0]
        == 7
    )
    assert (
        journal.db.execute("SELECT count(*) FROM earnings WHERE block=12").fetchone()[0]
        == 1
    )
    assert len(c.sent) == 1
    journal.close()


@pytest.mark.parametrize("cursor,reports", [(100, 1), (96, 2)])
def test_caught_up_worker_publishes_readiness_then_stops_redundant_chunks(
    tmp_path, cursor, reports
):
    import runpy
    from pathlib import Path

    module = runpy.run_path(
        str(Path(__file__).resolve().parents[2] / "scripts/treasury_manual_worker.py")
    )
    p, c, journal, _, mailbox = setup(tmp_path)
    journal.db.execute("UPDATE cursor SET block=?", (cursor,))
    c.income = {}
    calls, pulls = [], []
    original = c.earnings

    def earnings(policy, block):
        calls.append(block)
        return original(policy, block)

    def pull():
        pulls.append(True)
        return None

    c.earnings, mailbox.pull = earnings, pull
    module["consume_iteration"](mailbox, journal, p, c)
    assert calls == list(range(cursor + 1, 101))
    assert len(mailbox.reports) == reports
    assert all(r["status"] == "readiness" for r in mailbox.reports)
    assert pulls == [True] * reports
    assert len(c.sent) == 1 and not journal.db.in_transaction
    journal.close()


def test_archive_refusal_during_catchup_still_publishes_readiness_and_pulls(
    tmp_path, monkeypatch, capsys
):
    import runpy
    from pathlib import Path

    module = runpy.run_path(
        str(Path(__file__).resolve().parents[2] / "scripts/treasury_manual_worker.py")
    )
    consume = module["consume_iteration"]
    p, c, journal, _, mailbox = setup(tmp_path)
    cursor = journal.db.execute("SELECT block FROM cursor WHERE id=1").fetchone()[0]

    def refused(*_, **__):
        raise RuntimeError("Historical work rate limit exceeded")

    monkeypatch.setitem(consume.__globals__, "observe_earnings", refused)
    pulled = []
    mailbox.pull = lambda: pulled.append(True)
    consume(mailbox, journal, p, c)
    assert [r["status"] for r in mailbox.reports] == ["readiness"]
    assert pulled == [True]
    assert journal.db.execute("SELECT block FROM cursor WHERE id=1").fetchone()[0] == (
        cursor
    )
    log = capsys.readouterr().out
    assert "earnings_catchup_unavailable" in log and "rate limit" not in log
    journal.close()
