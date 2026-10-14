"""Synthetic source proof, real public crypto; no cloud or signing wallets."""

from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from bittensor_wallet import Keypair

from ditto.tests.test_collector_automation import Chain, open_journal, policy
from ditto.tests.test_treasury_manual_worker import Mailbox
from ditto.treasury.collector import (
    Settlement,
    TransferCanary,
    arm_manual_transfer,
    manual_transfer_readiness,
    observe_earning_source,
    observe_earnings,
    tick,
)
from ditto.treasury.manual_batch import ManualBatchTransfer
from ditto.treasury.manual_worker import consume_manual, process_manual
from ditto.treasury.service_allocation import ServiceDestination
from ditto_screening_protocol.treasury_manual import (
    ManualBatchEnvelope,
    ManualBatchRequest,
    ManualEnvelope,
    parse_manual_envelope,
)
from ditto_screening_protocol.treasury_manual_limit import (
    MANUAL_LIMIT_DOMAIN,
    ManualLimitApproval,
    ManualLimitPolicy,
)

RAO = 1_000_000_000


def fixture(tmp_path):
    key = Keypair.create_from_uri("//Alice")
    destination = Keypair.create_from_uri("//Bob").ss58_address
    p = policy(
        collector_coldkey=key.ss58_address,
        destinations=(ServiceDestination("gm", 1000, destination),),
        max_distribution_rao=100 * RAO,
    )
    c = Chain()
    c.observation = replace(c.observation, block=1000, uid=14, alpha_rao=9000 * RAO)
    c.income = dict.fromkeys(range(10, 190), 15 * RAO)
    j = open_journal(tmp_path, p, "transfer")
    tick(j, p, c, "transfer")
    # Synthetic archive transport independently returns each exact credit.
    for b in range(10, 190):
        proof = c.earnings(p, b)
        j.db.execute(
            "INSERT OR IGNORE INTO earnings VALUES (?,?,?,?,0)",
            (
                b,
                proof.amount_rao,
                proof.block_hash,
                proof.event_digest,
            ),
        )
    tick(j, p, c, "transfer", canary=TransferCanary(40, 0))
    c.settlement = Settlement(
        "finalized",
        1001,
        "0x" + "c" * 64,
        14,
        extrinsic_index=2,
        extrinsic_hash="0x" + "b" * 64,
    )
    tick(j, p, c, "transfer")
    limit = ManualLimitPolicy(
        collector_policy_digest=p.digest,
        collector_coldkey=key.ss58_address,
        revision=1,
        max_amount_rao=2500 * RAO,
    )
    approval = ManualLimitApproval(
        policy=limit,
        signature="0x" + key.sign((MANUAL_LIMIT_DOMAIN + limit.digest).encode()).hex(),
    )
    c.manual_approval = approval
    c.prepare_manual = lambda p, call, observed, _request: c.prepare(
        p, "transfer", call, observed
    )
    c.substrate = SimpleNamespace(get_block_hash=lambda _: "0x" + "d" * 64)
    c.query = lambda *_: 123
    sources, left = [], 2500 * RAO
    for b in range(11, 190):
        take = min(left, 15 * RAO)
        sources.append({"source_block": b, "amount_rao": take})
        left -= take
        if not left:
            break
    request = ManualBatchRequest(
        request_id=str(uuid4()),
        after_operation=1,
        source_block=11,
        bucket_id="gm",
        amount_rao=2500 * RAO,
        retained_alpha_rao=100 * RAO,
        expires_block=1500,
        reason="Manual GM funding within approved range",
        sources=sources,
    )
    env = ManualBatchEnvelope(
        collector_policy_digest=p.digest,
        destination=destination,
        manual_approval=approval,
        request=request,
    )
    return p, c, j, env


def intent(env):
    return ManualBatchTransfer(
        **env.request.model_dump(), manual_approval=env.manual_approval.model_dump()
    )


def test_2500_range_uses_one_durable_transfer_and_preserves_all_source_remainders(
    tmp_path,
):
    p, c, j, env = fixture(tmp_path)
    original = p.digest
    assert arm_manual_transfer(j, p, c, intent(env), record=False) == "ready_not_armed"
    box = Mailbox()
    assert process_manual(box, j, p, c, "ack", env.model_dump()).status == "pending"
    assert len(c.sent) == len(c.prepared) == 2  # original canary + one aggregate
    assert c.prepared[-1][1]["params"]["alpha_amount"] == 2500 * RAO
    report = process_manual(box, j, p, c, "again", env.model_dump())
    assert report.status == "finalized" and len(report.settlement.source_epochs) == 167
    readiness = manual_transfer_readiness(j, p, c)
    remaining = sum(
        part["alpha_rao"] for s in readiness["sources"] for part in s["remaining"]
    )
    assert remaining == 200 * RAO - 40
    assert p.digest == original and p.max_distribution_rao == 100 * RAO
    assert tick(j, p, object(), "transfer") == "canary_spent"
    assert process_manual(box, j, p, c, "old", env.model_dump()).status == "finalized"
    assert len(c.sent) == 2


def test_batch_timeout_reconciles_exact_hash_without_second_signature(tmp_path):
    p, c, j, env = fixture(tmp_path)
    c.error = True
    with pytest.raises(TimeoutError):
        process_manual(Mailbox(), j, p, c, "ack", env.model_dump())
    c.settlement = Settlement("pending")
    assert (
        process_manual(Mailbox(), j, p, c, "ack", env.model_dump()).status == "pending"
    )
    assert len(c.sent) == len(c.prepared) == 2


def test_invalid_cold_approval_is_refused_and_acked_without_starving_mailbox(tmp_path):
    p, c, j, env = fixture(tmp_path)
    raw = env.model_dump()
    raw["manual_approval"]["signature"] = "0x" + "00" * 64
    mailbox = Mailbox()
    result = consume_manual(mailbox, j, p, c, "invalid", raw)
    assert result.status == "refused" and mailbox.acks == ["invalid"]
    assert len(c.sent) == 1


@pytest.mark.parametrize(
    "fault",
    ["signature", "pin", "reserve", "source", "immature", "cap", "changed_credit"],
)
def test_bad_batch_never_signs_or_changes_history(tmp_path, fault):
    p, c, j, env = fixture(tmp_path)
    request = intent(env)
    if fault == "signature":
        request.manual_approval["signature"] = "0x" + "00" * 64
    elif fault == "pin":
        c.manual_approval = None
    elif fault == "reserve":
        request = replace(request, retained_alpha_rao=8000 * RAO)
    elif fault == "source":
        j.db.execute("DELETE FROM earnings WHERE block=12")
    elif fault == "immature":
        c.observation = replace(c.observation, block=100)
    elif fault == "cap":
        c.manual_approval = c.manual_approval.model_copy(
            update={
                "policy": c.manual_approval.policy.model_copy(
                    update={"max_amount_rao": 100 * RAO}
                )
            }
        )
    else:
        c.income[12] -= 1
    before = list(j.db.iterdump())
    with pytest.raises(ValueError):
        arm_manual_transfer(j, p, c, request)
    assert list(j.db.iterdump()) == before and len(c.sent) == 1


@pytest.mark.parametrize(
    "fault", ["duplicate", "sum", "anchor", "order", "zero", "limit"]
)
def test_wire_rejects_ambiguous_sources_and_above_2500(fault, tmp_path):
    _, _, _, env = fixture(tmp_path)
    body = env.model_dump()
    if fault == "limit":
        body["manual_approval"]["policy"]["max_amount_rao"] += 1
    else:
        sources = body["request"]["sources"]
        if fault == "duplicate":
            sources[1]["source_block"] = sources[0]["source_block"]
        if fault == "sum":
            sources[0]["amount_rao"] += 1
        if fault == "anchor":
            body["request"]["source_block"] += 1
        if fault == "order":
            sources.reverse()
        if fault == "zero":
            sources[0]["amount_rao"] = 0
    with pytest.raises(ValueError):
        parse_manual_envelope(body)


def test_historical_v1_digest_has_no_new_fields(tmp_path):
    _, _, _, env = fixture(tmp_path)
    body = env.model_dump()
    body["version"] = 1
    body.pop("manual_approval")
    body["request"].pop("sources")
    v1 = ManualEnvelope.model_validate(body)
    assert parse_manual_envelope(body).model_dump() == body
    assert parse_manual_envelope(body).digest == v1.digest


def test_native_adapter_raises_manual_ceiling_only_for_exact_batch(tmp_path):
    from ditto.treasury.collector_chain import PublicCollectorChain

    p, c, _, env = fixture(tmp_path)
    adapter = PublicCollectorChain.__new__(PublicCollectorChain)
    adapter.role = "transfer"
    adapter.substrate = object()
    adapter.manual_approval = c.manual_approval
    call = {
        "module": "SubtensorModule",
        "function": "transfer_stake",
        "params": {
            "destination_coldkey": env.destination,
            "hotkey": p.collector_hotkey,
            "origin_netuid": 118,
            "destination_netuid": 118,
            "alpha_amount": env.request.amount_rao,
        },
    }

    class ValidatedBeforeAnyKeyAccess(Exception):
        pass

    def observe(*_):
        raise ValidatedBeforeAnyKeyAccess()

    adapter.observe = observe
    with pytest.raises(ValueError, match="distribution intent"):
        adapter.prepare(p, "transfer", call, c.observation)
    with pytest.raises(ValidatedBeforeAnyKeyAccess):
        adapter.prepare_manual(p, call, c.observation, intent(env))
    wrong = {**call, "params": {**call["params"], "alpha_amount": 2500 * RAO + 1}}
    with pytest.raises(ValueError, match="manual signing intent"):
        adapter.prepare_manual(p, wrong, c.observation, intent(env))


def test_recent_source_proof_does_not_skip_history_or_duplicate_later_scan(tmp_path):
    p, c, j, _ = fixture(tmp_path)
    cursor = j.db.execute("SELECT block FROM cursor").fetchone()[0]
    j.db.execute("DELETE FROM earnings WHERE block=50")  # not yet scanned fixture gap
    c.income[50] = 150 * RAO  # above the automatic batch ceiling
    observe_earning_source(j, p, c, 50)
    assert j.db.execute("SELECT block FROM cursor").fetchone()[0] == cursor
    assert len(c.sent) == 1
    ready = manual_transfer_readiness(j, p, c)
    assert (
        next(s for s in ready["sources"] if s["source_block"] == 50)["remaining"][0][
            "alpha_rao"
        ]
        == 150 * RAO
    )
    observe_earnings(j, p, c)
    assert (
        j.db.execute("SELECT COUNT(*) FROM earnings WHERE block=50").fetchone()[0] == 1
    )
    assert (
        j.db.execute(
            "SELECT COUNT(*) FROM events WHERE event='emission_attributed' "
            "AND json_extract(payload,'$.block')=50"
        ).fetchone()[0]
        == 1
    )
    # A changed archive answer cannot reset the credit or its consumption.
    c.income[50] += 1
    before = list(j.db.iterdump())
    with pytest.raises(ValueError, match="previously proved"):
        observe_earning_source(j, p, c, 50)
    assert list(j.db.iterdump()) == before


def test_recent_probes_share_32_block_budget_and_never_run_money_tick(tmp_path):
    from scripts.treasury_manual_worker import consume_iteration

    p, c, j, _ = fixture(tmp_path)
    cursor = j.db.execute("SELECT block FROM cursor").fetchone()[0]
    c.income.update(dict.fromkeys(range(200, 208), 150 * RAO))
    c.recent_earning_candidates = lambda _: list(range(200, 208))
    mailbox = Mailbox()
    mailbox.pull = lambda: None
    consume_iteration(mailbox, j, p, c)
    assert len(c.sent) == 1
    assert j.db.execute("SELECT block FROM cursor").fetchone()[0] == cursor + 24
    assert (
        j.db.execute(
            "SELECT COUNT(*) FROM events WHERE event='manual_source_probed'"
        ).fetchone()[0]
        == 8
    )
    assert len(mailbox.reports) == 11  # fresh observation after each 8 + 3 chunks
