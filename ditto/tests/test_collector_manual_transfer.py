"""Manual intents preserve prior history and authorize exactly one claim."""

import hashlib
import json
import sys
from contextlib import nullcontext
from dataclasses import asdict, replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ditto.tests.test_collector_transfer_canary import setup_transfer
from ditto.treasury.collector import (
    ManualTransfer,
    Settlement,
    arm_manual_transfer,
    canonical,
    manual_transfer_readiness,
    observe_earnings,
    tick,
)
from scripts import treasury_collector as cli


def fixture(tmp_path):
    p, c, j, bound = setup_transfer(tmp_path, amount=501)
    assert tick(j, p, c, "transfer", canary=bound) == "dispatching"
    c.settlement = Settlement(
        "finalized",
        101,
        "0x" + "c" * 64,
        14,
        extrinsic_index=2,
        extrinsic_hash="0x" + "b" * 64,
    )
    assert tick(j, p, c, "transfer") == "finalized"
    request = ManualTransfer(
        str(uuid4()), 1, 10, "gm", 30, 900, 300, "operator manual topup request"
    )
    return p, c, j, request


def test_manual_is_one_exact_explicit_claim_with_retained_stake(tmp_path):
    p, c, j, request = fixture(tmp_path)
    original = tuple(j.db.execute("SELECT * FROM operations").fetchone())
    assert arm_manual_transfer(j, p, c, request) == "armed"
    assert tuple(j.db.execute("SELECT * FROM operations").fetchone()) == original
    assert len(c.prepared) == len(c.sent) == 1
    assert tick(j, p, c, "transfer") == "manual_ready"
    assert len(c.sent) == 1
    assert (
        tick(j, p, c, "transfer", manual_request_id=request.request_id) == "dispatching"
    )
    assert c.prepared[-1][1]["params"]["alpha_amount"] == 30
    assert c.prepared[-1][1]["params"]["destination_coldkey"] == "gm"
    assert tick(j, p, c, "transfer") == "finalized"
    assert tick(j, p, object(), "transfer") == "canary_spent"
    assert len(c.prepared) == len(c.sent) == 2
    assert j.db.execute("SELECT SUM(amount) FROM operations").fetchone()[0] == 70


def test_manual_unknown_send_reconciles_without_resigning(tmp_path):
    p, c, j, request = fixture(tmp_path)
    arm_manual_transfer(j, p, c, request)
    c.error = True
    with pytest.raises(TimeoutError):
        tick(j, p, c, "transfer", manual_request_id=request.request_id)
    c.settlement = Settlement("pending")
    assert tick(j, p, c, "transfer") == "pending"
    assert len(c.sent) == len(c.prepared) == 2


def test_manual_idempotency_and_stale_intent_refuse(tmp_path):
    p, c, j, request = fixture(tmp_path)
    assert arm_manual_transfer(j, p, c, request) == "armed"
    assert arm_manual_transfer(j, p, c, request) == "already_armed"
    with pytest.raises(ValueError, match="idempotency"):
        arm_manual_transfer(j, p, c, replace(request, amount_rao=31))
    with pytest.raises(ValueError, match="current exact"):
        tick(j, p, c, "transfer", manual_request_id=str(uuid4()))
    assert len(c.sent) == 1


def test_manual_preview_checks_proof_without_arming_or_mutating_journal(tmp_path):
    p, c, j, request = fixture(tmp_path)
    before = list(j.db.iterdump())
    assert arm_manual_transfer(j, p, c, request, record=False) == "ready_not_armed"
    assert list(j.db.iterdump()) == before
    assert tick(j, p, object(), "transfer") == "canary_spent"
    assert len(c.prepared) == len(c.sent) == 1
    c.settlement = replace(c.settlement, extrinsic_hash="wrong")
    with pytest.raises(ValueError):
        arm_manual_transfer(j, p, c, request, record=False)
    assert list(j.db.iterdump()) == before


def test_manual_readiness_is_read_only_and_preserves_remaining_entitlement(tmp_path):
    p, c, j, request = fixture(tmp_path)
    before = list(j.db.iterdump())
    readiness = manual_transfer_readiness(j, p, c)
    assert readiness["authority"] == "none"
    assert readiness["publication"] == "not_performed"
    assert readiness["bounded_claim_available"] is True
    assert readiness["after_operation"] == 1
    assert readiness["sources"][0]["source_block"] == request.source_block
    remaining = readiness["sources"][0]["remaining"]
    assert (
        next(part for part in remaining if part["bucket_id"] == "gm")["alpha_rao"]
        == 301
    )
    assert (
        next(part for part in remaining if part["bucket_id"] == "bitsec")["alpha_rao"]
        == 160
    )
    assert list(j.db.iterdump()) == before
    assert len(c.prepared) == len(c.sent) == 1
    j.db.execute("UPDATE operations SET state='pending'")
    with pytest.raises(ValueError, match="finalized receipt distribution"):
        manual_transfer_readiness(j, p, c)
    assert len(c.sent) == 1


@pytest.mark.parametrize("mode", ["readiness", "preview"])
def test_cli_manual_read_modes_do_not_create_spending_authority(
    tmp_path, monkeypatch, capsys, mode
):
    p, c, j, request = fixture(tmp_path)
    before = list(j.db.iterdump())
    public = tmp_path / "request.json"
    public.write_text(json.dumps(asdict(request)))
    monkeypatch.setattr(cli, "load_policy", lambda *_: p)
    monkeypatch.setattr(cli, "CollectorJournal", lambda *_: j)
    monkeypatch.setattr(j, "close", lambda: None)
    monkeypatch.setattr(cli, "PublicCollectorChain", lambda *_a, **_k: c)
    monkeypatch.setitem(
        sys.modules,
        "bittensor",
        SimpleNamespace(
            Subtensor=lambda **_: nullcontext(SimpleNamespace(substrate=object()))
        ),
    )
    argv = [
        "collector",
        "--role",
        "transfer",
        "--policy",
        "/signed/policy.json",
        "--policy-sha256",
        p.digest,
        "--journal",
        "/existing/journal.db",
    ]
    argv.extend(
        ["--manual-readiness"]
        if mode == "readiness"
        else ["--preview-manual-transfer", str(public)]
    )
    monkeypatch.setattr(sys, "argv", argv)
    cli.main()
    output = json.loads(capsys.readouterr().out)
    assert output["authority"] == "none"
    if mode == "preview":
        assert output["status"] == "ready_not_armed"
        assert (
            output["confirmation_digest"]
            == hashlib.sha256(canonical(asdict(request)).encode()).hexdigest()
        )
    assert list(j.db.iterdump()) == before
    assert len(c.prepared) == len(c.sent) == 1


@pytest.mark.parametrize(
    "fault",
    [
        "reserve",
        "bucket",
        "amount",
        "source",
        "expiry",
        "baseline",
        "prior_failed",
        "proof",
    ],
)
def test_manual_refusals_do_not_append_intent_or_sign(tmp_path, fault):
    p, c, j, request = fixture(tmp_path)
    if fault == "reserve":
        request = replace(request, retained_alpha_rao=999)
    elif fault == "bucket":
        request = replace(request, bucket_id="unapproved")
    elif fault == "amount":
        request = replace(request, amount_rao=302, retained_alpha_rao=1)
    elif fault == "source":
        request = replace(request, source_block=11)
    elif fault == "expiry":
        request = replace(request, expires_block=100)
    elif fault == "baseline":
        request = replace(request, after_operation=2)
    elif fault == "prior_failed":
        j.db.execute("UPDATE operations SET state='failed'")
    else:
        c.settlement = replace(c.settlement, extrinsic_hash="wrong")
    before = j.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    with pytest.raises(ValueError):
        arm_manual_transfer(j, p, c, request)
    assert j.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before
    assert len(c.prepared) == len(c.sent) == 1


def test_reserve_and_expiry_rechecked_at_dispatch(tmp_path):
    p, c, j, request = fixture(tmp_path)
    arm_manual_transfer(j, p, c, request)
    c.observation = replace(c.observation, alpha_rao=910)
    with pytest.raises(ValueError, match="retained"):
        tick(j, p, c, "transfer", manual_request_id=request.request_id)
    c.observation = replace(c.observation, block=301)
    assert (
        tick(j, p, c, "transfer", manual_request_id=request.request_id)
        == "manual_expired"
    )
    assert len(c.sent) == 1


def test_second_manual_intent_requires_prior_completion_and_preserves_all_claims(
    tmp_path,
):
    p, c, j, first = fixture(tmp_path)
    arm_manual_transfer(j, p, c, first)
    second = replace(first, request_id=str(uuid4()), after_operation=2, amount_rao=20)
    with pytest.raises(ValueError):
        arm_manual_transfer(j, p, c, second)
    tick(j, p, c, "transfer", manual_request_id=first.request_id)
    c.settlement = Settlement("pending")
    with pytest.raises(ValueError):
        arm_manual_transfer(j, p, c, second)
    c.settlement = Settlement(
        "finalized",
        102,
        "0x" + "c" * 64,
        14,
        extrinsic_index=2,
        extrinsic_hash="0x" + "b" * 64,
    )
    tick(j, p, c, "transfer")
    assert arm_manual_transfer(j, p, c, second) == "armed"
    assert arm_manual_transfer(j, p, c, first) == "already_armed"
    with pytest.raises(ValueError, match="current exact"):
        tick(j, p, c, "transfer", manual_request_id=first.request_id)
    assert (
        tick(j, p, c, "transfer", manual_request_id=second.request_id) == "dispatching"
    )
    assert j.db.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 3
    assert j.db.execute("SELECT SUM(amount) FROM operations").fetchone()[0] == 90


@pytest.mark.parametrize(
    "field,value",
    [
        ("amount_rao", 0),
        ("amount_rao", True),
        ("retained_alpha_rao", 0),
        ("after_operation", -1),
        ("expires_block", 1.5),
        ("request_id", "not-a-uuid"),
    ],
)
def test_manual_rejects_ambiguous_or_nonpositive_input(tmp_path, field, value):
    _p, _c, _j, request = fixture(tmp_path)
    with pytest.raises((ValueError, TypeError, AttributeError)):
        replace(request, **{field: value})


def test_receipt_only_scan_continues_after_spent_canary_without_signing(tmp_path):
    p, c, j, _request = fixture(tmp_path)
    cursor = j.db.execute("SELECT block FROM cursor").fetchone()[0]
    c.income[cursor + 2] = 90
    c.observation = replace(c.observation, block=1000)
    assert observe_earnings(j, p, c) == "observed"
    assert j.db.execute("SELECT block FROM cursor").fetchone()[0] == cursor + 32
    assert (
        j.db.execute(
            "SELECT amount FROM earnings WHERE block=?", (cursor + 2,)
        ).fetchone()[0]
        == 90
    )
    assert len(c.prepared) == len(c.sent) == 1
    assert tick(j, p, object(), "transfer") == "canary_spent"


def test_receipt_only_scan_rolls_back_on_unavailable_history(tmp_path):
    p, c, j, _request = fixture(tmp_path)
    cursor = j.db.execute("SELECT block FROM cursor").fetchone()[0]

    def unavailable(_policy, _block):
        raise TimeoutError("chain unavailable")

    c.earnings = unavailable
    with pytest.raises(TimeoutError):
        observe_earnings(j, p, c)
    assert j.db.execute("SELECT block FROM cursor").fetchone()[0] == cursor
    assert len(c.sent) == 1
