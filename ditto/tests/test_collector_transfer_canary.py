"""One monetary claim stays bounded across retries, restarts and ordinary ticks."""

import sys
from dataclasses import replace

import pytest

from ditto.tests.test_collector_automation import Chain, open_journal, policy
from ditto.treasury.collector import Settlement, TransferCanary, tick
from scripts import treasury_collector as cli


def setup_transfer(tmp_path, *, cap=40, amount=101):
    p, c = policy(), Chain()
    c.observation = replace(c.observation, uid=14)
    c.income = {10: amount}
    return p, c, open_journal(tmp_path, p, "transfer"), TransferCanary(cap, 0)


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
    p, c, j, bound = setup_transfer(tmp_path)
    tick(j, p, c, "transfer", canary=bound)
    c.settlement = Settlement(status, 101, "0x" + "c" * 64)
    assert tick(j, p, c, "transfer") == status
    assert tick(j, p, object(), "transfer", canary=bound) == "canary_spent"
    assert len(c.sent) == 1


def test_unknown_delivery_reopens_to_reconciliation_without_resigning(tmp_path):
    p, c, j, bound = setup_transfer(tmp_path)
    c.error = True
    with pytest.raises(TimeoutError):
        tick(j, p, c, "transfer", canary=bound)
    j.close()
    j = open_journal(tmp_path, p, "transfer")
    assert tick(j, p, c, "transfer") == "pending"
    assert len(c.sent) == len(c.prepared) == 1


def test_over_ceiling_source_is_retained_and_never_partially_split(tmp_path):
    p, c, j, bound = setup_transfer(tmp_path, cap=39)
    assert tick(j, p, c, "transfer", canary=bound) == "canary_amount_exceeded"
    assert tick(j, p, c, "transfer") == "canary_amount_exceeded"
    assert not c.prepared and not c.sent
    assert j.db.execute("SELECT amount,completed FROM earnings").fetchone()[:] == (
        101,
        0,
    )
    assert j.db.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0
    assert (
        j.db.execute(
            "SELECT COUNT(*) FROM events WHERE event='transfer_canary_armed'"
        ).fetchone()[0]
        == 1
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
