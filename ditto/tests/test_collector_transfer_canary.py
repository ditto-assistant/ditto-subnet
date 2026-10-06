"""One monetary claim stays bounded across retries, restarts and ordinary ticks."""

import json
import sqlite3
import sys
from dataclasses import replace

import pytest

from ditto.tests.test_collector_automation import Chain, open_journal, policy
from ditto.treasury.collector import (
    Settlement,
    TransferCanary,
    remaining_distribution,
    replace_failed_canary,
    tick,
)
from ditto.treasury.service_allocation import plan_service_distribution
from scripts import treasury_collector as cli


def setup_transfer(tmp_path, *, cap=40, amount=101):
    p, c = policy(), Chain()
    c.observation = replace(c.observation, uid=14)
    c.income = {10: amount}
    journal = open_journal(tmp_path, p, "transfer")
    assert tick(journal, p, c, "transfer") == "observing"
    assert not c.prepared and not c.sent
    return p, c, journal, TransferCanary(cap, 0)


@pytest.mark.parametrize(
    "maximum,baseline", [(0, 0), (-1, 0), (True, 0), (1.1, 0), (1, -1), (1, True)]
)
def test_canary_rejects_noninteger_or_invalid_bounds(maximum, baseline):
    with pytest.raises(ValueError):
        TransferCanary(maximum, baseline)


def test_canary_uses_unchanged_bucket_receipt_and_stops_ordinary_next_tick(tmp_path):
    p, c, j, bound = setup_transfer(tmp_path)
    assert tick(j, p, c, "transfer", canary=bound) == "dispatching"
    assert c.prepared[0][1]["params"]["alpha_amount"] == 40
    assert c.prepared[0][1]["params"]["destination_coldkey"] == "bitsec"
    c.settlement = Settlement("finalized", 101, "0x" + "c" * 64, 14)
    assert tick(j, p, c, "transfer") == "finalized"
    j.close()
    j = open_journal(tmp_path, p, "transfer")
    assert tick(j, p, object(), "transfer") == "canary_spent"
    assert len(c.sent) == len(c.prepared) == 1
    assert j.db.execute("SELECT completed FROM earnings").fetchone()[0] == 0
    assert j.db.execute("SELECT SUM(amount) FROM operations").fetchone()[0] == 40


@pytest.mark.parametrize("status", ["failed", "expired"])
def test_failed_canary_consumes_claim_instead_of_starting_another(tmp_path, status):
    p, c, j, bound = setup_transfer(tmp_path, cap=39)
    tick(j, p, c, "transfer", canary=bound)
    c.settlement = Settlement(status, 101, "0x" + "c" * 64)
    assert tick(j, p, c, "transfer") == status
    assert tick(j, p, object(), "transfer", canary=bound) == "canary_spent"
    assert len(c.sent) == 1


def failed_replacement_fixture(tmp_path):
    p, c, j, bound = setup_transfer(tmp_path, cap=39)
    assert tick(j, p, c, "transfer", canary=bound) == "dispatching"
    c.settlement = Settlement("failed", 101, "0x" + "c" * 64)
    assert tick(j, p, c, "transfer") == "failed"
    c.retryable_transfer_failure = lambda *_: replace(
        c.settlement, extrinsic_index=0, extrinsic_hash="0x" + "b" * 64
    )
    return p, c, j


def test_explicit_replacement_preserves_failed_claim_and_same_source_entitlement(
    tmp_path,
):
    p, c, j = failed_replacement_fixture(tmp_path)
    original = tuple(j.db.execute("SELECT * FROM operations").fetchone())
    replace_failed_canary(
        j, p, c, TransferCanary(40, 1), reason="operator approved replacement"
    )
    assert tuple(j.db.execute("SELECT * FROM operations").fetchone()) == original
    assert len(c.prepared) == len(c.sent) == 1  # rearming does not sign/send
    j.close()
    j = open_journal(tmp_path, p, "transfer")
    assert tick(j, p, c, "transfer") == "dispatching"
    assert c.prepared[-1][1]["params"]["alpha_amount"] == 40
    assert j.db.execute(
        "SELECT id,state,source_block,amount FROM operations ORDER BY id"
    ).fetchall()[0][:] == (1, "failed", 10, 39)
    assert j.db.execute(
        "SELECT id,state,source_block,amount FROM operations ORDER BY id"
    ).fetchall()[1][:] == (2, "dispatching", 10, 40)
    c.settlement = Settlement("finalized", 102, "0x" + "f" * 64, 14)
    assert tick(j, p, c, "transfer") == "finalized"
    assert tick(j, p, object(), "transfer") == "canary_spent"
    assert len(c.sent) == 2
    assert j.db.execute("SELECT amount,completed FROM earnings").fetchone()[:] == (
        101,
        0,
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "pending",
        "paid",
        "expired",
        "wrong_baseline",
        "over_cap",
        "wrong_proof",
        "changed_income",
        "bad_reason",
    ],
)
def test_replacement_refuses_unsafe_transition_without_history_change(
    tmp_path, mutation
):
    p, c, j = failed_replacement_fixture(tmp_path)
    requested, reason = TransferCanary(40, 1), "operator approved replacement"
    if mutation in {"pending", "paid", "expired"}:
        j.db.execute(
            "UPDATE operations SET state=?",
            (
                {"pending": "dispatching", "paid": "finalized", "expired": "expired"}[
                    mutation
                ],
            ),
        )
    elif mutation == "wrong_baseline":
        requested = TransferCanary(40, 0)
    elif mutation == "over_cap":
        requested = TransferCanary(1001, 1)
    elif mutation == "wrong_proof":
        c.retryable_transfer_failure = lambda *_: replace(
            c.settlement, extrinsic_index=0, extrinsic_hash="wrong"
        )
    elif mutation == "changed_income":
        c.income[10] = 102
    elif mutation == "bad_reason":
        reason = "short"
    before = j.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    with pytest.raises(ValueError):
        replace_failed_canary(j, p, c, requested, reason=reason)
    assert j.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before
    assert len(c.sent) == len(c.prepared) == 1


def test_replacement_cannot_be_repeated_or_reset_by_tick_flags(tmp_path):
    p, c, j = failed_replacement_fixture(tmp_path)
    bound = TransferCanary(40, 1)
    replace_failed_canary(j, p, c, bound, reason="operator approved replacement")
    with pytest.raises(ValueError, match="already replaced"):
        replace_failed_canary(j, p, c, bound, reason="operator approved replacement")
    with pytest.raises(ValueError, match="reset or widened"):
        tick(j, p, object(), "transfer", canary=TransferCanary(41, 1))


def test_replacement_uncertain_broadcast_only_reconciles_after_restart(tmp_path):
    p, c, j = failed_replacement_fixture(tmp_path)
    replace_failed_canary(
        j, p, c, TransferCanary(40, 1), reason="operator approved replacement"
    )
    c.error = True
    with pytest.raises(TimeoutError):
        tick(j, p, c, "transfer")
    j.close()
    j = open_journal(tmp_path, p, "transfer")
    c.settlement = Settlement("pending")
    assert tick(j, p, c, "transfer") == "pending"
    assert len(c.sent) == len(c.prepared) == 2


def test_unknown_delivery_reopens_to_reconciliation_without_resigning(tmp_path):
    p, c, j, bound = setup_transfer(tmp_path, cap=39)
    c.error = True
    with pytest.raises(TimeoutError):
        tick(j, p, c, "transfer", canary=bound)
    j.close()
    j = open_journal(tmp_path, p, "transfer")
    assert tick(j, p, c, "transfer") == "pending"
    assert len(c.sent) == len(c.prepared) == 1


def test_canary_transfers_only_ceiling_and_retains_receipt_remainder(tmp_path):
    p, c, j, bound = setup_transfer(tmp_path, cap=39)
    assert tick(j, p, c, "transfer", canary=bound) == "dispatching"
    assert c.prepared[0][1]["params"]["alpha_amount"] == 39
    assert c.prepared[0][1]["params"]["destination_coldkey"] == "bitsec"
    c.settlement = Settlement("finalized", 101, "0x" + "c" * 64, 14)
    assert tick(j, p, c, "transfer") == "finalized"
    j.close()
    j = open_journal(tmp_path, p, "transfer")
    assert tick(j, p, object(), "transfer") == "canary_spent"
    assert len(c.prepared) == len(c.sent) == 1
    assert j.db.execute("SELECT amount,completed FROM earnings").fetchone()[:] == (
        101,
        0,
    )
    assert j.db.execute("SELECT SUM(amount) FROM operations").fetchone()[0] == 39
    reserved = json.loads(
        j.db.execute(
            "SELECT payload FROM events WHERE event='canary_receipt_reserved'"
        ).fetchone()[0]
    )
    assert reserved == {
        "source_block": 10,
        "source_hash": "0x" + "d" * 64,
        "source_event_digest": "e" * 64,
        "source_amount": 101,
        "bucket": "bitsec",
        "bucket_remaining_before": 40,
        "canary_amount": 39,
        "receipt_remaining_after": 62,
    }
    assert (
        j.db.execute(
            "SELECT COUNT(*) FROM events WHERE event='transfer_canary_armed'"
        ).fetchone()[0]
        == 1
    )


def distribution_history(rows):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE history (bucket TEXT, amount INTEGER, destination TEXT, "
        "role TEXT, state TEXT)"
    )
    db.executemany("INSERT INTO history VALUES (?,?,?,?,?)", rows)
    result = db.execute("SELECT * FROM history").fetchall()
    db.close()
    return result


def receipt_parts():
    p = policy()
    return plan_service_distribution(
        attributed_alpha_rao=101,
        available_alpha_rao=101,
        collector_coldkey=p.collector_coldkey,
        destinations=p.destinations,
    )


def test_partial_finalized_leg_does_not_complete_its_bucket_or_other_buckets():
    parts = receipt_parts()
    rows = [("bitsec", 39, "bitsec", "transfer", "finalized")]
    remaining = remaining_distribution(parts, distribution_history(rows))
    assert {part.bucket_id: part.alpha_rao for part in remaining} == {
        "bitsec": 1,
        "gm": 61,
    }
    assert sum(part.alpha_rao for part in remaining) + 39 == 101
    rows.extend(
        [
            ("bitsec", 1, "bitsec", "transfer", "finalized"),
            ("gm", 60, "gm", "transfer", "finalized"),
        ]
    )
    remaining = remaining_distribution(parts, distribution_history(rows))
    assert [(part.bucket_id, part.alpha_rao) for part in remaining] == [("gm", 1)]
    rows.append(("gm", 1, "gm", "transfer", "finalized"))
    assert remaining_distribution(parts, distribution_history(rows)) == ()


@pytest.mark.parametrize(
    "row",
    [
        ("unknown", 1, "gm", "transfer", "finalized"),
        ("gm", 1, "wrong-wallet", "transfer", "finalized"),
        ("gm", 1, "gm", "registration", "finalized"),
        ("gm", 1, "gm", "transfer", "dispatching"),
        ("gm", 1, "gm", "transfer", "failed"),
        ("gm", 0, "gm", "transfer", "finalized"),
        ("gm", -1, "gm", "transfer", "finalized"),
        ("gm", 0.5, "gm", "transfer", "finalized"),
        ("gm", 62, "gm", "transfer", "finalized"),
    ],
)
def test_distribution_remainder_refuses_invalid_or_excess_history(row):
    with pytest.raises(ValueError):
        remaining_distribution(receipt_parts(), distribution_history([row]))


def test_distribution_remainder_refuses_aggregate_bucket_overpayment():
    with pytest.raises(ValueError, match="exceeds"):
        remaining_distribution(
            receipt_parts(),
            distribution_history(
                [
                    ("gm", 40, "gm", "transfer", "finalized"),
                    ("gm", 22, "gm", "transfer", "finalized"),
                ]
            ),
        )


@pytest.mark.parametrize("changed", [TransferCanary(41, 0), TransferCanary(40, 1)])
def test_persisted_canary_cannot_widen_or_reset_baseline(tmp_path, changed):
    p, c, j, bound = setup_transfer(tmp_path, amount=0)
    assert tick(j, p, c, "transfer", canary=bound) == "observing"
    with pytest.raises(ValueError, match="reset or widened"):
        tick(j, p, object(), "transfer", canary=changed)
    assert j.db.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0


@pytest.mark.parametrize("bound", [TransferCanary(1001, 0), TransferCanary(40, 1)])
def test_unarmed_invalid_canary_never_changes_history_or_calls_chain(tmp_path, bound):
    p, _c, j, _bound = setup_transfer(tmp_path)
    before = j.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    with pytest.raises(ValueError):
        tick(j, p, object(), "transfer", canary=bound)
    assert j.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before


def test_canary_cannot_arm_over_an_existing_unknown_dispatch(tmp_path):
    p, c, j, _bound = setup_transfer(tmp_path)
    assert tick(j, p, c, "transfer") == "dispatching"
    with pytest.raises(ValueError, match="quiesced"):
        tick(j, p, object(), "transfer", canary=TransferCanary(40, 1))
    assert len(c.sent) == 1


def test_canary_never_applies_to_registration(tmp_path):
    p = policy()
    j = open_journal(tmp_path, p)
    with pytest.raises(ValueError, match="transfer role"):
        tick(j, p, object(), "registration", canary=TransferCanary(40, 0))


@pytest.mark.parametrize(
    "extra",
    [
        ["--canary-max-alpha-rao", "10000000"],
        ["--canary-after-operation", "0"],
        ["--canary-max-alpha-rao", "0", "--canary-after-operation", "0"],
        [
            "--canary-max-alpha-rao",
            "10000000",
            "--canary-after-operation",
            "0",
            "--watch-only",
        ],
        [
            "--canary-max-alpha-rao",
            "10000000",
            "--canary-after-operation",
            "0",
            "--initialize-journal",
        ],
        [
            "--canary-max-alpha-rao",
            "10000000",
            "--canary-after-operation",
            "0",
            "--export-activity",
        ],
        [
            "--canary-max-alpha-rao",
            "10000000",
            "--canary-after-operation",
            "0",
            "--snapshot-only",
        ],
    ],
)
def test_cli_refuses_invalid_canary_before_policy_or_chain(monkeypatch, extra):
    def forbidden(*_):
        pytest.fail("invalid CLI accessed policy/chain")

    monkeypatch.setattr(cli, "load_policy", forbidden)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "collector",
            "--role",
            "transfer",
            "--policy",
            "/signed/policy.json",
            "--policy-sha256",
            "a" * 64,
            "--journal",
            "/existing/journal.db",
            *extra,
        ],
    )
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
