"""Failure-state controls for durable collector recovery and distribution."""

from dataclasses import replace
from pathlib import Path

import pytest

from ditto.treasury.collector import (
    CollectorJournal,
    CollectorPolicy,
    FinalizedEarnings,
    Observation,
    Settlement,
    SignedOperation,
    tick,
)
from ditto.treasury.collector_chain import AUDITED_CODE_HASH, FINNEY_GENESIS
from ditto.treasury.service_allocation import ServiceDestination


def policy(**changes):
    values = {
        "genesis_hash": FINNEY_GENESIS,
        "runtime_code_hash": AUDITED_CODE_HASH,
        "collector_coldkey": "collector-cold",
        "collector_hotkey": "collector-hot",
        "registration_delegate": "register",
        "transfer_delegate": "transfer",
        "revision": 1,
        "start_block": 10,
        "destinations": (
            ServiceDestination("gm", 600, "gm"),
            ServiceDestination("bitsec", 400, "bitsec"),
        ),
        "max_registration_burn_rao": 100,
        "registration_budget_rao": 220,
        "max_fee_rao": 10,
        "fee_reserve_rao": 10,
        "recovery_cooldown_blocks": 20,
        "distribution_interval_blocks": 5,
        "max_distribution_rao": 1000,
        "enabled": True,
        "gcp_project": "test-project",
        "registration_service_account": "reg@test-project.iam.gserviceaccount.com",
        "transfer_service_account": "transfer@test-project.iam.gserviceaccount.com",
        "registration_secret_version": 1,
        "transfer_secret_version": 1,
    }
    return CollectorPolicy(**(values | changes))


class Chain:
    def __init__(self):
        self.observation = Observation(100, "0x" + "a" * 64, None, 50, 2000, 200, 1000)
        self.sent = []
        self.prepared = []
        self.income = {}
        self.settlement = Settlement("pending")
        self.error = False

    def observe(self, *_):
        return self.observation

    def earnings(self, _, block):
        amount = self.income.get(block, 0)
        return FinalizedEarnings(amount, "0x" + "d" * 64, "e" * 64) if amount else None

    def prepare(self, _, role, call, observed):
        self.prepared.append((role, call))
        return SignedOperation(
            "0xab", "0x" + "b" * 64, observed.block, observed.block + 64, 5
        )

    def broadcast(self, encoded):
        self.sent.append(encoded)
        if self.error:
            raise TimeoutError("uncertain RPC")

    def reconcile(self, *_):
        return self.settlement


def open_journal(tmp_path: Path, p, role="registration"):
    path = tmp_path / (role + ".db")
    return CollectorJournal(path, p, role, initialize=not path.exists())


def test_disabled_never_reads_chain_or_signer(tmp_path):
    p = policy(enabled=False)
    j = open_journal(tmp_path, p)
    assert tick(j, p, object(), "registration") == "disabled"


def test_uncertain_rpc_is_persisted_and_reopen_never_resigns(tmp_path):
    p, c = policy(), Chain()
    c.error = True
    j = open_journal(tmp_path, p)
    with pytest.raises(TimeoutError):
        tick(j, p, c, "registration")
    j.close()
    j = open_journal(tmp_path, p)
    assert tick(j, p, c, "registration") == "pending"
    assert len(c.sent) == len(c.prepared) == 1
    assert c.prepared[0][1]["function"] == "register_limit"
    assert c.prepared[0][1]["params"]["limit_price"] == 100


@pytest.mark.parametrize("status", ["failed", "expired"])
def test_failed_or_expired_recovery_charges_full_reservation_and_cooldown(
    tmp_path, status
):
    p, c = policy(), Chain()
    j = open_journal(tmp_path, p)
    tick(j, p, c, "registration")
    c.settlement = Settlement(status, 101, "0x" + "c" * 64)
    assert tick(j, p, c, "registration") == status
    assert tick(j, p, c, "registration") == "cooldown"
    c.observation = replace(c.observation, block=121)
    assert tick(j, p, c, "registration") == "dispatching"
    tick(j, p, c, "registration")
    c.observation = replace(c.observation, block=145)
    with pytest.raises(ValueError, match="budget"):
        tick(j, p, c, "registration")
    assert len(c.sent) == 2


def test_registration_rebind_audited_only_with_finalized_uid(tmp_path):
    p, c = policy(), Chain()
    j = open_journal(tmp_path, p)
    tick(j, p, c, "registration")
    c.settlement = Settlement("finalized", 101, "0x" + "c" * 64, 14)
    assert tick(j, p, c, "registration") == "finalized"
    assert (
        j.db.execute(
            "SELECT COUNT(*) FROM events WHERE event='collector_uid_rebound'"
        ).fetchone()[0]
        == 1
    )


def test_missing_uid_does_not_release_claim(tmp_path):
    p, c = policy(), Chain()
    j = open_journal(tmp_path, p)
    tick(j, p, c, "registration")
    c.settlement = Settlement("finalized", 101, "0x" + "c" * 64)
    with pytest.raises(ValueError, match="UID"):
        tick(j, p, c, "registration")
    assert j.db.execute("SELECT state FROM operations").fetchone()[0] == "dispatching"


def test_registered_collector_never_registers(tmp_path):
    p, c = policy(), Chain()
    c.observation = replace(c.observation, uid=14)
    assert tick(open_journal(tmp_path, p), p, c, "registration") == "registered"
    assert not c.prepared


@pytest.mark.parametrize(
    "change",
    [{"burn_rao": 101}, {"collector_free_rao": 109}, {"delegate_free_rao": 19}],
)
def test_registration_refuses_cost_and_reserve_failure(tmp_path, change):
    p, c = policy(), Chain()
    c.observation = replace(c.observation, **change)
    with pytest.raises(ValueError):
        tick(open_journal(tmp_path, p), p, c, "registration")
    assert not c.prepared


def test_policy_or_role_swap_cannot_reuse_journal(tmp_path):
    p = policy()
    open_journal(tmp_path, p).close()
    with pytest.raises(ValueError, match="pin"):
        open_journal(tmp_path, replace(p, revision=2))
    with pytest.raises(ValueError, match="pin"):
        CollectorJournal(tmp_path / "registration.db", p, "transfer")


def test_distribution_exact_buckets_conserve_and_do_not_spend_principal(tmp_path):
    p, c = policy(), Chain()
    c.observation = replace(c.observation, uid=14)
    c.income = {10: 101}
    c.settlement = Settlement("finalized", 101, "0x" + "c" * 64, 14)
    j = open_journal(tmp_path, p, "transfer")
    assert tick(j, p, c, "transfer") == "dispatching"
    assert tick(j, p, c, "transfer") == "finalized"
    # Remaining stake may be less than the ORIGINAL batch, still enough for the
    # second reserved bucket. All rounding uses the original immutable batch.
    c.observation = replace(c.observation, alpha_rao=61)
    assert tick(j, p, c, "transfer") == "dispatching"
    assert tick(j, p, c, "transfer") == "finalized"
    assert tick(j, p, c, "transfer") == "distributed"
    assert sorted(op[1]["params"]["alpha_amount"] for op in c.prepared) == [40, 61]
    assert {op[1]["params"]["destination_coldkey"] for op in c.prepared} == {
        "gm",
        "bitsec",
    }
    assert j.db.execute("SELECT SUM(amount) FROM operations").fetchone()[0] == 101


def test_absent_collector_stops_distribution_without_advancing_cursor(tmp_path):
    p, c = policy(), Chain()
    j = open_journal(tmp_path, p, "transfer")
    with pytest.raises(ValueError, match="absent"):
        tick(j, p, c, "transfer")
    assert j.db.execute("SELECT block FROM cursor").fetchone()[0] == 9


def test_distribution_balance_increase_alone_is_never_income(tmp_path):
    p, c = policy(), Chain()
    c.observation = replace(c.observation, uid=14, alpha_rao=10000000)
    j = open_journal(tmp_path, p, "transfer")
    assert tick(j, p, c, "transfer") == "observing"
    assert j.db.execute("SELECT block FROM cursor").fetchone()[0] == 41
    assert not c.prepared


def test_failed_transfer_quarantines_batch_instead_of_duplicate_send(tmp_path):
    p, c = policy(), Chain()
    c.observation = replace(c.observation, uid=14)
    c.income = {10: 101}
    j = open_journal(tmp_path, p, "transfer")
    tick(j, p, c, "transfer")
    c.settlement = Settlement("failed", 101, "0x" + "c" * 64)
    assert tick(j, p, c, "transfer") == "failed"
    assert tick(j, p, c, "transfer") == "observing"
    assert len(c.sent) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"max_registration_burn_rao": True},
        {"enabled": "true"},
        {"netuid": 119},
        {"transfer_delegate": "register"},
        {"revision": -1},
    ],
)
def test_strict_policy_refuses_wrong_chain_roles_and_ambiguous_values(change):
    with pytest.raises(ValueError):
        policy(**change)


def test_missing_or_deleted_production_journal_never_reinitializes(tmp_path):
    p = policy()
    path = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError):
        CollectorJournal(path, p, "registration")
    assert not path.exists()
    j = CollectorJournal(path, p, "registration", initialize=True)
    j.close()
    path.unlink()
    with pytest.raises(FileNotFoundError):
        CollectorJournal(path, p, "registration")


def test_group_writable_or_symlink_journal_directory_refused(tmp_path):
    p = policy()
    directory = tmp_path / "role"
    directory.mkdir(mode=0o770)
    directory.chmod(0o770)
    with pytest.raises(ValueError, match="directory"):
        CollectorJournal(directory / "journal.db", p, "registration", initialize=True)
    directory.chmod(0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(directory, target_is_directory=True)
    with pytest.raises(ValueError, match="directory"):
        CollectorJournal(alias / "journal.db", p, "registration", initialize=True)
